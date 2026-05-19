"""State management — .fss-state.yaml load, save, merge, and resume detection."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml

from rakkib.schema import load_all_schemas

DEFAULT_STATE_FILE = ".fss-state.yaml"
_UNSET = object()

# Bump when the on-disk YAML schema gains a structural change that older
# rakkib versions cannot read transparently. Each bump must add an entry
# to ``_MIGRATIONS`` (index i upgrades from schema_version i to i+1).
STATE_VERSION = 1
_MIGRATIONS: list[Callable[[dict[str, Any]], None]] = []


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply any pending schema migrations to ``raw`` in place and return it.

    An empty dict is returned as-is so that loading a freshly-written empty
    state file does not synthesise a schema_version stamp.
    """
    if not raw:
        return raw
    current = int(raw.get("schema_version", 0) or 0)
    if current > STATE_VERSION:
        raise RuntimeError(
            f"State file schema_version={current} is newer than this rakkib supports "
            f"(STATE_VERSION={STATE_VERSION}). Upgrade rakkib or remove .fss-state.yaml "
            "to start fresh."
        )
    while current < STATE_VERSION:
        if current >= len(_MIGRATIONS):
            # No structural migration registered for this step yet — the version
            # bump itself is enough (e.g. v0 → v1 was a metadata-only addition).
            current += 1
            continue
        _MIGRATIONS[current](raw)
        current += 1
    raw["schema_version"] = STATE_VERSION
    return raw


def default_data_root(platform: str | None = None) -> Path:
    """Return the platform-specific default data root."""
    if str(platform or "linux").lower() == "mac":
        return Path.home() / "srv"
    return Path("/srv")


def default_state_path(repo_dir: Path | str) -> Path:
    """Return the canonical deployment state path for a Rakkib checkout."""
    repo_dir = Path(repo_dir)
    for candidate in (repo_dir, repo_dir.parent.parent):
        if (candidate / ".git").exists():
            return candidate / DEFAULT_STATE_FILE
    return repo_dir / DEFAULT_STATE_FILE


class StateBucket(str, Enum):
    """Registry state buckets used for service selection."""

    ALWAYS = "always"
    FOUNDATION_SERVICES = "foundation_services"
    SELECTED_SERVICES = "selected_services"


class State:
    """In-memory representation of .fss-state.yaml."""

    def __init__(self, data: dict[str, Any], path: Path | str | None = None) -> None:
        self._data = data
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path | None:
        """Return the path this state will save to by default, if bound."""
        return self._path

    @property
    def data_root(self) -> Path:
        """Return the configured Rakkib data root."""
        raw = self.get("data_root")
        if raw is None:
            return default_data_root(self.get("platform"))
        return Path(os.path.expandvars(os.path.expanduser(str(raw))))

    @classmethod
    def load(cls, path: Path | str = DEFAULT_STATE_FILE) -> "State":
        """Load state from YAML file, returning an empty State if missing."""
        path = Path(path)
        if not path.exists():
            return cls({}, path=path)
        raw = yaml.safe_load(path.read_text()) or {}
        raw = _migrate(raw)
        return cls(raw, path=path)

    def save(self, path: Path | str | object = _UNSET) -> None:
        """Persist state to YAML file, atomically and durably."""
        if path is _UNSET:
            if self._path is None:
                raise RuntimeError(
                    "State has no save path. Load it with State.load(path) or pass an explicit path to save(path)."
                )
            save_path = self._path
        else:
            save_path = Path(path)
            self._path = save_path

        self._data["schema_version"] = STATE_VERSION
        data = yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True)

        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        # fsync the parent directory so the rename is durable even across a
        # crash between rename() and the OS flushing the directory entry.
        parent = save_path.parent
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.replace(tmp_path, save_path)
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def get(self, key: str, default: Any = None) -> Any:
        """Dot-notated read, e.g. get('cloudflare.tunnel_uuid')."""
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def has(self, key: str) -> bool:
        """Return True if the dot-notated key exists in state (even if value is None)."""
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    def set(self, key: str, value: Any) -> None:
        """Dot-notated write, creating intermediate dicts as needed."""
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def delete(self, key: str) -> None:
        """Delete a dot-notated key if it exists."""
        parts = key.split(".")
        node = self._data
        parents: list[tuple[dict[str, Any], str]] = []

        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return
            parents.append((node, part))
            node = node[part]

        if not isinstance(node, dict) or parts[-1] not in node:
            return

        del node[parts[-1]]

        for parent, child_key in reversed(parents):
            child = parent.get(child_key)
            if isinstance(child, dict) and not child:
                del parent[child_key]
            else:
                break

    def clear(self) -> None:
        """Remove all recorded state values."""
        self._data.clear()

    def merge(self, other: dict[str, Any]) -> None:
        """Deep-merge another dict into this state."""
        _deep_merge(self._data, other)

    def is_confirmed(self) -> bool:
        """Return True if the user has confirmed past Phase 6."""
        return bool(self.get("confirmed", False))

    def resume_phase(self) -> int:
        """Return the first phase (1-6) with missing required fields, or 7 if complete."""
        schemas = load_all_schemas()
        for schema in schemas:
            if not self.is_phase_complete(schema.phase):
                return schema.phase
        return 7

    def is_phase_complete(self, phase: int) -> bool:
        """Return True if all required fields for the given phase have values in state."""
        schemas = load_all_schemas()
        schema = next((s for s in schemas if s.phase == phase), None)
        if schema is None:
            return False

        for field in schema.fields:
            if field.required is False:
                continue
            if field.type == "summary":
                continue
            if field.when and not _eval_when(field.when, self):
                continue
            for record_key in field.records:
                if not self.has(record_key):
                    return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the underlying data."""
        return dict(self._data)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Merge overlay into base in-place, recursing into dicts."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _eval_when(condition: str, state: State) -> bool:
    """Evaluate a simple when condition against current state."""
    condition = condition.strip()
    if not condition:
        return True

    # Handle 'and' conjunctions (no 'or' in current schemas)
    if " and " in condition:
        parts = condition.split(" and ")
        return all(_eval_when(p.strip(), state) for p in parts)

    # Handle 'is null' / 'is not null'
    if " is not null" in condition:
        key = condition.replace(" is not null", "").strip()
        return state.get(key) is not None
    if " is null" in condition:
        key = condition.replace(" is null", "").strip()
        return state.get(key) is None

    # Handle 'in' (value in key, where key is a list)
    if " in " in condition:
        value, key = condition.split(" in ", 1)
        value = value.strip().strip("\"'")
        actual = state.get(key.strip())
        return isinstance(actual, list) and value in actual

    # Handle '=='
    if " == " in condition:
        key, value = condition.split(" == ", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        actual = state.get(key)
        return _coerce_compare(actual, value)

    # Handle '!='
    if " != " in condition:
        key, value = condition.split(" != ", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        actual = state.get(key)
        return not _coerce_compare(actual, value)

    return False


def _coerce_compare(actual: Any, expected: str) -> bool:
    """Compare a state value to an expected string, coercing booleans."""
    if isinstance(actual, bool):
        return str(actual).lower() == expected.lower()
    if actual is None:
        return expected.lower() == "none"
    return str(actual) == expected


def subdomain_placeholder_key(slug: str) -> str:
    """Return the state key used to store a service's subdomain placeholder."""
    return f"{slug.upper().replace('-', '_')}_SUBDOMAIN"
