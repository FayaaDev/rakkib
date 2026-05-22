"""Rakkib CLI entrypoint.

Commands: init, pull, update, doctor, status, add, restart, uninstall, privileged, auth, web
"""

from __future__ import annotations

import copy
import os
import platform
import pwd
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console

if TYPE_CHECKING:
    from rich.table import Table

from rakkib.docker import DockerError, compose_down, docker_run, is_docker_permission_error
from rakkib.doctor import (
    attempt_fix_cloudflared,
    attempt_fix_docker,
    attempt_start_docker_desktop,
    check_disk,
    check_ram,
    docker_access_commands,
    docker_access_user,
    ensure_prereqs,
    process_owners_for_ports,
    prepare_docker_access,
    run_checks,
    summary_text,
    to_json,
)
from rakkib.interview import run_interview
from rakkib.secret_utils import token_urlsafe
from rakkib.service_catalog import caddy_enabled, cloudflare_enabled, deployed_service_urls
from rakkib.services_cli import (
    apply_planned_subdomains as _apply_planned_subdomains,
    apply_service_selection as _apply_service_selection,
    build_add_choices as _build_add_choices,
    build_remove_choices as _build_remove_choices,
    build_restart_choices as _build_restart_choices,
    deployed_service_lists as _deployed_service_lists,
    installed_service_ids as _installed_service_ids,
    persist_deployed_selection as _persist_deployed_selection,
    print_deployed_urls as _print_deployed_urls,
    prompt_service_subdomains as _prompt_service_subdomains,
    restart_order as _restart_order,
    selected_service_lists as _selected_service_lists,
    summarize_service_diff as _summarize_service_diff,
    validate_selection_dependencies as _validate_service_dependencies,
)
from rakkib.state import State, default_state_path
from rakkib.steps import STEP_MODULES, VerificationResult, load_service_registry, selected_service_defs
from rakkib.steps import postgres as postgres_step
from rakkib.steps import services as services_step
from rakkib.steps.cloudflare import _cloudflared_bin, _show_qr
from rakkib.tui import progress_spinner, prompt_checkbox, prompt_confirm
from rakkib.util import (
    checkout_dir as _checkout_dir,
    detect_lan_ip as _detect_lan_ip,
    resolve_user,
    web_url as _web_url,
)
from rakkib.web.host_auth import check_host_auth_readiness

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_doctor_table(checks: list, title: str) -> Table:
    from rich.table import Table

    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Status", style="bold", width=6)
    table.add_column("Check", style="dim", width=20)
    table.add_column("Blocking", width=8)
    table.add_column("Message")
    for check in checks:
        status_style = {
            "ok": "[green]ok[/green]",
            "warn": "[yellow]warn[/yellow]",
            "fail": "[red]fail[/red]",
        }.get(check.status, check.status)
        table.add_row(status_style, check.name, "yes" if check.blocking else "no", check.message)
    return table


def _resolve_admin_user(state: State, explicit: str | None = None) -> str:
    user = resolve_user(state, explicit=explicit, require=True)
    if user:
        return user
    console.print("[red]Admin user is required; pass --admin-user or record admin_user in state.[/red]")
    raise click.Abort()


def _stdin_is_interactive() -> bool:
    return sys.stdin.isatty()


def _run_auth_setup(ctx: click.Context) -> bool:
    """Validate sudo and prepare Docker access when possible."""
    if os.geteuid() == 0:
        console.print("[green]Authorization is ready.[/green]")
        return True

    if platform.system() == "Darwin":
        console.print("[green]macOS detected.[/green]")
        if shutil.which("docker") is None:
            console.print("[dim]Preparing Docker...[/dim]")
            message = attempt_fix_docker()
            console.print(f"[dim]{message}[/dim]")
            if shutil.which("docker") is None:
                return False
        try:
            docker_run(["info"])
        except DockerError as exc:
            console.print(f"[dim]{attempt_start_docker_desktop()}[/dim]")
            try:
                docker_run(["info"])
            except DockerError as retry_exc:
                console.print("[red]Docker Desktop is not ready. Run `rakkib auth`, then try again.[/red]")
                console.print(f"[dim]{retry_exc or exc}[/dim]")
                return False
        console.print("[green]Re-run `rakkib pull`.[/green]")
        return True

    if shutil.which("sudo") is None:
        console.print("[red]sudo is required. Install sudo, then try again.[/red]")
        return False

    console.print("[dim]Checking authorization. Rakkib will not store your password.[/dim]")
    result = subprocess.run(["sudo", "-v"], capture_output=True, text=True)
    if result.returncode != 0:
        console.print("[red]Sudo validation failed. Run `sudo -v` in your terminal first.[/red]")
        return False
    console.print("[green]Authorization is ready.[/green]")

    if shutil.which("docker") is None:
        return True

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)
    user = docker_access_user(state)
    try:
        docker_run(["info"])
        console.print("[green]Re-run `rakkib pull`.[/green]")
        return True
    except DockerError as exc:
        if not is_docker_permission_error(exc.stderr or str(exc)):
            console.print(f"[yellow]Docker is not ready yet:[/yellow] {exc}")
            return True

    message = prepare_docker_access(user, validate_sudo=False)
    console.print(f"[dim]{message}[/dim]")
    if not message.startswith("Docker is ready"):
        console.print("[dim]Manual setup:[/dim]")
        console.print(f"[dim]{docker_access_commands(user)}[/dim]")
        return False
    console.print("[green]Re-run `rakkib pull`.[/green]")
    return True


def _prompt_web_host_auth(ctx: click.Context) -> None:
    status = check_host_auth_readiness()
    if status.ok or not _stdin_is_interactive():
        return

    console.print(f"[yellow]{status.message}[/yellow]")
    if status.command and prompt_confirm("Run `rakkib auth` now?", default=True):
        _run_auth_setup(ctx)
        status = check_host_auth_readiness()
        if status.ok:
            console.print("[green]Host authorization is ready for browser deployment.[/green]")
            return
        console.print(f"[yellow]{status.message}[/yellow]")

    console.print(
        "[yellow]Rakkib web will start, but browser deployment will stay blocked until host authorization is ready.[/yellow]"
    )


