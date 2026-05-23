"""Step 3 — Cloudflare.

Render and deploy the Cloudflare tunnel after the user confirms the setup.
"""

from __future__ import annotations

import getpass
import json
import os
import pwd
import re
import shutil
import subprocess
import time
from pathlib import Path

from rakkib.docker import compose_up, container_running, DockerError, docker_run
from rakkib.doctor import attempt_fix_cloudflared
from rakkib.render import render_file
from rakkib.service_catalog import cloudflare_enabled, service_fqdn
from rakkib.state import State
from rakkib.steps import VerificationResult, load_service_registry
from rakkib.util import RAKKIB_DATA_DIR

METRICS_RETRY_ATTEMPTS = 20
METRICS_RETRY_INTERVAL_SEC = 3
LOGS_TAIL_LINES = 30
EDGE_LOGS_TAIL_LINES = 200


def _repo_dir() -> Path:
    """Return the package data directory (contains ``templates/``)."""
    return RAKKIB_DATA_DIR


def _home_for_user(user: str | None) -> Path | None:
    """Return a user's real home directory when it can be resolved."""
    if not user:
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return Path("/home") / user


def _cloudflared_env(admin_user: str | None) -> dict[str, str]:
    """Force cloudflared to use the install user's home, not an inherited root HOME."""
    home = _home_for_user(admin_user)
    if home is None or admin_user == "root":
        return {}
    return {"HOME": str(home)}


def _cloudflared_bin() -> str:
    """Return the path to the cloudflared binary."""
    local_bin = Path.home() / ".local" / "bin" / "cloudflared"
    if local_bin.exists():
        return str(local_bin)
    return "cloudflared"


def _show_qr(url: str) -> None:
    try:
        import qrcode
        import qrcode.constants

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            # A larger quiet zone improves scanner reliability.
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = 21 + 4 * (qr.version - 1)
        display_width = (size + 8) * 2
        display_height = size + 8
        rows = len(matrix)
        cols = len(matrix[0]) if rows else 0

        # Render each module as two terminal columns so the QR code stays
        # close to square and easier for phone cameras to scan.
        if rows != display_height or cols * 2 != display_width:
            display_height = rows
            display_width = cols * 2

        black = "\033[40m  \033[0m"
        white = "\033[107m  \033[0m"
        for y in range(display_height):
            row = matrix[y]
            print("".join(black if row[x] else white for x in range(cols)))
    except ImportError:
        pass


def _looks_like_cloudflared_ascii_qr(line: str) -> bool:
    """Heuristic filter for cloudflared's ASCII QR block."""
    s = line.rstrip("\n")
    if not s:
        return False
    stripped = s.strip()
    if len(stripped) < 10:
        return False
    hash_count = stripped.count("#")
    return hash_count / max(1, len(stripped)) >= 0.6


def _candidate_cloudflared_paths(name: str, admin_user: str | None = None) -> list[Path]:
    """Return likely host paths for cloudflared auth artifacts."""
    candidates = []
    admin_home = _home_for_user(admin_user)
    if admin_home is not None:
        candidates.append(admin_home / ".cloudflared" / name)

    current_home = Path.home()
    if current_home != Path("/root") or os.geteuid() == 0 or admin_user == "root":
        candidates.append(current_home / ".cloudflared" / name)

    if os.geteuid() == 0 or admin_user == "root":
        candidates.append(Path("/root/.cloudflared") / name)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def _is_readable_artifact(path: Path) -> bool:
    """Return True when the current process can read the artifact file."""
    try:
        if not path.is_file():
            return False
        with path.open("rb"):
            return True
    except OSError:
        return False


def _find_cloudflared_artifact(name: str, admin_user: str | None = None) -> Path | None:
    """Return the first readable auth artifact from common cloudflared locations."""
    for candidate in _candidate_cloudflared_paths(name, admin_user=admin_user):
        if _is_readable_artifact(candidate):
            return candidate
    return None


def _find_unreadable_cloudflared_artifact(name: str, admin_user: str | None = None) -> Path | None:
    """Return the first existing but unreadable auth artifact from common locations."""
    for candidate in _candidate_cloudflared_paths(name, admin_user=admin_user):
        try:
            exists = candidate.exists()
        except OSError:
            return candidate
        if exists and not _is_readable_artifact(candidate):
            return candidate
    return None


def _has_missing_creds_error(stderr: str | None) -> bool:
    """Return True when cloudflared reports missing local tunnel credentials."""
    text = (stderr or "").lower()
    return "credentials file" in text and "not found" in text


