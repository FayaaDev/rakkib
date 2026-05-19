"""Host platform detection shared by interactive and web setup flows."""

from __future__ import annotations

import platform
from typing import Any


def detect_host_platform() -> str:
    """Return Rakkib's canonical platform name for the current host."""
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "mac"
    raise RuntimeError(f"Unsupported platform: {system or 'unknown'}. Rakkib setup supports Linux and macOS.")


def ensure_state_platform(state: Any) -> None:
    """Record the detected platform unless state already has one."""
    if state.has("platform"):
        return
    state.set("platform", detect_host_platform())