def _run_steps(state: State, repo_dir: Path) -> bool:
    """Execute setup steps in order. Return True if all pass."""
    all_steps = STEP_MODULES + [("verify", "rakkib.steps.verify")]
    verify_cache: dict[str, VerificationResult] = {}

    for step_name, module_path in all_steps:
        if step_name == "caddy" and not caddy_enabled(state):
            console.print("[dim]Skipping public web routes.[/dim]")
            continue
        if step_name == "cloudflare" and not cloudflare_enabled(state):
            console.print("[dim]Skipping Cloudflare.[/dim]")
            continue
        console.print(f"[bold green]{step_name}[/bold green]")
        try:
            module = __import__(module_path, fromlist=["run", "verify"])
            run_fn = getattr(module, "run", None)
            verify_fn = getattr(module, "verify", None)

            if run_fn is None:
                console.print(f"[yellow]  {step_name} is not available — skipping[/yellow]")
                continue

            if step_name == "verify":
                # Pass cached results so verify.run() can skip re-running each step verify.
                state.set(
                    "_step_verify_cache",
                    {k: {"ok": v.ok, "step": v.step, "message": v.message} for k, v in verify_cache.items()},
                )

            run_fn(state)

            if step_name == "verify":
                # verify.run() already ran _collect_verifications and printed the summary;
                # calling verify_fn again would triple-run each step's verify().
                break

            if verify_fn is not None:
                result = verify_fn(state)
                verify_cache[step_name] = result
                if not result.ok:
                    console.print(f"[bold red]  {step_name} check failed:[/bold red] {result.message}")
                    if result.log_path:
                        console.print(f"[dim]  Log: {result.log_path}[/dim]")
                    return False
                console.print(f"[dim]  {step_name} ready[/dim]")
            else:
                console.print(f"[dim]  {step_name} completed[/dim]")

        except Exception as exc:
            console.print(f"[bold red]  {step_name} failed:[/bold red] {exc}")
            return False

    console.print("[bold green]Rakkib is ready.[/bold green]")
    _print_deployed_urls(state)
    return True


def _run_pre_service_steps(state: State) -> bool:
    """Run setup steps needed before deploying one selected service."""
    for step_name, module_path in STEP_MODULES:
        if step_name == "services":
            break
        if step_name == "caddy" and not caddy_enabled(state):
            console.print("[dim]Skipping public web routes.[/dim]")
            continue
        if step_name == "cloudflare" and not cloudflare_enabled(state):
            console.print("[dim]Skipping Cloudflare.[/dim]")
            continue

        console.print(f"[bold green]{step_name}[/bold green]")
        try:
            module = __import__(module_path, fromlist=["run", "verify"])
            run_fn = getattr(module, "run", None)
            verify_fn = getattr(module, "verify", None)
            if run_fn is not None:
                run_fn(state)
            if verify_fn is not None:
                result = verify_fn(state)
                if not result.ok:
                    console.print(f"[bold red]  {step_name} check failed:[/bold red] {result.message}")
                    return False
                console.print(f"[dim]  {step_name} ready[/dim]")
        except Exception as exc:
            console.print(f"[bold red]  {step_name} failed:[/bold red] {exc}")
            return False

    return True


def _select_service_for_deploy(state: State, registry: dict[str, Any], service: str) -> dict[str, Any] | None:
    by_id = {svc["id"]: svc for svc in registry["services"]}
    svc = by_id.get(service)
    if svc is None:
        console.print(f"[bold red]Error:[/bold red] Unknown service '{service}'.")
        return None
    if svc.get("state_bucket") == "always":
        console.print(
            f"[bold red]Error:[/bold red] '{service}' is an always-installed service; use full `rakkib pull`."
        )
        return None

    selected_ids = _installed_service_ids(state)
    selected_ids.add(service)
    dependency_errors = _validate_service_dependencies(selected_ids, registry)
    if dependency_errors:
        console.print("[bold red]Error:[/bold red] Invalid service selection:")
        for error in dependency_errors:
            console.print(f"  - {error}")
        return None

    _apply_service_selection(state, registry, selected_ids)
    services_step._generate_missing_secrets(state)
    return svc


def _restore_state(state: State, snapshot: dict[str, Any]) -> None:
    """Restore state._data to ``snapshot`` in place, preserving object identity."""
    state._data.clear()
    state._data.update(snapshot)


def _run_service_pull(state: State, state_path: Path, service: str) -> bool:
    registry = services_step._load_registry()

    snapshot = copy.deepcopy(state._data)
    try:
        svc = _select_service_for_deploy(state, registry, service)
        if svc is None:
            _restore_state(state, snapshot)
            return False

        state.save(state_path)
        if not _run_pre_service_steps(state):
            # Pre-steps run their own console output; leave state with the
            # recorded intent so the operator can resume after fixing the
            # blocker without reselecting the service.
            return False

        console.print(f"[bold green]Installing {service}[/bold green]")
        services_step.run_single_service(state, service)
    except Exception as exc:
        console.print(f"[bold red]  Service {service} failed:[/bold red] {exc}")
        _restore_state(state, snapshot)
        try:
            state.save(state_path)
        except Exception as save_exc:
            console.print(f"[bold red]  Rollback save failed:[/bold red] {save_exc}")
        return False

    _persist_deployed_selection(state)
    state.save(state_path)
    _print_deployed_urls(state, [service])
    console.print(f"[bold green]Service {service} deployed successfully.[/bold green]")
    return True


