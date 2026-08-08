"""Small JSON-RPC client for `codex app-server`.

The upgrade boundary is the external `codex app-server` command and its stdio
or managed-daemon transport.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # Defensive support for source runners without tomllib.
    tomllib = None  # type: ignore[assignment]

from . import __version__
from .daemon_transport import (
    DaemonProbeError,
    DaemonSocketError,
    DaemonVersionError,
    DaemonVersionInfo,
    UnixWebSocketConnection,
    WebSocketError,
    detect_local_app_transport,
    probe_shared_daemon,
)
from .app_runtime import AppRuntimeError, detect_running_codex_app
from .workspace import sibling_worktree_drift


class CodexRpcError(RuntimeError):
    """Raised for JSON-RPC errors or transport failures."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "CODEX_RPC_ERROR",
        retryable: bool = False,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "error": str(self),
            "retryable": self.retryable,
            **self.details,
        }


SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
APP_SERVER_TRANSPORTS = ("auto", "stdio", "daemon")
THREAD_LIST_SOURCE_KINDS = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)
DAEMON_INTERRUPT_CONFIRM_TIMEOUT = 10.0
DAEMON_TURN_START_RECOVERY_TIMEOUT = 5.0
DAEMON_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_INTERACTIVE_SERVER_REQUESTS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "mcpServer/elicitation/request",
    }
)
_READ_ONLY_COMMAND_ACTIONS = frozenset({"read", "listFiles", "search"})


@dataclass
class TurnResult:
    thread_id: str
    turn_id: str | None
    status: str | None
    final_text: str
    events: list[str]


@dataclass(frozen=True)
class SteerResult:
    thread_id: str
    turn_id: str
    client_message_id: str


@dataclass(frozen=True)
class ShellCommandResult:
    thread_id: str
    turn_id: str | None
    item_id: str | None
    status: str | None
    exit_code: int | None
    output: str
    receipt_observed: bool = False


@dataclass(frozen=True)
class CompatibleDaemonCli:
    """A Codex executable that successfully inspected the shared daemon."""

    path: Path
    source: str
    info: DaemonVersionInfo
    fallback_used: bool


def _command_may_write(item: dict[str, Any]) -> bool:
    actions = item.get("commandActions")
    if not isinstance(actions, list) or not actions:
        return True
    action_types = {
        str(action.get("type") or "")
        for action in actions
        if isinstance(action, dict)
    }
    return (
        not action_types
        or not action_types.issubset(_READ_ONLY_COMMAND_ACTIONS)
    )


def _workspace_binding_violation(
    item: dict[str, Any],
    *,
    configured_cwd: str | None,
) -> dict[str, Any] | None:
    if not configured_cwd:
        return None

    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        if not _command_may_write(item):
            return None
        observed_cwd = item.get("cwd")
        if not observed_cwd:
            return None
        drift = sibling_worktree_drift(configured_cwd, str(observed_cwd))
        if drift is None:
            return None
        actions = item.get("commandActions")
        return {
            **drift,
            "item_type": item_type,
            "item_id": item.get("id"),
            "observed_cwd": str(Path(str(observed_cwd)).expanduser().resolve()),
            "command": str(item.get("command") or "")[:2000],
            "command_action_types": sorted(
                {
                    str(action.get("type") or "")
                    for action in actions
                    if isinstance(action, dict)
                }
            )
            if isinstance(actions, list)
            else [],
        }

    if item_type != "fileChange":
        return None
    changes = item.get("changes")
    if not isinstance(changes, list):
        return None
    for change in changes:
        if not isinstance(change, dict) or not change.get("path"):
            continue
        raw_path = Path(str(change["path"])).expanduser()
        observed_path = (
            raw_path if raw_path.is_absolute() else Path(configured_cwd) / raw_path
        ).resolve()
        drift = sibling_worktree_drift(configured_cwd, observed_path)
        if drift is not None:
            return {
                **drift,
                "item_type": item_type,
                "item_id": item.get("id"),
                "observed_path": str(observed_path),
                "change_kind": change.get("kind"),
            }
    return None


