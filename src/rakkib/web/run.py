"""Background setup runner for the browser UI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import sys
import threading

from rakkib.state import State, default_state_path
from rakkib.util import checkout_dir

FULL_SETUP_OPERATION = "full_setup"
SERVICE_SYNC_OPERATION = "service_sync"


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _message_for_status(status: str) -> str:
    if status == "succeeded":
        return "Setup run completed successfully."
    if status == "canceled":
        return "Setup run was canceled by the user."
    return "Setup run exited with errors."


def _setup_child_env() -> dict[str, str]:
    """Return a clean environment for browser-triggered setup runs."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    if os.getuid() != 0:
        try:
            home_dir = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home_dir = ""
        if home_dir:
            env["HOME"] = home_dir

    return env


CLOUDFLARE_AUTH_URL_RE = re.compile(r"https://[^\s]+cloudflare[^\s]*", re.IGNORECASE)


def _cloudflare_attention_from_lines(lines: list[str]) -> dict[str, str] | None:
    """Return a browser action prompt when setup is waiting for Cloudflare auth."""
    for line in reversed(lines):
        match = CLOUDFLARE_AUTH_URL_RE.search(line)
        if match:
            return {
                "type": "cloudflare_auth",
                "url": match.group(0).rstrip(".,;)]}"),
            }
    return None


@dataclass
class RunRecord:
    """Current browser-triggered setup run state."""

    status: str = "idle"
    message: str = "No setup run has started yet."
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    command: list[str] = field(default_factory=list)
    operation: str = FULL_SETUP_OPERATION
    log_path: str | None = None
    pid: int | None = None


