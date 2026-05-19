"""Step 5 — Services.

Deploy foundation bundle services and selected optional services.
"""

from __future__ import annotations

from copy import deepcopy
import functools
import os
import pwd
import socket
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import yaml
from rich.console import Console

from rakkib.docker import (
    DockerError,
    compose_down,
    compose_up,
    container_publishes_port,
    container_running,
    create_network,
    docker_run,
    health_check,
)
from rakkib.hooks.services import (
    HookContext,
    POST_RENDER_HOOKS,
    POST_START_HOOKS,
    PRE_START_HOOKS,
    REMOVE_HOOKS,
    RESTART_HOOKS,
    sync_shared_artifacts,
)
from rakkib.normalize import eval_when
from rakkib.postgres_sql import (
    postgres_identifier,
    postgres_literal,
    validate_registry_postgres_identifiers,
)
from rakkib.render import render_file
from rakkib.service_catalog import caddy_enabled, cloudflare_enabled, service_url, validate_registry_internal_access
from rakkib.secret_utils import FACTORIES
from rakkib.state import State
from rakkib.steps import VerificationResult, selected_service_defs
from rakkib.tui import progress_spinner
from rakkib.util import RAKKIB_DATA_DIR


console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_dir() -> Path:
    """Return the package data directory (contains ``templates/``)."""
    return RAKKIB_DATA_DIR


@functools.lru_cache(maxsize=1)
def _load_registry() -> dict:
    with (_repo_dir() / "registry.yaml").open() as fh:
        registry = yaml.safe_load(fh)
    validate_registry_postgres_identifiers(registry)
    validate_registry_internal_access(registry)
    return registry


def _selected_and_always_services(state: State, registry: dict) -> list[dict]:
    always = [svc for svc in registry["services"] if svc.get("state_bucket") == "always" and svc.get("secrets")]
    return always + selected_service_defs(state, registry)


def _resolve_declared_value(spec: str | dict) -> str:
    if isinstance(spec, str):
        spec = {"factory": spec}

    if "value" in spec:
        return str(spec["value"])

    factory_name = spec.get("factory")
    if factory_name not in FACTORIES:
        raise ValueError(f"Unknown secret factory: {factory_name}")

    factory = FACTORIES[factory_name]
    kwargs = dict(spec.get("kwargs") or {})
    if "length" in spec:
        kwargs.setdefault("length", spec["length"])
    return factory(**kwargs)


def _condition_matches(condition: dict, state: State, selected_ids: set[str]) -> bool:
    required_services = condition.get("when_services", [])
    if any(service_id not in selected_ids for service_id in required_services):
        return False

    when = condition.get("when")
    if when and not eval_when(when, state):
        return False

    return True


def _ensure_service_runtime_env(state: State) -> None:
    """Populate common container env values that templates may reference."""
    if state.get("data_root") is None:
        state.set("data_root", str(state.data_root))
    if state.get("docker_net") is None:
        state.set("docker_net", "caddy_net")

    if state.get("admin_uid") is None or state.get("admin_gid") is None:
        admin_user = state.get("admin_user") or os.environ.get("SUDO_USER")
        if admin_user:
            try:
                user_info = pwd.getpwnam(str(admin_user))
                uid = user_info.pw_uid
                gid = user_info.pw_gid
            except KeyError:
                uid = int(os.environ.get("SUDO_UID") or os.getuid())
                gid = int(os.environ.get("SUDO_GID") or os.getgid())
        else:
            uid = int(os.environ.get("SUDO_UID") or os.getuid())
            gid = int(os.environ.get("SUDO_GID") or os.getgid())

        if state.get("admin_uid") is None:
            state.set("admin_uid", str(uid))
        if state.get("admin_gid") is None:
            state.set("admin_gid", str(gid))

    if state.get("TZ") is None:
        state.set("TZ", os.environ.get("TZ", "UTC"))