def _sync_services_to_state_selection(
    state: State,
    state_path: Path,
    *,
    rollback_snapshot: dict[str, Any] | None = None,
    fallback_previous_selection: tuple[list[str], list[str]] | None = None,
) -> bool:
    registry = services_step._load_registry()
    desired_foundation, desired_selected = _selected_service_lists(state)
    desired_ids = set(desired_foundation)
    desired_ids.update(desired_selected)

    if state.get("deployed.exists", False):
        previous_foundation, previous_selected = _deployed_service_lists(state)
    elif fallback_previous_selection is not None:
        previous_foundation, previous_selected = fallback_previous_selection
    else:
        previous_foundation, previous_selected = [], []
    previous_ids = set(previous_foundation)
    previous_ids.update(previous_selected)

    dependency_errors = _validate_service_dependencies(desired_ids, registry)
    if dependency_errors:
        console.print("[bold red]Error:[/bold red] Invalid service selection:")
        for error in dependency_errors:
            console.print(f"  - {error}")
        return False

    snapshot = copy.deepcopy(rollback_snapshot if rollback_snapshot is not None else state._data)
    successfully_added: list[str] = []
    added_in_progress: str | None = None
    successfully_removed: list[str] = []
    added_set = desired_ids - previous_ids
    postgres_cleanup_ids: set[str] = set()

    try:
        with progress_spinner("Applying service changes..."):
            previous_state = State(
                {
                    "foundation_services": previous_foundation,
                    "selected_services": previous_selected,
                }
            )
            removal_order = [
                svc["id"]
                for svc in reversed(selected_service_defs(previous_state, registry))
                if svc["id"] in (previous_ids - desired_ids)
            ]

            _apply_service_selection(state, registry, desired_ids)
            services_step._generate_missing_secrets(state)
            if _postgres_sync_needed(registry, previous_ids, desired_ids):
                postgres_cleanup_ids = set(added_set)
                postgres_step.run(state)

            added = [svc["id"] for svc in registry["services"] if svc["id"] in added_set]
            if added:
                for svc_id in added:
                    added_in_progress = svc_id
                    services_step.run_single_service(state, svc_id)
                    successfully_added.append(svc_id)
                    added_in_progress = None

            for svc_id in removal_order:
                services_step.remove_single_service(state, svc_id)
                successfully_removed.append(svc_id)

            if removal_order or not added:
                data_root = state.data_root
                if caddy_enabled(state):
                    services_step._reload_caddy(data_root)
                services_step.sync_shared_artifacts(state, services_step._repo_dir(), data_root, registry)
    except Exception as exc:
        console.print(f"[bold red]Service sync failed:[/bold red] {exc}")
        # Best-effort cleanup of services we created during this run.
        cleanup_ids = list(successfully_added)
        if added_in_progress is not None and added_in_progress not in cleanup_ids:
            cleanup_ids.append(added_in_progress)
        for svc_id in cleanup_ids:
            try:
                services_step.remove_single_service(state, svc_id)
            except Exception as remove_exc:
                console.print(f"[dim]  rollback: failed to remove {svc_id}: {remove_exc}[/dim]")
        _drop_added_postgres_resources(registry, postgres_cleanup_ids - set(cleanup_ids))
        _restore_state(state, snapshot)
        try:
            state.save(state_path)
        except Exception as save_exc:
            console.print(f"[bold red]  Rollback save failed:[/bold red] {save_exc}")
        for svc_id in reversed(successfully_removed):
            try:
                services_step.run_single_service(state, svc_id)
            except Exception as restore_exc:
                console.print(f"[dim]  rollback: failed to restore {svc_id}: {restore_exc}[/dim]")
        return False

    _persist_deployed_selection(state)
    state.save(state_path)
    return True


def _canonical_exposure_mode(state: State) -> str | None:
    raw_mode = state.get("exposure_mode")
    if raw_mode is not None:
        mode = str(raw_mode).strip().lower()
        if mode in {"internal", "cloudflare"}:
            return mode
    if cloudflare_enabled(state):
        return "cloudflare"
    return None


def _cleanup_previous_hosting_mode(previous_state: State, new_state: State) -> None:
    """Remove services rendered for the previous hosting mode before mode switch."""
    previous_mode = _canonical_exposure_mode(previous_state)
    new_mode = _canonical_exposure_mode(new_state)
    if (
        not previous_state.is_confirmed()
        or not new_state.is_confirmed()
        or previous_mode is None
        or new_mode is None
        or previous_mode == new_mode
    ):
        return

    registry = services_step._load_registry()
    previous_foundation, previous_selected = _deployed_service_lists(previous_state)
    previous_ids = set(previous_foundation)
    previous_ids.update(previous_selected)

    console.print(
        f"[yellow]Hosting mode changed from {previous_mode} to {new_mode}; removing previously hosted services first.[/yellow]"
    )
    with progress_spinner("Removing previous hosting deployment..."):
        previous_selection_state = State(
            {
                "foundation_services": previous_foundation,
                "selected_services": previous_selected,
            }
        )
        removal_order = [
            svc["id"]
            for svc in reversed(selected_service_defs(previous_selection_state, registry))
            if svc["id"] in previous_ids
        ]
        if previous_mode == "cloudflare":
            removal_order.extend(["cloudflared", "caddy"])

        for svc_id in dict.fromkeys(removal_order):
            services_step.remove_single_service(previous_state, svc_id)

    previous_state.set("deployed.exists", False)
    previous_state.set("deployed.foundation_services", [])
    previous_state.set("deployed.selected_services", [])
    previous_state.set("cloudflare.published_services", [])
    console.print("[green]Previous hosting deployment removed. Run `rakkib pull` to install the new mode.[/green]")


def _postgres_sync_needed(registry: dict[str, Any], previous_ids: set[str], desired_ids: set[str]) -> bool:
    """Return true when the selection change affects shared Postgres resources."""
    by_id = {svc["id"]: svc for svc in registry["services"]}
    changed_ids = previous_ids ^ desired_ids
    candidate_ids = changed_ids if changed_ids else desired_ids
    return any(bool((by_id.get(svc_id) or {}).get("postgres")) for svc_id in candidate_ids)


def _drop_added_postgres_resources(registry: dict[str, Any], service_ids: set[str]) -> None:
    """Best-effort cleanup for Postgres resources created during a failed sync."""
    by_id = {svc["id"]: svc for svc in registry["services"]}
    for svc_id in sorted(service_ids):
        svc = by_id.get(svc_id) or {}
        if not svc.get("postgres"):
            continue
        try:
            services_step._drop_service_postgres_resources(svc)
        except Exception as drop_exc:
            console.print(f"[dim]  rollback: failed to drop Postgres resources for {svc_id}: {drop_exc}[/dim]")


def _run_best_effort(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, **kwargs)


def _remove_path_aggressive(path: Path, *, label: str | None = None) -> bool:
    """Remove a file, symlink, or directory, falling back to sudo for root-owned paths."""
    if not path.exists() and not path.is_symlink():
        return False

    display = label or str(path)
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        console.print(f"[green]Removed {display}[/green]")
        return True
    except PermissionError:
        if os.geteuid() != 0 and shutil.which("sudo"):
            result = _run_best_effort(["sudo", "-n", "rm", "-rf", str(path)])
            if result.returncode == 0:
                console.print(f"[green]Removed {display}[/green]")
                return True
            detail = (result.stderr or result.stdout or "permission denied").strip()
            console.print(f"[yellow]Could not remove {display}: {detail}[/yellow]")
            return False
        raise
    except OSError as exc:
        console.print(f"[yellow]Could not remove {display}: {exc}[/yellow]")
        return False


def _remove_managed_path_blocks(home: Path) -> bool:
    marker = "# Added by Rakkib: user-local bin on PATH"
    profiles = [home / ".bashrc", home / ".zshrc", home / ".profile"]
    removed_any = False
    for profile in profiles:
        if not profile.exists():
            continue
        content = profile.read_text()
        if marker not in content:
            continue
        lines = content.splitlines()
        new_lines: list[str] = []
        skipping = False
        for line in lines:
            if line == marker:
                skipping = True
                continue
            if skipping and line == "esac":
                skipping = False
                continue
            if skipping:
                continue
            new_lines.append(line)
        profile.write_text("\n".join(new_lines).rstrip() + "\n")
        console.print(f"[green]Removed managed PATH block from {profile}[/green]")
        removed_any = True
    return removed_any


