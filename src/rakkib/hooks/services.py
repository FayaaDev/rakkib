"""Registry-driven service hooks used by Step 5."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from rich.console import Console

from rakkib.docker import container_running, docker_run
from rakkib.doctor import wait_for_apt_locks
from rakkib.render import render_file
from rakkib.service_catalog import caddy_enabled, service_url
from rakkib.steps import selected_service_defs
from rakkib.tui import progress_spinner, progress_wait


console = Console()


@dataclass(frozen=True)
class HookContext:
    state: object
    svc: dict
    repo: Path
    data_root: Path
    log_path: Path
    registry: dict


def _coerce_hook_context(ctx: HookContext | object, *legacy_args) -> HookContext:
    if isinstance(ctx, HookContext):
        return ctx
    if len(legacy_args) != 5:
        raise TypeError("hook functions require HookContext")
    svc, repo, data_root, log_path, registry = legacy_args
    return HookContext(ctx, svc, repo, data_root, log_path, registry)


_KUMA_MANAGED_PREFIX = "Managed by Rakkib (service: "
_OPENCLAW_INSTALL_URL = "https://openclaw.ai/install.sh"
_CLAUDE_INSTALL_URL = "https://claude.ai/install.sh"
_OPENCLAW_COMMAND_TIMEOUT = 900
_OPENCLAW_GATEWAY_TIMEOUT = 180
_OPENCLAW_PAIRING_TIMEOUT = 180
_OPENCLAW_GATEWAY_BIND = "lan"

PACKAGE_MANAGER_SAFE_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "a",
    "NEEDRESTART_SUSPEND": "1",
    "UCF_FORCE_CONFFOLD": "1",
}


def _run_as_root(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(PACKAGE_MANAGER_SAFE_ENV)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _ensure_node_and_npm() -> None:
    if shutil.which("npm") and shutil.which("node"):
        return

    # Some hosts block outbound HTTP/80. Ubuntu defaults to http:// URIs in ubuntu.sources.
    # Rewrite to https:// so apt can fetch over 443.
    sources = Path("/etc/apt/sources.list.d/ubuntu.sources")
    if sources.exists():
        try:
            text = sources.read_text()
            if "http://" in text:
                _run_as_root(
                    [
                        "sudo",
                        "-n",
                        "bash",
                        "-lc",
                        "sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources",
                    ],
                    timeout=60,
                )
        except Exception:
            # Best-effort: if we cannot rewrite, apt may still work on some networks.
            pass

    wait_for_apt_locks()
    _run_as_root(["sudo", "-n", "apt-get", "update"], timeout=600)
    _run_as_root(
        [
            "sudo",
            "-n",
            "apt-get",
            "install",
            "-y",
            "nodejs",
            "npm",
        ],
        timeout=900,
    )


def claude_install(ctx: HookContext, *legacy_args) -> None:
    """Install Claude Code CLI for the service user."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    _run_as_service_user(
        ctx.state,
        ["bash", "-lc", f"curl -fsSL '{_CLAUDE_INSTALL_URL}' | bash"],
        timeout=900,
        timeout_label="Claude install.sh",
    )


def claude_uninstall(ctx: HookContext, *legacy_args) -> None:
    """Best-effort uninstall of Claude Code CLI."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    # Installer sets up a `claude` launcher. If it exists, try the built-in uninstall.
    _run_as_service_user(ctx.state, ["bash", "-lc", "command -v claude"], check=False)
    _run_as_service_user(ctx.state, ["bash", "-lc", "claude uninstall"], check=False, timeout=300)
    _run_as_service_user(
        ctx.state,
        ["bash", "-lc", "rm -f ~/.local/bin/claude && rm -rf ~/.local/share/claude ~/.claude"],
        check=False,
        timeout=120,
    )


def codex_install(ctx: HookContext, *legacy_args) -> None:
    """Install OpenAI Codex CLI via npm for the service user."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    _ensure_node_and_npm()

    # Install into the user's ~/.local so we don't need root-level npm globals.
    _run_as_service_user(ctx.state, ["bash", "-lc", 'npm config set prefix "$HOME/.local"'], timeout=120)
    _run_as_service_user(
        ctx.state, ["bash", "-lc", "npm i -g @openai/codex"], timeout=900, timeout_label="npm i -g @openai/codex"
    )


def codex_uninstall(ctx: HookContext, *legacy_args) -> None:
    """Best-effort uninstall of OpenAI Codex CLI."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    if not shutil.which("npm"):
        return
    _run_as_service_user(ctx.state, ["bash", "-lc", "npm uninstall -g @openai/codex"], check=False, timeout=600)
    _run_as_service_user(
        ctx.state,
        ["bash", "-lc", "rm -f ~/.local/bin/codex && rm -rf ~/.local/lib/node_modules/@openai/codex"],
        check=False,
        timeout=120,
    )


def _write_text_if_changed(path: Path, content: str) -> bool:
    _ensure_writable_output(path)
    existing = path.read_text() if path.exists() else None
    if existing == content:
        return False
    path.write_text(content)
    return True


def _sudo_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", "-n", *command], capture_output=True, text=True)


def _repair_ownership(path: Path) -> None:
    uid = os.geteuid()
    gid = os.getegid()
    command = ["chown", "-R", f"{uid}:{gid}", str(path)]

    if os.geteuid() == 0:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return

    result = _sudo_run(command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "permission denied"
        raise RuntimeError(
            f"Cannot write generated service artifact {path}: {detail}. "
            "Run `rakkib auth` in the terminal that started the web session, then retry."
        )


def _ensure_writable_output(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        _repair_ownership(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)

    if not os.access(path.parent, os.W_OK | os.X_OK):
        _repair_ownership(path.parent)

    if path.exists() and not os.access(path, os.W_OK):
        _repair_ownership(path)


def _restart_service(data_root: Path, svc_id: str) -> None:
    svc_dir = data_root / "docker" / svc_id
    compose_path = svc_dir / "docker-compose.yml"
    if not compose_path.exists() or not container_running(svc_id):
        return
    docker_run(["compose", "--project-directory", str(svc_dir), "restart"])


def cloudflare_dns_delete(ctx: HookContext, *legacy_args) -> None:
    """Remove Cloudflare service DNS/routing and warn if DNS deletion fails."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    from rakkib.steps import cloudflare

    warning = cloudflare.unpublish_service(ctx.state, ctx.svc, warn=True)
    if warning:
        print(f"WARNING: {warning}")


