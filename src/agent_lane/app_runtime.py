"""Read-only discovery of the local Codex desktop runtime on macOS."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CODEX_APP_CANDIDATES = (
    Path("/Applications/ChatGPT.app"),
    Path.home() / "Applications" / "ChatGPT.app",
)
CODEX_APP_LOG_ROOT = Path.home() / "Library" / "Logs" / "com.openai.codex"


class AppRuntimeError(RuntimeError):
    """Stable structured failure while discovering the Codex App runtime."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool = False,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.details = details

    def as_dict(self, **base: Any) -> dict[str, Any]:
        return {
            "ok": False,
            **base,
            "error_code": self.error_code,
            "error": str(self),
            "retryable": self.retryable,
            **self.details,
        }


@dataclass(frozen=True)
class CodexDesktopApp:
    path: Path
    version: str | None
    build: str | None
    pid: int
    log_root: Path
    metadata_error: str | None = None


@dataclass(frozen=True)
class _RunningCodexProcess:
    pid: int
    app_path: Path


def detect_running_codex_app() -> CodexDesktopApp:
    """Return the exact running Codex App copy without changing App state."""

    if sys.platform != "darwin":
        raise AppRuntimeError(
            "CODEX_APP_NOT_RUNNING",
            "Codex App runtime discovery currently supports macOS only",
        )
    processes = _running_codex_processes()
    if not processes:
        raise AppRuntimeError(
            "CODEX_APP_NOT_RUNNING",
            "no supported Codex App installation is running",
            retryable=True,
        )

    process = _select_process(processes)
    metadata_error = None
    try:
        version, build = _read_app_version(process.app_path)
    except AppRuntimeError as exc:
        version, build = None, None
        metadata_error = str(exc)
    return CodexDesktopApp(
        path=process.app_path,
        version=version,
        build=build,
        pid=process.pid,
        log_root=CODEX_APP_LOG_ROOT,
        metadata_error=metadata_error,
    )


def _select_process(processes: list[_RunningCodexProcess]) -> _RunningCodexProcess:
    """Prefer the process with the newest runtime log, then the newest PID."""

    ranked: list[tuple[float, int, _RunningCodexProcess]] = []
    for process in processes:
        newest = 0.0
        try:
            paths = CODEX_APP_LOG_ROOT.glob(
                f"**/codex-desktop-*-{process.pid}-t0-*.log"
            )
            newest = max((path.stat().st_mtime for path in paths), default=0.0)
        except OSError:
            pass
        ranked.append((newest, process.pid, process))
    return max(ranked, key=lambda item: (item[0], item[1]))[2]


def _read_app_version(app_path: Path) -> tuple[str, str | None]:
    info_path = app_path / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise AppRuntimeError(
            "CODEX_APP_METADATA_UNAVAILABLE",
            f"failed to read Codex App metadata: {exc}",
        ) from exc
    version = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or "").strip() or None
    if not version:
        raise AppRuntimeError(
            "CODEX_APP_METADATA_UNAVAILABLE",
            "Codex App metadata does not contain a version",
        )
    return version, build


def _running_codex_processes() -> list[_RunningCodexProcess]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppRuntimeError(
            "CODEX_APP_PROCESS_UNAVAILABLE",
            f"could not inspect running Codex App processes: {exc}",
            retryable=True,
        ) from exc
    if completed.returncode != 0:
        raise AppRuntimeError(
            "CODEX_APP_PROCESS_UNAVAILABLE",
            f"`ps` exited with {completed.returncode} while inspecting the App",
            retryable=True,
        )

    processes: list[_RunningCodexProcess] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        raw_pid, command = parts
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        if pid <= 0 or not _pid_exists(pid):
            continue
        for app_path in CODEX_APP_CANDIDATES:
            executable = str(app_path / "Contents" / "MacOS" / "ChatGPT")
            if command == executable or command.startswith(executable + " "):
                processes.append(_RunningCodexProcess(pid=pid, app_path=app_path))
                break
    return processes


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