class WebRunManager:
    """Manage one background `rakkib pull` process for the web UI."""

    def __init__(self, repo_dir: Path | str) -> None:
        self._repo_dir = checkout_dir(Path(repo_dir))
        self._state_path = default_state_path(repo_dir)
        self._log_path = self._repo_dir / ".rakkib-web-run.log"
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._record = self._initial_record()

    def start(self, operation: str = FULL_SETUP_OPERATION) -> dict[str, object]:
        """Start a background Rakkib operation unless one is already active."""
        with self._lock:
            self._refresh_locked()
            if self._process is not None:
                return self._snapshot_locked()

            command = self._command_for_operation(operation)
            started_at = _now_iso()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self._log_path.open("w", encoding="utf-8")
            log_handle.write(f"[{started_at}] Starting browser-triggered setup run\n")
            log_handle.write(f"$ {' '.join(shlex.quote(part) for part in command)}\n\n")
            log_handle.flush()

            env = _setup_child_env()

            try:
                process = subprocess.Popen(
                    command,
                    cwd=self._repo_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except Exception as exc:
                message = f"Unable to start setup run: {exc}"
                log_handle.write(f"[{_now_iso()}] {message}\n")
                log_handle.close()
                self._record = RunRecord(
                    status="failed",
                    message=message,
                    started_at=started_at,
                    finished_at=_now_iso(),
                    exit_code=None,
                    command=command,
                    operation=operation,
                    log_path=str(self._log_path),
                )
                raise RuntimeError(message) from exc

            self._process = process
            self._record = RunRecord(
                status="running",
                message="Setup run is in progress.",
                started_at=started_at,
                finished_at=None,
                exit_code=None,
                command=command,
                operation=operation,
                log_path=str(self._log_path),
                pid=process.pid,
            )
            self._persist_record_status(self._record)

            watcher = threading.Thread(target=self._watch_process, args=(process, log_handle), daemon=True)
            watcher.start()
            return self._snapshot_locked()

    def cancel(self) -> dict[str, object]:
        """Terminate the active background process, if one is running."""
        with self._lock:
            self._refresh_locked()
            process = self._process
            if process is None:
                return self._snapshot_locked()

            pid = process.pid
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            canceled_at = _now_iso()
            self._process = None
            self._record = RunRecord(
                status="canceled",
                message="Setup run was canceled by the user.",
                started_at=self._record.started_at,
                finished_at=canceled_at,
                exit_code=None,
                command=list(self._record.command),
                operation=self._record.operation,
                log_path=str(self._log_path),
                pid=None,
            )
            self._append_log_line(f"[{canceled_at}] Terminated setup run process {pid}")
            self._persist_record_status(self._record)
            return self._snapshot_locked()

    def snapshot(self) -> dict[str, object]:
        """Return the current run status plus a log tail."""
        with self._lock:
            self._refresh_locked()
            return self._snapshot_locked()

    def _watch_process(self, process: subprocess.Popen[str], log_handle) -> None:
        """Wait for the child process and persist the final status."""
        exit_code = process.wait()
        finished_at = _now_iso()
        status = "succeeded" if exit_code == 0 else "failed"
        message = "Setup run completed successfully." if exit_code == 0 else "Setup run exited with errors."

        try:
            log_handle.write(f"\n[{finished_at}] Setup run finished with exit code {exit_code}\n")
            log_handle.flush()
        finally:
            log_handle.close()

        with self._lock:
            if self._process is process:
                self._process = None
                self._record = RunRecord(
                    status=status,
                    message=message,
                    started_at=self._record.started_at,
                    finished_at=finished_at,
                    exit_code=exit_code,
                    command=list(self._record.command),
                    operation=self._record.operation,
                    log_path=str(self._log_path),
                    pid=None,
                )
                self._persist_record_status(self._record)

    def _refresh_locked(self) -> None:
        """Synchronize the stored record if the child exited between polls."""
        if self._process is None:
            return

        exit_code = self._process.poll()
        if exit_code is None:
            return

        finished_at = _now_iso()
        status = "succeeded" if exit_code == 0 else "failed"
        message = "Setup run completed successfully." if exit_code == 0 else "Setup run exited with errors."
        self._process = None
        self._record = RunRecord(
            status=status,
            message=message,
            started_at=self._record.started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            command=list(self._record.command),
            operation=self._record.operation,
            log_path=str(self._log_path),
            pid=None,
        )
        self._persist_record_status(self._record)

    def _snapshot_locked(self) -> dict[str, object]:
        """Build the API payload for the current run."""
        log_tail = self._read_log_tail()
        return {
            "status": self._record.status,
            "message": self._record.message,
            "started_at": self._record.started_at,
            "finished_at": self._record.finished_at,
            "exit_code": self._record.exit_code,
            "command": list(self._record.command),
            "operation": self._record.operation,
            "log_path": self._record.log_path,
            "pid": self._record.pid,
            "running": self._record.status == "running",
            "can_start": self._record.status != "running",
            "log_tail": log_tail,
            "attention": _cloudflare_attention_from_lines(log_tail) if self._record.status == "running" else None,
        }

    def _initial_record(self) -> RunRecord:
        """Restore the last completed web run status for new `rakkib web` processes."""
        record = RunRecord(log_path=str(self._log_path))
        try:
            state = State.load(self._state_path)
        except Exception:
            return record

        status = state.get("web_deployment.status")
        if status not in {"succeeded", "failed", "canceled"}:
            return record

        return RunRecord(
            status=str(status),
            message=_message_for_status(str(status)),
            started_at=state.get("web_deployment.started_at"),
            finished_at=state.get("web_deployment.finished_at"),
            exit_code=state.get("web_deployment.exit_code"),
            command=self._command_for_operation(str(state.get("web_deployment.operation") or FULL_SETUP_OPERATION)),
            operation=str(state.get("web_deployment.operation") or FULL_SETUP_OPERATION),
            log_path=str(self._log_path),
            pid=None,
        )

    def _persist_record_status(self, record: RunRecord) -> None:
        """Persist web deployment state across process restarts."""
        try:
            state = State.load(self._state_path)
            state.set("web_deployment.status", record.status)
            state.set("web_deployment.started_at", record.started_at)
            state.set("web_deployment.finished_at", record.finished_at)
            state.set("web_deployment.exit_code", record.exit_code)
            state.set("web_deployment.operation", record.operation)
            state.set("web_deployment.pid", record.pid)
            state.save(self._state_path)
        except Exception:
            return

    def _append_log_line(self, line: str) -> None:
        try:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{line}\n")
        except OSError:
            return

    def _command_for_operation(self, operation: str) -> list[str]:
        if operation == SERVICE_SYNC_OPERATION:
            return [sys.executable, "-m", "rakkib.cli", "sync-services"]
        return [sys.executable, "-m", "rakkib.cli", "pull"]

    def _read_log_tail(self, limit: int = 200) -> list[str]:
        """Return the last N lines from the current log file."""
        if not self._log_path.exists():
            return []

        with self._log_path.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque((line.rstrip("\n") for line in handle), maxlen=limit))