def _generate_missing_secrets(state: State) -> None:
    """Generate secrets that are not yet present in state.

    Checks both the flat namespace and ``secrets.values`` (set by Step 4)
    so that passwords used in init-services.sql are reused in service .env
    files, avoiding divergence.
    """
    registry = _load_registry()
    services = _selected_and_always_services(state, registry)
    selected_ids = {svc["id"] for svc in selected_service_defs(state, registry)}
    secrets_values = dict(state.get("secrets.values", {}) or {})

    def _ensure(key: str, spec: str | dict) -> None:
        value = state.get(key)
        if value is None:
            value = secrets_values.get(key)
        if value is None:
            value = _resolve_declared_value(spec)
        state.set(key, value)
        secrets_values[key] = value

    for svc in services:
        for key, spec in (svc.get("secrets") or {}).items():
            _ensure(key, spec)

    for svc in services:
        for condition in svc.get("conditional_secrets", []):
            if not _condition_matches(condition, state, selected_ids):
                continue
            for key, spec in (condition.get("keys") or {}).items():
                _ensure(key, spec)

    if secrets_values:
        state.set("secrets.values", secrets_values)


# ---------------------------------------------------------------------------
# Per-service helpers
# ---------------------------------------------------------------------------


def _render_env_example(
    state: State,
    tmpl_path: Path,
    dst_path: Path,
    preserve_keys: list[str] | None = None,
) -> None:
    """Render an .env.example template to .env, preserving existing keys when requested."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    _preserve_env_keys_from_file(state, dst_path, preserve_keys)

    _ensure_writable_output(dst_path)
    render_file(tmpl_path, dst_path, state)
    dst_path.chmod(0o600)


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("'\"")
    return result


def _preserve_env_keys_from_file(
    state: State,
    env_path: Path,
    preserve_keys: list[str] | None = None,
) -> None:
    """Copy preserved env values from an existing file into state before rendering."""
    if not env_path.exists() or not preserve_keys:
        return

    existing = _parse_dotenv(env_path.read_text())
    for key in preserve_keys:
        if key in existing and existing[key]:
            state.set(key, existing[key])


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


def _available_ram_mb() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
    return None


def _format_ram_label(value_mb: int) -> str:
    if value_mb % 1024 == 0:
        return f"{value_mb // 1024} GB"
    return f"{value_mb / 1024:.1f} GB"


def _resource_disk_probe_path(state: State, svc: dict) -> Path:
    if not svc.get("host_service"):
        for candidate in (Path("/var/lib/containerd"), Path("/var/lib/docker")):
            if candidate.exists():
                return candidate
    return state.data_root


def _disk_probe_status(probe: Path) -> tuple[int, str] | None:
    candidate = probe
    while not candidate.exists() and candidate != Path("/"):
        candidate = candidate.parent

    try:
        result = subprocess.run(
            ["df", "-Pk", str(candidate)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            free_kb = int(parts[3])
            mount_point = parts[5]
            return free_kb // 1024 // 1024, mount_point
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, IndexError):
        pass

    return None


def _resource_disk_scope_label(probe: Path, mount_point: str | None = None) -> str:
    label = f"the filesystem backing {probe}"
    if probe in (Path("/var/lib/containerd"), Path("/var/lib/docker")):
        label += " (Docker image storage)"
    if mount_point:
        label += f" mounted at {mount_point}"
    return label


def _warn_service_resource_recommendations(
    svc: dict,
    requirements: dict,
    available_ram_mb: int | None,
    free_disk_gb: int | None,
    disk_scope: str,
) -> None:
    warnings: list[str] = []

    recommended_ram = requirements.get("recommended_ram_mb")
    if recommended_ram is not None and available_ram_mb is not None and available_ram_mb < int(recommended_ram):
        warnings.append(
            f"recommends {_format_ram_label(int(recommended_ram))} RAM available, current host has {_format_ram_label(available_ram_mb)}"
        )

    recommended_disk = requirements.get("recommended_disk_gb")
    if recommended_disk is not None and free_disk_gb is not None and free_disk_gb < int(recommended_disk):
        warnings.append(
            f"recommends {int(recommended_disk)} GB free on {disk_scope}, current host has {free_disk_gb} GB"
        )

    if not warnings:
        return

    install_warning = str(requirements.get("install_warning") or "").strip()
    message = f"Service '{svc['id']}' is resource-heavy: {'; '.join(warnings)}."
    if install_warning:
        message += f" {install_warning}"
    console.print(f"[yellow]Warning:[/yellow] {message}")


def _enforce_service_resource_requirements(state: State, svc: dict) -> None:
    requirements = svc.get("resource_requirements") or {}
    if not requirements:
        return

    available_ram_mb = _available_ram_mb()
    disk_probe = _resource_disk_probe_path(state, svc)
    disk_status = _disk_probe_status(disk_probe)
    free_disk_gb = disk_status[0] if disk_status is not None else None
    mount_point = disk_status[1] if disk_status is not None else None
    disk_scope = _resource_disk_scope_label(disk_probe, mount_point)

    failures: list[str] = []

    min_ram = requirements.get("min_ram_mb")
    if min_ram is not None and available_ram_mb is not None and available_ram_mb < int(min_ram):
        failures.append(
            f"needs at least {_format_ram_label(int(min_ram))} RAM available, current host has {_format_ram_label(available_ram_mb)}"
        )

    min_disk = requirements.get("min_disk_gb")
    if min_disk is not None and free_disk_gb is not None and free_disk_gb < int(min_disk):
        failures.append(f"needs at least {int(min_disk)} GB free on {disk_scope}, current host has {free_disk_gb} GB")

    if failures:
        install_warning = str(requirements.get("install_warning") or "").strip()
        message = (
            f"Service '{svc['id']}' cannot start installation because it does not meet the minimum resource requirements: "
            + "; ".join(failures)
            + "."
        )
        if install_warning:
            message += f" {install_warning}"
        if disk_probe in (Path("/var/lib/containerd"), Path("/var/lib/docker")):
            message += (
                " Free space must exist on the filesystem that stores Docker images and extracted layers; "
                "increasing the VM disk alone is not enough until that filesystem is expanded."
            )
        raise RuntimeError(message)

    _warn_service_resource_recommendations(svc, requirements, available_ram_mb, free_disk_gb, disk_scope)


def _service_render_changes(state: State, svc: dict, repo: Path, data_root: Path) -> dict[str, bool]:
    """Return whether current templates would change a service's rendered artifacts."""
    svc_id = svc["id"]
    service_dir = data_root / "docker" / svc_id
    route_path = data_root / "docker" / "caddy" / "routes" / f"{svc_id}.caddy"
    route_before = _read_text_if_exists(route_path) if caddy_enabled(state) else None
    extra_before = {
        extra["dst"]: _read_text_if_exists(data_root / extra["dst"]) for extra in svc.get("extra_templates", [])
    }

    with TemporaryDirectory(prefix=f"rakkib-{svc_id}-restart-") as tmpdir:
        tmp_root = Path(tmpdir)
        if caddy_enabled(state):
            _render_caddy_route(state, svc, repo, tmp_root, validate=False)
            route_after = _read_text_if_exists(tmp_root / "docker" / "caddy" / "routes" / f"{svc_id}.caddy")
            route_changed = route_before != route_after
        else:
            route_changed = False

        _render_extra_templates(state, svc, repo, tmp_root)
        extra_changed = any(
            extra_before[extra["dst"]] != _read_text_if_exists(tmp_root / extra["dst"])
            for extra in svc.get("extra_templates", [])
        )

        if svc.get("host_service"):
            service_changed = extra_changed
        else:
            tmp_service_dir = tmp_root / "docker" / svc_id
            tmp_service_dir.mkdir(parents=True, exist_ok=True)

            env_path = service_dir / ".env"
            env_changed = False
            env_tmpl = repo / "templates" / "docker" / svc_id / ".env.example"
            if env_tmpl.exists():
                temp_state = State(deepcopy(state.to_dict()))
                _preserve_env_keys_from_file(temp_state, env_path, svc.get("env_preserve_keys", []))
                _render_env_example(temp_state, env_tmpl, tmp_service_dir / ".env")
                env_changed = _read_text_if_exists(env_path) != _read_text_if_exists(tmp_service_dir / ".env")

            compose_path = service_dir / "docker-compose.yml"
            compose_changed = False
            compose_tmpl = repo / "templates" / "docker" / svc_id / "docker-compose.yml.tmpl"
            if compose_tmpl.exists():
                temp_compose_path = tmp_service_dir / "docker-compose.yml"
                render_file(compose_tmpl, temp_compose_path, state)
                _apply_internal_access_ports(state, svc, temp_compose_path)
                compose_changed = _read_text_if_exists(compose_path) != _read_text_if_exists(temp_compose_path)

            missing_data_dirs = any(
                not (data_root / relative_dir).exists() for relative_dir in svc.get("data_dirs", [])
            )
            service_changed = env_changed or compose_changed or extra_changed or missing_data_dirs

    return {
        "route": route_changed,
        "service": service_changed,
        "any": route_changed or service_changed,
    }


