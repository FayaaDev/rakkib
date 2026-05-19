"""Template rendering — placeholder substitution from state -> template files.

- {{PLACEHOLDER}} syntax for direct string substitution
- Nested state values must be flattened before substitution
- Missing placeholders are left as-is (uses jinja2.DebugUndefined)
- Supports both {{PLACEHOLDER}} and Jinja2 {{ PLACEHOLDER }} style
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import DebugUndefined, Environment, FileSystemLoader

from rakkib.state import State
from rakkib.steps import service_enabled_key

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")
UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_env = Environment(undefined=DebugUndefined)


class UnresolvedTemplateError(RuntimeError):
    """Raised when a rendered file still contains a template placeholder."""


def _file_env(template_root: Path) -> Environment:
    """Return a Jinja environment rooted at *template_root* for file imports."""
    return Environment(loader=FileSystemLoader(str(template_root)), undefined=DebugUndefined)


def _render_template_path(src_path: Path, context: dict[str, Any], template_root: Path) -> str:
    """Render *src_path* with imports resolved relative to *template_root*."""
    env = _file_env(template_root)
    template_name = str(src_path.relative_to(template_root))
    return env.get_template(template_name).render(**context)


def flatten_state(state: State) -> dict[str, Any]:
    """Flatten nested state keys into placeholder names."""
    flat: dict[str, Any] = {}
    data = state.to_dict()
    _flatten("", data, flat)
    selected_ids = set(state.get("foundation_services", []) or [])
    selected_ids.update(state.get("selected_services", []) or [])
    for service_id in selected_ids:
        flat[service_enabled_key(service_id)] = True
    if state.has("data_root"):
        flat["DATA_ROOT"] = str(state.data_root)
    return flat


def _flatten(prefix: str, node: Any, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            new_prefix = f"{prefix}.{key}" if prefix else key
            _flatten(new_prefix, value, out)
    elif isinstance(node, list):
        # Store as newline-joined string for multiline placeholders
        key = prefix.upper()
        out[key] = "\n".join(str(x) for x in node)
        out[key.replace(".", "_")] = out[key]
    else:
        key = prefix.upper()
        out[key] = str(node) if node is not None else ""
        out[key.replace(".", "_")] = out[key]


def render_string(template_text: str, context: dict[str, str]) -> str:
    """Substitute placeholders in a template string.

    Uses Jinja2 with :class:`jinja2.DebugUndefined` so missing placeholders
    are left as-is (e.g. ``{{ MISSING }}`` remains in the output) rather
    than raising an error or being silently removed.
    """
    return _env.from_string(template_text).render(**context)


def render_text(src_text: str, state: State) -> str:
    """Render a template string using flattened state as context."""
    context = flatten_state(state)
    return render_string(src_text, context)


def render_file(src: Path | str, dst: Path | str, state: State) -> None:
    """Render a template file to a destination path."""
    src_path = Path(src)
    dst_path = Path(dst)
    context = flatten_state(state)
    rendered = _render_template_path(src_path, context, src_path.parent)
    _ensure_no_unresolved_placeholders(rendered, src_path)
    dst_path.write_text(rendered)


def render_tree(src_dir: Path | str, dst_dir: Path | str, state: State) -> None:
    """Recursively render all ``.tmpl`` files in *src_dir* into *dst_dir*.

    Each ``.tmpl`` extension is stripped on output.  Directory structure
    is preserved.  Non-``.tmpl`` files are skipped.
    """
    src_path = Path(src_dir)
    dst_path = Path(dst_dir)
    context = flatten_state(state)

    for src_file in src_path.rglob("*.tmpl"):
        rel = src_file.relative_to(src_path)
        dst_file = dst_path / rel.with_suffix("")
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        rendered = _render_template_path(src_file, context, src_path)
        _ensure_no_unresolved_placeholders(rendered, src_file)
        dst_file.write_text(rendered)


def _ensure_no_unresolved_placeholders(rendered: str, src_path: Path) -> None:
    """Reject files that would ship literal Jinja placeholders."""
    matches = [match.strip() for match in UNRESOLVED_PLACEHOLDER_RE.findall(rendered)]
    if not matches:
        return

    keys = ", ".join(sorted(set(matches)))
    raise UnresolvedTemplateError(f"Rendered template {src_path} still contains unresolved placeholder(s): {keys}")