def _service_admin_user(state) -> tuple[str, Path, int]:
    admin_user = state.get("admin_user") or os.environ.get("SUDO_USER") or os.environ.get("USER")
    if not admin_user:
        raise RuntimeError("OpenClaw setup requires an admin_user in state or a real shell user in the environment.")

    record = pwd.getpwnam(str(admin_user))
    return str(admin_user), Path(record.pw_dir), int(record.pw_uid)


def _run_as_user(
    username: str,
    home_dir: Path,
    user_uid: int,
    command: list[str],
    *,
    check: bool = True,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "USER": username,
            "LOGNAME": username,
            "XDG_RUNTIME_DIR": f"/run/user/{user_uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{user_uid}/bus",
        }
    )
    env.update(PACKAGE_MANAGER_SAFE_ENV)
    if extra_env:
        env.update(extra_env)

    run_cmd = command
    if os.geteuid() == 0 and os.environ.get("USER") != username:
        run_cmd = [
            "sudo",
            "-n",
            "-u",
            username,
            "-H",
            "env",
            f"HOME={home_dir}",
            f"USER={username}",
            f"LOGNAME={username}",
            f"XDG_RUNTIME_DIR=/run/user/{user_uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user_uid}/bus",
        ]
        run_cmd.extend(f"{key}={value}" for key, value in PACKAGE_MANAGER_SAFE_ENV.items())
        if extra_env:
            run_cmd.extend(f"{key}={value}" for key, value in extra_env.items())
        run_cmd += command

    try:
        return subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            check=check,
            env=env,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        label = timeout_label or " ".join(command)
        raise RuntimeError(
            f"{label} timed out after {timeout} seconds. "
            "If apt, dpkg, needrestart, Ubuntu automatic updates, or another package manager is still running, "
            "wait for it to finish and rerun the Rakkib command."
        ) from exc