def _render_caddy_route(state: State, svc: dict, repo: Path, data_root: Path, *, validate: bool = True) -> bool:
    """Render the appropriate Caddy route template for a service.

    Returns True when the rendered fragment changed on disk.
    """
    svc_id = svc["id"]
    routes_dir = data_root / "docker" / "caddy" / "routes"
    routes_dir.mkdir(parents=True, exist_ok=True)

    caddy = svc.get("caddy") or {}
    tmpl_name = caddy.get("template")

    if tmpl_name is None:
        return False
    if "/" in tmpl_name or "\\" in tmpl_name or ".." in Path(tmpl_name).parts:
        raise ValueError(f"Invalid caddy.template for service {svc_id}: {tmpl_name!r}")

    tmpl_path = repo / "templates" / "caddy" / "routes" / tmpl_name
    if not tmpl_path.exists():
        raise FileNotFoundError(f"Service {svc_id} references missing caddy.template: {tmpl_path}")

    route_path = routes_dir / f"{svc_id}.caddy"
    before = _read_text_if_exists(route_path)
    render_file(tmpl_path, route_path, state)
    if validate:
        _validate_caddy_fragment(route_path, svc_id)
    return before != route_path.read_text()


def _validate_caddy_fragment(route_path: Path, svc_id: str) -> None:
    """Validate a single rendered Caddy route with service-specific diagnostics."""
    synthetic = route_path.with_name(f".{route_path.stem}.validate.caddy")
    synthetic.write_text(
        "# Synthetic Caddyfile for per-service route validation\n"
        "{\n\tauto_https off\n}\n"
        ":80 {\n"
        f"{route_path.read_text()}\n"
        '\thandle {\n\t\trespond "Not found" 404\n\t}\n'
        "}\n"
    )
    try:
        validate = docker_run(
            [
                "run",
                "--rm",
                "-v",
                f"{synthetic}:/etc/caddy/Caddyfile:ro",
                "caddy:2",
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
            ],
            check=False,
        )
    finally:
        synthetic.unlink(missing_ok=True)
    if validate.returncode != 0:
        raise RuntimeError(f"Caddy route validation failed for service {svc_id}: {validate.stderr.strip()}")


