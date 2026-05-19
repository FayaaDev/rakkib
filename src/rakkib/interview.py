"""Interview engine — phase loop: prompt -> validate -> normalize -> persist."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from questionary import Choice
from rich.console import Console

from rakkib.host_platform import ensure_state_platform
from rakkib.normalize import apply_normalize, eval_when
from rakkib.schema import FieldDef, QuestionSchema, load_all_schemas
from rakkib.state import State, subdomain_placeholder_key
from rakkib.service_catalog import (
    apply_service_catalog_selection,
    caddy_enabled,
    normalize_subdomain,
    mark_deployment_stale,
    validate_subdomain_label,
    validate_subdomain_map,
)
from rakkib.services_cli import append_service_suffixes
from rakkib.steps import load_service_registry
from rakkib.tui import prompt_checkbox, prompt_password, prompt_select, prompt_text

console = Console()
_TEMPLATE_KEY_RE = re.compile(r"\{\{([^{}]+)\}\}")
_SCHEMA_COMMAND_FORBIDDEN_RE = re.compile(r"[;|&$`(){}<>]")
_EXIT_VALUE = "__rakkib_interview_exit__"


class InterviewExit(Exception):
    """Raised when the user abandons the interview."""


def _split_schema_command(command: Any) -> list[str]:
    """Parse a schema-loaded command without enabling shell metacharacters."""
    if isinstance(command, list):
        if not all(isinstance(part, str) and part for part in command):
            raise ValueError("schema command argv must contain non-empty strings")
        return command

    command = str(command)
    if _SCHEMA_COMMAND_FORBIDDEN_RE.search(command):
        raise ValueError("schema command contains shell metacharacters")
    return shlex.split(command)


def run_interview(state: State, questions_dir: Path | str = "questions") -> State:
    """Drive Phases 1-6 using embedded AgentSchema blocks.

    Resume is automatic: load state, find first phase with unset required keys,
    start there. If ``confirmed: true``, ask once whether to start over.
    """
    schemas = load_all_schemas(questions_dir)

    if state.is_confirmed():
        overwrite = _prompt_bool(
            "An existing confirmed state was found. Start over?",
            default=False,
        )
        if overwrite:
            state = State({}, path=state.path)

    ensure_state_platform(state)

    resume = 1 if state.has("confirmed") and not state.is_confirmed() else state.resume_phase()
    try:
        for schema in schemas:
            if schema.phase < resume:
                continue
            _run_phase(schema, state)
            _save_if_bound(state)
    except InterviewExit:
        _discard_state(state)

    return state


def _save_if_bound(state: State) -> None:
    """Persist interview progress when the caller supplied a state path."""
    if state.path is not None:
        state.save()


def _discard_state(state: State) -> None:
    """Clear interview answers without writing an empty state file."""
    state.clear()
    if state.path is not None and state.path.exists():
        state.path.unlink()


def _run_phase(schema: QuestionSchema, state: State) -> None:
    """Execute a single phase's field list against the current state."""
    if schema.service_catalog:
        _handle_service_catalog(schema, state)
        _enforce_rules(schema, state)
        _save_if_bound(state)
        return

    for field in schema.fields:
        _run_field(field, state, schema)
    _enforce_rules(schema, state)


def _run_field(field: FieldDef, state: State, schema: QuestionSchema | None = None) -> None:
    """Process one field: detect, derive, prompt, validate, normalize, record."""
    if field.when and not eval_when(field.when, state):
        return

    if field.type == "derived":
        _handle_derived(field, state)
        return

    if field.type == "secret_group":
        _handle_secret_group(field, state)
        return

    if field.type == "summary":
        _handle_summary(field, state)
        return

    if field.repeat_for:
        _handle_repeat(field, state, schema)
        return

    value: Any = None
    if field.type == "text":
        value = _prompt_text(field, state)
    elif field.type == "confirm":
        value = _prompt_confirm(field, state)
    elif field.type == "single_select":
        value = _prompt_single_select(field, state)
    elif field.type == "multi_select":
        value = _prompt_multi_select(field, state)
    else:
        console.print(f"[yellow]Unknown field type: {field.type}[/yellow]")
        return

    if field.value_if_true is not None:
        if value is True:
            _record_dict(field.value_if_true, state)
        return

    _record_field_value(field, value, state)
    if _is_final_confirmation(field) and value is False:
        raise InterviewExit