def _remove_rakkib_cron_entries(user: str | None) -> bool:
    cmd = ["crontab", "-l"] if user is None else ["crontab", "-u", user, "-l"]
    result = _run_best_effort(cmd)
    if result.returncode != 0:
        return False

    lines = result.stdout.splitlines()
    kept = [line for line in lines if "# RAKKIB:" not in line]
    if kept == lines:
        return False

    write_cmd = ["crontab", "-"] if user is None else ["crontab", "-u", user, "-"]
    write = subprocess.run(write_cmd, input="\n".join(kept) + "\n", text=True, capture_output=True, check=False)
    if write.returncode == 0:
        suffix = f" for {user}" if user else ""
        console.print(f"[green]Removed Rakkib cron entries{suffix}[/green]")
        return True
    detail = (write.stderr or write.stdout or "unknown error").strip()
    console.print(f"[yellow]Could not update crontab: {detail}[/yellow]")
    return False


def _home_for_user(user: str | None) -> Path | None:
    if not user:
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _cloudflared_homes(state: State, home: Path) -> list[Path]:
    homes = [home]
    for user in (state.get("admin_user"), os.environ.get("SUDO_USER")):
        user_home = _home_for_user(str(user)) if user else None
        if user_home:
            homes.append(user_home)
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        homes.append(Path("/root"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in homes:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _remove_cloudflared_artifacts(state: State, home: Path) -> None:
    for user_home in _cloudflared_homes(state, home):
        _remove_path_aggressive(user_home / ".cloudflared")


def _state_data_root(state: State) -> Path:
    return Path(state.get("data_root", "/srv"))


def _registry_container_names(registry: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for svc in registry.get("services", []):
        svc_id = str(svc.get("id", "")).strip()
        if svc_id:
            names.add(svc_id)
        container_name = str(svc.get("container_name", "")).strip()
        if container_name:
            names.add(container_name)
    return names


def _remove_docker_container(name: str) -> bool:
    inspect = docker_run(["inspect", name], check=False)
    if inspect.returncode != 0:
        return False
    result = docker_run(["rm", "-f", "-v", name], check=False)
    if result.returncode == 0:
        console.print(f"[green]Removed Docker container {name}[/green]")
        return True
    detail = (result.stderr or result.stdout or "unknown error").strip()
    console.print(f"[yellow]Could not remove Docker container {name}: {detail}[/yellow]")
    return False


def _remove_rakkib_docker(state: State, registry: dict[str, Any]) -> None:
    if shutil.which("docker") is None:
        console.print("[yellow]Docker not found; skipping Docker container cleanup.[/yellow]")
        return

    if not state.has("data_root"):
        console.print("[yellow]No data_root recorded in state; skipping registry Docker container cleanup.[/yellow]")
        labeled = docker_run(["ps", "-aq", "--filter", "label=com.rakkib"], check=False)
        if labeled.returncode == 0:
            for container_id in labeled.stdout.splitlines():
                if container_id.strip():
                    _remove_docker_container(container_id.strip())
        return

    data_root = _state_data_root(state)
    docker_root = data_root / "docker"
    if docker_root.exists():
        for compose_file in sorted(docker_root.glob("*/docker-compose.yml")):
            compose_dir = compose_file.parent
            try:
                compose_down(
                    compose_dir, volumes=True, log_path=data_root / "logs" / f"uninstall-{compose_dir.name}.log"
                )
                console.print(f"[green]Removed Docker compose project {compose_dir.name}[/green]")
            except Exception as exc:
                console.print(f"[yellow]Could not remove Docker compose project {compose_dir.name}: {exc}[/yellow]")

    for name in sorted(_registry_container_names(registry)):
        _remove_docker_container(name)

    labeled = docker_run(["ps", "-aq", "--filter", "label=com.rakkib"], check=False)
    if labeled.returncode == 0:
        for container_id in labeled.stdout.splitlines():
            if container_id.strip():
                _remove_docker_container(container_id.strip())


def _run_remove_hooks(state: State, registry: dict[str, Any]) -> None:
    if not state.has("data_root"):
        return

    data_root = _state_data_root(state)
    for svc in reversed(registry.get("services", [])):
        svc_id = svc.get("id")
        if not svc_id:
            continue
        has_artifacts = (
            (data_root / "docker" / svc_id).exists()
            or (data_root / "data" / svc_id).exists()
            or bool(svc.get("host_service"))
        )
        if not has_artifacts:
            continue
        try:
            services_step.remove_single_service(state, str(svc_id))
        except Exception as exc:
            console.print(f"[yellow]Best-effort service cleanup for {svc_id} did not fully complete: {exc}[/yellow]")


def _remove_data_root(state: State) -> None:
    data_root = _state_data_root(state)
    if not state.has("data_root"):
        console.print("[yellow]No data_root recorded in state; skipping broad data-root removal.[/yellow]")
        return
    if str(data_root) in {"/", ""}:
        console.print("[yellow]Refusing to remove unsafe data_root path.[/yellow]")
        return
    _remove_path_aggressive(data_root, label=f"Rakkib data root {data_root}")


def _remove_checkout(repo_dir: Path) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        console.print("[yellow]Skipping checkout removal under pytest.[/yellow]")
        return
    checkout = _checkout_dir(repo_dir)
    if (checkout / ".git").exists() or (checkout / "pyproject.toml").exists():
        _remove_path_aggressive(checkout, label=f"Rakkib checkout {checkout}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__import__("rakkib").__version__, prog_name="rakkib")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Rakkib — personal server installer."""
    ctx.ensure_object(dict)
    if "repo_dir" not in ctx.obj:
        ctx.obj["repo_dir"] = Path(__file__).resolve().parent


@cli.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Gather configuration via interview and save to .fss-state.yaml.

    Confirmed configurations immediately install everything.
    """
    console.print("[bold green]Rakkib init[/bold green]")

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)
    previous_state = State(state.to_dict(), path=state.path)

    state = run_interview(state, questions_dir=repo_dir / "data" / "questions")
    state.save(state_path)
    console.print("[bold green]Interview complete. State saved to .fss-state.yaml[/bold green]")

    if state.is_confirmed():
        if not ensure_prereqs(state, console=console, cloudflared_bin=_cloudflared_bin()):
            ctx.exit(1)
        _cleanup_previous_hosting_mode(previous_state, state)
        state.save(state_path)
        if not _run_steps(state, repo_dir):
            ctx.exit(1)
        _persist_deployed_selection(state)
        state.save(state_path)
    else:
        console.print("[yellow]State is not confirmed — run `rakkib init` again to complete the interview.[/yellow]")


@cli.command()
@click.option("--service", "service", help="Deploy only one registry service and skip the full services pass.")
@click.pass_context
def pull(ctx: click.Context, service: str | None) -> None:
    """Install prerequisites and run all setup steps.

    Requires a confirmed state from `rakkib init`.
    """
    console.print("[bold green]Rakkib pull[/bold green]")

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    if service and not state.is_confirmed() and state.get("exposure_mode") is None and not state.get("cloudflare"):
        state.set("exposure_mode", "internal")

    if service:
        services_step._ensure_service_runtime_env(state)

    if not service and not state.is_confirmed():
        console.print("[bold red]State is not confirmed.[/bold red] Run [bold]rakkib init[/bold] first.")
        return

    if not ensure_prereqs(state, console=console, cloudflared_bin=_cloudflared_bin()):
        return

    if service:
        ok = _run_service_pull(state, state_path, service)
    else:
        ok = _run_steps(state, repo_dir)
    if not ok:
        ctx.exit(1)

    if not service:
        _persist_deployed_selection(state)
        state.save(state_path)


@cli.command()
@click.pass_context
def update(ctx: click.Context) -> None:
    """Pull the latest installed CLI code from the current origin branch."""
    repo_dir = _checkout_dir(ctx.obj["repo_dir"])
    if not (repo_dir / ".git").exists():
        console.print(
            f"[bold red]Error:[/bold red] {repo_dir} is not a git checkout. "
            "Reinstall Rakkib with `bash install.sh` or the curl installer first."
        )
        ctx.exit(1)

    console.print("[bold green]Rakkib update[/bold green]")

    try:
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo_dir, capture_output=True, text=True
        )
        if branch_result.returncode != 0:
            raise subprocess.CalledProcessError(
                branch_result.returncode,
                ["git", "branch", "--show-current"],
                output=branch_result.stdout,
                stderr=branch_result.stderr,
            )
        branch = branch_result.stdout.strip()
        if not branch:
            console.print("[bold red]Update failed:[/bold red] checkout is in detached HEAD state.")
            console.print(
                "[yellow]Reinstall Rakkib with `bash install.sh` or switch to a branch before updating.[/yellow]"
            )
            ctx.exit(1)

        for command in (["git", "fetch", "origin", branch], ["git", "pull", "--ff-only", "origin", branch]):
            result = subprocess.run(command, cwd=repo_dir, capture_output=True, text=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    command,
                    output=result.stdout,
                    stderr=result.stderr,
                )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.output or "unknown git error").strip()
        console.print(f"[bold red]Update failed:[/bold red] {detail}")
        console.print("[yellow]Local checkout changes may need to be resolved manually before updating.[/yellow]")
        ctx.exit(1)

    console.print(f"[green]Updated to the latest origin/{branch} code.[/green]")


@cli.command()
@click.option("--json", "json_output", is_flag=True, help="Emit JSON output")
@click.option("--interactive", is_flag=True, help="Interactive mode with auto-fix prompts")
@click.pass_context
def doctor(ctx: click.Context, json_output: bool, interactive: bool) -> None:
    """Run host diagnostics."""
    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    checks = run_checks(state)

    if json_output:
        click.echo(to_json(checks))
    else:
        if interactive:
            from rich.panel import Panel

            console.print(_render_doctor_table(checks, "Rakkib Doctor"))

            # Interactive fixes for blocking failures
            for check in checks:
                if check.blocking and check.status == "fail":
                    console.print(
                        Panel(
                            f"[bold red]{check.name}[/bold red]: {check.message}",
                            title="Blocking Failure",
                        )
                    )
                    fix_result = None
                    if check.name == "docker":
                        answer = click.prompt(
                            "Attempt to fix docker?", type=click.Choice(["y", "n"]), default="n", show_default=False
                        )
                        if answer == "y":
                            fix_result = attempt_fix_docker()
                    elif check.name == "cloudflared_cli":
                        answer = click.prompt(
                            "Attempt to fix cloudflared?",
                            type=click.Choice(["y", "n"]),
                            default="n",
                            show_default=False,
                        )
                        if answer == "y":
                            fix_result = attempt_fix_cloudflared()
                    elif check.name == "public_ports":
                        owners = process_owners_for_ports()
                        console.print("[bold yellow]Port ownership:[/bold yellow]")
                        for port, info in owners.items():
                            console.print(f"  {port}: {info}")
                    else:
                        console.print(f"[dim]No auto-fix available for {check.name}.[/dim]")

                    if fix_result:
                        console.print(f"[bold cyan]Fix result:[/bold cyan] {fix_result}")

            # Re-run checks after fixes
            console.print("\n[bold green]Re-running checks...[/bold green]")
            checks = run_checks(state)
            console.print(_render_doctor_table(checks, "Updated Results"))
        else:
            for check in checks:
                click.echo(f"[{check.status}] {check.name}: {check.message}")
            click.echo(summary_text(checks))

    fail_count = sum(1 for c in checks if c.status == "fail")
    if fail_count > 0:
        ctx.exit(1)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print deployment status and resume point."""
    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    if not state.is_confirmed():
        console.print("[yellow]No confirmed deployment state found. Run `rakkib init` to start.[/yellow]")
        return

    domain = state.get("domain", "") or ""
    data_root = str(state.data_root)

    console.print(f"\n[bold]Domain:[/bold] [cyan]{domain or '—'}[/cyan]")

    # System checks
    console.rule("[bold]System[/bold]")
    _icons = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    _colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for chk in (check_ram(), check_disk(data_root)):
        icon = _icons.get(chk.status, "?")
        color = _colors.get(chk.status, "white")
        console.print(f"  {icon}  [{color}]{chk.name.upper()}[/{color}]  {chk.message}")

    # Installed services
    console.rule("[bold]Installed Services[/bold]")
    registry = services_step._load_registry()
    foundation_ids = set(state.get("foundation_services", []) or [])
    selected_ids = set(state.get("selected_services", []) or [])
    installed_ids = foundation_ids | selected_ids
    urls = {row["service"]: row["url"] for row in deployed_service_urls(state, registry, installed_ids)}

    for svc in registry["services"]:
        svc_id = svc["id"]
        bucket = svc.get("state_bucket", "")
        if svc_id == "caddy" and not caddy_enabled(state):
            continue
        if svc_id == "cloudflared" and not cloudflare_enabled(state):
            continue
        if bucket != "always" and svc_id not in installed_ids:
            continue
        if svc_id in urls:
            console.print(f"  [cyan]{svc_id}[/cyan]  {urls[svc_id]}")
        else:
            console.print(f"  [cyan]{svc_id}[/cyan]")

    host_addons = state.get("host_addons", []) or []
    if host_addons:
        console.rule("[bold]Host Add-ons[/bold]")
        for addon in host_addons:
            console.print(f"  [cyan]{addon}[/cyan]")

    # Available services
    console.rule("[bold]Available Services[/bold]")
    available = [
        svc
        for svc in registry["services"]
        if svc.get("state_bucket") in ("foundation_services", "selected_services") and svc["id"] not in installed_ids
    ]
    if available:
        for svc in available:
            console.print(f"  [dim]{svc['id']}[/dim]")
    else:
        console.print("  [dim]All available services are installed.[/dim]")
    console.print()


@cli.command()
@click.option("--yes", is_flag=True, help="Apply service changes without the confirmation prompt.")
@click.option("--service", "service_option", help="Add one registry service without the checkbox prompt.")
@click.argument("service", required=False)
@click.pass_context
def add(ctx: click.Context, service: str | None, service_option: str | None, yes: bool) -> None:
    """Sync deployed services against the registry."""
    console.print("[bold green]Rakkib add[/bold green]")

    if service and service_option and service != service_option:
        console.print(
            "[bold red]Error:[/bold red] Provide the service either as an argument or with --service, not both."
        )
        sys.exit(1)
    service = service_option or service

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)
    if not state.is_confirmed() and state.get("exposure_mode") is None and not state.get("cloudflare"):
        state.set("exposure_mode", "internal")
    services_step._ensure_service_runtime_env(state)
    if not ensure_prereqs(state, console=console, cloudflared_bin=_cloudflared_bin()):
        ctx.exit(1)

    registry = services_step._load_registry()
    old_selected = set(state.get("foundation_services", []) or [])
    old_selected.update(state.get("selected_services", []) or [])

    if service:
        by_id = {svc["id"]: svc for svc in registry["services"]}
        if service not in by_id:
            console.print(f"[bold red]Error:[/bold red] Unknown service '{service}'.")
            sys.exit(1)
        selected_ids = set(old_selected)
        if by_id[service].get("state_bucket") != "always":
            selected_ids.add(service)
    else:
        selected = prompt_checkbox(
            "Select services to keep installed:",
            choices=_build_add_choices(state, registry),
        )
        selected_ids = set(selected)

    dependency_errors = _validate_service_dependencies(selected_ids, registry)
    if dependency_errors:
        console.print("[bold red]Error:[/bold red] Invalid service selection:")
        for error in dependency_errors:
            console.print(f"  - {error}")
        sys.exit(1)

    added = sorted(selected_ids - old_selected)
    removed = sorted(old_selected - selected_ids)
    planned_subdomains: dict[str, str] | None = None

    if added and not yes:
        try:
            planned_state = State(state.to_dict())
            _apply_service_selection(planned_state, registry, selected_ids)
            _prompt_service_subdomains(planned_state, registry, set(added))
            planned_subdomains = dict(planned_state.get("subdomains", {}) or {})
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            sys.exit(1)

    if added or removed:
        _summarize_service_diff(added, removed)
        if not yes and not prompt_confirm("Apply these service changes?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return
        if yes:
            console.print("[dim]Confirmation skipped because --yes was provided.[/dim]")
    else:
        console.print("[yellow]No selection changes; refreshing selected services.[/yellow]")

    rollback_snapshot = copy.deepcopy(state._data)
    fallback_previous_selection = _selected_service_lists(state)
    _apply_service_selection(state, registry, selected_ids)
    if planned_subdomains is not None:
        _apply_planned_subdomains(state, registry, planned_subdomains)

    if not _sync_services_to_state_selection(
        state,
        state_path,
        rollback_snapshot=rollback_snapshot,
        fallback_previous_selection=fallback_previous_selection,
    ):
        ctx.exit(1)

    console.print("[bold green]Service selection synced successfully.[/bold green]")
    deployed_ids: list[str] | None = None
    if service:
        deployed_ids = [service]
    elif added:
        deployed_ids = list(added)
    if deployed_ids:
        _print_deployed_urls(state, deployed_ids)


@cli.command(name="sync-services")
@click.pass_context
def sync_services(ctx: click.Context) -> None:
    """Apply the current saved service selection without re-running full setup."""
    console.print("[bold green]Rakkib sync-services[/bold green]")

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    if not _sync_services_to_state_selection(state, state_path):
        ctx.exit(1)

    console.print("[bold green]Service selection synced successfully.[/bold green]")
    _print_deployed_urls(state)


@cli.command()
@click.argument("service")
@click.pass_context
def smoke(ctx: click.Context, service: str) -> None:
    """Fetch a deployed service URL and verify its registry smoke marker."""
    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    result = services_step.smoke_check(state, service)
    if result.ok:
        console.print(f"[green]✓[/green] {result.message}")
        return

    console.print(f"[bold red]✗[/bold red] {result.message}")
    ctx.exit(1)


@cli.command()
@click.argument("service", required=False)
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
def remove(ctx: click.Context, service: str | None, yes: bool) -> None:
    """Remove services (containers, rendered files, data dirs).

    Without a service argument, installed services are shown checked; uncheck services
    to purge them.
    """
    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    registry = load_service_registry()
    selected_list = list(state.get("selected_services", []) or [])
    foundation_list = list(state.get("foundation_services", []) or [])
    selected = set(selected_list)
    foundation = set(foundation_list)
    old_selected = foundation | selected

    by_id = {svc["id"]: svc for svc in registry["services"]}
    if service:
        if service not in by_id:
            console.print(f"[bold red]Error:[/bold red] Unknown service '{service}'.")
            ctx.exit(1)
        if by_id[service].get("state_bucket") == "always":
            console.print(
                f"[bold red]Error:[/bold red] '{service}' is an always-installed service and cannot be removed."
            )
            ctx.exit(1)
        remove_ids = {service}
        if service not in old_selected:
            console.print(f"[yellow]{service} is not selected; attempting cleanup anyway.[/yellow]")
        keep_ids = old_selected - remove_ids
    else:
        if not old_selected:
            console.print("[yellow]No removable installed services found.[/yellow]")
            return
        keep = prompt_checkbox(
            "Select services to keep installed (uncheck to purge):",
            choices=_build_remove_choices(state, registry),
        )
        keep_ids = set(keep)
        remove_ids = old_selected - keep_ids
        if not remove_ids:
            console.print("[yellow]No services selected for removal.[/yellow]")
            return

        dependency_errors = _validate_service_dependencies(keep_ids, registry)
        if dependency_errors:
            console.print("[bold red]Error:[/bold red] Invalid remaining service selection:")
            for error in dependency_errors:
                console.print(f"  - {error}")
            ctx.exit(1)

    remove_list = sorted(remove_ids)
    if len(remove_list) > 1:
        _summarize_service_diff([], remove_list)

    confirm_message = (
        f"Remove '{remove_list[0]}' and delete its data?"
        if len(remove_list) == 1
        else "Remove selected services and delete their data?"
    )
    if not yes and not prompt_confirm(confirm_message, default=False):
        console.print("[yellow]Aborted.[/yellow]")
        return
    if yes:
        console.print("[dim]Confirmation skipped because --yes was provided.[/dim]")

    previous_state = State(
        {
            "foundation_services": foundation_list,
            "selected_services": selected_list,
        }
    )
    removal_order = [
        svc["id"] for svc in reversed(selected_service_defs(previous_state, registry)) if svc["id"] in remove_ids
    ]
    if service and service not in removal_order:
        removal_order.append(service)
    for svc_id in removal_order:
        services_step.remove_single_service(state, svc_id)

    # Update state selection so a later `rakkib pull` won't re-add it.
    state.set("selected_services", sorted(selected - remove_ids))
    state.set("foundation_services", sorted(foundation - remove_ids))
    _persist_deployed_selection(state)
    state.save(state_path)

    data_root = state.data_root
    if caddy_enabled(state) and (data_root / "docker" / "caddy" / "docker-compose.yml").exists():
        services_step._reload_caddy(data_root)
    services_step.sync_shared_artifacts(state, services_step._repo_dir(), data_root, registry)

    console.print(f"[bold green]Removed {', '.join(remove_list)}.[/bold green]")


@cli.command()
@click.argument("service", required=False)
@click.option("--all", "restart_all", is_flag=True, help="Restart all services in dependency order")
@click.pass_context
def restart(ctx: click.Context, service: str | None, restart_all: bool) -> None:
    """Restart one or all deployed services.

    \b
    rakkib restart caddy          # restart a single service
    rakkib restart --all          # restart all services in dependency order
    """
    if service and restart_all:
        console.print("[yellow]Use either a service name or --all, not both.[/yellow]")
        ctx.exit(1)

    repo_dir = ctx.obj["repo_dir"]
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)

    if restart_all:
        console.print("[bold green]Restarting all services...[/bold green]")
        try:
            restarted = services_step.restart_all(state)
        except subprocess.CalledProcessError as exc:
            console.print(f"[bold red]Restart failed:[/bold red] {exc}")
            sys.exit(1)
        for svc_id in restarted:
            console.print(f"  [green]✓[/green] {svc_id}")
        console.print(f"[bold green]Done.[/bold green] {len(restarted)} service(s) restarted.")
    elif service:
        console.print(f"[bold green]Restarting {service}...[/bold green]")
        try:
            services_step.restart_service(state, service)
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            sys.exit(1)
        except subprocess.CalledProcessError as exc:
            console.print(f"[bold red]Restart failed:[/bold red] {exc}")
            sys.exit(1)
        console.print(f"[green]✓[/green] {service} restarted.")
    else:
        registry = load_service_registry()
        choices = _build_restart_choices(state, registry)
        if not choices:
            console.print("[yellow]No deployed services found to restart.[/yellow]")
            ctx.exit(1)

        selected = prompt_checkbox(
            "Select deployed services to restart:",
            choices=choices,
        )
        if not selected:
            console.print("[yellow]No services selected.[/yellow]")
            return

        restart_ids = _restart_order(registry, set(selected))
        console.print("[bold green]Restarting selected services...[/bold green]")
        for svc_id in restart_ids:
            try:
                services_step.restart_service(state, svc_id)
            except ValueError as exc:
                console.print(f"[bold red]Error:[/bold red] {exc}")
                sys.exit(1)
            except subprocess.CalledProcessError as exc:
                console.print(f"[bold red]Restart failed:[/bold red] {exc}")
                sys.exit(1)
            console.print(f"  [green]✓[/green] {svc_id}")
        console.print(f"[bold green]Done.[/bold green] {len(restart_ids)} service(s) restarted.")


@cli.command()
@click.confirmation_option(prompt=("Remove Rakkib, its services, saved state, and local data?"))
@click.pass_context
def uninstall(ctx: click.Context) -> None:
    """Remove Rakkib-managed host, Docker, Cloudflare, and data-root artifacts."""
    home = Path.home()
    repo_dir = Path(ctx.obj["repo_dir"])
    checkout = _checkout_dir(repo_dir)
    state_path = default_state_path(repo_dir)
    state = State.load(state_path)
    registry = load_service_registry()

    console.print("[bold yellow]Uninstalling Rakkib...[/bold yellow]")

    _run_remove_hooks(state, registry)
    _remove_rakkib_docker(state, registry)
    cron_user = state.get("admin_user") if os.geteuid() == 0 and state.get("admin_user") else None
    if cron_user:
        _remove_rakkib_cron_entries(str(cron_user))
    _remove_rakkib_cron_entries(None)
    _remove_cloudflared_artifacts(state, home)

    shim = home / ".local" / "bin" / "rakkib"
    if shim.is_symlink():
        _remove_path_aggressive(shim, label=f"rakkib CLI shim at {shim}")
    elif shim.exists():
        console.print(f"[yellow]{shim} exists but is not a symlink; not removed[/yellow]")
    else:
        console.print(f"[yellow]No rakkib CLI shim found at {shim}[/yellow]")

    for helper in ("cloudflared-healthcheck.sh",):
        _remove_path_aggressive(home / ".local" / "bin" / helper)

    if not _remove_managed_path_blocks(home):
        console.print("[yellow]No managed PATH block found in shell profiles[/yellow]")

    for artifact in (repo_dir / ".rakkib-web-run.log", checkout / ".rakkib-web-run.log", state_path):
        _remove_path_aggressive(artifact)

    _remove_data_root(state)
    _remove_checkout(repo_dir)

    console.print(
        "\n[bold]Rakkib uninstall is complete.[/bold]\n"
        "If this terminal still resolves rakkib, refresh your shell command cache or open a new terminal:\n"
        "  hash -r"
    )


@cli.command()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Validate sudo and prepare Docker access when needed."""
    if not _run_auth_setup(ctx):
        ctx.exit(1)


@cli.command()
@click.option("--lan", is_flag=True, help="Bind to 0.0.0.0 and print a LAN URL.")
@click.option("--local", "local_only", is_flag=True, help="Bind locally and skip the LAN setup prompt.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host interface to bind.")
@click.option("--port", default=8080, show_default=True, type=int, help="TCP port to bind.")
@click.option("--token", "startup_token", default="", help="Explicit setup token to require.")
@click.option("--no-token", is_flag=True, help="Disable token auth for this web session.")
@click.option("--no-open", is_flag=True, help="Do not attempt to open a browser automatically.")
@click.pass_context
def web(
    ctx: click.Context,
    lan: bool,
    local_only: bool,
    host: str,
    port: int,
    startup_token: str,
    no_token: bool,
    no_open: bool,
) -> None:
    """Run the local browser UI and token bootstrap server."""
    import uvicorn

    from rakkib.web import WebRuntimeConfig, create_app

    if lan and local_only:
        console.print("[bold red]Error:[/bold red] Use either --lan or --local, not both.")
        raise click.Abort()

    if not lan and not local_only and host == "127.0.0.1" and _stdin_is_interactive():
        lan = prompt_confirm(
            "Do you want to set up your server from another machine on your network?",
            default=False,
        )

    if local_only:
        host = "127.0.0.1"

    _prompt_web_host_auth(ctx)

    bind_host = host
    if lan and host == "127.0.0.1":
        bind_host = "0.0.0.0"

    if no_token and startup_token:
        console.print("[bold red]Error:[/bold red] Use either --token or --no-token, not both.")
        raise click.Abort()

    token_auth_enabled = not no_token
    if token_auth_enabled:
        startup_token = startup_token or token_urlsafe(24)
    else:
        startup_token = ""

    config = WebRuntimeConfig(
        host=bind_host,
        port=port,
        repo_dir=ctx.obj["repo_dir"],
        token_auth_enabled=token_auth_enabled,
        startup_token=startup_token or None,
    )
    app = create_app(config)

    local_url = _web_url("127.0.0.1" if bind_host == "0.0.0.0" else bind_host, port, startup_token or None)
    lan_ip = _detect_lan_ip() if bind_host == "0.0.0.0" else None
    lan_url = _web_url(lan_ip, port, startup_token or None) if lan_ip else None

    console.print("[bold green]Rakkib web[/bold green]")
    console.print(f"[dim]Bind:[/dim] {bind_host}:{port}")
    console.print(f"[dim]Local:[/dim] {local_url}")
    if lan_url:
        console.print(f"[dim]LAN:[/dim] {lan_url}")
    elif lan and not lan_ip:
        console.print(
            "[yellow]LAN mode is enabled, but the primary LAN IP could not be detected automatically.[/yellow]"
        )

    if token_auth_enabled:
        console.print("[dim]Token auth:[/dim] required")
    else:
        console.print("[yellow]Token auth is disabled because --no-token was provided explicitly.[/yellow]")

    qr_url = lan_url or local_url
    console.print(f"\n[dim]Scan to open:[/dim] {qr_url}\n")
    _show_qr(qr_url)

    if not no_open:
        try:
            webbrowser.open(local_url)
        except Exception:
            console.print("[dim]Browser auto-open was skipped because no local browser handler was available.[/dim]")

    uvicorn.run(app, host=bind_host, port=port, log_level="info")


@cli.group()
@click.pass_context
def privileged(ctx: click.Context) -> None:
    """Root-only helper actions."""
    if os.geteuid() != 0:
        console.print("[bold red]Error:[/bold red] This helper must be run with sudo or from a root shell.")
        ctx.exit(1)


@privileged.command(name="check")
def privileged_check() -> None:
    """Verify the helper is running as root."""
    console.print("[green]Privileged helper is running as root.[/green]")


@privileged.command(name="ensure-layout")
@click.option("--state", "state_path", type=click.Path(path_type=Path), default=".fss-state.yaml")
@click.option("--data-root", type=str, default="")
@click.option("--admin-user", type=str, default="")
@click.pass_context
def privileged_ensure_layout(ctx: click.Context, state_path: Path, data_root: str, admin_user: str) -> None:
    """Create the base Rakkib data directories."""
    state = State.load(state_path)
    if not data_root:
        data_root = str(state.data_root)
    user = _resolve_admin_user(state, admin_user)

    console.print(f"[bold green]Creating Rakkib layout under {data_root}[/bold green]")
    root = Path(data_root)
    # These dirs are admin-owned by design; recurse into them safely.
    admin_dirs = [
        root / "docker",
        root / "apps" / "static",
        root / "backups",
        root / "MDs",
        root / "logs",
    ]
    # These dirs must be created but NOT recursed — data/ contains service-managed UIDs.
    top_only = [root, root / "apps", root / "data"]

    for p in admin_dirs + top_only:
        p.mkdir(parents=True, exist_ok=True)

    for p in top_only:
        shutil.chown(p, user=user, group=None)

    for p in admin_dirs:
        shutil.chown(p, user=user, group=None)
        for dirpath, dirs, files in os.walk(p):
            for d in dirs:
                shutil.chown(os.path.join(dirpath, d), user=user, group=None)
            for f in files:
                shutil.chown(os.path.join(dirpath, f), user=user, group=None)

    console.print(f"[green]Layout created and owned by {user}.[/green]")


@privileged.command(name="fix-repo-owner")
@click.option("--state", "state_path", type=click.Path(path_type=Path), default=".fss-state.yaml")
@click.option("--admin-user", type=str, default="")
@click.option("--repo-dir", type=click.Path(path_type=Path), default="")
@click.pass_context
def privileged_fix_repo_owner(ctx: click.Context, state_path: Path, admin_user: str, repo_dir: Path) -> None:
    """Assign the repo back to the admin user."""
    state = State.load(state_path)
    user = _resolve_admin_user(state, admin_user)
    if not repo_dir:
        repo_dir = ctx.obj["repo_dir"]
    if not repo_dir.exists():
        console.print(f"[red]Repo directory does not exist: {repo_dir}[/red]")
        ctx.exit(1)

    console.print(f"[bold green]Assigning {repo_dir} to {user}[/bold green]")
    for root, dirs, files in os.walk(repo_dir):
        for d in dirs:
            shutil.chown(os.path.join(root, d), user=user, group=None)
        for f in files:
            shutil.chown(os.path.join(root, f), user=user, group=None)
    shutil.chown(repo_dir, user=user, group=None)
    console.print(f"[green]Repo ownership updated to {user}.[/green]")


if __name__ == "__main__":
    cli()
