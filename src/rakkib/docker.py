"""Docker helpers — compose up/pull, health polling, log capture.

Design rule from pyplan.md: Docker output redirects to
${DATA_ROOT}/logs/<step>.log so no LLM watches the stream.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from rakkib.tui import progress_spinner


class DockerError(Exception):
    """Raised when a docker command fails."""

    def __init__(self, message: str, cmd: list[str], returncode: int, stderr: str = "") -> None:
        super().__init__(message)
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


DOCKER_PERMISSION_HINT_LINUX = (
    "Docker needs permission for this user. Run `rakkib auth`, then open a new shell and try again."
)
DOCKER_PERMISSION_HINT_MAC = "Docker is not ready. Run `rakkib auth`, then try again."


def _docker_timeout() -> int:
    raw = os.environ.get("RAKKIB_DOCKER_TIMEOUT", "3600")
    try:
        return int(raw)
    except ValueError:
        return 3600


_COMPOSE_FILE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def _compose_base_cmd(project_dir: Path | str) -> list[str]:
    """Build the `docker compose` invocation prefix with explicit -f / --env-file.

    Docker Compose v2's automatic .env discovery via --project-directory has
    been historically inconsistent across versions. Passing -f and --env-file
    explicitly guarantees the project's .env is loaded so ${VAR} substitutions
    in the compose file always resolve.
    """
    project_path = Path(project_dir)
    cmd = ["docker", "compose", "--project-directory", str(project_path)]
    for name in _COMPOSE_FILE_NAMES:
        compose_file = project_path / name
        if compose_file.is_file():
            cmd.extend(["-f", str(compose_file)])
            break
    env_file = project_path / ".env"
    if env_file.is_file():
        cmd.extend(["--env-file", str(env_file)])
    return cmd


def compose_up(
    project_dir: Path | str,
    profiles: list[str] | None = None,
    services: list[str] | None = None,
    log_path: Path | str | None = None,
    detach: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run docker compose up for the given project directory."""
    cmd = _compose_base_cmd(project_dir)
    if profiles:
        for profile in profiles:
            cmd.extend(["--profile", profile])
    cmd.append("up")
    if detach:
        cmd.append("-d")
    if services:
        cmd.extend(services)

    return _run(cmd, log_path=log_path, timeout=timeout, progress_message="Starting Docker services...")