class CodexAppServer:
    """One client connection for Codex App Server JSON-RPC."""

    def __init__(
        self,
        codex_bin: str = "codex",
        *,
        profile: str | None = None,
        extra_env: dict[str, str] | None = None,
        config_overrides: list[str] | None = None,
        transport: str | None = None,
        daemon_socket: str | os.PathLike[str] | None = None,
    ) -> None:
        self._next_id = 0
        self._request_id_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._server_request_lock = threading.Lock()
        self._pending_responses: dict[
            int, queue.Queue[dict[str, Any]]
        ] = {}
        self._owned_turn_ids: set[str] = set()
        self._deferred_server_requests: dict[
            int | str, dict[str, Any]
        ] = {}
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._closed = False
        self._reader_error: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._websocket: UnixWebSocketConnection | None = None
        self._thread_config: dict[str, Any] = {}

        selected_transport, socket_path = resolve_app_server_transport(
            codex_bin,
            requested=transport,
            daemon_socket=daemon_socket,
        )
        self.transport = selected_transport
        if selected_transport == "daemon" and profile:
            raise CodexRpcError(
                "shared daemon transport cannot apply a per-command Codex profile",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
                profile=profile,
            )

        if selected_transport == "daemon":
            self._thread_config = thread_config_from_overrides(
                config_overrides or [],
                extra_env=extra_env,
            )
            assert socket_path is not None
            try:
                self._websocket = UnixWebSocketConnection.connect(
                    socket_path,
                    resource="/rpc",
                    max_message_bytes=DAEMON_MAX_MESSAGE_BYTES,
                )
            except DaemonSocketError as exc:
                raise CodexRpcError(
                    f"could not connect to the shared Codex daemon: {exc}",
                    error_code="CODEX_DAEMON_SOCKET_INVALID",
                    retryable=True,
                ) from exc
            except WebSocketError as exc:
                raise CodexRpcError(
                    f"could not connect to the shared Codex daemon: {exc}",
                    error_code="CODEX_DAEMON_UNAVAILABLE",
                    retryable=True,
                ) from exc
            threading.Thread(
                target=self._read_websocket,
                daemon=True,
            ).start()
        else:
            self._start_stdio(
                codex_bin,
                profile=profile,
                extra_env=extra_env,
                config_overrides=config_overrides or [],
            )

        try:
            initialize_result = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agent-lane",
                        "title": "Agent Lane",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=15.0,
            )
            self.server_identity = initialize_result
            self.notify("initialized")
        except Exception:
            self.close()
            raise

    def _start_stdio(
        self,
        codex_bin: str,
        *,
        profile: str | None,
        extra_env: dict[str, str] | None,
        config_overrides: list[str],
    ) -> None:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        env.setdefault("RUST_LOG", "warn")
        command = app_server_command(
            codex_bin,
            config_overrides,
            profile=profile,
        )
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._websocket is not None:
            self._websocket.close()
            return
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        with self._request_id_lock:
            self._next_id += 1
            request_id = self._next_id
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending_responses[request_id] = response_queue
        try:
            self._send(
                {"id": request_id, "method": method, "params": params or {}}
            )
        except Exception:
            with self._pending_lock:
                self._pending_responses.pop(request_id, None)
            raise
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self._transport_exited():
                    if getattr(self, "transport", "stdio") == "daemon":
                        raise CodexRpcError(
                            f"shared daemon connection closed during {method}",
                            error_code="CODEX_DAEMON_UNAVAILABLE",
                            retryable=True,
                            method=method,
                        )
                    raise CodexRpcError(
                        f"codex app-server exited during {method}; "
                        f"stderr={self.stderr_tail()}"
                    )
                try:
                    msg = response_queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                if "error" in msg:
                    error = msg.get("error")
                    error_obj = error if isinstance(error, dict) else {}
                    rpc_code = error_obj.get("code")
                    retryable = rpc_code == -32001
                    raise CodexRpcError(
                        f"{method} failed: "
                        f"{json.dumps(error, ensure_ascii=False)}",
                        error_code=(
                            "CODEX_APP_SERVER_OVERLOADED"
                            if retryable
                            else "CODEX_RPC_ERROR"
                        ),
                        retryable=retryable,
                        rpc_code=rpc_code,
                        rpc_data=error_obj.get("data"),
                    )
                result = msg.get("result")
                return result if isinstance(result, dict) else {}
            raise TimeoutError(
                f"{method} timed out; stderr={self.stderr_tail()}"
            )
        finally:
            with self._pending_lock:
                self._pending_responses.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def start_thread(
        self,
        cwd: str,
        sandbox: str | None = None,
        *,
        model: str | None = None,
        runtime_workspace_roots: list[str] | None = None,
    ) -> str:
        params: dict[str, Any] = {"cwd": cwd}
        if sandbox:
            params["sandbox"] = normalize_sandbox_mode(sandbox)
        if model:
            params["model"] = model
        if runtime_workspace_roots:
            params["runtimeWorkspaceRoots"] = runtime_workspace_roots
        thread_config = getattr(self, "_thread_config", {})
        if thread_config:
            params["config"] = deepcopy(thread_config)
        result = self.request("thread/start", params, timeout=20.0)
        return _extract_thread_id(result)

    def resume_thread(
        self,
        thread_id: str,
        cwd: str | None = None,
        sandbox: str | None = None,
        *,
        model: str | None = None,
        runtime_workspace_roots: list[str] | None = None,
        apply_config: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
        }
        if getattr(self, "transport", "stdio") == "daemon":
            params["excludeTurns"] = True
        if cwd:
            params["cwd"] = cwd
        if sandbox:
            params["sandbox"] = normalize_sandbox_mode(sandbox)
        if model:
            params["model"] = model
        if runtime_workspace_roots:
            params["runtimeWorkspaceRoots"] = runtime_workspace_roots
        thread_config = getattr(self, "_thread_config", {})
        if apply_config and thread_config:
            params["config"] = deepcopy(thread_config)
        return self.request("thread/resume", params, timeout=30.0)

    def read_thread(
        self, thread_id: str, include_turns: bool = False
    ) -> dict[str, Any]:
        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            timeout=20.0,
        )

    def wait_thread_idle(
        self,
        thread_id: str,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> dict[str, Any]:
        """Wait until app-server reports that a thread has no active turn."""

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

        deadline = time.monotonic() + timeout
        last_status: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = self.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
                timeout=min(remaining, 2.0),
            )
            thread = result.get("thread") or {}
            raw_status = thread.get("status")
            status = (
                str(raw_status.get("type") or "")
                if isinstance(raw_status, dict)
                else str(raw_status or "")
            )
            last_status = status or None
            if status == "idle":
                return result
            if status in {"systemError", "notLoaded"}:
                raise CodexRpcError(
                    f"Codex thread entered {status} while waiting for idle",
                    error_code=(
                        "CODEX_THREAD_SYSTEM_ERROR"
                        if status == "systemError"
                        else "CODEX_THREAD_NOT_LOADED"
                    ),
                    retryable=status == "notLoaded",
                    thread_id=thread_id,
                    thread_status=status,
                )
            time.sleep(min(poll_interval, max(0.0, remaining)))
        raise TimeoutError(
            f"thread did not become idle after {timeout}s "
            f"for thread {thread_id}; last status: {last_status or 'unknown'}"
        )

    def archive_thread(self, thread_id: str) -> None:
        self.request(
            "thread/archive",
            {"threadId": thread_id},
            timeout=20.0,
        )

    def list_loaded_thread_ids(self) -> set[str]:
        loaded: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/loaded/list", params, timeout=20.0)
            for value in result.get("data") or []:
                thread_id = str(value).strip()
                if thread_id:
                    loaded.add(thread_id)
            raw_cursor = result.get("nextCursor")
            next_cursor = str(raw_cursor).strip() if raw_cursor else None
            if not next_cursor or next_cursor in seen_cursors:
                return loaded
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def run_thread_shell_command(
        self,
        thread_id: str,
        command: str,
        *,
        timeout: float = 30.0,
        success_receipt: tuple[Path, str] | None = None,
    ) -> ShellCommandResult:
        """Run one fixed shell command and wait for its command item receipt."""

        self.request(
            "thread/shellCommand",
            {"threadId": thread_id, "command": command},
            timeout=min(timeout, 20.0),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._transport_exited():
                raise CodexRpcError(
                    "app-server connection closed during thread/shellCommand",
                    error_code=(
                        "CODEX_DAEMON_UNAVAILABLE"
                        if getattr(self, "transport", "stdio") == "daemon"
                        else "CODEX_RPC_ERROR"
                    ),
                    retryable=True,
                    thread_id=thread_id,
                )
            if success_receipt is not None:
                receipt_path, expected_receipt = success_receipt
                try:
                    receipt_text = receipt_path.read_text(encoding="utf-8")
                except (FileNotFoundError, OSError):
                    receipt_text = ""
                if receipt_text.strip() == expected_receipt:
                    return ShellCommandResult(
                        thread_id=thread_id,
                        turn_id=None,
                        item_id=None,
                        status="completed",
                        exit_code=0,
                        output=f"{expected_receipt}\n",
                        receipt_observed=True,
                    )
            try:
                msg = self._notifications.get(timeout=0.25)
            except queue.Empty:
                continue
            if msg.get("method") != "item/completed":
                continue
            params = msg.get("params") or {}
            if str(params.get("threadId") or "") != thread_id:
                continue
            item = params.get("item") or {}
            if (
                item.get("type") != "commandExecution"
                or item.get("source") != "userShell"
                or item.get("command") != command
            ):
                continue
            raw_exit_code = item.get("exitCode")
            exit_code = (
                int(raw_exit_code)
                if isinstance(raw_exit_code, int)
                else None
            )
            return ShellCommandResult(
                thread_id=thread_id,
                turn_id=(
                    str(params.get("turnId"))
                    if params.get("turnId") is not None
                    else None
                ),
                item_id=(
                    str(item.get("id"))
                    if item.get("id") is not None
                    else None
                ),
                status=(
                    str(item.get("status"))
                    if item.get("status") is not None
                    else None
                ),
                exit_code=exit_code,
                output=str(item.get("aggregatedOutput") or ""),
            )
        raise TimeoutError(
            f"thread/shellCommand timed out after {timeout}s "
            f"for thread {thread_id}"
        )

    def list_threads(
        self,
        *,
        limit: int = 20,
        search_term: str | None = None,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": list(THREAD_LIST_SOURCE_KINDS),
        }
        if search_term:
            params["searchTerm"] = search_term
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        return self.request("thread/list", params, timeout=30.0)

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
            timeout=20.0,
        )

    def update_git_info(
        self,
        thread_id: str,
        git_info: dict[str, str],
    ) -> None:
        if not git_info:
            return
        self.request(
            "thread/metadata/update",
            {"threadId": thread_id, "gitInfo": git_info},
            timeout=20.0,
        )

    def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        result = self.request(
            "thread/goal/get",
            {"threadId": thread_id},
            timeout=20.0,
        )
        goal = result.get("goal")
        return goal if isinstance(goal, dict) else None

    def set_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        if token_budget is not None:
            params["tokenBudget"] = token_budget
        return self.request("thread/goal/set", params, timeout=20.0)

    def clear_goal(self, thread_id: str) -> dict[str, Any]:
        return self.request(
            "thread/goal/clear",
            {"threadId": thread_id},
            timeout=20.0,
        )

    def steer_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        expected_turn_id: str,
        timeout: float = 20.0,
    ) -> SteerResult:
        """Add one user message to an active daemon turn without owning it."""

        if getattr(self, "transport", "stdio") != "daemon":
            raise CodexRpcError(
                "turn steering requires the shared Codex daemon",
                error_code="CODEX_STEER_REQUIRES_SHARED_DAEMON",
                retryable=True,
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
            )
        client_message_id = f"agent-lane-steer-{uuid.uuid4()}"
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "expectedTurnId": expected_turn_id,
            "clientUserMessageId": client_message_id,
        }
        try:
            response = self.request("turn/steer", params, timeout=timeout)
        except TimeoutError as exc:
            raise CodexRpcError(
                "turn/steer timed out after submission; delivery is uncertain",
                error_code="CODEX_STEER_STATE_UNCERTAIN",
                retryable=False,
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                client_user_message_id=client_message_id,
            ) from exc
        except CodexRpcError as exc:
            rpc_code = exc.details.get("rpc_code")
            if exc.error_code == "CODEX_APP_SERVER_OVERLOADED":
                raise CodexRpcError(
                    str(exc),
                    error_code=exc.error_code,
                    retryable=True,
                    thread_id=thread_id,
                    expected_turn_id=expected_turn_id,
                    client_user_message_id=client_message_id,
                    **exc.details,
                ) from exc
            if rpc_code is None:
                raise CodexRpcError(
                    "turn/steer transport failed after submission; delivery is uncertain",
                    error_code="CODEX_STEER_STATE_UNCERTAIN",
                    retryable=False,
                    thread_id=thread_id,
                    expected_turn_id=expected_turn_id,
                    client_user_message_id=client_message_id,
                    cause_code=exc.error_code,
                ) from exc
            error_code = (
                "CODEX_STEER_UNSUPPORTED"
                if rpc_code == -32601
                else "CODEX_STEER_REJECTED"
            )
            raise CodexRpcError(
                str(exc),
                error_code=error_code,
                retryable=False,
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                client_user_message_id=client_message_id,
                **exc.details,
            ) from exc

        returned_turn_id = str(response.get("turnId") or "").strip()
        if returned_turn_id != expected_turn_id:
            raise CodexRpcError(
                "turn/steer response did not confirm the expected active turn",
                error_code="CODEX_STEER_RESPONSE_INVALID",
                retryable=False,
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                returned_turn_id=returned_turn_id or None,
                client_user_message_id=client_message_id,
            )
        return SteerResult(
            thread_id=thread_id,
            turn_id=returned_turn_id,
            client_message_id=client_message_id,
        )

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        sandbox: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        workspace_cwd: str | None = None,
        runtime_workspace_roots: list[str] | None = None,
        additional_context: dict[str, dict[str, str]] | None = None,
        timeout: float | None = None,
        on_started: Callable[[str | None], None] | None = None,
    ) -> TurnResult:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        daemon_transport = getattr(self, "transport", "stdio") == "daemon"
        client_message_id: str | None = None
        if daemon_transport:
            client_message_id = f"agent-lane-{uuid.uuid4()}"
            params["clientUserMessageId"] = client_message_id
        if sandbox:
            params["sandboxPolicy"] = sandbox_policy(sandbox)
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if runtime_workspace_roots:
            params["runtimeWorkspaceRoots"] = runtime_workspace_roots
        if additional_context:
            params["additionalContext"] = deepcopy(additional_context)
        try:
            started = self.request(
                "turn/start",
                params,
                timeout=20.0,
            )
        except TimeoutError:
            if daemon_transport:
                self._recover_timed_out_turn_start(
                    thread_id=thread_id,
                    client_message_id=client_message_id,
                )
            raise
        except CodexRpcError as exc:
            if daemon_transport and exc.error_code == "CODEX_DAEMON_UNAVAILABLE":
                raise CodexRpcError(
                    "daemon connection closed while turn/start state was uncertain",
                    error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
                    retryable=True,
                    thread_id=thread_id,
                    client_message_id=client_message_id,
                ) from exc
            raise
        turn_id = (started.get("turn") or {}).get("id")
        if daemon_transport and turn_id is None:
            raise CodexRpcError(
                "daemon turn/start response omitted the turn id",
                error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
                retryable=True,
                thread_id=thread_id,
                client_message_id=client_message_id,
            )
        if turn_id is not None:
            self._register_owned_turn(str(turn_id))
        if on_started:
            on_started(str(turn_id) if turn_id is not None else None)
        final_text = ""
        status: str | None = None
        events: list[str] = []
        deadline = time.monotonic() + timeout if timeout is not None else None
        while deadline is None or time.monotonic() < deadline:
            if self._transport_exited():
                if daemon_transport:
                    raise CodexRpcError(
                        "daemon connection closed while the turn state was uncertain",
                        error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
                        retryable=True,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                raise CodexRpcError(
                    f"codex app-server exited mid-turn; stderr={self.stderr_tail(30)}"
                )
            try:
                msg = self._notifications.get(timeout=0.25)
            except queue.Empty:
                continue
            method = msg.get("method")
            if isinstance(method, str):
                events.append(method)
            params = msg.get("params") or {}
            if method in _INTERACTIVE_SERVER_REQUESTS:
                if not _notification_matches_turn(
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    strict=daemon_transport,
                ):
                    continue
                try:
                    if daemon_transport:
                        self._interrupt_timed_out_turn(
                            thread_id=thread_id,
                            turn_id=(
                                str(turn_id)
                                if turn_id is not None
                                else None
                            ),
                            events=events,
                        )
                finally:
                    if turn_id is not None:
                        self._release_owned_turn(str(turn_id))
                raise CodexRpcError(
                    "Codex requested interactive approval or input, but "
                    "agent-lane has no interactive reviewer; preserve or "
                    "configure the thread's native approval routing before retrying",
                    error_code="CODEX_INTERACTION_REQUIRED",
                    retryable=False,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    request_method=method,
                )
            if method in {"item/started", "item/completed"}:
                if _notification_matches_turn(
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    strict=daemon_transport,
                ):
                    item = params.get("item") or {}
                    violation = (
                        _workspace_binding_violation(
                            item,
                            configured_cwd=workspace_cwd,
                        )
                        if isinstance(item, dict)
                        else None
                    )
                    if violation is not None:
                        try:
                            self._interrupt_timed_out_turn(
                                thread_id=thread_id,
                                turn_id=(
                                    str(turn_id)
                                    if turn_id is not None
                                    else None
                                ),
                                events=events,
                            )
                        finally:
                            if turn_id is not None:
                                self._release_owned_turn(str(turn_id))
                        raise CodexRpcError(
                            "Codex attempted a potentially mutating operation "
                            "in another worktree of the lane repository",
                            error_code="CODEX_WORKSPACE_BINDING_DRIFT",
                            retryable=False,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            configured_cwd=workspace_cwd,
                            rebind_required=True,
                            changes_may_exist=True,
                            **violation,
                        )
            if method == "item/completed":
                if not _notification_matches_turn(
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    strict=daemon_transport,
                ):
                    continue
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    final_text = item.get("text") or final_text
            if method == "turn/completed":
                if not _notification_matches_turn(
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    strict=daemon_transport,
                ):
                    continue
                turn = params.get("turn") or {}
                status = turn.get("status")
                if turn_id is not None:
                    self._release_owned_turn(str(turn_id))
                return TurnResult(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status=status,
                    final_text=final_text,
                    events=events,
                )
        if daemon_transport:
            self._interrupt_timed_out_turn(
                thread_id=thread_id,
                turn_id=str(turn_id) if turn_id is not None else None,
                events=events,
            )
            if turn_id is not None:
                self._release_owned_turn(str(turn_id))
        assert timeout is not None
        raise TimeoutError(
            f"turn timed out after {timeout}s; events={events[-20:]}; "
            f"stderr={self.stderr_tail(30)}"
        )

    def stderr_tail(self, n: int = 12) -> list[str]:
        return self._stderr[-n:]

    def _send(self, obj: dict[str, Any]) -> None:
        if self._closed:
            raise CodexRpcError("codex app-server client is closed")
        with self._send_lock:
            if self._websocket is not None:
                try:
                    self._websocket.send_json(obj)
                except WebSocketError as exc:
                    self._reader_error = str(exc)
                    raise CodexRpcError(
                        f"shared daemon transport write failed: {exc}",
                        error_code="CODEX_DAEMON_UNAVAILABLE",
                        retryable=True,
                    ) from exc
                return
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise CodexRpcError("codex app-server stdin is not available")
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._stderr.append(f"<non-json stdout> {line[:300]}")
                continue
            self._route_message(msg)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.rstrip())

    def _read_websocket(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            while not self._closed:
                msg = websocket.recv_json()
                if msg is None:
                    self._reader_error = "shared daemon closed the WebSocket"
                    return
                if not isinstance(msg, dict):
                    self._stderr.append("<non-object websocket message>")
                    continue
                self._route_message(msg)
        except WebSocketError as exc:
            if not self._closed:
                self._reader_error = str(exc)
                self._stderr.append(f"<websocket> {exc}")

    def _route_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if isinstance(method, str) and "id" in msg:
            self._handle_server_request(msg)
            return
        if isinstance(method, str):
            if method == "serverRequest/resolved":
                params = msg.get("params")
                request_id = (
                    params.get("requestId")
                    if isinstance(params, dict)
                    else None
                )
                if isinstance(request_id, (int, str)):
                    with self._server_request_lock:
                        self._deferred_server_requests.pop(request_id, None)
            self._notifications.put(msg)
            return
        request_id = msg.get("id")
        if not isinstance(request_id, int):
            self._stderr.append(
                f"<unroutable json-rpc message> {json.dumps(msg)[:300]}"
            )
            return
        with self._pending_lock:
            target = self._pending_responses.get(request_id)
        if target is None:
            self._stderr.append(
                f"<orphan json-rpc response id={request_id}>"
            )
            return
        try:
            target.put_nowait(msg)
        except queue.Full:
            self._stderr.append(
                f"<duplicate json-rpc response id={request_id}>"
            )

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Resolve only requests belonging to a turn started by this client."""

        request_id = msg.get("id")
        method = msg.get("method")
        if not isinstance(request_id, (int, str)) or not isinstance(method, str):
            self._stderr.append("<invalid server json-rpc request>")
            return
        self._notifications.put(msg)
        params = msg.get("params")
        turn_id = params.get("turnId") if isinstance(params, dict) else None
        with self._server_request_lock:
            owned = (
                turn_id is not None
                and str(turn_id) in self._owned_turn_ids
            )
            if not owned:
                self._deferred_server_requests[request_id] = msg
                return
        self._respond_to_server_request(msg)

    def _respond_to_server_request(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("id")
        method = msg.get("method")
        if not isinstance(request_id, (int, str)) or not isinstance(method, str):
            return
        if method in _INTERACTIVE_SERVER_REQUESTS:
            response = {
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": (
                        "interactive request requires a reviewer; agent-lane "
                        "does not approve, decline, or synthesize input"
                    ),
                    "data": {"errorCode": "CODEX_INTERACTION_REQUIRED"},
                },
            }
        elif method == "currentTime/read":
            response = {
                "id": request_id,
                "result": {"currentTimeAt": int(time.time())},
            }
        else:
            response = {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": (
                        "agent-lane cannot service interactive server request "
                        f"{method}"
                    ),
                },
            }
        try:
            self._send(response)
        except Exception as exc:
            self._reader_error = str(exc)
            self._stderr.append(
                f"<server request response failed method={method}> {exc}"
            )

    def _register_owned_turn(self, turn_id: str) -> None:
        ready: list[dict[str, Any]] = []
        with self._server_request_lock:
            self._owned_turn_ids.add(turn_id)
            for request_id, msg in list(
                self._deferred_server_requests.items()
            ):
                params = msg.get("params")
                request_turn_id = (
                    params.get("turnId")
                    if isinstance(params, dict)
                    else None
                )
                if request_turn_id is None or str(request_turn_id) != turn_id:
                    continue
                ready.append(msg)
                self._deferred_server_requests.pop(request_id, None)
        for msg in ready:
            self._respond_to_server_request(msg)

    def _release_owned_turn(self, turn_id: str) -> None:
        with self._server_request_lock:
            self._owned_turn_ids.discard(turn_id)

    def _interrupt_timed_out_turn(
        self,
        *,
        thread_id: str,
        turn_id: str | None,
        events: list[str],
    ) -> None:
        """Stop a daemon-owned turn before reporting a local timeout."""

        interrupt_error: Exception | None = None
        if turn_id is not None:
            try:
                self.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=10.0,
                )
            except Exception as exc:
                interrupt_error = exc

        deadline = time.monotonic() + DAEMON_INTERRUPT_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            try:
                msg = self._notifications.get(timeout=0.25)
            except queue.Empty:
                continue
            method = msg.get("method")
            if isinstance(method, str):
                events.append(method)
            if method != "turn/completed":
                continue
            params = msg.get("params") or {}
            if not _notification_matches_turn(
                params,
                thread_id=thread_id,
                turn_id=turn_id,
                strict=True,
            ):
                continue
            turn = params.get("turn") or {}
            if str(turn.get("status") or "") in {
                "completed",
                "failed",
                "interrupted",
            }:
                return

        observed_status = None
        try:
            recent = self.request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 20,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
                timeout=10.0,
            )
            for turn in recent.get("data") or []:
                if not isinstance(turn, dict):
                    continue
                if turn_id is not None and str(turn.get("id")) != turn_id:
                    continue
                observed_status = str(turn.get("status") or "")
                if observed_status in {"completed", "failed", "interrupted"}:
                    return
        except Exception:
            pass

        raise CodexRpcError(
            "the daemon turn timed out and a terminal interruption was not verified",
            error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
            retryable=True,
            thread_id=thread_id,
            turn_id=turn_id,
            observed_status=observed_status,
            interrupt_error=(
                str(interrupt_error) if interrupt_error is not None else None
            ),
        )

    def _recover_timed_out_turn_start(
        self,
        *,
        thread_id: str,
        client_message_id: str | None,
    ) -> None:
        """Find and stop a daemon turn whose start response was lost."""

        deadline = time.monotonic() + DAEMON_TURN_START_RECOVERY_TIMEOUT
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                turn_id, status = self._find_turn_by_client_message_id(
                    thread_id,
                    client_message_id,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
                continue
            if turn_id is None:
                time.sleep(0.25)
                continue
            if status in {"completed", "failed", "interrupted"}:
                return
            self._register_owned_turn(turn_id)
            self._interrupt_timed_out_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                events=[],
            )
            return

        raise CodexRpcError(
            "turn/start timed out and the daemon-owned turn could not be identified",
            error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
            retryable=True,
            thread_id=thread_id,
            client_message_id=client_message_id,
            recovery_error=str(last_error) if last_error is not None else None,
        )

    def _find_turn_by_client_message_id(
        self,
        thread_id: str,
        client_message_id: str | None,
    ) -> tuple[str | None, str | None]:
        if client_message_id is None:
            return None, None
        turns = self.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 10,
                "sortDirection": "desc",
                "itemsView": "full",
            },
            timeout=10.0,
        )
        for turn in turns.get("data") or []:
            if not isinstance(turn, dict) or turn.get("id") is None:
                continue
            for item in turn.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("type") == "userMessage"
                    and item.get("clientId") == client_message_id
                ):
                    return str(turn["id"]), str(turn.get("status") or "")
        return None, None

    def _transport_exited(self) -> bool:
        websocket = getattr(self, "_websocket", None)
        if websocket is not None:
            return websocket.closed or getattr(self, "_reader_error", None) is not None
        proc = getattr(self, "_proc", None)
        return proc is None or proc.poll() is not None


def _extract_thread_id(result: dict[str, Any]) -> str:
    thread = result.get("thread") or {}
    thread_id = (
        thread.get("id")
        or thread.get("sessionId")
        or result.get("threadId")
        or result.get("sessionId")
    )
    if not thread_id:
        raise CodexRpcError(
            "codex thread response did not include a thread id: "
            f"{json.dumps(result, ensure_ascii=False)[:800]}"
        )
    return str(thread_id)


def _notification_matches_turn(
    params: dict[str, Any],
    *,
    thread_id: str,
    turn_id: str | None,
    strict: bool = False,
) -> bool:
    """Ignore explicit notifications for another shared-daemon task or turn."""

    event_thread_id = params.get("threadId")
    if strict and event_thread_id is None:
        return False
    if event_thread_id is not None and str(event_thread_id) != thread_id:
        return False
    event_turn_id = params.get("turnId")
    turn = params.get("turn")
    if event_turn_id is None and isinstance(turn, dict):
        event_turn_id = turn.get("id")
    if strict and (event_turn_id is None or turn_id is None):
        return False
    if (
        event_turn_id is not None
        and turn_id is not None
        and str(event_turn_id) != str(turn_id)
    ):
        return False
    return True


def app_server_command(
    codex_bin: str,
    config_overrides: list[str],
    *,
    profile: str | None = None,
) -> list[str]:
    command = [codex_bin]
    if profile:
        command.extend(["--profile", profile])
    command.append("app-server")
    for override in config_overrides:
        command.extend(["-c", override])
    return command


def resolve_compatible_daemon_cli(
    codex_bin: str = "codex",
    *,
    app_path: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    which_command: Callable[[str], str | None] | None = None,
    probe: Callable[[str], DaemonVersionInfo] | None = None,
) -> CompatibleDaemonCli:
    """Select a CLI that can inspect the running app-server daemon.

    Reported CLI and app-server versions are diagnostics, not an admission
    gate. The first available candidate must complete the daemon version probe;
    socket safety and the later WebSocket ``initialize`` exchange remain the
    compatibility boundary.
    """

    which_fn = shutil.which if which_command is None else which_command
    probe_fn = probe_shared_daemon if probe is None else probe
    requested = str(codex_bin).strip() or "codex"
    automatic = requested == "codex"
    candidates: list[tuple[Path, str]] = []

    def add_candidate(
        path: str | os.PathLike[str] | None,
        source: str,
        *,
        require_executable: bool = True,
    ) -> None:
        if path is None:
            return
        candidate = Path(path).expanduser()
        if require_executable and not _is_executable_file(candidate):
            return
        try:
            identity = candidate.resolve(strict=True)
        except OSError:
            identity = candidate.absolute()
        if any(existing.resolve(strict=False) == identity for existing, _ in candidates):
            return
        candidates.append((candidate, source))

    if automatic:
        add_candidate(which_fn("codex"), "path")
        candidate_home = (
            Path.home() if home is None else Path(home).expanduser()
        )
        managed_root = candidate_home / ".codex" / "packages" / "standalone"
        managed = managed_root / "current" / "codex"
        if _is_safe_managed_cli(managed, managed_root):
            add_candidate(managed, "managed_standalone")
        if app_path is not None:
            app_root = Path(app_path).expanduser()
            bundled_cli = _resolve_safe_app_bundle_cli(
                app_root / "Contents" / "Resources" / "codex",
                app_root,
            )
            add_candidate(bundled_cli, "app_bundle")
    else:
        resolved = which_fn(requested)
        add_candidate(
            resolved or requested,
            "explicit",
            require_executable=False,
        )

    if not candidates:
        raise DaemonProbeError(
            "no Codex CLI executable is available for the shared daemon probe"
        )

    candidate, source = candidates[0]
    info = probe_fn(os.fspath(candidate))
    return CompatibleDaemonCli(
        path=candidate,
        source=source,
        info=info,
        fallback_used=False,
    )


def _is_executable_file(path: Path) -> bool:
    try:
        details = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and os.access(path, os.X_OK)


def _is_safe_managed_cli(path: Path, managed_root: Path) -> bool:
    if not _is_executable_file(path):
        return False
    try:
        resolved = path.resolve(strict=True)
        root = managed_root.resolve(strict=True)
        resolved.relative_to(root)
        details = resolved.stat()
    except (OSError, ValueError):
        return False
    return details.st_uid == os.getuid()


def _resolve_safe_app_bundle_cli(
    path: Path,
    app_root: Path,
) -> Path | None:
    if not _is_executable_file(path):
        return None
    try:
        resolved = path.resolve(strict=True)
        root = app_root.resolve(strict=True)
        resolved.relative_to(root)
        root_details = root.stat()
        details = resolved.stat()
    except (OSError, ValueError):
        return None
    trusted_owners = {0, os.getuid()}
    if root_details.st_uid not in trusted_owners:
        return None
    if details.st_uid != root_details.st_uid:
        return None
    return resolved


def resolve_app_server_transport(
    codex_bin: str,
    *,
    requested: str | None = None,
    daemon_socket: str | os.PathLike[str] | None = None,
) -> tuple[str, Path | None]:
    """Choose stdio or the App's verified shared daemon transport."""

    raw = requested
    if raw is None:
        raw = os.environ.get("AGENT_LANE_CODEX_TRANSPORT", "auto")
    transport = str(raw).strip().casefold()
    if transport not in APP_SERVER_TRANSPORTS:
        choices = ", ".join(APP_SERVER_TRANSPORTS)
        raise CodexRpcError(
            f"unsupported app-server transport {raw!r}; choose {choices}",
            error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
        )
    if daemon_socket is not None:
        if transport == "stdio":
            raise CodexRpcError(
                "daemon_socket cannot be combined with stdio transport",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
            )
        return "daemon", Path(daemon_socket).expanduser()
    if transport == "stdio":
        return "stdio", None

    observation = None
    try:
        app = detect_running_codex_app()
        observation = detect_local_app_transport(
            app.log_root,
            pid=app.pid,
        )
    except AppRuntimeError as exc:
        if transport == "auto" and exc.error_code == "CODEX_APP_NOT_RUNNING":
            return "stdio", None
        raise CodexRpcError(
            "the running Codex App transport could not be observed; "
            "refusing to assume stdio",
            error_code="CODEX_APP_TRANSPORT_UNOBSERVED",
            retryable=True,
            cause_code=exc.error_code,
        ) from exc
    except DaemonProbeError as exc:
        raise CodexRpcError(
            "the running Codex App transport could not be observed; "
            "refusing to assume stdio",
            error_code="CODEX_APP_TRANSPORT_UNOBSERVED",
            retryable=True,
            cause_type=type(exc).__name__,
        ) from exc

    if observation is None:
        raise CodexRpcError(
            "the running Codex App transport could not be observed; "
            "refusing to assume stdio",
            error_code="CODEX_APP_TRANSPORT_UNOBSERVED",
            retryable=True,
        )
    if observation.transport != "websocket":
        if transport == "daemon":
            raise CodexRpcError(
                "the running Codex App uses a separate stdio app-server",
                error_code="CODEX_APP_NOT_ON_SHARED_DAEMON",
                retryable=True,
                observed_app_transport=(
                    observation.transport if observation is not None else None
                ),
                observed_app_state=(
                    getattr(observation, "state", None)
                    if observation is not None
                    else None
                ),
            )
        return "stdio", None
    if not getattr(observation, "connected", True):
        raise CodexRpcError(
            "the running Codex App is configured for the shared daemon "
            "but is currently disconnected",
            error_code="CODEX_APP_SHARED_DAEMON_DISCONNECTED",
            retryable=True,
            observed_app_transport=observation.transport,
            observed_app_state=getattr(observation, "state", None),
        )

    try:
        resolution = resolve_compatible_daemon_cli(
            codex_bin,
            app_path=getattr(app, "path", None),
            probe=probe_shared_daemon,
        )
        info = resolution.info
    except DaemonSocketError as exc:
        raise CodexRpcError(
            str(exc),
            error_code="CODEX_DAEMON_SOCKET_INVALID",
            retryable=True,
        ) from exc
    except DaemonVersionError as exc:
        raise CodexRpcError(
            str(exc),
            error_code="CODEX_DAEMON_VERSION_INVALID",
            retryable=True,
        ) from exc
    except DaemonProbeError as exc:
        raise CodexRpcError(
            str(exc),
            error_code="CODEX_DAEMON_UNAVAILABLE",
            retryable=True,
        ) from exc
    return "daemon", info.socket_path


def thread_config_from_overrides(
    config_overrides: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert process-level CLI overrides into thread-scoped daemon config."""

    merged: dict[str, Any] = {}
    for override in config_overrides:
        text = str(override).strip()
        if "=" not in text:
            raise CodexRpcError(
                "Codex config override requires KEY=VALUE",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
            )
        if tomllib is None:
            parsed = _parse_basic_config_override(text)
        else:
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                key, raw_value = text.split("=", 1)
                try:
                    parsed = tomllib.loads(
                        f"{key.strip()} = {json.dumps(raw_value.strip())}"
                    )
                except tomllib.TOMLDecodeError as fallback_exc:
                    raise CodexRpcError(
                        f"could not map config override into thread config: {text!r}",
                        error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
                    ) from fallback_exc
        _merge_config_dict(merged, parsed)

    if extra_env:
        policy = merged.setdefault("shell_environment_policy", {})
        if not isinstance(policy, dict):
            raise CodexRpcError(
                "shell_environment_policy must be a config object",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
            )
        configured = policy.setdefault("set", {})
        if not isinstance(configured, dict):
            raise CodexRpcError(
                "shell_environment_policy.set must be a config object",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
            )
        configured.update({str(key): str(value) for key, value in extra_env.items()})
    return merged


def _parse_basic_config_override(text: str) -> dict[str, Any]:
    """Parse the CLI's dotted KEY=VALUE subset without a TOML dependency."""

    raw_key, raw_value = text.split("=", 1)
    keys = [part.strip() for part in raw_key.strip().split(".")]
    if not keys or any(not key for key in keys):
        raise CodexRpcError(
            f"could not map config override into thread config: {text!r}",
            error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
        )

    value_text = raw_value.strip()
    try:
        value = json.loads(value_text)
    except json.JSONDecodeError:
        if (
            len(value_text) >= 2
            and value_text[0] == value_text[-1]
            and value_text[0] in {"'", '"'}
        ):
            value = value_text[1:-1]
        else:
            value = value_text

    parsed: dict[str, Any] = {}
    cursor = parsed
    for key in keys[:-1]:
        child: dict[str, Any] = {}
        cursor[key] = child
        cursor = child
    cursor[keys[-1]] = value
    return parsed


def _merge_config_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_config_dict(current, value)
            continue
        if current is not None and isinstance(current, dict) != isinstance(value, dict):
            raise CodexRpcError(
                f"conflicting Codex config override at {key!r}",
                error_code="CODEX_DAEMON_RUNTIME_MISMATCH",
            )
        target[key] = deepcopy(value)


def normalize_sandbox_mode(sandbox: str | None) -> str:
    value = (sandbox or "workspace-write").strip().casefold()
    if value not in SANDBOX_MODES:
        choices = ", ".join(SANDBOX_MODES)
        raise ValueError(f"unsupported sandbox {sandbox!r}; choose {choices}")
    return value


def sandbox_policy(sandbox: str) -> dict[str, Any]:
    value = normalize_sandbox_mode(sandbox)
    if value == "read-only":
        return {"type": "readOnly"}
    if value == "workspace-write":
        return {"type": "workspaceWrite"}
    if value == "danger-full-access":
        return {"type": "dangerFullAccess"}
    raise AssertionError(f"unhandled sandbox mode: {value}")