def _is_final_confirmation(field: FieldDef) -> bool:
    return field.id == "confirmed" or "confirmed" in field.records


# ---------------------------------------------------------------------------
# Service catalog (Phase 3 combined checkbox)
# ---------------------------------------------------------------------------


def _service_catalog_category(slug: str, registry: dict[str, Any]) -> str:
    by_id = {svc["id"]: svc for svc in registry.get("services", [])}
    homepage = (by_id.get(slug) or {}).get("homepage") or {}
    category = str(homepage.get("category") or "").strip()
    return category or "Other"


def _handle_service_catalog(schema: QuestionSchema, state: State) -> None:
    """Present the service catalog as a single grouped checkbox, then
    record foundation_services, selected_services, host_addons, and subdomains.

    Uses questionary.checkbox with sectioned Choices so the user sees
    Foundation (pre-checked), categorized services (unchecked), and Host Addons (unchecked)
    in one prompt.
    """
    catalog = schema.service_catalog or {}

    foundation_items = catalog.get("foundation_bundle", [])
    optional_items = catalog.get("optional_services", [])
    host_items = catalog.get("host_addons", [])
    registry = load_service_registry()
    by_id = {svc["id"]: svc for svc in registry.get("services", [])}
    prior_foundation = set(state.get("foundation_services", []) or [])
    prior_selected = set(state.get("selected_services", []) or [])
    prior_host_addons = set(state.get("host_addons", []) or [])
    has_prior_selection = bool(prior_foundation or prior_selected or prior_host_addons)

    choices: list[Choice] = []

    if foundation_items:
        choices.append(
            Choice(
                title="━━ Foundation Bundle ━━",
                value="__header_foundation__",
                disabled=True,
            )
        )
        for item in foundation_items:
            slug = item["slug"]
            label = item.get("label", slug)
            svc = by_id.get(slug, {"id": slug})
            is_checked = slug in prior_foundation if has_prior_selection else True
            choices.append(Choice(title=f"  {append_service_suffixes(label, svc)}", value=slug, checked=is_checked))

    optional_groups: dict[str, list[dict[str, Any]]] = {}
    for item in optional_items:
        optional_groups.setdefault(_service_catalog_category(item["slug"], registry), []).append(item)

    for category, items in optional_groups.items():
        choices.append(
            Choice(
                title=f"━━ {category} ━━",
                value=f"__header_{category}__",
                disabled=True,
            )
        )
        for item in items:
            slug = item["slug"]
            label = item.get("label", slug)
            svc = by_id.get(slug, {"id": slug})
            choices.append(
                Choice(title=f"  {append_service_suffixes(label, svc)}", value=slug, checked=slug in prior_selected)
            )

    if host_items:
        choices.append(Choice(title="━━ Host Addons ━━", value="__header_host__", disabled=True))
        for item in host_items:
            slug = item["slug"]
            label = item.get("label", slug)
            svc = by_id.get(slug, {"id": slug})
            choices.append(
                Choice(title=f"  {append_service_suffixes(label, svc)}", value=slug, checked=slug in prior_host_addons)
            )

    console.print("\n[bold]=== Service Selection ===[/bold]\n")

    choices.append(_exit_choice())
    selected = prompt_checkbox("Select services to install:", choices=choices)
    _raise_if_exit(selected)

    foundation_defaults = [item["slug"] for item in foundation_items]
    foundation_selected = [s for s in selected if s in foundation_defaults]
    optional_selected = [s for s in selected if s in {item["slug"] for item in optional_items}]
    host_selected = [s for s in selected if s in {item["slug"] for item in host_items}]

    state.set("host_addons", host_selected)
    apply_service_catalog_selection(state, registry, set(foundation_selected + optional_selected))
    if caddy_enabled(state):
        _prompt_selected_service_subdomains(state, registry, set(foundation_selected + optional_selected))
    mark_deployment_stale(state)

    state.set("customize_subdomains", caddy_enabled(state))