def _is_existing_tunnel_name_error(stderr: str | None) -> bool:
    """Return True when create failed because the tunnel name already exists."""
    text = (stderr or "").lower()
    markers = (
        "already exists",
        "name is taken",
        "duplicate name",
    )
    return any(marker in text for marker in markers)


def _next_tunnel_name(base_name: str, existing_names: set[str]) -> str:
    """Return a fresh tunnel name derived from the requested base name."""
    suffix = int(time.time())
    candidate = f"{base_name}-{suffix}"
    while candidate in existing_names:
        suffix += 1
        candidate = f"{base_name}-{suffix}"
    return candidate


def _extract_created_tunnel_name(stderr: str | None, requested_name: str) -> str:
    """Extract the actual created tunnel name from cloudflared output when available."""
    text = stderr or ""
    match = re.search(r"with name\s+([^\s]+)", text)
    if match:
        return match.group(1).strip("'\"")
    return requested_name


def _run(
    cmd: list[str],
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with optional env override."""
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            env=merged_env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {result.stderr.strip() if result.stderr else ''}")
    return result


def _cloudflare_tunnel_ids(state: State) -> tuple[str | None, str | None]:
    return (
        state.get("cloudflare.tunnel_uuid") or state.get("tunnel_uuid"),
        state.get("cloudflare.tunnel_name") or state.get("tunnel_name"),
    )


def _required_cloudflare_value(state: State, key: str, label: str) -> str:
    """Return a non-empty Cloudflare state value or raise an actionable error."""
    raw = state.get(key)
    value = str(raw).strip() if raw is not None else ""
    if value:
        return value
    raise RuntimeError(
        f"Cloudflare setup is missing {label} (`{key}`). "
        "Run `rakkib init` to regenerate Cloudflare settings, then rerun `rakkib pull`."
    )


def _optional_cloudflare_value(state: State, key: str) -> str | None:
    raw = state.get(key)
    value = str(raw).strip() if raw is not None else ""
    return value or None


def _validate_cloudflare_state(state: State) -> tuple[str, str, str | None, str, str]:
    """Validate state used by cloudflared before any subprocess command runs."""
    auth_method = _required_cloudflare_value(state, "cloudflare.auth_method", "authentication method")
    tunnel_strategy = _required_cloudflare_value(state, "cloudflare.tunnel_strategy", "tunnel strategy")
    domain = _required_cloudflare_value(state, "domain", "domain")
    tunnel_name = _optional_cloudflare_value(state, "cloudflare.tunnel_name")
    tunnel_uuid = _optional_cloudflare_value(state, "cloudflare.tunnel_uuid")
    ssh_subdomain = _optional_cloudflare_value(state, "cloudflare.ssh_subdomain") or "ssh"

    if auth_method not in {"browser_login", "api_token", "existing_tunnel"}:
        raise RuntimeError(
            f"Cloudflare setup has unsupported authentication method `{auth_method}`. "
            "Run `rakkib init` to regenerate Cloudflare settings, then rerun `rakkib pull`."
        )
    if tunnel_strategy not in {"new", "existing"}:
        raise RuntimeError(
            f"Cloudflare setup has unsupported tunnel strategy `{tunnel_strategy}`. "
            "Run `rakkib init` to regenerate Cloudflare settings, then rerun `rakkib pull`."
        )
    if tunnel_strategy == "new" and not tunnel_name:
        raise RuntimeError(
            "Cloudflare setup is missing tunnel name (`cloudflare.tunnel_name`). "
            "Run `rakkib init` to regenerate Cloudflare settings, then rerun `rakkib pull`."
        )
    if tunnel_strategy == "existing" and not tunnel_name and not tunnel_uuid:
        raise RuntimeError(
            "Cloudflare setup is missing tunnel identity (`cloudflare.tunnel_name` or `cloudflare.tunnel_uuid`). "
            "Run `rakkib init` to regenerate Cloudflare settings, then rerun `rakkib pull`."
        )

    return auth_method, tunnel_strategy, tunnel_name, domain, ssh_subdomain.strip(".") or "ssh"


def create_dns_route(state: State, fqdn: str, env: dict[str, str] | None = None) -> None:
    """Create or update a Cloudflare tunnel DNS route for a hostname."""
    hostname = str(fqdn or "").strip().strip(".")
    if not hostname:
        return

    tunnel_uuid, tunnel_name = _cloudflare_tunnel_ids(state)
    tunnel = tunnel_uuid or tunnel_name
    if not tunnel:
        raise RuntimeError(f"DNS route creation failed for {hostname}: Cloudflare tunnel is not recorded")

    command = [_cloudflared_bin(), "tunnel", "route", "dns", "--overwrite-dns", str(tunnel), hostname]
    cloudflared_env = env if env is not None else _cloudflared_env(state.get("admin_user"))
    result = _run(command, env=cloudflared_env, check=False)
    if result.returncode != 0 and tunnel_name and tunnel_name != tunnel:
        result = _run(
            [_cloudflared_bin(), "tunnel", "route", "dns", "--overwrite-dns", str(tunnel_name), hostname],
            env=cloudflared_env,
            check=False,
        )

    if result.returncode == 0:
        return

    raise RuntimeError(
        f"DNS route creation failed for {hostname}: {result.stderr.strip() if result.stderr else 'unknown error'}"
    )


def delete_dns_route(state: State, fqdn: str, env: dict[str, str] | None = None) -> str | None:
    """Delete a Cloudflare tunnel DNS route. Return a warning string on failure."""
    hostname = str(fqdn or "").strip().strip(".")
    if not hostname:
        return None

    tunnel_uuid, tunnel_name = _cloudflare_tunnel_ids(state)
    tunnel = tunnel_uuid or tunnel_name
    if not tunnel:
        return f"Cloudflare DNS record {hostname} may still exist: Cloudflare tunnel is not recorded."

    cloudflared_env = env if env is not None else _cloudflared_env(state.get("admin_user"))
    commands = [[_cloudflared_bin(), "tunnel", "route", "dns", "delete", str(tunnel), hostname]]
    if tunnel_name and tunnel_name != tunnel:
        commands.append([_cloudflared_bin(), "tunnel", "route", "dns", "delete", str(tunnel_name), hostname])

    last_error = "unknown error"
    for command in commands:
        result = _run(command, env=cloudflared_env, check=False)
        if result.returncode == 0:
            return None
        last_error = result.stderr.strip() or result.stdout.strip() or last_error
        lowered = last_error.lower()
        if "not found" in lowered or "does not exist" in lowered or "doesn't exist" in lowered:
            return None

    return f"Cloudflare DNS record {hostname} may still exist: {last_error}"


def _published_service_ids(state: State) -> list[str]:
    raw = state.get("cloudflare.published_services") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _set_published_service_ids(state: State, service_ids: list[str]) -> None:
    state.set("cloudflare.published_services", sorted(dict.fromkeys(service_ids)))


def _service_ingress_lines(state: State) -> str:
    registry = load_service_registry()
    by_id = {svc["id"]: svc for svc in registry.get("services", [])}
    lines: list[str] = []
    for svc_id in _published_service_ids(state):
        svc = by_id.get(svc_id)
        if not svc:
            continue
        fqdn = service_fqdn(state, svc)
        if not fqdn:
            continue
        lines.extend(
            [
                f"  - hostname: {fqdn}",
                "    service: http://caddy:80",
            ]
        )
    return "\n".join(lines)


def render_config(state: State) -> Path:
    """Render cloudflared config with explicit service ingress entries."""
    data_root = state.data_root
    config_path = data_root / "data" / "cloudflared" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    state.set("cloudflare.service_ingress", _service_ingress_lines(state))
    render_file(_repo_dir() / "templates" / "cloudflared" / "config.yml.tmpl", config_path, state)
    return config_path


def reload_container(state: State) -> None:
    if not cloudflare_enabled(state):
        return
    data_root = state.data_root
    cloudflared_dir = data_root / "docker" / "cloudflared"
    if (cloudflared_dir / "docker-compose.yml").exists():
        docker_run(["compose", "--project-directory", str(cloudflared_dir), "restart"], check=False)


def publish_service(state: State, svc: dict) -> None:
    """Publish a successfully deployed service through Cloudflare."""
    if not cloudflare_enabled(state):
        return
    fqdn = service_fqdn(state, svc)
    if not fqdn:
        return
    create_dns_route(state, fqdn)
    published = _published_service_ids(state)
    if svc["id"] not in published:
        published.append(svc["id"])
    _set_published_service_ids(state, published)
    render_config(state)
    reload_container(state)


def unpublish_service(state: State, svc: dict, *, warn: bool = True) -> str | None:
    """Remove Cloudflare DNS and local routing for a service."""
    if not cloudflare_enabled(state):
        return None
    fqdn = service_fqdn(state, svc)
    published = [svc_id for svc_id in _published_service_ids(state) if svc_id != svc["id"]]
    _set_published_service_ids(state, published)
    render_config(state)
    reload_container(state)
    if fqdn:
        warning = delete_dns_route(state, fqdn)
        if warn and warning:
            return (
                f"{warning} Rakkib removed local routing; removed service hostnames should no longer reach the service."
            )
    return None


def _repair_dir_ownership(directory: Path, uid: int, gid: int) -> None:
    """Recursively rewrite ownership under *directory* to uid:gid.

    Skips silently if the directory is missing. When non-root, escalates via
    sudo. This makes the cloudflare step self-healing for machines where a
    prior container run wrote files as the image's default `nonroot` user.
    """
    if not directory.exists():
        return
    if os.geteuid() == 0:
        for entry in [directory, *directory.rglob("*")]:
            try:
                os.chown(entry, uid, gid)
            except OSError:
                pass
        return
    needs_repair = False
    try:
        for entry in [directory, *directory.rglob("*")]:
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_uid != uid or st.st_gid != gid:
                needs_repair = True
                break
    except OSError:
        needs_repair = True
    if needs_repair:
        _sudo_run(["chown", "-R", f"{uid}:{gid}", str(directory)])


def _sudo_run(cmd: list[str]) -> bool:
    """Run a sudo command non-interactively. Return True on success."""
    try:
        result = subprocess.run(
            ["sudo", "-n", *cmd],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _set_owner_mode(path: Path, uid: int, gid: int, mode: int) -> None:
    """Set ownership and mode on *path*, escalating via sudo when needed.

    When running as root, use os.chown directly. Otherwise, only chown via
    sudo when the current ownership doesn't already match the desired uid/gid
    — this avoids gratuitous sudo calls on fresh installs where the file is
    already owned by the admin user. If chmod fails because the file is
    owned by another UID (e.g. stale 65532-owned tree from a prior broken
    container run), retry via sudo.
    """
    try:
        st = path.stat()
    except OSError:
        st = None

    if os.geteuid() == 0:
        try:
            os.chown(path, uid, gid)
        except OSError:
            pass
    elif st is not None and (st.st_uid != uid or st.st_gid != gid):
        _sudo_run(["chown", f"{uid}:{gid}", str(path)])

    try:
        os.chmod(path, mode)
    except PermissionError:
        _sudo_run(["chmod", oct(mode)[2:], str(path)])


def _merged_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Merge subprocess overrides with the current environment."""
    merged = {**os.environ}
    if env:
        merged.update(env)
    return merged


def _get_tunnel_uuid(tunnel_name: str, env: dict[str, str] | None = None) -> str | None:
    """Look up tunnel UUID by name. Returns None if not found."""
    result = _run(
        [_cloudflared_bin(), "tunnel", "list", "--output", "json"],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        tunnels = json.loads(result.stdout)
        for t in tunnels:
            if t.get("name") == tunnel_name:
                return t.get("id")
    except json.JSONDecodeError:
        pass
    return None


def _list_tunnels(env: dict[str, str] | None = None) -> list[dict]:
    """Return tunnel list data, or an empty list when unavailable."""
    result = _run(
        [_cloudflared_bin(), "tunnel", "list", "--output", "json"],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _create_tunnel_with_fallback(
    requested_name: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Create a fresh tunnel, retrying with a derived name when needed."""
    current_names = {str(t.get("name")) for t in _list_tunnels(env=env) if t.get("name")}
    create_result = _run(
        [_cloudflared_bin(), "tunnel", "create", requested_name],
        env=env,
        check=False,
    )
    tunnel_name = requested_name
    if create_result.returncode != 0:
        if _has_missing_creds_error(create_result.stderr):
            raise RuntimeError(create_result.stderr.strip() or "cloudflared tunnel create failed")
        if not _is_existing_tunnel_name_error(create_result.stderr):
            raise RuntimeError(create_result.stderr.strip() or f"Failed to create tunnel '{requested_name}'")
        tunnel_name = _next_tunnel_name(requested_name, current_names)
        create_result = _run(
            [_cloudflared_bin(), "tunnel", "create", tunnel_name],
            env=env,
            check=False,
        )
        if create_result.returncode != 0:
            raise RuntimeError(create_result.stderr.strip() or f"Failed to create tunnel '{tunnel_name}'")
    else:
        tunnel_name = _extract_created_tunnel_name(create_result.stderr, requested_name)

    tunnel_uuid = _get_tunnel_uuid(tunnel_name, env=env)
    if not tunnel_uuid:
        raise RuntimeError(f"Failed to get UUID for newly created tunnel '{tunnel_name}'")
    return tunnel_name, tunnel_uuid


def run(state: State) -> None:
    if not cloudflare_enabled(state):
        return

    data_root = state.data_root
    admin_user = state.get("admin_user")
    cloudflared_env = _cloudflared_env(admin_user)
    zone_in_cloudflare = state.get("cloudflare.zone_in_cloudflare", False)

    cloudflared_dir = data_root / "data" / "cloudflared"
    docker_dir = data_root / "docker" / "cloudflared"
    log_path = data_root / "logs" / "cloudflare.log"

    cloudflared_dir.mkdir(parents=True, exist_ok=True)
    docker_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve admin uid/gid up front and repair any stale 65532-owned tree
    # left by an earlier broken container run BEFORE attempting any copy or
    # template render — otherwise those writes would fail with EACCES on a
    # dir that's still owned by `nonroot`.
    admin_uid = os.getuid()
    admin_gid = os.getgid()
    if admin_user:
        try:
            pw = pwd.getpwnam(admin_user)
            admin_uid = pw.pw_uid
            admin_gid = pw.pw_gid
        except KeyError:
            pass
    _repair_dir_ownership(cloudflared_dir, admin_uid, admin_gid)

    # 0. Stop if zone is not in Cloudflare
    if not zone_in_cloudflare:
        raise RuntimeError("The domain is not active in Cloudflare. Complete Cloudflare zone setup first, then resume.")

    auth_method, tunnel_strategy, tunnel_name, domain, ssh_subdomain = _validate_cloudflare_state(state)
    metrics_port = state.get("cloudflared_metrics_port", "20241")

    # 1. Confirm cloudflared CLI is installed
    try:
        _run([_cloudflared_bin(), "--version"], env=cloudflared_env)
    except RuntimeError:
        print("Preparing Cloudflare tunnel tool...")
        msg = attempt_fix_cloudflared()
        print(f"[dim]{msg}[/dim]")
        if shutil.which("cloudflared") is None and not Path(_cloudflared_bin()).exists():
            raise RuntimeError(
                "cloudflared installation failed. Install manually: https://github.com/cloudflare/cloudflared/releases"
            )

    cert_path = cloudflared_dir / "cert.pem"
    token_env = dict(cloudflared_env)
    previous_tunnel_name = tunnel_name

    # 4-5. Handle auth methods
    if auth_method == "browser_login":
        if not cert_path.exists():
            default_cert = _find_cloudflared_artifact("cert.pem", admin_user=admin_user)
            if default_cert is not None:
                shutil.copy2(default_cert, cert_path)
            else:
                unreadable_cert = _find_unreadable_cloudflared_artifact("cert.pem", admin_user=admin_user)
                if unreadable_cert is not None:
                    print(
                        "Found an existing Cloudflare browser-login cert, but the current setup run cannot read it: "
                        f"{unreadable_cert}\n"
                        "Continuing with a fresh cloudflared login for the current user instead."
                    )
                headless = state.get("cloudflare.headless", False)
                if headless:
                    print("\nCloudflare login required")
                    print("Waiting for login link...\n")

                    proc = subprocess.Popen(
                        [_cloudflared_bin(), "tunnel", "login"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        env=_merged_env(token_env),
                    )

                    login_url: str | None = None
                    showed_qr = False
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        if not login_url and "https://" in line:
                            login_url = line.strip().split()[-1]
                            if login_url.startswith("https://"):
                                print("\nScan this QR code on your phone to approve the domain:\n")
                                _show_qr(login_url)
                                showed_qr = True
                                print(
                                    f"\nOr open this link:\n  {login_url}\n\n"
                                    "Waiting for approval. Keep this terminal open...\n"
                                )
                                continue

                        # Avoid printing cloudflared's ASCII QR block; it is
                        # commonly unscannable due to terminal aspect ratios.
                        if showed_qr and _looks_like_cloudflared_ascii_qr(line):
                            continue

                        print(line, end="", flush=True)

                    proc.wait()
                    if proc.returncode != 0:
                        raise RuntimeError(
                            f"cloudflared tunnel login failed (exit {proc.returncode}). "
                            "Try again or use auth_method=api_token."
                        )
                else:
                    print(
                        "\nCloudflare approval is required.\n"
                        "A browser window will open for Cloudflare login.\n"
                        "Approve the domain, then return here.\n"
                    )
                    result = subprocess.run(
                        [_cloudflared_bin(), "tunnel", "login"],
                        text=True,
                        env=_merged_env(token_env),
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"cloudflared tunnel login failed: "
                            f"{result.stderr.strip() if result.stderr else 'unknown error'}"
                        )

                default_cert = _find_cloudflared_artifact("cert.pem", admin_user=admin_user)
                if default_cert is not None and not cert_path.exists():
                    shutil.copy2(default_cert, cert_path)

        # Verify login succeeded
        list_result = _run(
            [_cloudflared_bin(), "tunnel", "list"],
            env=token_env,
            check=False,
        )
        if list_result.returncode != 0:
            raise RuntimeError("cloudflared tunnel list failed after login. Resolve auth before continuing.")

    elif auth_method == "api_token":
        api_token = getpass.getpass("Cloudflare API token: ")
        if not api_token:
            raise RuntimeError("API token is required for api_token auth method.")

        verify_result = subprocess.run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "10",
                "-H",
                f"Authorization: Bearer {api_token}",
                "https://api.cloudflare.com/client/v4/user/tokens/verify",
            ],
            capture_output=True,
            text=True,
        )
        if verify_result.returncode != 0:
            raise RuntimeError("Cloudflare API token verification failed.")

        token_env = {**cloudflared_env, "CLOUDFLARE_API_TOKEN": api_token}

        list_result = _run(
            [_cloudflared_bin(), "tunnel", "list"],
            env=token_env,
            check=False,
        )
        if list_result.returncode != 0:
            raise RuntimeError("cloudflared tunnel list failed with API token. Resolve auth before continuing.")

    elif auth_method == "existing_tunnel":
        tunnel_uuid = state.get("cloudflare.tunnel_uuid")
        creds_host_path = state.get("cloudflare.tunnel_creds_host_path")

        if not tunnel_uuid or not creds_host_path or not Path(creds_host_path).exists():
            # Need to repair auth
            if not cert_path.exists():
                default_cert = _find_cloudflared_artifact("cert.pem", admin_user=admin_user)
                if default_cert is not None:
                    shutil.copy2(default_cert, cert_path)
                else:
                    print(
                        "\nCloudflare login is needed to repair missing credentials.\n"
                        "cloudflared tunnel login will be initiated.\n"
                    )
                    result = subprocess.run(
                        [_cloudflared_bin(), "tunnel", "login"],
                        text=True,
                        env=_merged_env(token_env),
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"cloudflared tunnel login failed: "
                            f"{result.stderr.strip() if result.stderr else 'unknown error'}"
                        )
                    default_cert = _find_cloudflared_artifact("cert.pem", admin_user=admin_user)
                    if default_cert is not None and not cert_path.exists():
                        shutil.copy2(default_cert, cert_path)

    # 7-9. Handle tunnel discovery / creation
    tunnel_uuid = state.get("cloudflare.tunnel_uuid")

    if tunnel_strategy == "new":
        existing_uuid = _get_tunnel_uuid(tunnel_name, env=token_env)
        if existing_uuid:
            tunnel_uuid = existing_uuid
        else:
            tunnel_name, tunnel_uuid = _create_tunnel_with_fallback(tunnel_name, env=token_env)

    elif tunnel_strategy == "existing":
        if not tunnel_uuid:
            existing_uuid = _get_tunnel_uuid(tunnel_name, env=token_env)
            if existing_uuid:
                tunnel_uuid = existing_uuid
            else:
                raise RuntimeError(
                    f"Existing tunnel '{tunnel_name}' not found. Verify the tunnel name or create a new one."
                )

        info_result = _run(
            [_cloudflared_bin(), "tunnel", "info", tunnel_uuid],
            env=token_env,
            check=False,
        )
        if info_result.returncode != 0:
            raise RuntimeError(
                f"Tunnel info failed for UUID {tunnel_uuid}. The tunnel may not exist or auth may be invalid."
            )

    # 11. Ensure credentials JSON is at standardized host path
    creds_host_path = cloudflared_dir / f"{tunnel_uuid}.json"
    creds_container_path = f"/etc/cloudflared/{tunnel_uuid}.json"

    if not creds_host_path.exists():
        recorded_creds = state.get("cloudflare.tunnel_creds_host_path")
        default_creds = None
        if recorded_creds:
            recorded_path = Path(str(recorded_creds))
            if recorded_path.exists():
                default_creds = recorded_path
        if default_creds is None:
            default_creds = _find_cloudflared_artifact(
                f"{tunnel_uuid}.json",
                admin_user=admin_user,
            )
        if default_creds is not None:
            shutil.copy2(default_creds, creds_host_path)
        elif tunnel_strategy == "new":
            replacement_name, replacement_uuid = _create_tunnel_with_fallback(
                previous_tunnel_name,
                env=token_env,
            )
            print(
                "Existing Cloudflare tunnel was found but local credentials were unavailable. "
                f"Creating a fresh tunnel '{replacement_name}' and continuing."
            )
            tunnel_name = replacement_name
            tunnel_uuid = replacement_uuid
            creds_host_path = cloudflared_dir / f"{tunnel_uuid}.json"
            creds_container_path = f"/etc/cloudflared/{tunnel_uuid}.json"
            replacement_creds = _find_cloudflared_artifact(
                f"{tunnel_uuid}.json",
                admin_user=admin_user,
            )
            if replacement_creds is not None and replacement_creds != creds_host_path:
                shutil.copy2(replacement_creds, creds_host_path)
            elif not creds_host_path.exists():
                raise RuntimeError(
                    "Created a fresh Cloudflare tunnel but could not locate its credentials file. "
                    "Run cloudflared tunnel login and re-run rakkib pull."
                )
        else:
            searched_paths = ", ".join(
                str(path) for path in _candidate_cloudflared_paths(f"{tunnel_uuid}.json", admin_user=admin_user)
            )
            raise RuntimeError(
                f"Tunnel credentials file not found at {creds_host_path}, "
                f"or any searched cloudflared path ({searched_paths}). "
                "Run cloudflared tunnel login and ensure the tunnel was created in the correct account."
            )

    # 12. Apply explicit modes for the dir, config, and credentials. Re-run
    # the ownership repair in case new files were copied in from sources
    # owned by a different uid (e.g. cert.pem from /root via sudo cp).
    _repair_dir_ownership(cloudflared_dir, admin_uid, admin_gid)
    _set_owner_mode(cloudflared_dir, admin_uid, admin_gid, 0o755)
    _set_owner_mode(creds_host_path, admin_uid, admin_gid, 0o600)

    state.set("admin_uid", str(admin_uid))
    state.set("admin_gid", str(admin_gid))

    # 13. Update state with final values
    state.set("cloudflare.tunnel_uuid", tunnel_uuid)
    state.set("cloudflare.tunnel_name", tunnel_name)
    state.set("cloudflare.tunnel_creds_host_path", str(creds_host_path))
    state.set("cloudflare.tunnel_creds_container_path", creds_container_path)

    # Set top-level aliases so templates resolve {{TUNNEL_UUID}} etc.
    state.set("tunnel_uuid", tunnel_uuid)
    state.set("tunnel_creds_host_path", str(creds_host_path))
    state.set("tunnel_creds_container_path", creds_container_path)
    state.set("ssh_subdomain", ssh_subdomain)
    if not state.has("cloudflared_metrics_port"):
        state.set("cloudflared_metrics_port", metrics_port)

    if state.path is not None:
        state.save()

    # 14-15. Render templates
    repo = _repo_dir()
    config_path = render_config(state)
    _set_owner_mode(config_path, admin_uid, admin_gid, 0o644)
    render_file(
        repo / "templates" / "docker" / "cloudflared" / ".env.example",
        docker_dir / ".env",
        state,
    )
    (docker_dir / ".env").chmod(0o600)
    render_file(
        repo / "templates" / "docker" / "cloudflared" / "docker-compose.yml.tmpl",
        docker_dir / "docker-compose.yml",
        state,
    )

    # 16. Verify tunnel can be inspected before DNS changes
    _run(
        [_cloudflared_bin(), "tunnel", "info", tunnel_uuid],
        env=token_env,
    )

    # 17. Create or update only explicit DNS routes. Service hostnames are
    # added after each service installs successfully.
    for route in [domain, f"{ssh_subdomain}.{domain}"]:
        create_dns_route(state, route, env=token_env)

    # 19. Temporary API token was never persisted; token_env goes out of scope here.

    # 20. Start container (redirect output to log file)
    try:
        compose_up(docker_dir, log_path=log_path)
    except DockerError as exc:
        raise RuntimeError(f"docker compose up failed for cloudflared: {exc.stderr}")

    log_path.write_text("cloudflare step completed\n")


def verify(state: State) -> VerificationResult:
    if not cloudflare_enabled(state):
        return VerificationResult.success("cloudflare", "skipped for internal exposure mode")

    data_root = state.data_root
    auth_method = state.get("cloudflare.auth_method")
    cloudflared_env = _cloudflared_env(state.get("admin_user"))
    tunnel_uuid = state.get("cloudflare.tunnel_uuid") or state.get("tunnel_uuid")
    tunnel_creds_host_path = state.get("cloudflare.tunnel_creds_host_path") or state.get("tunnel_creds_host_path")
    metrics_port = state.get("cloudflared_metrics_port", "20241")

    # cloudflared --version
    version = subprocess.run(
        [_cloudflared_bin(), "--version"],
        capture_output=True,
        text=True,
        env=_merged_env(cloudflared_env),
    )
    if version.returncode != 0:
        return VerificationResult.failure(
            "cloudflare",
            "cloudflared CLI is not installed or runnable",
        )

    # data dir exists
    cloudflared_dir = data_root / "data" / "cloudflared"
    if not cloudflared_dir.exists():
        return VerificationResult.failure(
            "cloudflare",
            f"Cloudflared data directory {cloudflared_dir} does not exist",
        )

    # cert.pem for browser_login
    if auth_method == "browser_login":
        cert_path = cloudflared_dir / "cert.pem"
        if not cert_path.exists():
            return VerificationResult.failure(
                "cloudflare",
                f"Browser login cert.pem not found at {cert_path}",
            )

    # cloudflared tunnel list
    list_result = subprocess.run(
        [_cloudflared_bin(), "tunnel", "list"],
        capture_output=True,
        text=True,
        env=_merged_env(cloudflared_env),
    )
    if list_result.returncode != 0:
        return VerificationResult.failure(
            "cloudflare",
            f"cloudflared tunnel list failed: {list_result.stderr.strip()}",
        )

    # cloudflared tunnel info
    if tunnel_uuid:
        info_result = subprocess.run(
            [_cloudflared_bin(), "tunnel", "info", tunnel_uuid],
            capture_output=True,
            text=True,
            env=_merged_env(cloudflared_env),
        )
        if info_result.returncode != 0:
            return VerificationResult.failure(
                "cloudflare",
                f"cloudflared tunnel info failed for {tunnel_uuid}: {info_result.stderr.strip()}",
            )

    # config.yml exists
    config_path = cloudflared_dir / "config.yml"
    if not config_path.exists():
        return VerificationResult.failure(
            "cloudflare",
            f"config.yml not found at {config_path}",
        )

    # credentials JSON exists
    if tunnel_creds_host_path:
        if not Path(tunnel_creds_host_path).exists():
            return VerificationResult.failure(
                "cloudflare",
                f"Tunnel credentials not found at {tunnel_creds_host_path}",
            )

    # docker container running
    if not container_running("cloudflared"):
        return VerificationResult.failure(
            "cloudflare",
            "cloudflared container is not running",
        )

    # metrics endpoint responds (cloudflared can take 30-60s to dial edge and bind metrics)
    metrics_ok = False
    restart_count = 0
    for _ in range(METRICS_RETRY_ATTEMPTS):
        health = subprocess.run(
            ["curl", "-fsS", f"http://127.0.0.1:{metrics_port}/metrics"],
            capture_output=True,
            text=True,
        )
        if health.returncode == 0:
            metrics_ok = True
            break
        rc = docker_run(["inspect", "-f", "{{.RestartCount}}", "cloudflared"], check=False)
        try:
            restart_count = int(rc.stdout.strip())
        except ValueError:
            restart_count = 0
        if restart_count > 0:
            break
        time.sleep(METRICS_RETRY_INTERVAL_SEC)
    if not metrics_ok:
        logs = docker_run(
            ["logs", "--tail", str(LOGS_TAIL_LINES), "cloudflared"],
            check=False,
            timeout=5,
        )
        log_tail = (logs.stdout + logs.stderr).strip() or "(no logs available)"
        msg = f"cloudflared metrics endpoint failed on port {metrics_port}"
        if restart_count > 0:
            msg += f"\ncloudflared container is restarting (RestartCount={restart_count}) — likely crash-looping"
        msg += f"\n--- last {LOGS_TAIL_LINES} lines of docker logs cloudflared ---\n{log_tail}"
        return VerificationResult.failure("cloudflare", msg)

    edge_logs = docker_run(
        ["logs", "--tail", str(EDGE_LOGS_TAIL_LINES), "cloudflared"],
        check=False,
        timeout=5,
    )
    edge_log_text = (edge_logs.stdout + edge_logs.stderr).strip()
    if "Registered tunnel connection" not in edge_log_text:
        msg = "cloudflared metrics endpoint responded, but docker logs do not show a registered edge connection"
        tail = edge_log_text or "(no logs available)"
        msg += f"\n--- last {EDGE_LOGS_TAIL_LINES} lines of docker logs cloudflared ---\n{tail}"
        return VerificationResult.failure("cloudflare", msg)

    return VerificationResult.success("cloudflare", "Cloudflare tunnel is running and healthy")
