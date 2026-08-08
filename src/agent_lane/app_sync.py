"""macOS login integration for the optional App Sync execution mode."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .app_runtime import AppRuntimeError, detect_running_codex_app
from .codex_rpc import CodexAppServer, CodexRpcError
from .daemon_transport import (
    DaemonProbeError,
    detect_local_app_transport,
    probe_shared_daemon,
)
from .workspace import WorkspaceError


APP_SYNC_LABEL = "io.github.unitary-orz.agent-lane.app-sync"
APP_SYNC_ENV = "CODEX_APP_SERVER_USE_LOCAL_DAEMON"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def app_sync_enable(
    *,
    codex_bin: str = "codex",
    executable: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
    run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """Install and load the user LaunchAgent without restarting the App."""

    _require_macos()
    paths = _paths(home)
    resolved_agent_lane = _resolve_agent_lane_executable(executable)
    resolved_codex = _resolve_executable(codex_bin, label="Codex CLI")
    user_id = os.getuid() if uid is None else uid
    paths["launch_agents"].mkdir(parents=True, exist_ok=True)
    paths["logs"].mkdir(parents=True, exist_ok=True)

    payload = _launch_agent_payload(
        agent_lane=resolved_agent_lane,
        codex_bin=resolved_codex,
        stdout_path=paths["stdout"],
        stderr_path=paths["stderr"],
    )
    current = _read_plist(paths["plist"])
    loaded = _launch_agent_loaded(user_id, run=run)
    changed = current != payload
    if loaded and changed:
        _run_launchctl(
            ["bootout", f"gui/{user_id}/{APP_SYNC_LABEL}"],
            run=run,
            check=False,
        )
        loaded = False
    if changed:
        _atomic_write_plist(paths["plist"], payload)
    if not loaded:
        _run_launchctl(
            ["bootstrap", f"gui/{user_id}", str(paths["plist"])],
            run=run,
        )
    else:
        _run_launchctl(
            ["kickstart", f"gui/{user_id}/{APP_SYNC_LABEL}"],
            run=run,
        )

    report = app_sync_status(
        codex_bin=resolved_codex,
        home=paths["home"],
        uid=user_id,
        run=run,
    )
    return {
        **report,
        "operation": "app-sync.enable",
        "changed": changed or not loaded,
    }


def app_sync_disable(
    *,
    home: Path | None = None,
    uid: int | None = None,
    run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """Remove future login activation without stopping the shared daemon."""

    _require_macos()
    paths = _paths(home)
    user_id = os.getuid() if uid is None else uid
    was_loaded = _launch_agent_loaded(user_id, run=run)
    if was_loaded:
        _run_launchctl(
            ["bootout", f"gui/{user_id}/{APP_SYNC_LABEL}"],
            run=run,
            check=False,
        )
    removed = False
    try:
        paths["plist"].unlink()
        removed = True
    except FileNotFoundError:
        pass
    _run_launchctl(["unsetenv", APP_SYNC_ENV], run=run, check=False)
    return {
        "operation": "app-sync.disable",
        "installed": False,
        "loaded": False,
        "changed": was_loaded or removed,
        "environment_enabled": False,
        "daemon_left_running": True,
        "app_reopen_required": _app_is_running(),
        "launch_agent_path": str(paths["plist"]),
    }


def app_sync_status(
    *,
    codex_bin: str = "codex",
    home: Path | None = None,
    uid: int | None = None,
    run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """Report installed state and live daemon/App observations."""

    _require_macos()
    paths = _paths(home)
    user_id = os.getuid() if uid is None else uid
    installed = paths["plist"].is_file()
    loaded = _launch_agent_loaded(user_id, run=run)
    environment_enabled = _launchctl_environment_enabled(run=run)

    daemon: dict[str, Any]
    try:
        info = _probe_daemon_readiness(codex_bin, run=run)
        daemon = {
            "ready": True,
            "protocol_ready": True,
            "cli_version": info.cli_version,
            "app_server_version": info.app_server_version,
            "version_mismatch": info.cli_version != info.app_server_version,
        }
    except (
        CodexRpcError,
        DaemonProbeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        daemon = {
            "ready": False,
            "protocol_ready": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    app_transport: str | None = None
    app_connected: bool | None = None
    app_running = False
    try:
        app = detect_running_codex_app()
        app_running = True
        observation = detect_local_app_transport(app.log_root, pid=app.pid)
        if observation is not None:
            app_transport = observation.transport
            app_connected = observation.connected
    except (AppRuntimeError, DaemonProbeError, OSError):
        pass

    ready = bool(
        installed
        and loaded
        and environment_enabled
        and daemon.get("ready")
    )
    warnings = []
    if daemon.get("ready") and daemon.get("version_mismatch"):
        warnings.append(
            {
                "code": "APP_SYNC_VERSION_MISMATCH",
                "message": (
                    "Codex CLI and shared runtime versions differ, but the "
                    "WebSocket and initialize probe succeeded"
                ),
                "cli_version": daemon.get("cli_version"),
                "app_server_version": daemon.get("app_server_version"),
            }
        )
    return {
        "operation": "app-sync.status",
        "installed": installed,
        "loaded": loaded,
        "ready": ready,
        "environment_enabled": environment_enabled,
        "daemon": daemon,
        "app_running": app_running,
        "app_transport": app_transport,
        "app_connected": app_connected,
        "app_reopen_required": bool(
            app_running and ready and app_transport != "websocket"
        ),
        "launch_agent_path": str(paths["plist"]),
        "warnings": warnings,
    }


def app_sync_login(
    *,
    codex_bin: str,
    run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """LaunchAgent entrypoint: start, verify, then advertise the daemon."""

    _require_macos()
    try:
        completed = run(
            [codex_bin, "app-server", "daemon", "start"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(
                "APP_SYNC_DAEMON_START_FAILED",
                "Codex shared runtime did not start",
                retryable=True,
                stderr=(completed.stderr or "").strip(),
            )
        info = _probe_daemon_readiness(codex_bin, run=run)
        _run_launchctl(["setenv", APP_SYNC_ENV, "1"], run=run)
        warnings = []
        if info.cli_version != info.app_server_version:
            warnings.append(
                {
                    "code": "APP_SYNC_VERSION_MISMATCH",
                    "message": (
                        "Codex CLI and shared runtime versions differ, but the "
                        "WebSocket and initialize probe succeeded"
                    ),
                    "cli_version": info.cli_version,
                    "app_server_version": info.app_server_version,
                }
            )
        return {
            "operation": "app-sync.login",
            "ready": True,
            "environment_enabled": True,
            "warnings": warnings,
        }
    except Exception:
        _run_launchctl(["unsetenv", APP_SYNC_ENV], run=run, check=False)
        raise


def _probe_daemon_readiness(
    codex_bin: str,
    *,
    run: RunCommand,
):
    """Verify daemon identity, socket safety, WebSocket, and initialize."""

    info = probe_shared_daemon(codex_bin, run_command=run)
    with CodexAppServer(
        codex_bin,
        transport="daemon",
        daemon_socket=info.socket_path,
    ):
        pass
    return info


def _launch_agent_payload(
    *,
    agent_lane: str,
    codex_bin: str,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    return {
        "Label": APP_SYNC_LABEL,
        "ProgramArguments": [
            agent_lane,
            "_app-sync-login",
            "--codex-bin",
            codex_bin,
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def _paths(home: Path | None) -> dict[str, Path]:
    user_home = Path.home() if home is None else Path(home).expanduser()
    logs = user_home / ".agent-lane" / "logs"
    launch_agents = user_home / "Library" / "LaunchAgents"
    return {
        "home": user_home,
        "logs": logs,
        "stdout": logs / "app-sync.log",
        "stderr": logs / "app-sync.error.log",
        "launch_agents": launch_agents,
        "plist": launch_agents / f"{APP_SYNC_LABEL}.plist",
    }


def _atomic_write_plist(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        plistlib.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_plist(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
    except (FileNotFoundError, OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, dict) else None


def _launch_agent_loaded(uid: int, *, run: RunCommand) -> bool:
    completed = _run_launchctl(
        ["print", f"gui/{uid}/{APP_SYNC_LABEL}"],
        run=run,
        check=False,
    )
    return completed.returncode == 0


def _launchctl_environment_enabled(*, run: RunCommand) -> bool:
    completed = _run_launchctl(
        ["getenv", APP_SYNC_ENV],
        run=run,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _run_launchctl(
    arguments: Sequence[str],
    *,
    run: RunCommand,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = run(
        ["/bin/launchctl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorkspaceError(
            "APP_SYNC_LAUNCHCTL_FAILED",
            f"launchctl {arguments[0]} failed",
            retryable=True,
            stderr=(completed.stderr or "").strip(),
        )
    return completed


def _resolve_agent_lane_executable(value: str | None) -> str:
    if value:
        return _resolve_executable(value, label="agent-lane executable")
    candidate = shutil.which("agent-lane")
    if candidate is None and sys.argv:
        candidate = sys.argv[0]
    return _resolve_executable(candidate or "", label="agent-lane executable")


def _resolve_executable(value: str, *, label: str) -> str:
    candidate = shutil.which(value) or value
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise WorkspaceError(
            "APP_SYNC_EXECUTABLE_MISSING",
            f"{label} is not an executable file",
            executable=str(path),
            retryable=False,
        )
    return str(path.resolve())


def _app_is_running() -> bool:
    try:
        detect_running_codex_app()
    except (AppRuntimeError, OSError):
        return False
    return True


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise WorkspaceError(
            "APP_SYNC_UNSUPPORTED_PLATFORM",
            "App Sync login integration currently supports macOS only",
            platform=sys.platform,
            retryable=False,
        )