def _prompt_selected_service_subdomains(state: State, registry: dict[str, Any], selected_ids: set[str]) -> None:
    """Let users accept or customize selected service subdomain defaults."""
    subdomains = dict(state.get("subdomains", {}) or {})
    domain = str(state.get("domain") or "").strip().strip(".")

    for svc in registry.get("services", []):
        svc_id = svc["id"]
        if svc_id not in selected_ids or not svc.get("default_subdomain"):
            continue

        default = normalize_subdomain(subdomains.get(svc_id) or svc.get("default_subdomain") or svc_id)
        while True:
            suffix = f".{domain}" if domain else ""
            answer = prompt_text(f"Subdomain for {svc_id}{suffix}", default=default)
            _raise_if_exit(answer)
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
            placeholder = svc.get("subdomain_placeholder") or subdomain_placeholder_key(svc_id)
            state.set(str(placeholder), label)
            break

    errors = validate_subdomain_map(subdomains)
    if errors:
        raise ValueError("Invalid subdomain configuration: " + "; ".join(errors))
    state.set("subdomains", subdomains)


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------


def _handle_derived(field: FieldDef, state: State) -> None:
    """Handle derived field types."""
    if field.derived_value:
        _record_dict(_render_template_dict(field.derived_value, state), state)
        return

    if field.detect:
        result = _run_detect(field, state)
        if isinstance(result, dict):
            for k, v in result.items():
                state.set(k, v)
        elif result is not None:
            _record_field_value(field, result, state)
        return

    if field.template:
        template = field.template
        derive_keys = field.derive_from
        if isinstance(derive_keys, str):
            derive_keys = [derive_keys]
        for key in derive_keys or []:
            val = state.get(key, "")
            template = template.replace(f"{{{{{key}}}}}", str(val))
        _record_field_value(field, template, state)
        return

    if field.value is not None:
        value = field.value
        if isinstance(value, dict):
            if field.records and all(r in value for r in field.records):
                rendered = _render_template_dict(value, state)
                for k, v in rendered.items():
                    state.set(k, v)
                return
            platform = state.get("platform")
            if platform and platform in value:
                value = value[platform]
            elif "default" in value:
                value = value["default"]

        _record_field_value(field, value, state)
        return


def _run_detect(field: FieldDef, state: State) -> Any:
    """Run detection command and normalize result."""
    detect = field.detect
    command = None

    if isinstance(detect, dict):
        if "command" in detect:
            command = detect["command"]
        else:
            platform = state.get("platform")
            if platform == "linux" and "linux" in detect:
                command = detect["linux"]
            elif platform == "mac" and "mac" in detect:
                command = detect["mac"]

    if not command:
        return None

    try:
        result = subprocess.run(
            _split_schema_command(command),
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout.strip()
    except Exception:
        output = ""

    normalize = detect.get("normalize") if isinstance(detect, dict) else None
    if normalize and isinstance(normalize, dict):
        if output in normalize:
            val = normalize[output]
            if isinstance(val, dict):
                return val
            return val
        if "default" in normalize:
            val = normalize["default"]
            if isinstance(val, dict):
                return val
            return val

    if field.normalize:
        return apply_normalize(output, field.normalize)

    return output


def _render_template_dict(value: dict[str, Any], state: State) -> dict[str, Any]:
    """Render {{key}} placeholders in a dict of template strings."""
    rendered: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, str):
            for key in _extract_template_keys(v):
                val = state.get(key, "")
                v = v.replace(f"{{{{{key}}}}}", str(val))
        rendered[k] = v
    return rendered


def _extract_template_keys(template: str) -> list[str]:
    return _TEMPLATE_KEY_RE.findall(template)


# ---------------------------------------------------------------------------
# Prompt wrappers (questionary-based)
# ---------------------------------------------------------------------------