def _publish_cloudflare_service(state: State, svc: dict) -> None:
    if not cloudflare_enabled(state):
        return
    from rakkib.steps import cloudflare

    cloudflare.publish_service(state, svc)


def _sudo_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a non-interactive sudo command for host path repairs."""
    return subprocess.run(["sudo", "-n", *command], capture_output=True, text=True)


def _chown_path(path: Path, uid: int, gid: int, *, recursive: bool) -> None:
    args = ["chown"]
    if recursive:
        args.append("-R")
    args.extend([f"{uid}:{gid}", str(path)])

    if os.geteuid() == 0:
        subprocess.run(args, check=True, capture_output=True, text=True)
        return

    result = _sudo_run(args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "permission denied"
        raise RuntimeError(
            f"Cannot repair ownership for {path}: {detail}. "
            "Run `rakkib auth` in the terminal that started the web session, then retry."
        )


def _ensure_writable_dir(path: Path) -> None:
    """Create a service data dir and repair stale root-owned bind mounts."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        parent = path.parent
        result = _sudo_run(["mkdir", "-p", str(path)])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "permission denied"
            raise RuntimeError(
                f"Cannot create service data directory {path}: {detail}. "
                f"Ensure {parent} is writable or run `rakkib auth`, then retry."
            )

    if os.access(path, os.W_OK | os.X_OK):
        return

    _chown_path(path, os.geteuid(), os.getegid(), recursive=True)


def _ensure_writable_output(path: Path) -> None:
    _ensure_writable_dir(path.parent)
    if path.exists() and not os.access(path, os.W_OK):
        _chown_path(path, os.geteuid(), os.getegid(), recursive=False)


def _prepare_service_data(state: State, svc: dict, data_root: Path) -> None:
    for relative_dir in svc.get("data_dirs", []):
        _ensure_writable_dir(data_root / relative_dir)

    chown = svc.get("chown")
    if not chown or state.get("platform", "linux") != "linux":
        return

    service_data_root = data_root / "data" / svc["id"]
    if not service_data_root.exists():
        return
    _chown_path(service_data_root, int(chown["uid"]), int(chown["gid"]), recursive=True)


def _render_extra_templates(state: State, svc: dict, repo: Path, data_root: Path) -> None:
    for extra in svc.get("extra_templates", []):
        src = repo / extra["src"]
        dst = data_root / extra["dst"]
        if not src.exists():
            raise FileNotFoundError(f"Service {svc['id']} references missing extra_templates source: {src}")
        _ensure_writable_output(dst)
        render_file(src, dst, state)


