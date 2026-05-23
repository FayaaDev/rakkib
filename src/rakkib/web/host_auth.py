"""Host authorization readiness checks for browser-triggered setup runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import platform
import shutil
import subprocess

from rakkib.docker import DockerError, docker_run, is_docker_permission_error


AUTH_COMMAND = "rakkib auth"


@dataclass(frozen=True)
class HostAuthStatus:
    ok: bool
    code: str
    message: str
    command: str | None = AUTH_COMMAND
    requires_restart: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _sudo_noninteractive_ok() -> bool:
    if os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        return False
    result = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
    return result.returncode == 0


def check_host_auth_readiness() -> HostAuthStatus:
    """Return whether a web-triggered setup run can use host privileges safely."""
    if os.geteuid() == 0:
        return HostAuthStatus(True, "root", "Rakkib is running as root.", command=None)

    if platform.system() == "Darwin":
        if shutil.which("docker") is None:
            return HostAuthStatus(
                False,
                "docker_missing",
                "Docker needs setup. Run `rakkib auth`, then restart `rakkib web`.",
            )

        try:
            docker_run(["info"])
        except DockerError as exc:
            return HostAuthStatus(
                False,
                "docker_unavailable",
                f"Docker is not ready. Run `rakkib auth`, then restart `rakkib web`: {exc}",
                requires_restart=True,
            )

        compose = docker_run(["compose", "version"], check=False)
        if compose.returncode != 0:
            return HostAuthStatus(
                False,
                "compose_unavailable",
                "Docker needs setup. Run `rakkib auth`, then restart `rakkib web`.",
                requires_restart=True,
            )

        return HostAuthStatus(True, "ready", "Docker is ready for local service testing.", command=None)

    if shutil.which("sudo") is None:
        return HostAuthStatus(
            False,
            "sudo_missing",
            "sudo is required. Install sudo, then run Rakkib again.",
            command=None,
        )

    sudo_ready = _sudo_noninteractive_ok()
    if not sudo_ready:
        return HostAuthStatus(
            False,
            "sudo_required",
            "Authorization is needed. Run `rakkib auth`, then recheck.",
        )

    if shutil.which("docker") is None:
        return HostAuthStatus(
            True,
            "sudo_ready_docker_missing",
            "Authorization is ready.",
            command=None,
        )

    try:
        docker_run(["info"])
    except DockerError as exc:
        detail = exc.stderr or str(exc)
        if is_docker_permission_error(detail):
            return HostAuthStatus(
                False,
                "docker_permission",
                "Docker needs permission. Run `rakkib auth`, then restart `rakkib web` from a new terminal.",
                requires_restart=True,
            )
        return HostAuthStatus(
            False,
            "docker_unavailable",
            f"Docker is not ready: {exc}",
            command=None,
        )

    compose = docker_run(["compose", "version"], check=False)
    compose_output = f"{compose.stdout or ''}\n{compose.stderr or ''}"
    if compose.returncode != 0 and is_docker_permission_error(compose_output):
        return HostAuthStatus(
            False,
            "docker_permission",
            "Docker needs permission. Run `rakkib auth`, then restart `rakkib web` from a new terminal.",
            requires_restart=True,
        )

    return HostAuthStatus(True, "ready", "Authorization is ready.", command=None)