def _prompt_text(field: FieldDef, state: State) -> str:
    """Prompt for text input with validation."""
    default = _get_prompt_default(field, state)
    prompt = field.prompt

    while True:
        default_str = str(default) if default is not None and default != "" else None
        answer = prompt_text(prompt, default=default_str)
        _raise_if_exit(answer)

        if answer == "" and default is not None:
            answer = str(default)

        if _validate(answer, field):
            return answer


def _prompt_confirm(field: FieldDef, state: State) -> Any:
    """Prompt for confirmation or yes/no mapped to arbitrary values.

    For boolean confirms (y/n → True/False), uses a select prompt with Exit.
    For non-boolean mappings (y/n → generate/manual), uses questionary.select.
    """
    prompt = field.prompt
    default = _get_prompt_default(field, state)

    if field.accepted_inputs:
        values = set(field.accepted_inputs.values())
        if values <= {True, False}:
            bool_default = default if isinstance(default, bool) else False
            return _prompt_bool(prompt, default=bool_default)
        else:
            input_choices = [str(k) for k in field.accepted_inputs.keys()]
            choices = input_choices + [_exit_choice()]
            values_map = {str(k): v for k, v in field.accepted_inputs.items()}
            default_choice = next(
                (key for key, value in values_map.items() if value == default),
                None,
            )
            while True:
                result = prompt_select(prompt, choices=choices, default=default_choice)
                _raise_if_exit(result)
                if result is None:
                    if default is not None:
                        return default
                    continue
                matched = values_map.get(result.lower())
                if matched is not None:
                    return matched
                console.print(f"[red]Invalid choice. Valid options: {', '.join(input_choices)}[/red]")
    else:
        bool_default = default if isinstance(default, bool) else False
        return _prompt_bool(prompt, default=bool_default)


def _prompt_single_select(field: FieldDef, state: State) -> str:
    """Prompt for single select using questionary.select."""
    prompt = field.prompt
    values = field.canonical_values
    default = _get_prompt_default(field, state)
    disabled_values = field.disabled_values
    enabled_values = [value for value in values if value not in disabled_values]

    choices = []
    for canonical in values:
        title = field.display_labels.get(canonical, canonical)
        disabled_reason = disabled_values.get(canonical)
        if disabled_reason:
            choices.append(
                Choice(
                    title=title,
                    value=canonical,
                    disabled=disabled_reason,
                )
            )
        else:
            choices.append(Choice(title=title, value=canonical))

    default_choice = (
        default if isinstance(default, str) and default in values and default not in disabled_values else None
    )
    choices.append(_exit_choice())
    result = prompt_select(prompt, choices=choices, default=default_choice)
    _raise_if_exit(result)
    if result is not None and result not in disabled_values:
        return result
    if result in disabled_values:
        console.print(f"[red]{result} is not available yet: {disabled_values[result]}[/red]")

    while True:
        fallback = prompt_text(prompt)
        _raise_if_exit(fallback)
        if not fallback:
            continue
        fallback_lower = fallback.strip().lower()
        for canonical, aliases in field.aliases.items():
            if fallback_lower in [a.lower() for a in aliases] and canonical not in disabled_values:
                return canonical
        if fallback_lower in [v.lower() for v in values] and fallback_lower not in disabled_values:
            return fallback_lower
        console.print(f"[red]Invalid choice. Valid options: {', '.join(enabled_values)}[/red]")


def _prompt_multi_select(field: FieldDef, state: State) -> list[str]:
    """Prompt for multi-select using questionary.checkbox."""
    prompt = field.prompt
    selection_mode = getattr(field, "selection_mode", "") or ""
    default = _get_prompt_default(field, state)
    if default is None:
        default = field.default if field.default is not None else []
    if not isinstance(default, list):
        default = []

    canonical = field.canonical_values

    choices = []
    for item in canonical:
        is_checked = item in default
        choices.append(Choice(title=item, value=item, checked=is_checked))
    choices.append(_exit_choice())

    selected = prompt_checkbox(prompt, choices=choices)
    _raise_if_exit(selected)

    if not selected:
        if selection_mode == "deselect_from_default":
            return list(default)
        return []

    if selection_mode == "deselect_from_default":
        return [d for d in default if d not in selected]
    else:
        return list(dict.fromkeys(selected))