def _internal_access(svc: dict) -> dict:
    access = svc.get("internal_access") or {}
    return access if access.get("enabled") else {}


def _internal_port_mapping(svc: dict) -> str | None:
    access = _internal_access(svc)
    if not access:
        return None
    return f"0.0.0.0:{int(access['host_port'])}:{int(access['container_port'])}"


def _target_compose_service(compose: dict, svc: dict) -> str:
    services = compose.get("services") or {}
    requested = (_internal_access(svc).get("compose_service") or "").strip()
    if requested:
        if requested not in services:
            raise ValueError(f"Service {svc['id']} internal_access.compose_service {requested!r} not found in compose")
        return requested
    if svc["id"] in services:
        return svc["id"]
    container_name = svc.get("container_name")
    if container_name:
        for service_name, service_def in services.items():
            if isinstance(service_def, dict) and service_def.get("container_name") == container_name:
                return service_name
    if len(services) == 1:
        return next(iter(services))
    raise ValueError(f"Service {svc['id']} internal_access needs compose_service for multi-service compose")


def _apply_internal_access_ports(state: State, svc: dict, compose_path: Path) -> None:
    """Inject the registry-declared direct port into internal-mode compose."""
    if caddy_enabled(state) or not _internal_access(svc) or svc.get("host_service"):
        return
    mapping = _internal_port_mapping(svc)
    if not mapping or not compose_path.exists():
        return

    compose = yaml.safe_load(compose_path.read_text()) or {}
    services = compose.get("services") or {}
    service_name = _target_compose_service(compose, svc)
    service_def = services.setdefault(service_name, {})
    if not isinstance(service_def, dict):
        raise ValueError(f"Service {svc['id']} compose service {service_name!r} must be a mapping")
    service_def["ports"] = [mapping]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))


def _ensure_internal_port_available(state: State, svc: dict, data_root: Path) -> None:
    access = _internal_access(svc)
    if caddy_enabled(state) or not access or svc.get("host_service"):
        return

    # Existing compose projects may legitimately own this port during a restart.
    if (data_root / "docker" / svc["id"] / "docker-compose.yml").exists():
        return

    host_port = int(access["host_port"])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        in_use = sock.connect_ex(("127.0.0.1", host_port)) == 0
    if in_use:
        raise RuntimeError(
            f"Internal access port {host_port} for service '{svc['id']}' is already in use. "
            f"Free port {host_port} or change {svc['id']}.internal_access.host_port in the registry."
        )


def _run_named_hooks(
    hook_names: list[str],
    hook_registry: dict[str, Callable],
    state: State,
    svc: dict,
    repo: Path,
    data_root: Path,
    log_path: Path,
    registry: dict,
) -> None:
    for hook_name in hook_names:
        hook = hook_registry.get(hook_name)
        if hook is None:
            raise ValueError(f"Unknown service hook: {hook_name}")
        hook(HookContext(state, svc, repo, data_root, log_path, registry))


