"""Read-only diagnostics for the local Codex control plane."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .codex_rpc import (
    CodexAppServer,
    CompatibleDaemonCli,
    resolve_compatible_daemon_cli,
)
from .daemon_transport import (
    DaemonProbeError,
    DaemonSocketError,
    DaemonVersionError,
    detect_local_app_transport,
    probe_shared_daemon,
)
from .app_runtime import AppRuntimeError, detect_running_codex_app

try:
    import tomllib
except ModuleNotFoundError:  # Defensive support for source runners without tomllib.
    tomllib = None  # type: ignore[assignment]


def doctor_report(
    *,
    alias_root: Path,
    run_probe: bool = False,
    verbose: bool = False,
    codex_bin: str = "codex",
) -> dict[str, Any]:
    codex = _codex_check(codex_bin)
    config = _config_check()
    auth = _auth_check()
    recent = _recent_check(alias_root)
    apps = _app_paths()
    shared_daemon = _shared_daemon_check(codex_bin)
    _apply_daemon_cli_resolution(codex, shared_daemon)
    probe = _probe_check(codex.get("path"), requested=run_probe)
    required = (codex, config, auth, recent) + (
        (shared_daemon,) if shared_daemon.get("required") else ()
    )
    ok = all(item.get("ok") is True for item in required) and (
        not run_probe or probe.get("ok") is True
    )
    issues = _doctor_issues(
        codex=codex,
        config=config,
        auth=auth,
        recent=recent,
        probe=probe,
        shared_daemon=shared_daemon,
    )
    details = {
        "ok": ok,
        "status": "ready" if ok else "issues",
        "codex_cli": codex,
        "config": config,
        "auth": auth,
        "recent": recent,
        "probe": probe,
        "shared_daemon": shared_daemon,
        "apps": apps,
        "issues": issues,
    }
    if verbose:
        return details
    return {
        "ok": ok,
        "status": details["status"],
        "codex_cli": _select(
            codex,
            "ok",
            "version",
            "source",
            "fallback_used",
        ),
        "config": _select(config, "ok", "default_model"),
        "auth": _select(auth, "ok", "mode"),
        "recent": _select(recent, "ok"),
        "probe": _select(probe, "requested", "ok", "status"),
        "shared_daemon": _select(
            shared_daemon,
            "ready",
            "status",
            "app_transport",
            "app_connected",
            "required",
            "probe_cli_source",
            "fallback_used",
            "version_mismatch",
            "warnings",
        ),
        "apps": {
            name: _select(item, "installed") for name, item in apps.items()
        },
        "issues": issues,
    }


def _select(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item.get(key) for key in keys}


def _apply_daemon_cli_resolution(
    codex: dict[str, Any],
    shared_daemon: dict[str, Any],
) -> None:
    if shared_daemon.get("ready") is not True:
        return
    selected_path = shared_daemon.get("probe_cli_path")
    selected_version = shared_daemon.get("probe_cli_version")
    if not selected_path or not selected_version:
        return
    previous_path = codex.get("path")
    previous_version = codex.get("version")
    if previous_path != selected_path:
        codex["path_candidate"] = previous_path
        codex["version_candidate"] = previous_version
    codex.update(
        {
            "ok": True,
            "path": selected_path,
            "version": f"codex-cli {selected_version}",
            "source": shared_daemon.get("probe_cli_source"),
            "fallback_used": bool(shared_daemon.get("fallback_used")),
        }
    )
    codex.pop("error", None)


def _doctor_issues(
    *,
    codex: dict[str, Any],
    config: dict[str, Any],
    auth: dict[str, Any],
    recent: dict[str, Any],
    probe: dict[str, Any],
    shared_daemon: dict[str, Any],
) -> list[dict[str, str]]:
    checks = [
        ("codex_cli", codex, "Install Codex CLI or add `codex` to PATH."),
        (
            "config",
            config,
            "Run with --verbose and fix the Codex config reported there.",
        ),
        ("auth", auth, "Run `codex login`, then retry doctor."),
        (
            "recent",
            recent,
            "Run with --verbose and check session and alias directory permissions.",
        ),
    ]
    if probe.get("requested"):
        checks.append(
            (
                "probe",
                probe,
                "Run with --verbose --probe and inspect the app-server error.",
            )
        )
    if shared_daemon.get("required"):
        checks.append(
            (
                "shared_daemon",
                shared_daemon,
                _shared_daemon_action(shared_daemon),
            )
        )
    return [
        {"check": name, "action": action}
        for name, result, action in checks
        if result.get("ok") is not True
    ]


def _shared_daemon_action(shared_daemon: dict[str, Any]) -> str:
    if shared_daemon.get("status") in {
        "app_not_running",
        "app_stdio_sync_required",
    }:
        return (
            "Start the standalone managed daemon, set "
            "`CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` with `launchctl setenv`, "
            "then reopen the App once."
        )
    return "Run with --verbose and fix the managed daemon readiness error."


def _codex_check(codex_bin: str) -> dict[str, Any]:
    path = shutil.which(codex_bin)
    result: dict[str, Any] = {
        "ok": path is not None,
        "requested": codex_bin,
        "path": path,
        "version": None,
    }
    if not path:
        result["error"] = f"{codex_bin!r} was not found on PATH"
        return result
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        result["version"] = (completed.stdout or completed.stderr).strip() or None
        if completed.returncode != 0:
            result["ok"] = False
            result["error"] = f"codex --version exited {completed.returncode}"
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result


def _config_check() -> dict[str, Any]:
    path = Path.home() / ".codex" / "config.toml"
    result: dict[str, Any] = {
        "ok": False,
        "path": str(path),
        "exists": path.is_file(),
        "default_model": None,
    }
    if not path.is_file():
        result["error"] = "config.toml does not exist"
        return result
    try:
        if tomllib is not None:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            result["default_model"] = data.get("model")
            result["parser"] = "tomllib"
        else:
            result["default_model"] = _top_level_toml_string(path, "model")
            result["parser"] = "minimal_top_level_fallback"
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _top_level_toml_string(path: Path, key: str) -> str | None:
    """Read one quoted top-level string without pretending to parse all TOML."""
    prefix = key + "="
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        compact = line.replace(" ", "")
        if not compact.startswith(prefix):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] in {'"', "'"}:
            quote = value[0]
            end = value.find(quote, 1)
            if end > 0:
                return value[1:end]
        return None
    return None


def _auth_check() -> dict[str, Any]:
    path = Path.home() / ".codex" / "auth.json"
    result: dict[str, Any] = {
        "ok": False,
        "path": str(path),
        "exists": path.is_file(),
        "mode": None,
        "account_id": None,
    }
    if not path.is_file():
        result["error"] = "auth.json does not exist"
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("auth.json is not an object")
        tokens = data.get("tokens")
        result["mode"] = data.get("auth_mode")
        result["account_id"] = (
            tokens.get("account_id") if isinstance(tokens, dict) else None
        )
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _recent_check(alias_root: Path) -> dict[str, Any]:
    sessions = Path.home() / ".codex" / "sessions"
    alias_parent = alias_root.expanduser()
    sessions_exists = sessions.is_dir()
    aliases_exist = alias_parent.is_dir()
    sessions_readable = sessions_exists and os.access(sessions, os.R_OK | os.X_OK)
    aliases_readable = (not aliases_exist) or os.access(alias_parent, os.R_OK | os.X_OK)
    return {
        "ok": sessions_readable and aliases_readable,
        "codex_sessions_path": str(sessions),
        "codex_sessions_exists": sessions_exists,
        "codex_sessions_readable": sessions_readable,
        "alias_root": str(alias_parent),
        "alias_root_exists": aliases_exist,
        "alias_root_readable": aliases_readable,
        "note": (
            "Live recent/find readability is verified by --probe; this check only "
            "validates the local session and alias stores."
        ),
    }


def _probe_check(codex_path: Any, *, requested: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "requested": requested,
        "ok": None,
        "status": "skipped",
        "operation": "initialize + thread/list(limit=1)",
    }
    if not requested:
        return result
    if not codex_path:
        result.update({"ok": False, "status": "failed", "error": "codex missing"})
        return result
    try:
        with CodexAppServer(codex_bin=str(codex_path)) as codex:
            response = codex.list_threads(limit=1)
        data = response.get("data")
        result.update(
            {
                "ok": True,
                "status": "passed",
                "recent_readable": isinstance(data, list),
                "returned_threads": len(data) if isinstance(data, list) else None,
            }
        )
    except Exception as exc:
        result.update({"ok": False, "status": "failed", "error": str(exc)})
    return result


def _shared_daemon_check(codex_request: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ready": False,
        "ok": True,
        "required": False,
        "status": "app_unavailable",
        "app_transport": None,
        "app_connected": None,
        "app_state": None,
        "daemon_version": None,
        "socket_path": None,
        "probe_cli_path": None,
        "probe_cli_source": None,
        "probe_cli_version": None,
        "fallback_used": False,
        "version_mismatch": False,
        "warnings": [],
    }
    try:
        app = detect_running_codex_app()
        result["app_version"] = getattr(app, "version", None)
        result["app_build"] = getattr(app, "build", None)
        result["app_metadata_error"] = getattr(app, "metadata_error", None)
        observation = detect_local_app_transport(app.log_root, pid=app.pid)
    except AppRuntimeError as exc:
        if exc.error_code == "CODEX_APP_NOT_RUNNING":
            result.update(
                {
                    "ok": False,
                    "status": "app_not_running",
                    "setup_required": True,
                }
            )
        else:
            result.update(
                {
                    "ok": False,
                    "status": "transport_unobserved",
                }
            )
        result["error"] = str(exc)
        return result
    except DaemonProbeError as exc:
        result.update(
            {
                "ok": False,
                "status": "transport_unobserved",
                "error": str(exc),
            }
        )
        return result
    if observation is None:
        result.update(
            {
                "ok": False,
                "status": "transport_unobserved",
            }
        )
        return result
    result["app_transport"] = observation.transport
    connected = bool(getattr(observation, "connected", True))
    result["app_connected"] = connected
    result["app_state"] = getattr(observation, "state", None)
    result["ok"] = False
    if not connected:
        result["status"] = "app_disconnected"
        return result
    if observation.transport != "websocket":
        result["status"] = "app_stdio_sync_required"
        result["setup_required"] = True
        return result
    try:
        resolution: CompatibleDaemonCli = resolve_compatible_daemon_cli(
            str(codex_request or "codex"),
            app_path=getattr(app, "path", None),
            probe=probe_shared_daemon,
        )
        info = resolution.info
    except DaemonVersionError as exc:
        result["status"] = "daemon_version_invalid"
        result["error"] = str(exc)
        return result
    except DaemonSocketError as exc:
        result["status"] = "daemon_socket_invalid"
        result["error"] = str(exc)
        return result
    except DaemonProbeError as exc:
        result["status"] = "daemon_unavailable"
        result["error"] = str(exc)
        return result
    try:
        with CodexAppServer(
            codex_bin=str(resolution.path),
            transport="daemon",
            daemon_socket=info.socket_path,
        ):
            pass
    except Exception as exc:
        result["status"] = "daemon_handshake_failed"
        result["error"] = str(exc)
        return result
    version_mismatch = info.cli_version != info.app_server_version
    result.update(
        {
            "ready": True,
            "ok": True,
            "status": "ready",
            "daemon_version": info.app_server_version,
            "socket_path": str(info.socket_path),
            "probe_cli_path": str(resolution.path),
            "probe_cli_source": resolution.source,
            "probe_cli_version": info.cli_version,
            "fallback_used": resolution.fallback_used,
            "version_mismatch": version_mismatch,
            "warnings": (
                [
                    {
                        "code": "CODEX_DAEMON_VERSION_MISMATCH",
                        "message": (
                            "daemon CLI and app-server versions differ; "
                            "the live capability handshake succeeded"
                        ),
                        "cli_version": info.cli_version,
                        "app_server_version": info.app_server_version,
                    }
                ]
                if version_mismatch
                else []
            ),
        }
    )
    return result


def _app_paths() -> dict[str, Any]:
    candidates = {
        "codex": [
            Path("/Applications/Codex.app"),
            Path.home() / "Applications" / "Codex.app",
        ],
        "chatgpt": [
            Path("/Applications/ChatGPT.app"),
            Path.home() / "Applications" / "ChatGPT.app",
        ],
    }
    return {
        name: {
            "installed": any(path.is_dir() for path in paths),
            "detected_path": next(
                (str(path) for path in paths if path.is_dir()),
                None,
            ),
            "candidates": [str(path) for path in paths],
        }
        for name, paths in candidates.items()
    }