def docker_run(
    args: list[str],
    *,
    log_path: Path | str | None = None,
    check: bool = True,
    timeout: int | None = None,
    progress_message: str | None = None,
    cwd: Path | str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a docker command with consistent diagnostics."""
    return _run(
        ["docker", *args],
        log_path=log_path,
        check=check,
        timeout=timeout,
        progress_message=progress_message,
        cwd=cwd,
        input=input,
    )


def compose_pull(
    project_dir: Path | str,
    services: list[str] | None = None,
    log_path: Path | str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run docker compose pull for the given project directory."""
    cmd = _compose_base_cmd(project_dir)
    cmd.append("pull")
    if services:
        cmd.extend(services)
    return _run(cmd, log_path=log_path, timeout=timeout, progress_message="Pulling Docker images...")


def compose_down(
    project_dir: Path | str,
    volumes: bool = False,
    log_path: Path | str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run docker compose down for the given project directory."""
    cmd = _compose_base_cmd(project_dir)
    cmd.append("down")
    if volumes:
        cmd.append("--volumes")
    return _run(cmd, log_path=log_path, timeout=timeout, progress_message="Removing Docker services...")


def health_check(
    container_name: str,
    timeout: int = 60,
) -> bool:
    """Poll docker container health status until healthy or timeout.

    If the container has no healthcheck configured, fall back to checking
    whether the container is running.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = docker_run(
                ["inspect", "-f", "{{.State.Health.Status}}", container_name],
                check=False,
            )
            status = result.stdout.strip()
            # If status is empty, no healthcheck is configured; fall back.
            if not status or status == "<no value>":
                if container_running(container_name):
                    return True
            elif status == "healthy":
                return True
            elif status == "unhealthy":
                # Some images briefly report unhealthy during first-boot work,
                # then recover once migrations or model downloads finish.
                pass
        except DockerError:
            # Container may not exist yet
            pass
        time.sleep(2)
    return False


def container_running(container_name: str) -> bool:
    """Return True if the named container is running."""
    try:
        result = docker_run(
            ["inspect", "-f", "{{.State.Running}}", container_name],
            check=False,
        )
        return result.stdout.strip().lower() == "true"
    except DockerError:
        return False


def container_publishes_port(container_name: str, port: int) -> bool:
    """Return True if the container publishes the given host port."""
    try:
        result = docker_run(
            ["inspect", "-f", "{{json .NetworkSettings.Ports}}", container_name],
            check=False,
        )
        ports: dict[str, Any] = json.loads(result.stdout or "{}")
        for key, bindings in ports.items():
            if not key:
                continue
            container_port = key.split("/")[0]
            if int(container_port) == port:
                if bindings:
                    return True
        return False
    except (DockerError, json.JSONDecodeError, ValueError):
        return False


def network_exists(network_name: str) -> bool:
    """Return True if the docker network exists."""
    try:
        docker_run(["network", "inspect", network_name])
        return True
    except DockerError:
        return False


def capture_container_logs(container_name: str, log_path: Path | str, tail: int = 200) -> None:
    """Append the last *tail* lines of container logs to *log_path*."""
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = docker_run(["logs", "--tail", str(tail), container_name], check=False)
        with log_file.open("a") as fh:
            fh.write(f"\n--- logs: {container_name} (last {tail} lines) ---\n")
            fh.write(result.stdout)
            if result.stderr:
                fh.write(result.stderr)
    except Exception:
        pass


def create_network(network_name: str, driver: str = "bridge") -> None:
    """Create a docker network if it does not already exist."""
    if network_exists(network_name):
        return
    docker_run(["network", "create", "--driver", driver, network_name])


def is_docker_permission_error(text: str) -> bool:
    lower = text.lower()
    return "permission denied" in lower and (
        "docker.sock" in lower or "docker daemon socket" in lower or "/var/run/docker" in lower
    )


def _is_docker_permission_error(text: str) -> bool:
    return is_docker_permission_error(text)


def docker_permission_hint() -> str:
    if platform.system() == "Darwin":
        return DOCKER_PERMISSION_HINT_MAC
    return DOCKER_PERMISSION_HINT_LINUX


def _error_message(cmd: list[str], returncode: int, stderr: str, log_hint: str) -> str:
    message = f"Command failed with exit code {returncode}: {' '.join(cmd)}.{log_hint}"
    detail = stderr.strip()
    if detail:
        if len(detail) > 2048:
            detail = detail[-2048:]
            detail = f"...{detail}"
        message = f"{message}\nstderr:\n{detail}"
    if cmd and cmd[0] == "docker" and _is_docker_permission_error(stderr):
        message = f"{message} {docker_permission_hint()}"
    return message


def _run(
    cmd: list[str],
    log_path: Path | str | None = None,
    check: bool = True,
    timeout: int | None = None,
    progress_message: str | None = None,
    cwd: Path | str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally redirecting stdout/stderr to a log file.

    When check is True (default), raises DockerError on non-zero exit codes.
    """
    effective_timeout = timeout if timeout is not None else _docker_timeout()
    log_file = Path(log_path) if log_path else None
    try:
        with progress_spinner(progress_message) if progress_message else nullcontext():
            if log_file:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a") as fh:
                    result = subprocess.run(
                        cmd,
                        stdout=fh,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=effective_timeout,
                        cwd=str(cwd) if cwd is not None else None,
                        input=input,
                    )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    cwd=str(cwd) if cwd is not None else None,
                    input=input,
                )
    except subprocess.TimeoutExpired as exc:
        log_hint = f" See log: {log_file}" if log_file else ""
        if log_file:
            with log_file.open("a") as fh:
                fh.write(f"\nCommand timed out after {effective_timeout}s: {' '.join(cmd)}\n")
        raise DockerError(
            message=f"Command timed out after {effective_timeout}s: {' '.join(cmd)}.{log_hint}",
            cmd=cmd,
            returncode=124,
            stderr=str(exc),
        ) from exc

    if check and result.returncode != 0:
        log_hint = f" See log: {log_file}" if log_file else ""
        stderr = result.stderr or ""
        if not stderr and log_file and log_file.exists():
            try:
                stderr = log_file.read_text()
            except OSError:
                stderr = ""
        raise DockerError(
            message=_error_message(cmd, result.returncode, stderr, log_hint),
            cmd=cmd,
            returncode=result.returncode,
            stderr=stderr,
        )
    return result
