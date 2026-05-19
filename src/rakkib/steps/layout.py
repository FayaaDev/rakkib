"""Step 1 — Layout.

Create the target directory structure under ``DATA_ROOT``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rakkib.service_catalog import caddy_enabled, cloudflare_enabled
from rakkib.state import State
from rakkib.steps import VerificationResult


def _service_ids(state: State) -> list[str]:
    """Return the union of required, foundation, and selected service IDs."""
    required = ["postgres"]
    if caddy_enabled(state):
        required.append("caddy")
    if cloudflare_enabled(state):
        required.append("cloudflared")
    foundation = state.get("foundation_services", []) or []
    selected = state.get("selected_services", []) or []
    return required + foundation + selected


def _sudo_chown(path: Path, user: str, *, recursive: bool = False) -> None:
    """Assign *path* to *user* using non-interactive sudo."""
    cmd = ["sudo", "-n", "chown"]
    if recursive:
        cmd.append("-R")
    cmd.extend([user, str(path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f" {detail}" if detail else ""
        raise RuntimeError(
            f"sudo authorization required to repair ownership for {path}.{suffix} Please run `rakkib auth` first."
        )


def run(state: State) -> None:
    data_root = state.data_root
    admin_user = state.get("admin_user")
    platform = state.get("platform", "linux")
    services = _service_ids(state)

    dirs: list[Path] = [
        data_root,
        data_root / "docker",
        data_root / "data",
        data_root / "apps" / "static",
        data_root / "backups",
        data_root / "MDs",
        data_root / "logs",
    ]
    if cloudflare_enabled(state):
        # Pre-create cloudflared's data dir so the cloudflare step starts
        # from a clean admin-owned tree even if a prior broken run left
        # files owned by the container's default `nonroot` user (UID 65532).
        dirs.append(data_root / "data" / "cloudflared")
    for svc in services:
        dirs.append(data_root / "docker" / svc)

    if platform == "linux" and os.geteuid() != 0:
        # Attempt password-less sudo for directory creation.
        result = subprocess.run(
            ["sudo", "-n", "mkdir", "-p"] + [str(d) for d in dirs],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "sudo authorization required to create layout directories. Please run `rakkib auth` first."
            )

        # Set ownership to admin_user where applicable.
        if admin_user:
            # Rakkib writes rendered configs under these trees as the admin
            # user. Repair them recursively in case an earlier root run left
            # files such as postgres/init-scripts/init-services.sql root-owned.
            admin_trees = [
                data_root / "docker",
                data_root / "apps" / "static",
                data_root / "backups",
                data_root / "MDs",
                data_root / "logs",
            ]
            # Service data remains container-managed; only ensure the roots are
            # admin-owned so new service directories can be created safely.
            top_only = [
                data_root,
                data_root / "apps",
                data_root / "data",
            ]
            if cloudflare_enabled(state):
                top_only.append(data_root / "data" / "cloudflared")
            for d in top_only:
                _sudo_chown(d, str(admin_user))
            for d in admin_trees:
                _sudo_chown(d, str(admin_user), recursive=True)
    else:
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    # Write a simple log entry for idempotency tracking.
    log_path = data_root / "logs" / "layout.log"
    log_path.write_text("layout step completed\n")


def verify(state: State) -> VerificationResult:
    data_root = state.data_root
    dirs = [
        data_root / "docker",
        data_root / "data",
        data_root / "apps" / "static",
        data_root / "backups",
        data_root / "MDs",
        data_root / "logs",
    ]
    for d in dirs:
        if not d.exists():
            return VerificationResult.failure("layout", f"Directory {d} does not exist")
        if not os.access(d, os.W_OK):
            return VerificationResult.failure("layout", f"Directory {d} is not writable")
    return VerificationResult.success("layout", "Layout directories created")