# ---------------------------------------------------------------------------
# Special field types
# ---------------------------------------------------------------------------


def _handle_secret_group(field: FieldDef, state: State) -> None:
    """Handle secret group entries — uses password (hidden) input."""
    for entry in field.entries:
        key = entry.get("key", "")
        when = entry.get("when", "always")

        if when != "always" and not eval_when(when, state):
            continue

        while True:
            value = prompt_password(f"Enter value for {key}:")
            _raise_if_exit(value)
            if value.strip() != "":
                state.set(f"secrets.values.{key}", value)
                break
            console.print("[red]Value cannot be empty.[/red]")


def _handle_summary(field: FieldDef, state: State) -> None:
    """Display a formatted summary of selected state fields."""
    console.print("\n[bold]Deployment Summary[/bold]")
    for key in field.summary_fields:
        value = state.get(key)
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            value_str = ", ".join(str(v) for v in value) if value else "None"
        else:
            value_str = str(value) if value is not None else "None"
        console.print(f"  {label}: {value_str}")
    console.print()


def _handle_repeat(field: FieldDef, state: State, schema: QuestionSchema | None) -> None:
    """Handle repeat_for fields (e.g., subdomains)."""
    repeat_for = field.repeat_for

    if repeat_for == "selected_service_slugs":
        foundation = state.get("foundation_services", []) or []
        selected = state.get("selected_services", []) or []
        slugs = foundation + selected
    else:
        console.print(f"[yellow]Unknown repeat_for: {repeat_for}[/yellow]")
        return

    defaults: dict[str, str] = {}
    if schema and schema.service_catalog:
        for section in ("foundation_bundle", "optional_services"):
            for item in schema.service_catalog.get(section, []):
                slug = item.get("slug", "")
                defaults[slug] = item.get("default_subdomain", slug)

    for slug in slugs:
        default = defaults.get(slug, slug)
        state.set(f"subdomains.{slug}", default)
        state.set(subdomain_placeholder_key(slug), default)


def _build_subdomain_defaults(items: list[dict[str, Any]], state: State) -> None:
    """Populate default subdomains for currently selected services only."""
    selected_ids = set(state.get("foundation_services", []) or []) | set(state.get("selected_services", []) or [])

    for item in items:
        slug = str(item.get("slug") or "")
        if not slug or slug not in selected_ids:
            continue
        default = normalize_subdomain(item.get("default_subdomain") or slug)
        state.set(f"subdomains.{slug}", default)
        state.set(subdomain_placeholder_key(slug), default)


# ---------------------------------------------------------------------------
# Rules enforcement
# ---------------------------------------------------------------------------


def _enforce_rules(schema: QuestionSchema, state: State) -> None:
    """Enforce phase-level rules after fields have run."""
    for rule in schema.rules:
        if not isinstance(rule, dict):
            continue

        if_selected = rule.get("if_selected")
        selected_services = state.get("selected_services", []) or []

        if if_selected and if_selected in selected_services:
            requires = rule.get("requires")
            if requires and isinstance(requires, dict):
                foundation = state.get("foundation_services", []) or []
                for req_key, req_values in requires.items():
                    if req_key == "foundation_services":
                        for req_val in req_values:
                            if req_val not in foundation:
                                console.print(f"[yellow]Re-selecting required service {req_val}.[/yellow]")
                                foundation = foundation + [req_val]
                        state.set("foundation_services", foundation)


# ---------------------------------------------------------------------------
# Defaults & validation
# ---------------------------------------------------------------------------