def _run_as_service_user(
    state,
    command: list[str],
    *,
    check: bool = True,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    admin_user, home_dir, user_uid = _service_admin_user(state)
    return _run_as_user(
        admin_user,
        home_dir,
        user_uid,
        command,
        check=check,
        timeout=timeout,
        extra_env=extra_env,
        timeout_label=timeout_label,
    )


def _root_user() -> tuple[str, Path, int]:
    record = pwd.getpwnam("root")
    return "root", Path(record.pw_dir), int(record.pw_uid)


def _resolve_openclaw_bin_for_user(state, username: str) -> Path | None:
    direct = shutil.which("openclaw") if os.environ.get("USER") == username else None
    if direct:
        return Path(direct).resolve()

    if username == "root":
        _, home_dir, user_uid = _root_user()
        result = _run_as_user("root", home_dir, user_uid, ["bash", "-lc", "command -v openclaw"], check=False)
    else:
        result = _run_as_service_user(state, ["bash", "-lc", "command -v openclaw"], check=False)
    if result.returncode != 0:
        return None

    resolved = result.stdout.strip()
    return Path(resolved).resolve() if resolved else None


def _resolve_openclaw_bin(state) -> Path | None:
    admin_user, _, _ = _service_admin_user(state)
    return _resolve_openclaw_bin_for_user(state, admin_user)


def _run_openclaw(
    state, openclaw_bin: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run_as_service_user(
        state,
        [str(openclaw_bin), *args],
        check=check,
        timeout=_OPENCLAW_COMMAND_TIMEOUT,
        timeout_label=f"OpenClaw command `{openclaw_bin} {' '.join(args)}`",
    )


def _openclaw_output(result: subprocess.CompletedProcess[str]) -> str:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    parts = [f"exit code: {result.returncode}"]
    if stdout:
        parts.append(f"stdout: {stdout}")
    if stderr:
        parts.append(f"stderr: {stderr}")
    return " | ".join(parts)


def _openclaw_paths(home_dir: Path) -> tuple[Path, Path]:
    return (
        home_dir / ".openclaw" / "openclaw.json",
        home_dir / ".config" / "systemd" / "user" / "openclaw-gateway.service",
    )


def _purge_openclaw_user_artifacts(username: str, home_dir: Path, user_uid: int) -> None:
    script = r"""
set +e
if command -v openclaw >/dev/null 2>&1; then
  openclaw gateway stop >/dev/null 2>&1 || true
  openclaw gateway uninstall >/dev/null 2>&1 || true
fi
if command -v npm >/dev/null 2>&1; then
  npm uninstall -g openclaw >/dev/null 2>&1 || true
  npm_root="$(npm root -g 2>/dev/null || true)"
  npm_prefix="$(npm prefix -g 2>/dev/null || true)"
  if [ -n "$npm_root" ]; then
    rm -rf "$npm_root"/openclaw "$npm_root"/.openclaw-* 2>/dev/null || true
  fi
  if [ -n "$npm_prefix" ]; then
    rm -f "$npm_prefix/bin/openclaw" 2>/dev/null || true
  fi
fi
rm -f "$HOME/.local/bin/openclaw" "$HOME/.config/systemd/user/default.target.wants/openclaw-gateway.service"
rm -rf "$HOME/.openclaw" "$HOME/.config/openclaw" "$HOME/.local/share/openclaw" "$HOME/.cache/openclaw" "$HOME/openclaw"
systemctl --user daemon-reload >/dev/null 2>&1 || true
"""
    result = _run_as_user(username, home_dir, user_uid, ["bash", "-lc", script], check=False, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"OpenClaw artifact purge failed. Command output: {result.stdout.strip() or result.stderr.strip()}"
        )


def _openclaw_dashboard_url(state) -> str | None:
    """Return the public dashboard URL with token pre-filled, or None if unavailable."""
    _, home_dir, _ = _service_admin_user(state)
    config_path, _ = _openclaw_paths(home_dir)
    try:
        config = json.loads(config_path.read_text())
        token = config.get("gateway", {}).get("auth", {}).get("token")
    except Exception:
        return None
    if not token or not isinstance(token, str):
        return None
    domain = str(state.get("domain") or "").strip()
    subdomain = str(state.get("subdomains.openclaw") or state.get("OPENCLAW_SUBDOMAIN") or "claw").strip()
    base = (
        f"https://{subdomain}.{domain}" if caddy_enabled(state) and domain and subdomain else "http://127.0.0.1:18789"
    )
    return f"{base}/?token={token}"


def _openclaw_wait_for_pairing(state, openclaw_bin: Path) -> None:
    """If no devices are paired yet, prompt the user and auto-approve the first pairing request."""
    list_result = _run_openclaw(state, openclaw_bin, ["devices", "list", "--json"], check=False)
    if list_result.returncode != 0:
        return
    try:
        devices = json.loads(list_result.stdout)
    except Exception:
        return

    if devices.get("paired"):
        return  # Already has paired devices — nothing to do.

    console.print(
        "  [bold]OpenClaw:[/bold] Open the dashboard and click [bold]Connect[/bold] to pair your device."
        f" Waiting up to {_OPENCLAW_PAIRING_TIMEOUT}s..."
    )

    def poll_pairing() -> bool:
        list_result = _run_openclaw(state, openclaw_bin, ["devices", "list", "--json"], check=False)
        if list_result.returncode != 0:
            return False
        try:
            devices = json.loads(list_result.stdout)
        except Exception:
            return False

        if devices.get("paired"):
            # Approved via another path before we could catch the pending state.
            console.print("[green]  OpenClaw: device paired.[/green]")
            return True

        if devices.get("pending"):
            approve = _run_openclaw(state, openclaw_bin, ["devices", "approve", "--latest"], check=False)
            if approve.returncode == 0:
                console.print("[green]  OpenClaw: device paired and approved automatically.[/green]")
            else:
                console.print(
                    f"[yellow]  OpenClaw: pairing request found but auto-approve failed "
                    f"({_openclaw_output(approve)}). Run `openclaw devices approve --latest` manually.[/yellow]"
                )
            return True

        return False

    if progress_wait("Waiting for OpenClaw device pairing...", _OPENCLAW_PAIRING_TIMEOUT, poll_pairing, interval=3):
        return

    console.print(
        "[dim]  OpenClaw: no pairing request received within the timeout. "
        "Open the dashboard and click Connect, then run `openclaw devices approve --latest` on the server.[/dim]"
    )


def _wait_for_openclaw_package_locks() -> None:
    if shutil.which("apt-get") is None:
        return

    with progress_spinner("Ubuntu automatic updates are running; waiting for apt/dpkg to become available..."):
        lock_error = wait_for_apt_locks()
    if lock_error:
        raise RuntimeError(f"OpenClaw setup cannot continue while apt/dpkg is locked. {lock_error}")


def _openclaw_gateway_healthcheck(timeout: int = _OPENCLAW_GATEWAY_TIMEOUT) -> bool:
    def poll() -> bool:
        result = subprocess.run(
            ["curl", "-fsS", "http://127.0.0.1:18789/healthz", "-o", "/dev/null"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    return progress_wait("Waiting for OpenClaw gateway health...", timeout, poll, interval=2)


def _migrate_root_openclaw_service(state) -> None:
    admin_user, _, _ = _service_admin_user(state)
    if admin_user == "root":
        return

    root_service = Path("/root/.config/systemd/user/openclaw-gateway.service")
    root_config = Path("/root/.openclaw/openclaw.json")
    if not root_service.exists() and not root_config.exists():
        return

    root_bin = _resolve_openclaw_bin_for_user(state, "root")
    if root_bin is None:
        return

    root_stop = _run_as_user("root", Path("/root"), 0, [str(root_bin), "gateway", "stop"], check=False)
    root_uninstall = _run_as_user("root", Path("/root"), 0, [str(root_bin), "gateway", "uninstall"], check=False)
    if root_uninstall.returncode == 0:
        return

    output = f"{root_stop.stdout}\n{root_stop.stderr}\n{root_uninstall.stdout}\n{root_uninstall.stderr}".lower()
    if "not installed" in output or "not found" in output:
        return
    raise RuntimeError(
        "Root-owned OpenClaw gateway cleanup failed before migrating to the admin user. "
        f"Command output: {root_uninstall.stdout.strip() or root_uninstall.stderr.strip() or root_stop.stdout.strip() or root_stop.stderr.strip()}"
    )


def _ensure_openclaw_gateway_bind(state, openclaw_bin: Path) -> None:
    bind = _run_openclaw(state, openclaw_bin, ["config", "set", "gateway.bind", _OPENCLAW_GATEWAY_BIND], check=False)
    if bind.returncode != 0:
        raise RuntimeError(
            f"OpenClaw gateway bind update failed. Command output: {bind.stdout.strip() or bind.stderr.strip()}"
        )


def _openclaw_allowed_origins(state) -> list[str]:
    origins = ["http://127.0.0.1:18789", "http://localhost:18789"]
    domain = str(state.get("domain") or "").strip()
    subdomain = str(state.get("subdomains.openclaw") or state.get("OPENCLAW_SUBDOMAIN") or "claw").strip()
    if caddy_enabled(state) and domain and subdomain:
        origins.insert(0, f"https://{subdomain}.{domain}")
        origins.insert(1, f"http://{subdomain}.{domain}")
    return origins


def _ensure_openclaw_control_ui_allowed_origins(state, openclaw_bin: Path) -> None:
    origins = json.dumps(_openclaw_allowed_origins(state))
    result = _run_openclaw(
        state,
        openclaw_bin,
        ["config", "set", "gateway.controlUi.allowedOrigins", origins],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "OpenClaw Control UI allowedOrigins update failed. "
            f"Command output: {result.stdout.strip() or result.stderr.strip()}"
        )


def _homepage_services_content(state, registry: dict) -> str:
    groups: dict[str, list[str]] = {}
    for selected_svc in selected_service_defs(state, registry):
        homepage = selected_svc.get("homepage") or {}
        if not homepage:
            continue

        href = service_url(state, selected_svc)
        block_lines = [f"    - {homepage['name']}:"]
        if href:
            block_lines.append(f"        href: {href}")
        block_lines.extend(
            [
                f"        description: {homepage['description']}",
                f"        icon: {homepage['icon']}",
            ]
        )
        block = "\n".join(block_lines)
        groups.setdefault(homepage["category"], []).append(block)

    lines = ["# Generated by Rakkib — edit manually or re-run Rakkib to regenerate."]
    for category in sorted(groups):
        lines.append(f"- {category}:")
        lines.extend(groups[category])
    return "\n".join(lines) + "\n"


def _resolve_monitoring_target(state, svc: dict, monitoring: dict) -> dict[str, object]:
    target = monitoring.get("target", "public_url")
    monitor_type = monitoring.get("type", "http")
    path = monitoring.get("path", "/")
    domain = state.get("domain", "localhost")
    subdomain = state.get(f"subdomains.{svc['id']}", svc["id"])
    hostname = f"{subdomain}.{domain}"

    if target == "public_url":
        if monitor_type == "ping":
            return {"hostname": hostname}
        if monitor_type == "tcp":
            if caddy_enabled(state):
                return {"hostname": hostname, "port": int(monitoring.get("port", 443))}
            access = svc.get("internal_access") or {}
            if not access.get("enabled") or access.get("host_port") is None:
                raise ValueError(f"Service {svc['id']} monitoring.public_url requires internal_access.host_port")
            return {"hostname": str(state.get("lan_ip") or "127.0.0.1"), "port": int(access["host_port"])}
        url = service_url(state, svc, path=path)
        if not url:
            raise ValueError(f"Service {svc['id']} monitoring.public_url has no user-facing URL")
        return {"url": url}

    if target == "host_port":
        port = monitoring.get("port") or svc.get("default_port")
        if port is None:
            raise ValueError(f"Service {svc['id']} monitoring.host_port requires a port")
        if monitor_type == "tcp":
            return {"hostname": "127.0.0.1", "port": int(port)}
        normalized_path = path if str(path).startswith("/") else f"/{path}"
        scheme = "https" if monitor_type == "https" else "http"
        return {"url": f"{scheme}://127.0.0.1:{int(port)}{normalized_path}"}

    if target == "container":
        port = monitoring.get("port") or svc.get("default_port")
        container_name = svc.get("container_name", svc["id"])
        if monitor_type == "ping":
            return {"hostname": container_name}
        if monitor_type == "tcp":
            if port is None:
                raise ValueError(f"Service {svc['id']} monitoring.container requires a port")
            return {"hostname": container_name, "port": int(port)}
        if port is None:
            raise ValueError(f"Service {svc['id']} monitoring.container requires a port")
        normalized_path = path if str(path).startswith("/") else f"/{path}"
        scheme = "https" if monitor_type == "https" else "http"
        return {"url": f"{scheme}://{container_name}:{int(port)}{normalized_path}"}

    if target == "custom":
        custom_url = monitoring.get("custom_url")
        if not custom_url:
            raise ValueError(f"Service {svc['id']} monitoring.custom requires custom_url")
        if monitor_type in {"ping", "tcp"}:
            host = monitoring.get("hostname")
            port = monitoring.get("port")
            if monitor_type == "ping" and host:
                return {"hostname": host}
            if monitor_type == "tcp" and host and port is not None:
                return {"hostname": host, "port": int(port)}
        return {"url": str(custom_url)}

    raise ValueError(f"Unknown monitoring target '{target}' for service {svc['id']}")


def _kuma_monitor_spec(state, svc: dict) -> dict[str, object] | None:
    monitoring = svc.get("monitoring") or {}
    if not monitoring.get("enabled"):
        return None

    spec: dict[str, object] = {
        "service_id": svc["id"],
        "name": monitoring.get("name") or (svc.get("homepage") or {}).get("name") or svc["id"],
        "type": monitoring.get("type", "http"),
        "interval": int(monitoring.get("interval", 60)),
        "timeout": int(monitoring.get("timeout", 10)),
        "maxretries": int(monitoring.get("retries", 3)),
    }
    spec.update(_resolve_monitoring_target(state, svc, monitoring))
    return spec


def _uptime_kuma_sync_payload(state, registry: dict) -> dict[str, object]:
    monitors = []
    for svc in selected_service_defs(state, registry):
        spec = _kuma_monitor_spec(state, svc)
        if spec is not None:
            monitors.append(spec)

    return {
        "admin": {
            "username": state.get("UPTIME_KUMA_ADMIN_USER", "admin"),
            "password": state.get("UPTIME_KUMA_ADMIN_PASS"),
        },
        "managed_prefix": _KUMA_MANAGED_PREFIX,
        "monitors": monitors,
    }


def homepage_services_yaml(ctx: HookContext, *legacy_args) -> None:
    """Generate Homepage services.yaml from registry homepage metadata."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    config_dir = ctx.data_root / "data" / "homepage" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    _write_text_if_changed(config_dir / "services.yaml", _homepage_services_content(ctx.state, ctx.registry))


def _service_postgres_credentials(state, svc: dict) -> tuple[str, str, str]:
    postgres = svc.get("postgres") or {}
    role = postgres.get("role", svc["id"])
    db_name = postgres.get("db", role)
    password_key = postgres.get("password_key")
    if not password_key:
        raise RuntimeError(f"postgres login pre-flight is configured for service '{svc['id']}' without a password_key")

    password = state.get(f"secrets.values.{password_key}")
    if password is None:
        password = state.get(password_key)
    if not password:
        raise RuntimeError(
            f"postgres login pre-flight failed for service '{svc['id']}': missing database password "
            f"'{password_key}' in state. Re-run `rakkib pull` or sync services again so Step 4 can provision Postgres."
        )

    return role, db_name, str(password)


def sync_shared_artifacts(state, repo: Path, data_root: Path, registry: dict) -> None:
    """Regenerate shared artifacts that derive from the full selected service set."""
    selected_ids = {svc["id"] for svc in selected_service_defs(state, registry)}

    homepage_changed = False
    if "homepage" in selected_ids:
        config_dir = data_root / "data" / "homepage" / "config"
        homepage_changed = _write_text_if_changed(
            config_dir / "services.yaml",
            _homepage_services_content(state, registry),
        )

    if homepage_changed:
        _restart_service(data_root, "homepage")

    if "uptime-kuma" in selected_ids:
        kuma_data_dir = data_root / "data" / "uptime-kuma"
        kuma_data_dir.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(_uptime_kuma_sync_payload(state, registry), indent=2, sort_keys=True) + "\n"
        _write_text_if_changed(kuma_data_dir / "rakkib-monitors.json", payload)

        sync_script = repo / "templates" / "docker" / "uptime-kuma" / "sync-monitors.cjs.tmpl"
        _ensure_writable_output(kuma_data_dir / "sync-monitors.cjs")
        render_file(sync_script, kuma_data_dir / "sync-monitors.cjs", state)

        if container_running("uptime-kuma"):
            result = docker_run(
                ["exec", "uptime-kuma", "node", "/app/data/sync-monitors.cjs"],
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
                print(f"Warning: Uptime Kuma monitor sync failed: {detail}")


def service_postgres_login_preflight(ctx: HookContext, *legacy_args) -> None:
    """Fail fast if a service cannot log into the shared postgres database."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    role, db_name, password = _service_postgres_credentials(ctx.state, ctx.svc)

    result = docker_run(
        [
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            "postgres",
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            role,
            "-d",
            db_name,
            "-c",
            "select 1;",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"postgres login pre-flight failed for service '{ctx.svc['id']}': the database login is not ready "
            "in the postgres container. Ensure Step 4 completed successfully and that the "
            "rendered service password matches the Postgres role password.\n"
            f"psql output: {result.stdout.strip() or result.stderr.strip()}"
        )


def _homecinema_secret(state, key: str) -> str:
    value = state.get(key)
    if value is None:
        value = state.get(f"secrets.values.{key}")
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Automated Home Cinema is missing generated secret {key}. Re-run `rakkib pull`.")
    return str(value)


def _homecinema_arr_config(app_name: str, port: int, api_key: str) -> str:
    return f"""<Config>
  <BindAddress>*</BindAddress>
  <Port>{port}</Port>
  <SslPort>9898</SslPort>
  <EnableSsl>False</EnableSsl>
  <LaunchBrowser>False</LaunchBrowser>
  <AuthenticationMethod>Forms</AuthenticationMethod>
  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>
  <Branch>master</Branch>
  <LogLevel>info</LogLevel>
  <SslCertPath></SslCertPath>
  <SslCertPassword></SslCertPassword>
  <UrlBase></UrlBase>
  <InstanceName>{app_name}</InstanceName>
  <UpdateMechanism>Docker</UpdateMechanism>
  <ApiKey>{api_key}</ApiKey>
</Config>
"""


def _homecinema_write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    _ensure_writable_output(path)
    path.write_text(content)
    return True


def _homecinema_ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        _repair_ownership(path.parent)
        path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK | os.X_OK):
        _repair_ownership(path)


def _homecinema_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    expected: tuple[int, ...] = (200,),
    timeout: int = 15,
) -> tuple[int, str]:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            if response.status not in expected:
                raise RuntimeError(f"HTTP {response.status} from {url}: {text[:300]}")
            return response.status, text
    except HTTPError as error:
        text = error.read().decode("utf-8", errors="replace")
        if error.code in expected:
            return error.code, text
        raise RuntimeError(f"HTTP {error.code} from {url}: {text[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach {url}: {error.reason}") from error


def _homecinema_json(
    url: str,
    *,
    method: str = "GET",
    api_key: str | None = None,
    payload: object | None = None,
    expected: tuple[int, ...] = (200,),
) -> object:
    headers = {"Accept": "application/json"}
    body = None
    if api_key:
        headers["X-Api-Key"] = api_key
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    _, text = _homecinema_request(url, method=method, headers=headers, body=body, expected=expected)
    return json.loads(text) if text.strip() else None


def _homecinema_form(url: str, data: dict[str, object], *, expected: tuple[int, ...] = (200,)) -> int:
    body = urlencode({key: str(value) for key, value in data.items()}).encode("utf-8")
    status, _ = _homecinema_request(
        url,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
        expected=expected,
    )
    return status


def _homecinema_wait(url: str, *, api_key: str | None = None, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    headers = {"X-Api-Key": api_key} if api_key else None
    while time.monotonic() < deadline:
        try:
            _homecinema_request(url, headers=headers)
            return
        except RuntimeError as error:
            last_error = str(error)
            time.sleep(2)
    raise RuntimeError(f"Automated Home Cinema dependency did not become ready at {url}: {last_error}")


def homecinema_seed_configs(ctx: HookContext, *legacy_args) -> None:
    """Seed first-run HomeCinema config files without overwriting user-managed config."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    media_root = ctx.data_root / "data" / "plex" / "media"
    media_dirs = ("movies", "tv", "downloads/torrents/movies", "downloads/torrents/tv", "downloads/incomplete")
    for relative in media_dirs:
        _homecinema_ensure_dir(media_root / relative)

    app_configs = (
        ("radarr", "Radarr", 7878, "RADARR_API_KEY"),
        ("sonarr", "Sonarr", 8989, "SONARR_API_KEY"),
        ("prowlarr", "Prowlarr", 9696, "PROWLARR_API_KEY"),
    )
    for service_id, app_name, port, key in app_configs:
        config_path = ctx.data_root / "data" / "plex" / service_id / "config" / "config.xml"
        _homecinema_write_if_missing(
            config_path,
            _homecinema_arr_config(app_name, port, _homecinema_secret(ctx.state, key)),
        )

    qbittorrent_conf = ctx.data_root / "data" / "plex" / "qbittorrent" / "config" / "qBittorrent" / "qBittorrent.conf"
    _homecinema_write_if_missing(
        qbittorrent_conf,
        r"""[Application]
FileLogger\Enabled=true

[BitTorrent]
Session\DefaultSavePath=/data/downloads/torrents/
Session\TempPath=/data/downloads/incomplete/
Session\TempPathEnabled=true

[LegalNotice]
Accepted=true

[Preferences]
Downloads\SavePath=/data/downloads/torrents/
Downloads\TempPath=/data/downloads/incomplete/
Downloads\TempPathEnabled=true
WebUI\Address=*
WebUI\AuthSubnetWhitelist=172.16.0.0/12,127.0.0.1/32
WebUI\AuthSubnetWhitelistEnabled=true
WebUI\LocalHostAuth=false
WebUI\Port=8080
""",
    )


def _homecinema_configure_qbittorrent(ctx: HookContext) -> None:
    username = _homecinema_secret(ctx.state, "QBITTORRENT_USERNAME")
    password = _homecinema_secret(ctx.state, "QBITTORRENT_PASSWORD")
    base_url = "http://127.0.0.1:8081/api/v2"
    _homecinema_wait(f"{base_url}/app/version")

    _homecinema_form(
        f"{base_url}/app/setPreferences",
        {
            "json": json.dumps(
                {
                    "save_path": "/data/downloads/torrents/",
                    "temp_path": "/data/downloads/incomplete/",
                    "temp_path_enabled": True,
                    "web_ui_username": username,
                    "web_ui_password": password,
                    "bypass_local_auth": True,
                    "bypass_auth_subnet_whitelist_enabled": True,
                    "bypass_auth_subnet_whitelist": "172.16.0.0/12,127.0.0.1/32",
                }
            )
        },
    )

    for category, save_path in {
        "movies": "/data/downloads/torrents/movies",
        "tv": "/data/downloads/torrents/tv",
    }.items():
        status = _homecinema_form(
            f"{base_url}/torrents/createCategory",
            {"category": category, "savePath": save_path},
            expected=(200, 409),
        )
        if status == 409:
            _homecinema_form(f"{base_url}/torrents/editCategory", {"category": category, "savePath": save_path})


def _homecinema_configure_arr(
    *,
    app_name: str,
    base_url: str,
    api_key: str,
    root_path: str,
    category: str,
    category_field: str,
    recent_priority_field: str,
    older_priority_field: str,
    username: str,
    password: str,
) -> None:
    _homecinema_wait(f"{base_url}/ping")

    roots = _homecinema_json(f"{base_url}/api/v3/rootfolder", api_key=api_key)
    if not any(str(item.get("path")) == root_path for item in roots or [] if isinstance(item, dict)):
        _homecinema_json(
            f"{base_url}/api/v3/rootfolder",
            method="POST",
            api_key=api_key,
            payload={"path": root_path},
            expected=(200, 201, 202),
        )

    clients = _homecinema_json(f"{base_url}/api/v3/downloadclient", api_key=api_key)
    if any(str(item.get("name")) == "qBittorrent" for item in clients or [] if isinstance(item, dict)):
        return

    _homecinema_json(
        f"{base_url}/api/v3/downloadclient",
        method="POST",
        api_key=api_key,
        payload={
            "enable": True,
            "protocol": "torrent",
            "priority": 1,
            "removeCompletedDownloads": True,
            "removeFailedDownloads": True,
            "name": "qBittorrent",
            "implementation": "QBittorrent",
            "implementationName": "qBittorrent",
            "configContract": "QBittorrentSettings",
            "fields": [
                {"name": "host", "value": "qbittorrent"},
                {"name": "port", "value": 8080},
                {"name": "useSsl", "value": False},
                {"name": "urlBase", "value": ""},
                {"name": "username", "value": username},
                {"name": "password", "value": password},
                {"name": category_field, "value": category},
                {"name": recent_priority_field, "value": 0},
                {"name": older_priority_field, "value": 0},
                {"name": "initialState", "value": 0},
            ],
            "tags": [],
        },
        expected=(200, 201, 202),
    )
    console.print(f"[dim]Configured {app_name} download client.[/dim]")


def _homecinema_configure_prowlarr(ctx: HookContext) -> None:
    prowlarr_key = _homecinema_secret(ctx.state, "PROWLARR_API_KEY")
    radarr_key = _homecinema_secret(ctx.state, "RADARR_API_KEY")
    sonarr_key = _homecinema_secret(ctx.state, "SONARR_API_KEY")
    base_url = "http://127.0.0.1:9696"
    _homecinema_wait(f"{base_url}/ping")

    apps = _homecinema_json(f"{base_url}/api/v1/applications", api_key=prowlarr_key)
    existing = {str(item.get("name")) for item in apps or [] if isinstance(item, dict)}
    definitions = [
        (
            "Radarr",
            "Radarr",
            "RadarrSettings",
            "http://radarr:7878",
            radarr_key,
            [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060],
        ),
        (
            "Sonarr",
            "Sonarr",
            "SonarrSettings",
            "http://sonarr:8989",
            sonarr_key,
            [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080],
        ),
    ]
    for name, implementation, contract, app_url, api_key, categories in definitions:
        if name in existing:
            continue
        _homecinema_json(
            f"{base_url}/api/v1/applications",
            method="POST",
            api_key=prowlarr_key,
            payload={
                "name": name,
                "syncLevel": "fullSync",
                "implementation": implementation,
                "implementationName": implementation,
                "configContract": contract,
                "fields": [
                    {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                    {"name": "baseUrl", "value": app_url},
                    {"name": "apiKey", "value": api_key},
                    {"name": "syncCategories", "value": categories},
                    {"name": "animeSyncCategories", "value": []},
                ],
                "tags": [],
            },
            expected=(200, 201, 202),
        )
        console.print(f"[dim]Configured Prowlarr application sync for {name}.[/dim]")


def homecinema_configure(ctx: HookContext, *legacy_args) -> None:
    """Wire HomeCinema internals after containers are up."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    homecinema_seed_configs(ctx)

    username = _homecinema_secret(ctx.state, "QBITTORRENT_USERNAME")
    password = _homecinema_secret(ctx.state, "QBITTORRENT_PASSWORD")
    _homecinema_configure_qbittorrent(ctx)
    _homecinema_configure_arr(
        app_name="Radarr",
        base_url="http://127.0.0.1:7878",
        api_key=_homecinema_secret(ctx.state, "RADARR_API_KEY"),
        root_path="/data/movies",
        category="movies",
        category_field="movieCategory",
        recent_priority_field="recentMoviePriority",
        older_priority_field="olderMoviePriority",
        username=username,
        password=password,
    )
    _homecinema_configure_arr(
        app_name="Sonarr",
        base_url="http://127.0.0.1:8989",
        api_key=_homecinema_secret(ctx.state, "SONARR_API_KEY"),
        root_path="/data/tv",
        category="tv",
        category_field="tvCategory",
        recent_priority_field="recentTvPriority",
        older_priority_field="olderTvPriority",
        username=username,
        password=password,
    )
    _homecinema_configure_prowlarr(ctx)


def openclaw_install(ctx: HookContext, *legacy_args) -> None:
    """Install OpenClaw and ensure a minimal local gateway is configured."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    if shutil.which("curl") is None:
        raise RuntimeError("OpenClaw setup requires curl. Install curl and re-run `rakkib pull`.")

    _wait_for_openclaw_package_locks()

    if os.geteuid() == 0:
        admin_user, _, _ = _service_admin_user(ctx.state)
        subprocess.run(["loginctl", "enable-linger", admin_user], capture_output=True, text=True, check=True)
        _migrate_root_openclaw_service(ctx.state)

    openclaw_bin = _resolve_openclaw_bin(ctx.state)
    if openclaw_bin is None:
        with progress_spinner("Installing OpenClaw CLI..."):
            install = _run_as_service_user(
                ctx.state,
                ["bash", "-lc", f"curl -fsSL {_OPENCLAW_INSTALL_URL} | bash -s -- --no-onboard --no-prompt"],
                check=False,
                timeout=_OPENCLAW_COMMAND_TIMEOUT,
                extra_env={"OPENCLAW_NO_PROMPT": "1"},
                timeout_label="OpenClaw installer",
            )
        if install.returncode != 0:
            raise RuntimeError(f"OpenClaw installation failed. Command output: {_openclaw_output(install)}")
        openclaw_bin = _resolve_openclaw_bin(ctx.state)
        if openclaw_bin is None:
            raise RuntimeError(
                "OpenClaw installation completed but the `openclaw` CLI is still unavailable on PATH. "
                "Check the target user's global npm/bin path, such as `$(npm prefix -g)/bin`, and re-run `rakkib pull`."
            )

    version = _run_openclaw(ctx.state, openclaw_bin, ["--version"], check=False)
    if version.returncode != 0:
        raise RuntimeError(
            f"OpenClaw CLI was found but `openclaw --version` failed. Command output: {_openclaw_output(version)}"
        )

    _, home_dir, _ = _service_admin_user(ctx.state)
    config_path, service_path = _openclaw_paths(home_dir)
    if config_path.exists():
        console.print("[dim]  openclaw: already onboarded, updating config...[/dim]")
        _ensure_openclaw_gateway_bind(ctx.state, openclaw_bin)
        _ensure_openclaw_control_ui_allowed_origins(ctx.state, openclaw_bin)
        return

    with progress_spinner("Running OpenClaw onboarding..."):
        onboard = _run_openclaw(
            ctx.state,
            openclaw_bin,
            [
                "onboard",
                "--non-interactive",
                "--mode",
                "local",
                "--auth-choice",
                "skip",
                "--gateway-port",
                "18789",
                "--gateway-bind",
                _OPENCLAW_GATEWAY_BIND,
                "--install-daemon",
                "--skip-bootstrap",
                "--skip-skills",
                "--accept-risk",
            ],
            check=False,
        )
    if onboard.returncode != 0:
        if not (config_path.exists() and service_path.exists()):
            raise RuntimeError(f"OpenClaw onboarding failed. Command output: {_openclaw_output(onboard)}")

    _ensure_openclaw_gateway_bind(ctx.state, openclaw_bin)
    _ensure_openclaw_control_ui_allowed_origins(ctx.state, openclaw_bin)


def openclaw_gateway_restart(ctx: HookContext, *legacy_args) -> None:
    """Ensure the OpenClaw gateway daemon is installed, running, and healthy."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    openclaw_bin = _resolve_openclaw_bin(ctx.state)
    if openclaw_bin is None:
        raise RuntimeError(
            "OpenClaw gateway restart requested but the `openclaw` CLI is not installed for the admin user."
        )

    with progress_spinner("Installing OpenClaw gateway service..."):
        install = _run_openclaw(ctx.state, openclaw_bin, ["gateway", "install", "--force"], check=False)
    if install.returncode != 0:
        raise RuntimeError(f"OpenClaw gateway install failed. Command output: {_openclaw_output(install)}")

    with progress_spinner("Restarting OpenClaw gateway..."):
        restart = _run_openclaw(ctx.state, openclaw_bin, ["gateway", "restart"], check=False)
    if restart.returncode != 0:
        raise RuntimeError(f"OpenClaw gateway restart failed. Command output: {_openclaw_output(restart)}")

    if not _openclaw_gateway_healthcheck():
        status = _run_openclaw(ctx.state, openclaw_bin, ["gateway", "status", "--require-rpc"], check=False)
        raise RuntimeError(
            "OpenClaw gateway did not become healthy on 127.0.0.1:18789/healthz. "
            f"Status output: {_openclaw_output(status)}"
        )

    url = _openclaw_dashboard_url(ctx.state)
    if url:
        ctx.state.set("deployed.special_urls.openclaw", url)
        console.print(f"[green]  OpenClaw ready:[/green] {url}")

    _openclaw_wait_for_pairing(ctx.state, openclaw_bin)


def openclaw_gateway_uninstall(ctx: HookContext, *legacy_args) -> None:
    """Fully remove OpenClaw CLI/package and managed gateway state."""
    ctx = _coerce_hook_context(ctx, *legacy_args)
    openclaw_bin = _resolve_openclaw_bin(ctx.state)
    if openclaw_bin is not None:
        uninstall = _run_openclaw(ctx.state, openclaw_bin, ["gateway", "uninstall"], check=False)
        if uninstall.returncode != 0:
            output = f"{uninstall.stdout}\n{uninstall.stderr}".lower()
            if "not installed" not in output and "not found" not in output:
                raise RuntimeError(
                    "OpenClaw gateway uninstall failed. "
                    f"Command output: {uninstall.stdout.strip() or uninstall.stderr.strip()}"
                )

    username, home_dir, user_uid = _service_admin_user(ctx.state)
    _purge_openclaw_user_artifacts(username, home_dir, user_uid)
    root_config = Path("/root/.openclaw")
    root_service = Path("/root/.config/systemd/user/openclaw-gateway.service")
    if username != "root" and (root_config.exists() or root_service.exists()):
        _purge_openclaw_user_artifacts("root", Path("/root"), 0)


POST_RENDER_HOOKS = {
    "homepage_services_yaml": homepage_services_yaml,
}

PRE_START_HOOKS = {
    "service_postgres_login_preflight": service_postgres_login_preflight,
    "homecinema_seed_configs": homecinema_seed_configs,
    "openclaw_install": openclaw_install,
    "claude_install": claude_install,
    "codex_install": codex_install,
}

POST_START_HOOKS = {
    "openclaw_gateway_restart": openclaw_gateway_restart,
    "homecinema_configure": homecinema_configure,
}

RESTART_HOOKS = {
    "openclaw_gateway_restart": openclaw_gateway_restart,
}

REMOVE_HOOKS = {
    "cloudflare_dns_delete": cloudflare_dns_delete,
    "openclaw_gateway_uninstall": openclaw_gateway_uninstall,
    "claude_uninstall": claude_uninstall,
    "codex_uninstall": codex_uninstall,
}