def _reload_caddy(data_root: Path) -> None:
    caddy_dir = data_root / "docker" / "caddy"
    caddyfile = caddy_dir / "Caddyfile"

    # Format the Caddyfile by running caddy fmt inside the container and
    # writing the result to the host-side file (the bind mount is read-only
    # inside the container, so --overwrite cannot work).
    with progress_spinner("Formatting Caddy configuration..."):
        fmt_result = docker_run(
            ["compose", "exec", "caddy", "caddy", "fmt", "/etc/caddy/Caddyfile"],
            cwd=str(caddy_dir),
            check=False,
        )
    if fmt_result.returncode == 0 and fmt_result.stdout.strip():
        caddyfile.write_text(fmt_result.stdout)

    reload_result = docker_run(
        ["compose", "exec", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"],
        cwd=caddy_dir,
        check=False,
        progress_message="Reloading Caddy...",
    )
    if reload_result.returncode == 0:
        return

    docker_run(
        ["compose", "restart", "caddy"],
        cwd=caddy_dir,
        progress_message="Restarting Caddy...",
    )


def _drop_service_postgres_resources(svc: dict) -> None:
    postgres = svc.get("postgres") or {}
    if not postgres:
        return

    role = postgres_identifier(
        postgres.get("role", svc["id"]),
        field=f"postgres role for service {svc['id']}",
    )
    db_name = postgres_identifier(
        postgres.get("db", role),
        field=f"postgres database for service {svc['id']}",
    )
    db_literal = postgres_literal(db_name)
    sql = "\n".join(
        [
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {db_literal} AND pid <> pg_backend_pid();",
            f"DROP DATABASE IF EXISTS {db_name};",
            f"DROP ROLE IF EXISTS {role};",
        ]
    )
    with progress_spinner(f"Dropping Postgres resources for {svc['id']}..."):
        docker_run(
            ["exec", "-i", "postgres", "psql", "-U", "postgres"],
            input=sql,
        )


def remove_single_service(state: State, svc_id: str) -> None:
    """Fully remove a single service from the host."""
    registry = _load_registry()
    by_id = {s["id"]: s for s in registry["services"]}
    if svc_id not in by_id:
        raise ValueError(f"Service {svc_id} not found in registry")

    svc = by_id[svc_id]
    data_root = state.data_root
    service_dir = data_root / "docker" / svc_id
    log_path = data_root / "logs" / f"step5-{svc_id}.log"
    hooks = svc.get("hooks") or {}

    _run_named_hooks(hooks.get("remove", []), REMOVE_HOOKS, state, svc, _repo_dir(), data_root, log_path, registry)

    if (service_dir / "docker-compose.yml").exists():
        compose_down(service_dir, volumes=True, log_path=log_path)

    shutil.rmtree(service_dir, ignore_errors=True)
    for relative_dir in svc.get("data_dirs", []):
        shutil.rmtree(data_root / relative_dir, ignore_errors=True)
    shutil.rmtree(data_root / "data" / svc_id, ignore_errors=True)

    route_path = data_root / "docker" / "caddy" / "routes" / f"{svc_id}.caddy"
    route_path.unlink(missing_ok=True)

    for extra in svc.get("extra_templates", []):
        dst = data_root / extra["dst"]
        if dst.exists():
            dst.unlink()

    _drop_service_postgres_resources(svc)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _deploy_single_service(state: State, svc: dict, repo: Path, data_root: Path) -> None:
    """Render templates, create dirs, and start a single service."""
    svc_id = svc["id"]
    is_host = svc.get("host_service")
    log_path = data_root / "logs" / f"step5-{svc_id}.log"
    registry = _load_registry()
    hooks = svc.get("hooks") or {}
    console.print(f"[dim]Installing {svc_id}...[/dim]")

    _enforce_service_resource_requirements(state, svc)

    route_changed = False
    if caddy_enabled(state):
        route_changed = _render_caddy_route(state, svc, repo, data_root)

    _run_named_hooks(hooks.get("post_render", []), POST_RENDER_HOOKS, state, svc, repo, data_root, log_path, registry)
    _run_named_hooks(hooks.get("pre_start", []), PRE_START_HOOKS, state, svc, repo, data_root, log_path, registry)

    if is_host:
        _run_named_hooks(hooks.get("post_start", []), POST_START_HOOKS, state, svc, repo, data_root, log_path, registry)
        if route_changed:
            _reload_caddy(data_root)
        _publish_cloudflare_service(state, svc)
        return

    svc_dir = data_root / "docker" / svc_id
    _ensure_writable_dir(svc_dir)

    _prepare_service_data(state, svc, data_root)
    _ensure_internal_port_available(state, svc, data_root)

    # --- Render templates ------------------------------------------------

    # .env.example -> .env
    env_tmpl = repo / "templates" / "docker" / svc_id / ".env.example"
    env_path = svc_dir / ".env"
    if env_tmpl.exists():
        preserve = svc.get("env_preserve_keys", [])
        _render_env_example(state, env_tmpl, env_path, preserve)

    # docker-compose.yml
    compose_tmpl = repo / "templates" / "docker" / svc_id / "docker-compose.yml.tmpl"
    if compose_tmpl.exists():
        compose_path = svc_dir / "docker-compose.yml"
        _ensure_writable_output(compose_path)
        render_file(compose_tmpl, compose_path, state)
        _apply_internal_access_ports(state, svc, compose_path)

    _render_extra_templates(state, svc, repo, data_root)

    # --- Start service ---------------------------------------------------
    create_network(str(state.get("docker_net", "caddy_net")))
    try:
        compose_up(svc_dir, log_path=log_path)
    except DockerError as exc:
        raise RuntimeError(f"Service '{svc_id}' failed to start. Log: {log_path}. {exc}") from exc

    container_name = svc.get("container_name", svc_id)
    if not health_check(container_name, timeout=int(svc.get("health_timeout", 120))):
        raise RuntimeError(f"Service '{svc_id}' did not become healthy. Log: {log_path}.")

    if route_changed:
        _reload_caddy(data_root)

    _run_named_hooks(hooks.get("post_start", []), POST_START_HOOKS, state, svc, repo, data_root, log_path, registry)
    _publish_cloudflare_service(state, svc)


def _host_service_responds(svc: dict) -> bool:
    port = svc.get("default_port")
    if not svc.get("host_port"):
        check = str(svc.get("installed_check") or "").strip()
        if not check:
            return False
        result = subprocess.run(
            ["bash", "-lc", check],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0

    if not port:
        return False

    path = str((svc.get("monitoring") or {}).get("path") or "/")
    path = path if path.startswith("/") else f"/{path}"
    result = subprocess.run(
        ["curl", "-sf", f"http://127.0.0.1:{port}{path}", "-o", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def _container_usable(container_name: str) -> bool:
    result = docker_run(
        [
            "inspect",
            "-f",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container_name,
        ],
        check=False,
    )
    status, _, health = result.stdout.strip().partition(" ")
    if status != "running":
        return False
    return health.strip() not in {"starting", "unhealthy"}


def service_is_installed(state: State, svc: dict, data_root: Path | None = None) -> bool:
    """Return true when a selected service already has a usable deployment."""
    data_root = data_root or state.data_root

    if svc.get("host_service"):
        return _host_service_responds(svc)

    svc_id = svc["id"]
    container_name = svc.get("container_name", svc_id)
    compose_path = data_root / "docker" / svc_id / "docker-compose.yml"
    return compose_path.exists() and _container_usable(container_name)


def run(state: State) -> None:
    repo = _repo_dir()
    data_root = state.data_root
    registry = _load_registry()

    _ensure_service_runtime_env(state)
    _generate_missing_secrets(state)
    services = selected_service_defs(state, registry)

    for svc in services:
        if service_is_installed(state, svc, data_root):
            console.print(f"[dim]{svc['id']} is already installed.[/dim]")
            continue
        _deploy_single_service(state, svc, repo, data_root)

    sync_shared_artifacts(state, repo, data_root, registry)


def run_single_service(state: State, svc_id: str) -> None:
    """Deploy a single service by ID."""
    repo = _repo_dir()
    data_root = state.data_root
    registry = _load_registry()

    by_id = {s["id"]: s for s in registry["services"]}
    if svc_id not in by_id:
        raise ValueError(f"Service {svc_id} not found in registry")

    _ensure_service_runtime_env(state)
    _generate_missing_secrets(state)
    svc = by_id[svc_id]
    _deploy_single_service(state, svc, repo, data_root)
    sync_shared_artifacts(state, repo, data_root, registry)


def smoke_check(state: State, svc_id: str) -> VerificationResult:
    """Fetch a service's public URL and assert its registry smoke marker is present."""
    registry = _load_registry()
    by_id = {s["id"]: s for s in registry["services"]}
    if svc_id not in by_id:
        return VerificationResult.failure("services", f"Service {svc_id} not found in registry")

    svc = by_id[svc_id]
    smoke = svc.get("smoke") or {}
    if not smoke:
        return VerificationResult.success("services", f"No smoke check declared for {svc_id}")

    path = str(smoke.get("path") or "/")
    path = path if path.startswith("/") else f"/{path}"
    url = service_url(state, svc, path=path)
    if not url:
        return VerificationResult.failure("services", f"No user-facing URL recorded for {svc_id}")

    timeout = int(smoke.get("timeout", 20))
    deadline = time.monotonic() + timeout
    result: subprocess.CompletedProcess[str] | None = None
    while True:
        remaining = max(1, int(deadline - time.monotonic()))
        attempt_timeout = str(min(10, remaining))
        result = subprocess.run(
            ["curl", "-fsSL", "--max-time", attempt_timeout, url],
            capture_output=True,
            text=True,
            timeout=int(attempt_timeout) + 5,
        )
        if result.returncode == 0:
            break
        if time.monotonic() >= deadline:
            detail = result.stderr.strip() or result.stdout.strip() or "curl failed"
            return VerificationResult.failure("services", f"Smoke check failed for {svc_id} at {url}: {detail}")
        time.sleep(2)

    expected = smoke.get("expected_text")
    if expected and expected not in result.stdout:
        return VerificationResult.failure(
            "services",
            f"Smoke check for {svc_id} did not find expected text {expected!r} at {url}",
        )

    return VerificationResult.success("services", f"Smoke check passed for {svc_id} at {url}")


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def restart_service(state: State, svc_id: str) -> None:
    """Restart a single service, regenerating rendered artifacts when they drift."""
    data_root = state.data_root
    repo = _repo_dir()
    registry = _load_registry()
    by_id = {s["id"]: s for s in registry["services"]}
    if svc_id not in by_id:
        raise ValueError(f"Service {svc_id} not found in registry")

    svc = by_id[svc_id]
    changes = _service_render_changes(state, svc, repo, data_root)

    if svc.get("host_service"):
        if changes["any"]:
            if caddy_enabled(state):
                _render_caddy_route(state, svc, repo, data_root)
            _render_extra_templates(state, svc, repo, data_root)
            if changes["route"] and caddy_enabled(state):
                _reload_caddy(data_root)

        hooks = svc.get("hooks") or {}
        log_path = data_root / "logs" / f"step5-{svc_id}.log"
        _run_named_hooks(hooks.get("restart", []), RESTART_HOOKS, state, svc, repo, data_root, log_path, registry)
        return

    svc_dir = data_root / "docker" / svc_id
    if not (svc_dir / "docker-compose.yml").exists():
        raise ValueError(f"No docker-compose.yml found for service '{svc_id}' at {svc_dir}")

    if changes["service"]:
        _deploy_single_service(state, svc, repo, data_root)
    else:
        docker_run(
            ["compose", "--project-directory", str(svc_dir), "restart"],
            progress_message=f"Restarting {svc_id}...",
        )

    if changes["route"] and caddy_enabled(state) and not changes["service"]:
        _render_caddy_route(state, svc, repo, data_root)
        _reload_caddy(data_root)


def restart_all(state: State) -> list[str]:
    """Restart all deployed services in dependency order. Returns ids of restarted services.

    Order: postgres → cloudflared → foundation/selected (dependency order) → caddy
    Caddy is always last so routes are live after all services are up.
    """
    data_root = state.data_root
    registry = _load_registry()

    always_ids = [
        s["id"]
        for s in registry["services"]
        if s.get("state_bucket") == "always"
        and s["id"] != "caddy"
        and not (s["id"] == "cloudflared" and not cloudflare_enabled(state))
    ]
    selected = selected_service_defs(state, registry)
    selected_ids = [s["id"] for s in selected]

    order = []
    for svc_id in always_ids:
        if svc_id not in order:
            order.append(svc_id)
    for svc_id in selected_ids:
        if svc_id not in order:
            order.append(svc_id)
    if caddy_enabled(state):
        order.append("caddy")

    restarted: list[str] = []
    for svc_id in order:
        svc = next((item for item in registry["services"] if item["id"] == svc_id), None)
        if svc is None:
            continue
        if not svc.get("host_service"):
            svc_dir = data_root / "docker" / svc_id
            if not (svc_dir / "docker-compose.yml").exists():
                continue

        restart_service(state, svc_id)
        restarted.append(svc_id)

    return restarted


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify(state: State) -> VerificationResult:
    registry = _load_registry()
    services = selected_service_defs(state, registry)

    for svc in services:
        svc_id = svc["id"]
        port = svc.get("default_port")

        if svc.get("host_service"):
            if port and svc.get("host_port"):
                path = str((svc.get("monitoring") or {}).get("path") or "/healthz")
                path = path if path.startswith("/") else f"/{path}"
                result = subprocess.run(
                    ["curl", "-sf", f"http://127.0.0.1:{port}{path}", "-o", "/dev/null"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return VerificationResult.failure(
                        "services",
                        f"Host service {svc_id} health check on port {port} failed",
                    )
            continue

        # Determine expected container name
        container_name = svc.get("container_name", svc_id)

        if not container_running(container_name):
            return VerificationResult.failure(
                "services",
                f"Container {container_name} ({svc_id}) is not running",
            )

        access = _internal_access(svc) if not caddy_enabled(state) else {}
        published_port = int(access["container_port"]) if access else port
        needs_host_port = bool(svc.get("host_port", False) or access)
        if published_port and needs_host_port and not container_publishes_port(container_name, published_port):
            return VerificationResult.failure(
                "services",
                f"Container {container_name} does not publish port {published_port}",
            )

    return VerificationResult.success("services", "All selected services are running")
