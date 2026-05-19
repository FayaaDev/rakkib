"""Rakkib setup steps.

Each step module exports ``run()`` and ``verify()``:
- ``run()`` executes the step idempotently.
- ``verify()`` returns ok or a structured failure (step, log path, state slice).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rakkib.postgres_sql import validate_registry_postgres_identifiers
from rakkib.service_catalog import validate_registry_internal_access
from rakkib.util import RAKKIB_DATA_DIR


class RegistryError(ValueError):
    """Raised when the service registry cannot be resolved safely."""


STEP_MODULES: list[tuple[str, str]] = [
    ("layout", "rakkib.steps.layout"),
    ("caddy", "rakkib.steps.caddy"),
    ("cloudflare", "rakkib.steps.cloudflare"),
    ("postgres", "rakkib.steps.postgres"),
    ("services", "rakkib.steps.services"),
    ("cron", "rakkib.steps.cron"),
]


@dataclass
class VerificationResult:
    """Result of a step verification check."""

    ok: bool
    step: str
    log_path: Path | None = None
    state_slice: dict[str, Any] | None = None
    message: str = ""

    @classmethod
    def success(cls, step: str, message: str = "") -> "VerificationResult":
        return cls(ok=True, step=step, message=message)

    @classmethod
    def failure(
        cls,
        step: str,
        message: str,
        log_path: Path | None = None,
        state_slice: dict[str, Any] | None = None,
    ) -> "VerificationResult":
        return cls(
            ok=False,
            step=step,
            message=message,
            log_path=log_path,
            state_slice=state_slice,
        )


def data_dir() -> Path:
    """Return the package data directory."""
    return RAKKIB_DATA_DIR


@functools.lru_cache(maxsize=1)
def load_service_registry() -> dict[str, Any]:
    """Load and cache the service registry."""
    with (data_dir() / "registry.yaml").open() as fh:
        registry = yaml.safe_load(fh)
    validate_registry_postgres_identifiers(registry)
    validate_registry_internal_access(registry)
    return registry


def selected_service_defs(state: Any, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return registry definitions for selected services in dependency order."""
    registry = registry or load_service_registry()
    selected_ids: set[str] = set(state.get("foundation_services", []) or [])
    selected_ids.update(state.get("selected_services", []) or [])

    by_id = {svc["id"]: svc for svc in registry["services"]}
    in_degree: dict[str, int] = {sid: 0 for sid in selected_ids}
    adjacency: dict[str, list[str]] = {sid: [] for sid in selected_ids}

    for sid in selected_ids:
        for dep in by_id.get(sid, {}).get("depends_on", []):
            if dep in selected_ids:
                adjacency[dep].append(sid)
                in_degree[sid] += 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    ordered: list[str] = []
    while queue:
        queue.sort()
        sid = queue.pop(0)
        ordered.append(sid)
        for neighbor in sorted(adjacency[sid]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    remaining = sorted(sid for sid in selected_ids if sid not in ordered)
    if remaining:
        raise RegistryError("dependency cycle: " + " -> ".join(remaining))
    return [by_id[sid] for sid in ordered if sid in by_id]


def service_enabled_key(service_id: str) -> str:
    """Return the template boolean key for a selected service."""
    return f"{service_id.upper().replace('-', '_')}_ENABLED"
