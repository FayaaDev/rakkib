"""Host preflight checks.

Each check returns a CheckResult with name, status, blocking flag, and message.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from rakkib.docker import DockerError, docker_run, is_docker_permission_error
from rakkib.service_catalog import cloudflare_enabled
from rakkib.state import State
from rakkib.tui import progress_spinner, prompt_confirm
from rakkib.util import resolve_user


@dataclass
class CheckResult:
    """Result of a single diagnostic check."""

    name: str
    status: str  # "ok", "warn", "fail"
    blocking: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _command_exists(cmd: str) -> bool:
    if platform.system() == "Darwin":
        _ensure_macos_tool_path()
    return shutil.which(cmd) is not None


def _macos_brew_cmd() -> str | None:
    brew = shutil.which("brew")
    if brew:
        return brew
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(candidate).is_file():
            return candidate
    return None


def _ensure_macos_tool_path() -> None:
    if platform.system() != "Darwin":
        return
    entries = os.environ.get("PATH", "").split(os.pathsep)
    for path in ("/Applications/Docker.app/Contents/Resources/bin", "/opt/homebrew/bin", "/usr/local/bin"):
        if Path(path).is_dir() and path not in entries:
            entries.insert(0, path)
    os.environ["PATH"] = os.pathsep.join(entry for entry in entries if entry)


def _macos_tool_cmd(name: str) -> str | None:
    _ensure_macos_tool_path()
    tool = shutil.which(name)
    if tool:
        return tool
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.is_file():
            return str(candidate)
    return None


def attempt_start_docker_desktop(wait_seconds: int = 90) -> str:
    """Open Docker Desktop and wait for the Docker daemon on macOS."""
    if platform.system() != "Darwin":
        return "Docker Desktop is only used on macOS."
    _ensure_macos_tool_path()
    if not Path("/Applications/Docker.app").exists():
        return "Docker Desktop is not installed. Run `rakkib auth` to install it with Homebrew."

    result = subprocess.run(["open", "-a", "Docker"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return (
            f"Docker Desktop failed to open: {detail}\n"
            "Open Docker Desktop manually, finish any setup prompts, then run `docker info` and `rakkib pull`."
        )

    deadline = time.monotonic() + wait_seconds
    last_error = "Docker Desktop has not reported ready yet."
    while time.monotonic() < deadline:
        try:
            docker_run(["info"])
            return "Docker Desktop is running."
        except (DockerError, FileNotFoundError) as exc:
            last_error = str(exc)
            time.sleep(2)

    return (
        f"Docker Desktop opened, but Docker is not ready yet: {last_error}\n"
        "Finish Docker Desktop setup prompts, then run `docker info` and retry `rakkib pull`. "
        "If this Mac is itself a VM, enable nested virtualization or use a real Mac/Linux host."
    )


APT_LOCK_PATHS = (
    Path("/var/lib/dpkg/lock-frontend"),
    Path("/var/lib/dpkg/lock"),
    Path("/var/lib/apt/lists/lock"),
    Path("/var/cache/apt/archives/lock"),
)

APT_LOCK_WAIT_MESSAGE = "Ubuntu automatic updates are running; waiting for apt/dpkg to become available..."

PACKAGE_MANAGER_SAFE_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "a",
    "NEEDRESTART_SUSPEND": "1",
    "UCF_FORCE_CONFFOLD": "1",
}

MACOS_DOCKER_CASK = "docker"

CLOUDFLARED_VERSION = "2026.3.0"
CLOUDFLARED_SHA256 = {
    ("darwin", "amd64"): "0f30140c4a5e213d22f951ef4c964cac5fb6a5f061ba6eba5ea932999f7c0394",
    ("darwin", "arm64"): "2aae4f69b0fc1c671b8353b4f594cbd902cd1e360c8eed2b8cad4602cb1546fb",
    ("linux", "amd64"): "4a9e50e6d6d798e90fcd01933151a90bf7edd99a0a55c28ad18f2e16263a5c30",
    ("linux", "arm64"): "0755ba4cbab59980e6148367fcf53a8f3ec85a97deefd63c2420cf7850769bee",
}

COMPOSE_VERSION = "v5.1.3"
COMPOSE_SHA256 = {
    "x86_64": "a0298760c9772d2c06888fc8703a487c94c3c3b0134adeef830742a2fc7647b4",
    "aarch64": "e8105a3e687ea7e0b0f81abe4bf9269c8a2801fb72c2b498b5ff2472bc54145f",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_download_sha256(path: Path, expected: str) -> str | None:
    try:
        actual = _sha256_file(path)
    except OSError as exc:
        return f"unable to read {path} for checksum verification: {exc}"
    if actual == expected:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return f"checksum mismatch for {path.name}: expected {expected}, got {actual}"


def _locked_apt_files() -> list[str]:
    """Return apt/dpkg lock files currently held by another process."""
    lock_targets: dict[tuple[int, int, int], str] = {}
    for path in APT_LOCK_PATHS:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        identity = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
        lock_targets[identity] = str(path)

    if not lock_targets:
        return []

    try:
        lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    locked: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            major, minor, inode = parts[5].split(":", 2)
            identity = (int(major, 16), int(minor, 16), int(inode))
        except ValueError:
            continue
        if identity in lock_targets:
            locked.append(lock_targets[identity])
    return sorted(set(locked))


def wait_for_apt_locks(
    timeout: int = 900,
    interval: float = 5,
    on_wait: Callable[[list[str]], None] | None = None,
) -> str | None:
    """Wait until apt/dpkg locks clear. Return an error message on timeout."""
    deadline = time.monotonic() + timeout
    notified = False
    while True:
        locked = _locked_apt_files()
        if not locked:
            return None
        if not notified:
            if on_wait is not None:
                on_wait(locked)
            else:
                print(APT_LOCK_WAIT_MESSAGE, file=sys.stderr)
            notified = True
        if time.monotonic() >= deadline:
            files = ", ".join(locked)
            return (
                f"Timed out waiting for apt/dpkg locks: {files}. "
                "Ubuntu automatic updates or another package manager is still running. "
                "Wait for it to finish and rerun; if it is stuck run "
                "'sudo systemctl stop unattended-upgrades' and rerun."
            )
        time.sleep(interval)


def _notify_apt_wait(_locked: list[str]) -> None:
    print(APT_LOCK_WAIT_MESSAGE, file=sys.stderr)


def _package_manager_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(PACKAGE_MANAGER_SAFE_ENV)
    return env


def _sudo_install_ready() -> str | None:
    if os.geteuid() == 0:
        return None
    if shutil.which("sudo") is None:
        return "sudo is required to install Docker on Linux. Install sudo or run Rakkib from a root shell."
    result = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
    if result.returncode == 0:
        return None
    return "Run `rakkib auth` from an interactive terminal before `rakkib pull` so Docker can be installed with sudo."


def _normalize_arch(raw: str) -> str | None:
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return mapping.get(raw)


def _port_listeners(port: int) -> tuple[str | None, int]:
    """Return (output, rc). rc==2 means neither ss nor lsof available."""
    if _command_exists("ss"):
        result = subprocess.run(
            ["ss", "-H", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
        )
        return result.stdout, 0
    if _command_exists("lsof"):
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", f"{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        return result.stdout, 0
    return None, 2


def _docker_container_running(name: str) -> bool:
    if not _command_exists("docker"):
        return False
    result = docker_run(["ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"], check=False)
    return result.returncode == 0 and result.stdout.strip() == name


def _docker_container_publishes_port(name: str, port: int) -> bool:
    if not _command_exists("docker"):
        return False
    result = docker_run(["ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}} {{.Ports}}"], check=False)
    if result.returncode != 0:
        return False
    line = result.stdout.strip()
    # Heuristic: port appears in the ports column
    import re

    pattern = rf"(:|->){port}(/|->|$)|:{port}->"
    return bool(re.search(pattern, line))


def check_os() -> CheckResult:
    kernel = platform.system()
    if kernel == "Darwin":
        return CheckResult("os", "ok", True, "Mac detected")
    if kernel != "Linux":
        return CheckResult("os", "fail", True, f"unsupported OS: {kernel or 'unknown'}")

    distro = ""
    version = ""
    if _command_exists("lsb_release"):
        try:
            dresult = subprocess.run(["lsb_release", "-is"], capture_output=True, text=True, check=True)
            vresult = subprocess.run(["lsb_release", "-rs"], capture_output=True, text=True, check=True)
            distro = dresult.stdout.strip()
            version = vresult.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    else:
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                text = os_release.read_text()
                for line in text.splitlines():
                    if line.startswith("ID="):
                        distro = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("VERSION_ID="):
                        version = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass

    distro_lower = distro.lower()
    if distro_lower == "ubuntu":
        return CheckResult("os", "ok", True, f"Ubuntu {version or 'unknown'} detected")
    return CheckResult(
        "os",
        "fail",
        True,
        f"Linux distro must be Ubuntu for the documented helper path; found {distro or 'unknown'}",
    )


def check_arch() -> CheckResult:
    raw = platform.machine()
    normalized = _normalize_arch(raw)
    if normalized:
        return CheckResult("architecture", "ok", False, f"{normalized} ({raw})")
    return CheckResult(
        "architecture",
        "fail",
        False,
        f"unsupported architecture: {raw or 'unknown'}; expected amd64 or arm64",
    )


def check_ram() -> CheckResult:
    mb: int | None = None
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            text = meminfo.read_text()
            for line in text.splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    kb = int(parts[1])
                    mb = kb // 1024
                    break
    except (OSError, ValueError):
        pass

    if mb is None and _command_exists("sysctl"):
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
            )
            bytes_str = result.stdout.strip()
            if bytes_str.isdigit():
                mb = int(bytes_str) // 1024 // 1024
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass

    if mb is None:
        return CheckResult("ram", "warn", False, "could not determine RAM")
    if mb < 2048:
        return CheckResult("ram", "fail", False, f"{mb} MB available; minimum is 2048 MB")
    if mb < 4096:
        return CheckResult("ram", "warn", False, f"{mb} MB available; 4 GB or more is recommended")
    return CheckResult("ram", "ok", False, f"{mb} MB available")


def check_disk(data_root: str) -> CheckResult:
    probe = Path(data_root)
    while not probe.exists() and probe != Path("/"):
        probe = probe.parent

    try:
        result = subprocess.run(
            ["df", "-Pk", str(probe)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            free_kb = int(lines[1].split()[3])
            free_gb = free_kb // 1024 // 1024
            if free_gb < 20:
                return CheckResult(
                    "disk",
                    "warn",
                    False,
                    f"{free_gb} GB free at {probe}; 20 GB or more is recommended for {data_root}",
                )
            return CheckResult("disk", "ok", False, f"{free_gb} GB free at {probe}")
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, IndexError):
        pass

    return CheckResult("disk", "warn", False, f"could not determine free space for {data_root}")


def check_docker() -> CheckResult:
    if not _command_exists("docker"):
        if platform.system() == "Darwin":
            return CheckResult("docker", "fail", True, "Docker is missing; run `rakkib auth`.")
        return CheckResult("docker", "fail", True, "docker command is missing")
    try:
        docker_run(["info"])
        return CheckResult("docker", "ok", True, "Docker is ready")
    except DockerError as exc:
        return CheckResult("docker", "fail", True, str(exc))


def docker_access_user(state: State | None = None) -> str:
    return resolve_user(state) or "root"


def docker_access_commands(user: str) -> str:
    if platform.system() == "Darwin":
        return "brew install --cask docker\nopen -a Docker\ndocker info\nrakkib pull"
    return (
        "sudo groupadd -f docker\n"
        f"sudo usermod -aG docker {user}\n"
        "sudo systemctl enable --now docker\n"
        "newgrp docker\n"
        "docker info\n"
        "rakkib pull"
    )


def prepare_docker_access(user: str, *, validate_sudo: bool = True) -> str:
    if os.geteuid() == 0:
        return "Rakkib is running as root; Docker is ready."
    if sys.platform != "linux":
        if sys.platform == "darwin":
            return "Run `rakkib auth`, then try again."
        return "Run `rakkib auth` from a supported Linux or macOS host."
    if shutil.which("sudo") is None:
        return "sudo is required. Install sudo, then run `rakkib auth`."

    if validate_sudo and sys.stdin.isatty():
        print("Rakkib needs sudo once to prepare Docker.", file=sys.stderr)
        sudo_check = subprocess.run(["sudo", "-v"])
        if sudo_check.returncode != 0:
            return "Sudo validation failed. Run `rakkib auth` from an interactive terminal."
    elif validate_sudo:
        return "Run `rakkib auth` from an interactive terminal to prepare Docker access."

    commands = [
        ["sudo", "-n", "groupadd", "-f", "docker"],
        ["sudo", "-n", "usermod", "-aG", "docker", user],
        ["sudo", "-n", "systemctl", "enable", "--now", "docker"],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            return f"Could not run {' '.join(cmd)}: {detail}"
    return f"Docker is ready for {user}."


def _docker_group_rerun_command() -> list[str] | None:
    if sys.platform != "linux" or not sys.stdin.isatty():
        return None
    if shutil.which("sg") is None or not sys.argv:
        return None
    return ["sg", "docker", "-c", shlex.join(sys.argv)]


def _offer_docker_group_rerun(console) -> None:
    command = _docker_group_rerun_command()
    if command is None:
        return
    if not prompt_confirm("Continue with updated Docker access now?", default=True):
        return
    console.print("[dim]Continuing with updated Docker access...[/dim]")
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        console.print(f"[yellow]Could not continue automatically: {exc}[/yellow]")


def handle_docker_permission_denied(console, user: str) -> bool:
    if platform.system() == "Darwin":
        console.print("[bold red]Docker is installed, but it is not ready yet.[/bold red]")
        console.print("[yellow]Run `rakkib auth`, then try again.[/yellow]")
        return False

    console.print("[bold red]Docker needs permission for this user.[/bold red]")
    repair_message = prepare_docker_access(user)
    console.print(f"[dim]{repair_message}[/dim]")
    if repair_message.startswith("Docker is ready"):
        _offer_docker_group_rerun(console)
    console.print("[yellow]Open a new shell, then rerun Rakkib.[/yellow]")
    console.print("[dim]Setup command: `rakkib auth`[/dim]")
    return False


def check_docker_prereq(state: State | None = None, console=None) -> bool:
    """Verify docker and docker compose are available. Install if missing."""
    docker_user = docker_access_user(state)
    if platform.system() == "Darwin":
        _ensure_macos_tool_path()
    if shutil.which("docker") is None:
        if platform.system() != "Darwin":
            sudo_error = _sudo_install_ready()
            if sudo_error:
                if console:
                    console.print(f"[bold red]{sudo_error}[/bold red]")
                return False
        with progress_spinner("Installing Docker..."):
            msg = attempt_fix_docker()
        if console:
            console.print(f"[dim]{msg}[/dim]")
        if shutil.which("docker") is None:
            if console:
                console.print("[bold red]Docker setup failed.[/bold red]")
            return False
        if console:
            console.print("[green]Docker is ready.[/green]")

    try:
        docker_run(["info"])
    except DockerError as exc:
        if is_docker_permission_error(exc.stderr or str(exc)):
            return handle_docker_permission_denied(console, docker_user) if console else False
        if platform.system() == "Darwin":
            with progress_spinner("Starting Docker Desktop..."):
                msg = attempt_start_docker_desktop()
            if console:
                console.print(f"[dim]{msg}[/dim]")
            try:
                docker_run(["info"])
            except DockerError as retry_exc:
                if console:
                    console.print(f"[bold red]Docker is not ready:[/bold red] {retry_exc}")
                return False
        else:
            if console:
                console.print(f"[bold red]Docker is not ready:[/bold red] {exc}")
            return False

    compose_check = docker_run(["compose", "version"], check=False)
    compose_output = f"{compose_check.stdout or ''}\n{compose_check.stderr or ''}"
    if compose_check.returncode != 0 and is_docker_permission_error(compose_output):
        return handle_docker_permission_denied(console, docker_user) if console else False
    if compose_check.returncode != 0:
        if platform.system() == "Darwin":
            with progress_spinner("Starting Docker Desktop..."):
                msg = attempt_fix_compose()
            if console:
                console.print(f"[dim]{msg}[/dim]")
            compose_check = docker_run(["compose", "version"], check=False)
            if compose_check.returncode != 0:
                if console:
                    console.print("[bold red]Docker Compose setup failed.[/bold red]")
                return False
            if console:
                console.print("[green]Docker Compose is ready.[/green]")
            return True
        with progress_spinner("Installing Docker Compose..."):
            msg = attempt_fix_compose()
        if console:
            console.print(f"[dim]{msg}[/dim]")
        compose_check = docker_run(["compose", "version"], check=False)
        if compose_check.returncode != 0:
            if console:
                console.print("[bold red]Docker Compose setup failed.[/bold red]")
            return False
        if console:
            console.print("[green]Docker Compose is ready.[/green]")

    return True


def ensure_prereqs(state: State | None = None, console=None, cloudflared_bin: str = "cloudflared") -> bool:
    """Install host prerequisites (Docker, cloudflared) if missing."""
    if not check_docker_prereq(state, console=console):
        return False

    if state is not None and not cloudflare_enabled(state):
        return True

    local_cf = Path.home() / ".local" / "bin" / "cloudflared"
    cf_ok = local_cf.is_file()
    if not cf_ok:
        try:
            cf_ok = subprocess.run([cloudflared_bin, "--version"], capture_output=True, text=True).returncode == 0
        except FileNotFoundError:
            pass

    if not cf_ok:
        with progress_spinner("Installing cloudflared..."):
            msg = attempt_fix_cloudflared()
        if console:
            console.print(f"[dim]{msg}[/dim]")
        cf_ok = local_cf.is_file()
        if cf_ok:
            try:
                cf_ok = subprocess.run([str(local_cf), "--version"], capture_output=True, text=True).returncode == 0
            except FileNotFoundError:
                cf_ok = False
        if not cf_ok:
            if console:
                console.print(
                    "[bold red]cloudflared installation failed. "
                    "Check your network connection and supported platform, then retry.[/bold red]"
                )
            return False

    return True


def check_compose() -> CheckResult:
    if not _command_exists("docker"):
        if platform.system() == "Darwin":
            return CheckResult("compose", "fail", True, "Docker is missing; run `rakkib auth`.")
        return CheckResult("compose", "fail", True, "docker command is missing")
    result = docker_run(["compose", "version"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return CheckResult("compose", "ok", True, result.stdout.strip())
    return CheckResult("compose", "fail", True, "Docker Compose v2 is not available through 'docker compose'")


def check_cloudflared_binary() -> CheckResult:
    if _command_exists("cloudflared"):
        return CheckResult("cloudflared_cli", "ok", False, "cloudflared is on PATH")
    local_bin = Path.home() / ".local" / "bin" / "cloudflared"
    if local_bin.exists() and local_bin.is_file():
        return CheckResult("cloudflared_cli", "ok", False, f"cloudflared is available at {local_bin}")
    return CheckResult(
        "cloudflared_cli",
        "warn",
        False,
        "Cloudflare tunnel tool is missing; run `rakkib auth` or retry setup",
    )


def check_public_ports() -> CheckResult:
    failures = 0
    messages: list[str] = []

    for port in (80, 443):
        listeners, rc = _port_listeners(port)
        if rc == 2:
            return CheckResult(
                "public_ports",
                "warn",
                True,
                "neither ss nor lsof is available to inspect ports 80/443",
            )

        if not listeners:
            messages.append(f"{port}=free")
            continue

        if "caddy" in listeners.lower() or _docker_container_publishes_port("caddy", port):
            messages.append(f"{port}=owned by caddy")
        else:
            messages.append(f"{port}=conflict")
            failures += 1

    if failures == 0:
        return CheckResult("public_ports", "ok", True, " ".join(messages))
    return CheckResult(
        "public_ports",
        "fail",
        True,
        f"ports 80/443 must be free or owned by Rakkib caddy; {' '.join(messages)}",
    )


def check_ssh_port() -> CheckResult:
    listeners, rc = _port_listeners(22)
    if rc == 2:
        return CheckResult("ssh_port", "warn", False, "neither ss nor lsof is available to inspect port 22")
    if listeners:
        return CheckResult("ssh_port", "ok", False, "port 22 has a listener")
    return CheckResult(
        "ssh_port",
        "warn",
        False,
        "port 22 is not listening; SSH over Cloudflare will not work until SSH is enabled",
    )


def check_domain_dns(domain: str) -> CheckResult:
    if not domain or domain == "null":
        return CheckResult("dns", "warn", False, "domain is not recorded yet")
    if not _command_exists("dig"):
        return CheckResult("dns", "warn", False, f"dig is not installed; cannot resolve {domain}")

    ips: list[str] = []
    for qtype in ("A", "AAAA"):
        result = subprocess.run(
            ["dig", "+short", domain, qtype],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line:
                    ips.append(line)

    if ips:
        return CheckResult("dns", "ok", False, f"{domain} resolves: {' '.join(ips)}")
    return CheckResult("dns", "warn", False, f"{domain} does not currently resolve")


def check_cloudflare_readiness(state: State) -> list[CheckResult]:
    results: list[CheckResult] = []
    zone_in = state.get("cloudflare.zone_in_cloudflare")
    if zone_in is False:
        results.append(
            CheckResult(
                "cloudflare_zone",
                "warn",
                False,
                "domain is not active in Cloudflare yet; public routing will wait until it is ready",
            )
        )
    elif zone_in is True:
        results.append(CheckResult("cloudflare_zone", "ok", False, "domain is marked as active in Cloudflare"))
    else:
        results.append(CheckResult("cloudflare_zone", "warn", False, "Cloudflare zone state is not recorded yet"))

    auth_method = state.get("cloudflare.auth_method")
    if not auth_method or auth_method == "null":
        results.append(CheckResult("cloudflare_auth", "warn", False, "Cloudflare auth method is not recorded yet"))
        return results

    data_root = str(state.data_root)
    if auth_method == "browser_login":
        cert_path = Path(data_root) / "data" / "cloudflared" / "cert.pem"
        if cert_path.exists():
            results.append(
                CheckResult(
                    "cloudflare_auth",
                    "ok",
                    False,
                    f"browser-login auth cert is present at {cert_path}",
                )
            )
        else:
            results.append(
                CheckResult(
                    "cloudflare_auth",
                    "warn",
                    False,
                    "Cloudflare login will be needed during setup",
                )
            )
    elif auth_method == "api_token":
        results.append(
            CheckResult(
                "cloudflare_auth",
                "ok",
                False,
                "API token mode is selected; the token will be requested when needed",
            )
        )
    elif auth_method == "existing_tunnel":
        results.append(CheckResult("cloudflare_auth", "ok", False, "existing tunnel mode recorded"))
    else:
        results.append(
            CheckResult(
                "cloudflare_auth",
                "warn",
                False,
                f"unrecognized Cloudflare auth method recorded: {auth_method}",
            )
        )

    creds_path = state.get("cloudflare.tunnel_creds_host_path")
    tunnel_uuid = state.get("cloudflare.tunnel_uuid")
    if creds_path and creds_path != "null":
        if Path(creds_path).exists():
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "ok",
                    False,
                    f"tunnel credentials JSON is present at {creds_path}",
                )
            )
        else:
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "warn",
                    False,
                    f"tunnel credentials JSON is recorded but missing at {creds_path}",
                )
            )
    elif tunnel_uuid and tunnel_uuid != "null":
        if _docker_container_running("cloudflared"):
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "ok",
                    False,
                    "tunnel is running (credentials path not recorded in state)",
                )
            )
        else:
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "warn",
                    False,
                    "tunnel UUID is recorded but the standardized credentials path is not recorded yet",
                )
            )
    else:
        if _docker_container_running("cloudflared"):
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "ok",
                    False,
                    "tunnel is running (credentials path not recorded in state)",
                )
            )
        else:
            results.append(
                CheckResult(
                    "cloudflare_creds",
                    "warn",
                    False,
                    "Cloudflare tunnel credentials will be created or recovered during setup",
                )
            )

    return results


def check_conflicts() -> CheckResult:
    conflicts: list[str] = []

    if _command_exists("systemctl"):
        for service in ("nginx", "apache2", "httpd", "postgresql"):
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", service],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                conflicts.append(f"active systemd service: {service}")

    pg_listeners, rc = _port_listeners(5432)
    if rc != 2 and pg_listeners:
        if _docker_container_running("postgres"):
            pass
        elif "docker" not in pg_listeners.lower() and "postgres" not in pg_listeners.lower():
            conflicts.append("port 5432 listener is not clearly Rakkib postgres")
        else:
            conflicts.append("port 5432 is already listening before the Rakkib postgres container is running")

    if not conflicts:
        return CheckResult("conflicts", "ok", False, "no obvious nginx/apache/host-postgres conflicts found")
    return CheckResult("conflicts", "warn", False, "; ".join(conflicts))


def run_checks(state: State) -> list[CheckResult]:
    """Run all diagnostic checks and return results."""
    data_root = str(state.data_root)
    domain = state.get("domain") or ""

    results: list[CheckResult] = []
    results.append(check_os())
    results.append(check_arch())
    results.append(check_ram())
    results.append(check_disk(data_root))
    results.append(check_docker())
    results.append(check_compose())
    if cloudflare_enabled(state):
        results.append(check_cloudflared_binary())
    results.append(check_public_ports())
    results.append(check_ssh_port())
    results.append(check_domain_dns(domain))
    if cloudflare_enabled(state):
        results.extend(check_cloudflare_readiness(state))
    results.append(check_conflicts())
    return results


def to_json(checks: list[CheckResult]) -> str:
    """Emit JSON matching the original bash script shape."""
    fail_count = sum(1 for c in checks if c.status == "fail")
    ok_count = sum(1 for c in checks if c.status == "ok")
    warn_count = sum(1 for c in checks if c.status == "warn")
    payload = {
        "ok": fail_count == 0,
        "summary": {"ok": ok_count, "warn": warn_count, "fail": fail_count},
        "checks": [c.to_dict() for c in checks],
    }
    return json.dumps(payload)


def summary_text(checks: list[CheckResult]) -> str:
    fail_count = sum(1 for c in checks if c.status == "fail")
    ok_count = sum(1 for c in checks if c.status == "ok")
    warn_count = sum(1 for c in checks if c.status == "warn")
    return f"doctor: {ok_count} ok, {warn_count} warn, {fail_count} fail"


def attempt_fix_docker() -> str:
    """Attempt to install Docker for the current host. Returns a message describing the result."""
    if platform.system() == "Darwin":
        _ensure_macos_tool_path()
        brew = _macos_brew_cmd()
        if brew is None:
            return "Homebrew is required. Rerun install.sh, then run `rakkib auth`."
        result = subprocess.run([brew, "install", "--cask", MACOS_DOCKER_CASK], capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            return f"Docker Desktop setup failed: {detail}"
        _ensure_macos_tool_path()
        start_msg = attempt_start_docker_desktop()
        return f"Docker Desktop installed. {start_msg}"
    if platform.system() != "Linux":
        return "Automatic Docker installation is only supported on Linux."

    if not _command_exists("curl"):
        return "curl is required but not found. Install curl first."

    sudo_error = _sudo_install_ready()
    if sudo_error:
        return sudo_error

    if _command_exists("apt-get"):
        lock_error = wait_for_apt_locks(on_wait=_notify_apt_wait)
        if lock_error:
            return lock_error

    install_cmd = ["sh", "-c", "curl -fsSL https://get.docker.com | sh"]
    if os.geteuid() != 0:
        install_cmd = [
            "sudo",
            "-n",
            "env",
            *[f"{key}={value}" for key, value in PACKAGE_MANAGER_SAFE_ENV.items()],
            *install_cmd,
        ]

    result = subprocess.run(
        install_cmd,
        capture_output=True,
        text=True,
        env=_package_manager_env(),
    )
    if result.returncode != 0:
        return f"get.docker.com install failed: {result.stderr.strip() or 'unknown error'}"

    subprocess.run(["sudo", "systemctl", "enable", "--now", "docker"], capture_output=True, text=True)
    return "Docker installed."


def attempt_fix_compose() -> str:
    """Install or recover Docker Compose. Returns a message describing the result."""
    if platform.system() == "Darwin":
        return f"Docker Compose is bundled with Docker Desktop. {attempt_start_docker_desktop()}"

    # get.docker.com adds the Docker apt repo, so the plugin is available via apt.
    if _command_exists("apt-get"):
        lock_error = wait_for_apt_locks(on_wait=_notify_apt_wait)
        if lock_error:
            return lock_error
        result = subprocess.run(
            [
                "sudo",
                "env",
                *[f"{key}={value}" for key, value in PACKAGE_MANAGER_SAFE_ENV.items()],
                "apt-get",
                "install",
                "-y",
                "-q",
                "-o",
                "DPkg::Lock::Timeout=900",
                "docker-compose-plugin",
            ],
            capture_output=True,
            text=True,
            env=_package_manager_env(),
        )
        if result.returncode == 0:
            return "Docker Compose installed."

    # Fallback: download the latest binary directly.
    machine = platform.machine()
    arch = _normalize_arch(machine)
    if not arch:
        return f"Unsupported architecture for Docker Compose: {machine}"
    arch_release = "x86_64" if arch == "amd64" else "aarch64"
    expected_sha256 = COMPOSE_SHA256.get(arch_release)
    if not expected_sha256:
        return f"Unsupported architecture for Docker Compose: {arch_release}"

    url = f"https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/docker-compose-linux-{arch_release}"
    plugin_path = Path("/usr/local/lib/docker/cli-plugins/docker-compose")

    try:
        mkdir = subprocess.run(["sudo", "mkdir", "-p", str(plugin_path.parent)], capture_output=True, text=True)
        if mkdir.returncode != 0:
            return f"compose cli-plugins directory creation failed: {mkdir.stderr.strip() or 'unknown error'}"
        result = subprocess.run(
            ["sudo", "curl", "-fsSL", "-o", str(plugin_path), url],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"compose binary download failed: {result.stderr.strip() or 'unknown error'}"
        checksum_error = _verify_download_sha256(plugin_path, expected_sha256)
        if checksum_error:
            return f"compose binary download failed verification: {checksum_error}"
        chmod = subprocess.run(["sudo", "chmod", "+x", str(plugin_path)], capture_output=True, text=True)
        if chmod.returncode != 0:
            return f"compose binary chmod failed: {chmod.stderr.strip() or 'unknown error'}"
        verify = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if verify.returncode != 0:
            return f"docker compose plugin install verification failed: {verify.stderr.strip() or 'unknown error'}"
        return "Docker Compose installed."
    except FileNotFoundError as e:
        return f"Required command not found: {e}"


def attempt_fix_cloudflared() -> str:
    """Install cloudflared binary into ~/.local/bin. Returns a message describing the result."""
    if platform.system() == "Darwin":
        _ensure_macos_tool_path()
        brew = _macos_brew_cmd()
        if brew is not None:
            result = subprocess.run([brew, "install", "cloudflared"], capture_output=True, text=True)
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
                return f"Homebrew cloudflared install failed: {detail}"
            cloudflared = _macos_tool_cmd("cloudflared") or "cloudflared"
            verify = subprocess.run([cloudflared, "--version"], capture_output=True, text=True)
            if verify.returncode != 0:
                detail = verify.stderr.strip() or verify.stdout.strip() or "unknown error"
                return f"Homebrew cloudflared install verification failed: {detail}"
            return "Cloudflare tunnel tool installed."

    if not _command_exists("curl"):
        return "curl is required but not found. Install curl first."

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    arch = _normalize_arch(platform.machine()) or "amd64"
    kernel = "darwin" if platform.system() == "Darwin" else "linux"
    expected_sha256 = CLOUDFLARED_SHA256.get((kernel, arch))
    if not expected_sha256:
        return f"Unsupported platform for Cloudflare tunnel tool: {kernel}/{arch}"
    asset_name = f"cloudflared-{kernel}-{arch}"
    if kernel == "darwin":
        asset_name = f"{asset_name}.tgz"
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/{asset_name}"
    dest = local_bin / "cloudflared"
    download_path = local_bin / asset_name if kernel == "darwin" else dest

    result = subprocess.run(
        ["curl", "-fsSL", "-o", str(download_path), url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"cloudflared download failed: {result.stderr.strip() or 'unknown error'}"
    checksum_error = _verify_download_sha256(download_path, expected_sha256)
    if checksum_error:
        return f"cloudflared download failed verification: {checksum_error}"
    if kernel == "darwin":
        try:
            with tarfile.open(download_path, "r:gz") as archive:
                member = archive.getmember("cloudflared")
                source = archive.extractfile(member)
                if source is None:
                    return "cloudflared archive extraction failed: cloudflared member is not a file"
                with dest.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        except (KeyError, OSError, tarfile.TarError) as exc:
            return f"cloudflared archive extraction failed: {exc}"
        try:
            download_path.unlink()
        except OSError:
            pass
    dest.chmod(0o755)
    return "Cloudflare tunnel tool installed."


def process_owners_for_ports() -> dict[int, str]:
    """Return a mapping of port -> process info for ports 80 and 443."""
    owners: dict[int, str] = {}
    for port in (80, 443):
        listeners, rc = _port_listeners(port)
        if rc == 2:
            owners[port] = "unable to determine (ss/lsof missing)"
        elif listeners:
            # Take first non-empty line as owner info
            lines = [ln.strip() for ln in listeners.splitlines() if ln.strip()]
            owners[port] = lines[0] if lines else "unknown"
        else:
            owners[port] = "free"
    return owners