def _get_default(field: FieldDef, state: State) -> Any:
    """Compute the default value for a prompt field."""
    if field.default_from_state:
        return state.get(field.default_from_state)

    if field.default_from_host:
        host_default = field.default_from_host
        if isinstance(host_default, dict):
            platform = state.get("platform")
            if platform == "linux":
                sudo_user = os.environ.get("SUDO_USER")
                if "sudo_linux" in host_default and sudo_user:
                    cmd = host_default["sudo_linux"]
                    if cmd == "SUDO_USER":
                        return sudo_user
                if "linux" in host_default:
                    cmd = host_default["linux"]
                    if cmd == "SUDO_USER":
                        return os.environ.get("SUDO_USER", "")
                    try:
                        result = subprocess.run(_split_schema_command(cmd), shell=False, capture_output=True, text=True)
                        return result.stdout.strip()
                    except Exception:
                        return ""
            elif platform == "mac" and "mac" in host_default:
                cmd = host_default["mac"]
                try:
                    result = subprocess.run(_split_schema_command(cmd), shell=False, capture_output=True, text=True)
                    return result.stdout.strip()
                except Exception:
                    return ""
        else:
            try:
                result = subprocess.run(
                    _split_schema_command(str(host_default)),
                    shell=False,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip()
            except Exception:
                return ""

    return field.default


def _get_prompt_default(field: FieldDef, state: State) -> Any:
    """Return an existing answer when present, otherwise fall back to schema defaults."""
    if _is_final_confirmation(field) and field.default is not None:
        return field.default
    current = _get_recorded_value(field, state)
    if current is not None:
        return current
    return _get_default(field, state)


def _get_recorded_value(field: FieldDef, state: State) -> Any:
    """Return the first recorded state value for a field, if one exists."""
    for key in field.records:
        if state.has(key):
            return state.get(key)
    if state.has(field.id):
        return state.get(field.id)
    return None


def _prompt_bool(message: str, default: bool = False) -> bool:
    choices: list[Choice] = [
        Choice(title="Yes", value=True),
        Choice(title="No", value=False),
        _exit_choice(),
    ]
    default_choice: Choice = choices[0] if default else choices[1]
    result = prompt_select(message, choices=choices, default=default_choice)
    _raise_if_exit(result)
    if result is None:
        return default
    return bool(result)


def _exit_choice() -> Choice:
    return Choice(title="Exit", value=_EXIT_VALUE)


def _raise_if_exit(value: Any) -> None:
    if isinstance(value, str) and value.strip().lower() == "exit":
        raise InterviewExit
    if value == _EXIT_VALUE:
        raise InterviewExit
    if isinstance(value, list) and any(item == _EXIT_VALUE for item in value):
        raise InterviewExit


def _validate(answer: str, field: FieldDef) -> bool:
    """Validate an answer according to the field's validate spec."""
    validate_spec = field.validate
    if not validate_spec:
        return True

    if isinstance(validate_spec, dict):
        if validate_spec.get("non_empty"):
            if not answer or not answer.strip():
                msg = validate_spec.get("message", "Value cannot be empty.")
                console.print(f"[red]{msg}[/red]")
                return False

        pattern = validate_spec.get("pattern")
        if pattern:
            if not re.match(pattern, answer):
                msg = validate_spec.get("message", "Invalid format.")
                console.print(f"[red]{msg}[/red]")
                return False

        return True

    if isinstance(validate_spec, str):
        if not re.match(validate_spec, answer):
            console.print("[red]Invalid format.[/red]")
            return False
        return True

    return True


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


def _record_field_value(field: FieldDef, value: Any, state: State) -> None:
    """Record a field's result into state.

    If ``records`` is empty, falls back to recording under the field ``id``
    so that downstream ``when`` clauses can reference it.
    """
    if field.records:
        for record_key in field.records:
            state.set(record_key, _normalize_record_value(record_key, value))
    else:
        state.set(field.id, _normalize_record_value(field.id, value))


def _record_dict(value_dict: dict[str, Any], state: State) -> None:
    """Record a flat dict of key-value pairs into state."""
    for key, value in value_dict.items():
        state.set(key, _normalize_record_value(key, value))


def _normalize_record_value(record_key: str, value: Any) -> Any:
    if record_key in {"data_root", "backup_dir"} and isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    return value
