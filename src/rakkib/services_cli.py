"""CLI-facing service selection helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from questionary import Choice
from rich.console import Console

from rakkib.service_catalog import (
    apply_service_catalog_selection,
    caddy_enabled,
    cloudflare_enabled,
    deployed_service_urls,
    normalize_subdomain,
    validate_subdomain_label,
    validate_subdomain_map,
    validate_service_dependencies,
)
from rakkib.state import State, StateBucket
from rakkib.tui import prompt_text


console = Console()


@dataclass(frozen=True)
class PickerOptions:
    """Options for rendering registry service checkbox choices."""

    active_ids: set[str]
    restart_mode: bool = False
    include_only_active: bool = False


def service_label(svc: dict[str, Any]) -> str:
    return append_service_suffixes(svc["id"], svc)


def service_description_suffix(svc: dict[str, Any]) -> str:
    homepage = svc.get("homepage") or {}
    description = str(homepage.get("description") or "").strip()
    if description:
        return f" [{description}]"

    notes = str(svc.get("notes", "")).strip()
    summary = notes.split(".", 1)[0].strip()
    if summary:
        return f" [{summary}]"

    return ""


def _format_ram_label(value_mb: int) -> str:
    if value_mb % 1024 == 0:
        return f"{value_mb // 1024} GB RAM"
    return f"{value_mb / 1024:.1f} GB RAM"


def resource_warning_suffix(svc: dict[str, Any]) -> str:
    requirements = svc.get("resource_requirements") or {}
    if not requirements:
        return ""

    ram_target = requirements.get("recommended_ram_mb") or requirements.get("min_ram_mb")
    disk_target = requirements.get("recommended_disk_gb") or requirements.get("min_disk_gb")
    if ram_target is None and disk_target is None:
        return ""

    parts: list[str] = []
    if ram_target is not None:
        parts.append(_format_ram_label(int(ram_target)))
    if disk_target is not None:
        parts.append(f"{int(disk_target)} GB disk")

    qualifier = (
        "recommended"
        if requirements.get("recommended_ram_mb") or requirements.get("recommended_disk_gb")
        else "minimum"
    )
    return f" [heavy: {', '.join(parts)} {qualifier}]"


def append_resource_warning(label: str, svc: dict[str, Any]) -> str:
    return f"{label}{resource_warning_suffix(svc)}"


def append_service_suffixes(label: str, svc: dict[str, Any]) -> str:
    return append_resource_warning(f"{label}{service_description_suffix(svc)}", svc)


def service_selection_category(svc: dict[str, Any]) -> str:
    homepage = svc.get("homepage") or {}
    category = str(homepage.get("category") or "").strip()
    return category or "Other"


def installed_service_ids(state: State) -> set[str]:
    installed = set(state.get(StateBucket.FOUNDATION_SERVICES, []) or [])
    installed.update(state.get(StateBucket.SELECTED_SERVICES, []) or [])
    return installed


def selected_service_lists(state: State) -> tuple[list[str], list[str]]:
    foundation = list(state.get(StateBucket.FOUNDATION_SERVICES, []) or [])
    selected = list(state.get(StateBucket.SELECTED_SERVICES, []) or [])
    return foundation, selected


def persist_deployed_selection(state: State) -> None:
    foundation, selected = selected_service_lists(state)
    state.set("deployed.exists", True)
    state.set("deployed.foundation_services", foundation)
    state.set("deployed.selected_services", selected)


def deployed_service_lists(state: State) -> tuple[list[str], list[str]]:
    foundation = state.get("deployed.foundation_services")
    selected = state.get("deployed.selected_services")
    if isinstance(foundation, list) and isinstance(selected, list):
        return list(foundation), list(selected)
    return selected_service_lists(state)


def _append_bucket_choices(
    choices: list[Choice],
    title: str,
    bucket: StateBucket,
    registry: dict[str, Any],
    state: State,
    options: PickerOptions,
) -> None:
    bucket_services = [
        svc
        for svc in registry["services"]
        if svc.get("state_bucket") == bucket
        and not (svc["id"] == "caddy" and not caddy_enabled(state))
        and not (svc["id"] == "cloudflared" and not cloudflare_enabled(state))
        and (not options.include_only_active or svc["id"] in options.active_ids)
    ]
    if not bucket_services:
        return
    prefix = "__header_restart" if options.restart_mode else "__header"
    choices.append(Choice(title=f"━━ {title} ━━", value=f"{prefix}_{bucket}__", disabled=True))
    for svc in bucket_services:
        checked = False if options.restart_mode else bucket == StateBucket.ALWAYS or svc["id"] in options.active_ids
        disabled = False if options.restart_mode else bucket == StateBucket.ALWAYS
        choices.append(
            Choice(
                title=f"  {service_label(svc)}",
                value=svc["id"],
                checked=checked,
                disabled=disabled,
            )
        )


def build_service_choices(state: State, registry: dict[str, Any], options: PickerOptions) -> list[Choice]:
    choices: list[Choice] = []
    _append_bucket_choices(choices, "Always Installed", StateBucket.ALWAYS, registry, state, options)
    _append_bucket_choices(choices, "Foundation Bundle", StateBucket.FOUNDATION_SERVICES, registry, state, options)

    optional_services = [
        svc
        for svc in registry["services"]
        if svc.get("state_bucket") == StateBucket.SELECTED_SERVICES
        and (not options.include_only_active or svc["id"] in options.active_ids)
    ]
    optional_groups: dict[str, list[dict[str, Any]]] = {}
    for svc in optional_services:
        optional_groups.setdefault(service_selection_category(svc), []).append(svc)

    for category, services in optional_groups.items():
        prefix = "__header_restart" if options.restart_mode else "__header"
        choices.append(Choice(title=f"━━ {category} ━━", value=f"{prefix}_{category}__", disabled=True))
        for svc in services:
            choices.append(
                Choice(
                    title=f"  {service_label(svc)}",
                    value=svc["id"],
                    checked=False if options.restart_mode else svc["id"] in options.active_ids,
                )
            )

    return choices


def build_add_choices(state: State, registry: dict[str, Any]) -> list[Choice]:
    return build_service_choices(state, registry, PickerOptions(active_ids=installed_service_ids(state)))


def build_remove_choices(state: State, registry: dict[str, Any]) -> list[Choice]:
    return build_service_choices(
        state,
        registry,
        PickerOptions(active_ids=installed_service_ids(state), include_only_active=True),
    )


def build_restart_choices(state: State, registry: dict[str, Any]) -> list[Choice]:
    if not state.get("deployed.exists", False):
        return []

    foundation, selected = deployed_service_lists(state)
    active_ids = set(foundation)
    active_ids.update(selected)
    active_ids.update(svc["id"] for svc in registry["services"] if svc.get("state_bucket") == StateBucket.ALWAYS)
    return build_service_choices(
        state,
        registry,
        PickerOptions(active_ids=active_ids, restart_mode=True, include_only_active=True),
    )


def restart_order(registry: dict[str, Any], selected_ids: set[str]) -> list[str]:
    ordered = [svc["id"] for svc in registry["services"] if svc["id"] in selected_ids and svc["id"] != "caddy"]
    if "caddy" in selected_ids:
        ordered.append("caddy")
    return ordered


def apply_service_selection(state: State, registry: dict[str, Any], selected_ids: set[str]) -> None:
    apply_service_catalog_selection(state, registry, selected_ids)


def validate_selection_dependencies(selected_ids: set[str], registry: dict[str, Any]) -> list[str]:
    return validate_service_dependencies(selected_ids, registry)


def prompt_service_subdomains(state: State, registry: dict[str, Any], service_ids: set[str]) -> None:
    """Prompt for custom subdomains for the given service ids."""
    if not service_ids or not caddy_enabled(state):
        return

    by_id = {svc["id"]: svc for svc in registry["services"]}
    subdomains = dict(state.get("subdomains", {}) or {})
    domain = str(state.get("domain") or "").strip().strip(".")

    for svc_id in sorted(service_ids):
        svc = by_id.get(svc_id)
        if not svc or not svc.get("default_subdomain"):
            continue

        default = normalize_subdomain(subdomains.get(svc_id) or svc.get("default_subdomain") or svc_id)
        while True:
            suffix = f".{domain}" if domain else ""
            answer = prompt_text(f"Subdomain for {svc_id}{suffix}", default=default)
            label = normalize_subdomain(answer or default)
            message = validate_subdomain_label(label)
            if message:
                console.print(f"[red]{message}[/red]")
                continue
            duplicate = next(
                (
                    other_id
                    for other_id, other_label in subdomains.items()
                    if other_id != svc_id and normalize_subdomain(other_label) == label
                ),
                None,
            )
            if duplicate:
                console.print(f"[red]{label} is already used by {duplicate}.[/red]")
                continue
            subdomains[svc_id] = label
            state.set(f"subdomains.{svc_id}", label)
            placeholder = svc.get("subdomain_placeholder")
            if placeholder:
                state.set(str(placeholder), label)
            break

    errors = validate_subdomain_map(subdomains)
    if errors:
        raise ValueError("Invalid subdomain configuration: " + "; ".join(errors))
    state.set("subdomains", subdomains)


def apply_planned_subdomains(state: State, registry: dict[str, Any], planned: dict[str, str]) -> None:
    state.set("subdomains", planned)
    for svc in registry["services"]:
        svc_id = svc["id"]
        if svc_id not in planned:
            continue
        placeholder = svc.get("subdomain_placeholder")
        if placeholder:
            state.set(str(placeholder), planned[svc_id])


def summarize_service_diff(added: list[str], removed: list[str]) -> None:
    if added:
        console.print(f"[green]Will install:[/green] {', '.join(added)}")
    if removed:
        console.print(f"[red]Will remove:[/red] {', '.join(removed)}")
        console.print("[yellow]Unchecked services will be fully purged, including data and service databases.[/yellow]")


def print_deployed_urls(state: State, svc_ids: list[str] | None = None) -> None:
    from rakkib.steps import load_service_registry

    registry = load_service_registry()
    active_ids = set(svc_ids) if svc_ids is not None else installed_service_ids(state)
    rows = deployed_service_urls(state, registry, active_ids)
    if not rows:
        return
    console.print("\n[bold]Deployed services:[/bold]")
    for row in rows:
        console.print(f"  [cyan]{row['service']}[/cyan]  {row['url']}")
