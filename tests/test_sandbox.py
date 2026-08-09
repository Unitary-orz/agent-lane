import queue
import threading
from types import SimpleNamespace

import pytest

import agent_lane.codex_rpc as codex_rpc
import agent_lane.app_runtime as app_runtime
from agent_lane.control_plane import _resolve_sandbox
from agent_lane.codex_rpc import (
    CodexAppServer,
    CodexRpcError,
    app_server_command,
    normalize_sandbox_mode,
    resolve_compatible_daemon_cli,
    resolve_app_server_transport,
    sandbox_policy,
    thread_config_from_overrides,
)
from agent_lane.daemon_transport import (
    DaemonProbeError,
    DaemonSocketError,
    DaemonVersionError,
    DaemonVersionInfo,
)


def _fake_codex(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("test", encoding="utf-8")
    codex.chmod(0o700)
    return str(codex)


def test_normalize_sandbox_mode_defaults_to_workspace_write():
    assert normalize_sandbox_mode(None) == "workspace-write"
    assert normalize_sandbox_mode("DANGER-FULL-ACCESS") == "danger-full-access"


def test_normalize_sandbox_mode_rejects_unknown_value():
    with pytest.raises(ValueError):
        normalize_sandbox_mode("full-access")


def test_sandbox_policy_maps_to_app_server_turn_shape():
    assert sandbox_policy("read-only") == {"type": "readOnly"}
    assert sandbox_policy("workspace-write") == {"type": "workspaceWrite"}
    assert sandbox_policy("danger-full-access") == {"type": "dangerFullAccess"}


def test_resolve_sandbox_prefers_cli_then_alias_then_native_default():
    assert _resolve_sandbox("danger-full-access", {"sandbox": "read-only"}) == (
        "danger-full-access"
    )
    assert _resolve_sandbox(None, {"sandbox": "read-only"}) == "read-only"
    assert _resolve_sandbox(None, {}) is None


def test_thread_and_turn_omit_sandbox_when_not_explicit():
    client = FakeCodexAppServer()

    assert client.start_thread("/repo") == "thread-1"
    client.resume_thread("thread-1", cwd="/repo")
    client.run_turn("thread-1", "hello", timeout=1)

    assert client.requests[0] == ("thread/start", {"cwd": "/repo"})
    assert client.requests[1] == (
        "thread/resume",
        {"threadId": "thread-1", "cwd": "/repo"},
    )
    assert "sandboxPolicy" not in client.requests[2][1]
    for _method, params in client.requests:
        assert "approvalPolicy" not in params
        assert "approvalsReviewer" not in params
        assert "permissions" not in params


def test_thread_start_and_resume_send_sandbox_param():
    client = FakeCodexAppServer()

    assert client.start_thread("/repo", sandbox="danger-full-access") == "thread-1"
    client.resume_thread("thread-1", cwd="/repo", sandbox="workspace-write")

    assert client.requests[0] == (
        "thread/start",
        {"cwd": "/repo", "sandbox": "danger-full-access"},
    )
    assert client.requests[1] == (
        "thread/resume",
        {
            "threadId": "thread-1",
            "cwd": "/repo",
            "sandbox": "workspace-write",
        },
    )


def test_thread_archive_sends_exact_thread_id():
    client = FakeCodexAppServer()

    client.archive_thread("thread-1")

    assert client.requests == [
        ("thread/archive", {"threadId": "thread-1"}),
    ]


def test_turn_start_sends_sandbox_policy_param():
    client = FakeCodexAppServer()

    result = client.run_turn("thread-1", "hello", sandbox="read-only", timeout=1)

    assert result.turn_id == "turn-1"
    assert result.status == "completed"
    assert client.requests[0] == (
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "hello"}],
            "sandboxPolicy": {"type": "readOnly"},
        },
    )


def test_run_turn_without_timeout_waits_for_completion():
    client = FakeCodexAppServer()

    result = client.run_turn("thread-1", "hello", timeout=None)

    assert result.turn_id == "turn-1"
    assert result.status == "completed"


def test_thread_list_passes_recency_sort_and_cursor():
    client = FakeCodexAppServer()

    client.list_threads(limit=5, cursor="next-page")

    assert client.requests[0] == (
        "thread/list",
        {
            "limit": 5,
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": [
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
            ],
            "cursor": "next-page",
        },
    )


def test_turn_ignores_explicit_completion_for_another_shared_daemon_turn():
    client = FakeCodexAppServer()
    client._notifications = queue.Queue()
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-other",
                "turn": {"id": "turn-other", "status": "completed"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "done"},
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )

    result = client.run_turn("thread-1", "hello", timeout=1)

    assert result.turn_id == "turn-1"
    assert result.status == "completed"
    assert result.final_text == "done"


def test_turn_interrupts_potential_write_in_sibling_worktree(monkeypatch):
    client = FakeDaemonCodexAppServer()
    client._notifications = queue.Queue()
    client._notifications.put(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "item-1",
                    "type": "commandExecution",
                    "cwd": "/repo-sibling",
                    "command": "pytest -q",
                    "commandActions": [
                        {"type": "unknown", "command": "pytest -q"}
                    ],
                },
            },
        }
    )
    monkeypatch.setattr(
        codex_rpc,
        "sibling_worktree_drift",
        lambda configured, observed: {
            "configured_worktree": configured,
            "observed_worktree": observed,
            "git_common_dir": "/repo/.git",
        },
    )

    with pytest.raises(CodexRpcError) as caught:
        client.run_turn(
            "thread-1",
            "continue",
            workspace_cwd="/repo",
            timeout=1,
        )

    assert caught.value.error_code == "CODEX_WORKSPACE_BINDING_DRIFT"
    assert caught.value.retryable is False
    assert caught.value.details["configured_worktree"] == "/repo"
    assert caught.value.details["observed_worktree"] == "/repo-sibling"
    assert caught.value.details["rebind_required"] is True
    assert any(
        method == "turn/interrupt" for method, _params in client.requests
    )


def test_turn_allows_read_only_command_in_sibling_worktree(monkeypatch):
    client = FakeDaemonCodexAppServer()
    client._notifications = queue.Queue()
    client._notifications.put(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "item-1",
                    "type": "commandExecution",
                    "cwd": "/repo-sibling",
                    "command": "sed -n 1,20p README.md",
                    "commandActions": [
                        {
                            "type": "read",
                            "command": "sed",
                            "name": "README.md",
                            "path": "/repo-sibling/README.md",
                        }
                    ],
                },
            },
        }
    )
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )
    monkeypatch.setattr(
        codex_rpc,
        "sibling_worktree_drift",
        lambda *_args: pytest.fail("read-only command must not trigger drift"),
    )

    result = client.run_turn(
        "thread-1",
        "inspect",
        workspace_cwd="/repo",
        timeout=1,
    )

    assert result.status == "completed"
    assert not any(
        method == "turn/interrupt" for method, _params in client.requests
    )


def test_file_change_in_sibling_worktree_is_a_binding_violation(monkeypatch):
    monkeypatch.setattr(
        codex_rpc,
        "sibling_worktree_drift",
        lambda configured, observed: {
            "configured_worktree": configured,
            "observed_worktree": str(observed.parent),
            "git_common_dir": "/repo/.git",
        },
    )

    violation = codex_rpc._workspace_binding_violation(
        {
            "id": "patch-1",
            "type": "fileChange",
            "changes": [
                {
                    "path": "/repo-sibling/file.py",
                    "kind": "update",
                    "diff": "@@",
                }
            ],
        },
        configured_cwd="/repo",
    )

    assert violation is not None
    assert violation["item_type"] == "fileChange"
    assert violation["observed_path"] == "/repo-sibling/file.py"


def test_runtime_overrides_reach_thread_resume_and_turn_start():
    client = FakeCodexAppServer()
    started = []

    client.resume_thread(
        "thread-1",
        cwd="/repo",
        sandbox="workspace-write",
        model="gpt-test",
        runtime_workspace_roots=["/repo", "/shared"],
    )
    client.run_turn(
        "thread-1",
        "hello",
        sandbox="workspace-write",
        model="gpt-test",
        effort="high",
        runtime_workspace_roots=["/repo", "/shared"],
        timeout=1,
        on_started=started.append,
    )

    assert client.requests[0] == (
        "thread/resume",
        {
            "threadId": "thread-1",
            "cwd": "/repo",
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "runtimeWorkspaceRoots": ["/repo", "/shared"],
        },
    )
    assert client.requests[1][1]["model"] == "gpt-test"
    assert client.requests[1][1]["effort"] == "high"
    assert client.requests[1][1]["runtimeWorkspaceRoots"] == ["/repo", "/shared"]
    assert started == ["turn-1"]


def test_app_server_command_adds_config_overrides():
    assert app_server_command("codex", ['shell_environment_policy.inherit="core"']) == [
        "codex",
        "app-server",
        "-c",
        'shell_environment_policy.inherit="core"',
    ]


def test_app_server_command_layers_profile_before_subcommand():
    assert app_server_command("codex", ["feature=true"], profile="work") == [
        "codex",
        "--profile",
        "work",
        "app-server",
        "-c",
        "feature=true",
    ]


def test_thread_config_maps_overrides_and_signing_env():
    config = thread_config_from_overrides(
        [
            "features.example=true",
            'shell_environment_policy.inherit="core"',
            'shell_environment_policy.include_only=["PATH","SSH_AUTH_SOCK"]',
        ],
        extra_env={
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GIT_CONFIG_COUNT": "1",
        },
    )

    assert config == {
        "features": {"example": True},
        "shell_environment_policy": {
            "inherit": "core",
            "include_only": ["PATH", "SSH_AUTH_SOCK"],
            "set": {
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
                "GIT_CONFIG_COUNT": "1",
            },
        },
    }


def test_daemon_thread_config_reaches_start_and_resume():
    client = FakeCodexAppServer()
    client.transport = "daemon"
    client._thread_config = {"features": {"example": True}}

    client.start_thread("/repo")
    client.resume_thread("thread-1", cwd="/repo")

    assert client.requests[0][1]["config"] == {"features": {"example": True}}
    assert client.requests[1][1]["excludeTurns"] is True
    assert client.requests[1][1]["config"] == {"features": {"example": True}}


def test_loaded_thread_list_follows_cursor_pages():
    client = bare_client()
    pages = {
        None: {"data": ["thread-1"], "nextCursor": "next"},
        "next": {"data": ["thread-2"], "nextCursor": None},
    }

    def request(method, params=None, *, timeout=30.0):
        assert method == "thread/loaded/list"
        assert timeout == 20.0
        return pages[(params or {}).get("cursor")]

    client.request = request

    assert client.list_loaded_thread_ids() == {"thread-1", "thread-2"}


def test_thread_shell_command_waits_for_matching_user_shell_item():
    client = bare_client()
    client.transport = "daemon"
    command = "printf 'probe-ok\\n'"

    def request(method, params=None, *, timeout=30.0):
        assert method == "thread/shellCommand"
        client._notifications.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "other-thread",
                    "turnId": "other-turn",
                    "item": {
                        "type": "commandExecution",
                        "source": "userShell",
                        "command": command,
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "wrong thread",
                    },
                },
            }
        )
        client._notifications.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "probe-turn",
                    "item": {
                        "id": "probe-item",
                        "type": "commandExecution",
                        "source": "userShell",
                        "command": command,
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "probe-ok\n",
                    },
                },
            }
        )
        return {}

    client.request = request

    result = client.run_thread_shell_command("thread-1", command, timeout=1.0)

    assert result.turn_id == "probe-turn"
    assert result.item_id == "probe-item"
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == "probe-ok\n"


def test_thread_shell_command_accepts_matching_success_receipt(tmp_path):
    client = bare_client()
    client.transport = "daemon"
    receipt_path = tmp_path / "probe.ok"
    marker = "AGENT_LANE_SIGNING_OK:receipt"

    def request(method, params=None, *, timeout=30.0):
        assert method == "thread/shellCommand"
        receipt_path.write_text(f"{marker}\n", encoding="utf-8")
        return {}

    client.request = request

    result = client.run_thread_shell_command(
        "thread-1",
        "probe command",
        timeout=1.0,
        success_receipt=(receipt_path, marker),
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == f"{marker}\n"
    assert result.receipt_observed is True


def test_wait_thread_idle_polls_authoritative_status(monkeypatch):
    client = bare_client()
    statuses = iter(["active", "idle"])
    requests = []

    def request(method, params=None, *, timeout=30.0):
        requests.append((method, params, timeout))
        return {
            "thread": {
                "id": "thread-1",
                "status": {"type": next(statuses)},
            }
        }

    client.request = request
    monkeypatch.setattr(codex_rpc.time, "sleep", lambda _seconds: None)

    result = client.wait_thread_idle(
        "thread-1",
        timeout=1.0,
        poll_interval=0.01,
    )

    assert result["thread"]["status"]["type"] == "idle"
    assert [call[0] for call in requests] == ["thread/read", "thread/read"]
    assert all(
        call[1] == {"threadId": "thread-1", "includeTurns": False}
        for call in requests
    )


def test_wait_thread_idle_fails_on_terminal_thread_error():
    client = bare_client()
    client.request = lambda *_args, **_kwargs: {
        "thread": {
            "id": "thread-1",
            "status": {"type": "systemError"},
        }
    }

    with pytest.raises(CodexRpcError) as caught:
        client.wait_thread_idle("thread-1", timeout=1.0)

    assert caught.value.error_code == "CODEX_THREAD_SYSTEM_ERROR"
    assert caught.value.retryable is False
    assert caught.value.details["thread_status"] == "systemError"


def test_server_request_cannot_collide_with_pending_client_response():
    client = bare_client()
    response_queue = queue.Queue(maxsize=1)
    client._pending_responses[7] = response_queue
    sent = []
    client._send = sent.append

    client._route_message(
        {
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
    )

    assert response_queue.empty()
    assert sent == []
    client._register_owned_turn("turn-1")
    assert sent == [
        {
            "id": 7,
            "error": {
                "code": -32000,
                "message": (
                    "interactive request requires a reviewer; agent-lane "
                    "does not approve, decline, or synthesize input"
                ),
                "data": {"errorCode": "CODEX_INTERACTION_REQUIRED"},
            },
        }
    ]
    assert client._notifications.get_nowait()["method"] == (
        "item/commandExecution/requestApproval"
    )


@pytest.mark.parametrize(
    "method",
    [
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "mcpServer/elicitation/request",
    ],
)
def test_run_turn_surfaces_interactive_requests(method):
    client = bare_client()
    client.transport = "stdio"

    def request(request_method, _params=None, *, timeout=30.0):
        assert request_method == "turn/start"
        client._notifications.put(
            {
                "id": 11,
                "method": method,
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                },
            }
        )
        return {"turn": {"id": "turn-1"}}

    client.request = request

    with pytest.raises(CodexRpcError) as caught:
        client.run_turn("thread-1", "hello", timeout=1)

    assert caught.value.error_code == "CODEX_INTERACTION_REQUIRED"
    assert caught.value.retryable is False
    assert caught.value.details["request_method"] == method


def test_owned_turn_registration_does_not_answer_another_turn_request():
    client = bare_client()
    sent = []
    client._send = sent.append

    client._route_message(
        {
            "id": 8,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-2", "turnId": "turn-foreign"},
        }
    )
    client._register_owned_turn("turn-owned")

    assert sent == []
    assert 8 in client._deferred_server_requests


@pytest.mark.parametrize(
    "method",
    [
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "mcpServer/elicitation/request",
    ],
)
def test_interactive_server_requests_are_not_silently_denied(method):
    client = bare_client()
    sent = []
    client._send = sent.append

    client._respond_to_server_request(
        {
            "id": 9,
            "method": method,
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
    )

    assert sent[0]["error"]["data"] == {
        "errorCode": "CODEX_INTERACTION_REQUIRED"
    }
    assert "result" not in sent[0]


def test_overloaded_rpc_error_is_structured_and_retryable():
    client = bare_client()

    def send(message):
        client._route_message(
            {
                "id": message["id"],
                "error": {
                    "code": -32001,
                    "message": "Server overloaded; retry later.",
                },
            }
        )

    client._send = send

    with pytest.raises(CodexRpcError) as caught:
        client.request("thread/list", {"limit": 1}, timeout=1)

    assert caught.value.error_code == "CODEX_APP_SERVER_OVERLOADED"
    assert caught.value.retryable is True
    assert caught.value.details["rpc_code"] == -32001


def test_daemon_turn_timeout_interrupts_and_confirms_terminal():
    client = FakeDaemonCodexAppServer()

    with pytest.raises(TimeoutError):
        client.run_turn("thread-1", "hello", timeout=0)

    interrupt = next(
        request for request in client.requests if request[0] == "turn/interrupt"
    )
    assert interrupt[1] == {"threadId": "thread-1", "turnId": "turn-1"}
    turn_start = client.requests[0][1]
    assert turn_start["clientUserMessageId"].startswith("agent-lane-")


def test_daemon_steer_sends_expected_turn_precondition_without_owning_turn():
    client = FakeDaemonCodexAppServer()

    def request(method, params=None, *, timeout=30.0):
        client.requests.append((method, params or {}, timeout))
        return {"turnId": "turn-live"}

    client.request = request

    result = client.steer_turn(
        "thread-1",
        "Focus on tests.",
        expected_turn_id="turn-live",
        timeout=7.0,
    )

    method, params, timeout = client.requests[-1]
    assert method == "turn/steer"
    assert params["threadId"] == "thread-1"
    assert params["input"] == [{"type": "text", "text": "Focus on tests."}]
    assert params["expectedTurnId"] == "turn-live"
    assert params["clientUserMessageId"].startswith("agent-lane-steer-")
    assert timeout == 7.0
    assert result.turn_id == "turn-live"
    assert result.client_message_id == params["clientUserMessageId"]
    assert client._owned_turn_ids == set()


def test_steer_rejects_stdio_before_sending_request():
    client = FakeCodexAppServer()

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == "CODEX_STEER_REQUIRES_SHARED_DAEMON"
    assert caught.value.retryable is True
    assert client.requests == []


@pytest.mark.parametrize(
    ("rpc_code", "error_code"),
    [
        (-32601, "CODEX_STEER_UNSUPPORTED"),
        (-32600, "CODEX_STEER_REJECTED"),
    ],
)
def test_steer_normalizes_rpc_rejections(rpc_code, error_code):
    client = FakeDaemonCodexAppServer()

    def reject(*_args, **_kwargs):
        raise CodexRpcError(
            "turn/steer failed",
            rpc_code=rpc_code,
            rpc_data={"reason": "not steerable"},
        )

    client.request = reject

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == error_code
    assert caught.value.retryable is False
    assert caught.value.details["expected_turn_id"] == "turn-live"


def test_steer_timeout_is_non_retryable_because_delivery_is_uncertain():
    client = FakeDaemonCodexAppServer()
    client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        TimeoutError("late response")
    )

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == "CODEX_STEER_STATE_UNCERTAIN"
    assert caught.value.retryable is False
    assert caught.value.details["client_user_message_id"].startswith(
        "agent-lane-steer-"
    )


def test_steer_transport_failure_is_non_retryable_after_submission():
    client = FakeDaemonCodexAppServer()

    def disconnect(*_args, **_kwargs):
        raise CodexRpcError(
            "shared daemon connection closed",
            error_code="CODEX_DAEMON_UNAVAILABLE",
            retryable=True,
        )

    client.request = disconnect

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == "CODEX_STEER_STATE_UNCERTAIN"
    assert caught.value.retryable is False
    assert caught.value.details["cause_code"] == "CODEX_DAEMON_UNAVAILABLE"


def test_steer_preserves_safe_overload_retry_semantics():
    client = FakeDaemonCodexAppServer()

    def overload(*_args, **_kwargs):
        raise CodexRpcError(
            "Server overloaded; retry later.",
            error_code="CODEX_APP_SERVER_OVERLOADED",
            retryable=True,
            rpc_code=-32001,
        )

    client.request = overload

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == "CODEX_APP_SERVER_OVERLOADED"
    assert caught.value.retryable is True
    assert caught.value.details["expected_turn_id"] == "turn-live"


def test_steer_rejects_unconfirmed_or_changed_response_turn():
    client = FakeDaemonCodexAppServer()
    client.request = lambda *_args, **_kwargs: {"turnId": "turn-next"}

    with pytest.raises(CodexRpcError) as caught:
        client.steer_turn(
            "thread-1",
            "Focus on tests.",
            expected_turn_id="turn-live",
        )

    assert caught.value.error_code == "CODEX_STEER_RESPONSE_INVALID"
    assert caught.value.retryable is False
    assert caught.value.details["returned_turn_id"] == "turn-next"


def test_daemon_ignores_terminal_notification_without_task_identity():
    client = FakeDaemonCodexAppServer()
    client._notifications.put(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )

    result = client.run_turn("thread-1", "hello", timeout=1)

    assert result.turn_id == "turn-1"
    assert result.status == "completed"


def test_turn_start_recovery_matches_client_id_within_the_same_turn():
    client = bare_client()
    client.request = lambda method, _params, **_kwargs: {
        "data": [
            {
                "id": "turn-newer-app",
                "status": "inProgress",
                "items": [
                    {
                        "type": "userMessage",
                        "clientId": "another-client",
                    }
                ],
            },
            {
                "id": "turn-owned",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "clientId": "agent-lane-marker",
                    }
                ],
            },
        ]
    }

    assert client._find_turn_by_client_message_id(
        "thread-1",
        "agent-lane-marker",
    ) == ("turn-owned", "completed")


def test_daemon_socket_validation_error_is_structured(tmp_path):
    with pytest.raises(CodexRpcError) as caught:
        CodexAppServer(
            transport="daemon",
            daemon_socket=tmp_path / "missing.sock",
        )

    assert caught.value.error_code == "CODEX_DAEMON_SOCKET_INVALID"


def test_daemon_cli_accepts_reported_version_difference_without_fallback(tmp_path):
    path_cli = tmp_path / "path" / "codex"
    path_cli.parent.mkdir()
    path_cli.write_text("path", encoding="utf-8")
    path_cli.chmod(0o700)
    socket_path = tmp_path / "daemon.sock"
    calls = []

    def probe(candidate):
        calls.append(candidate)
        return DaemonVersionInfo(
            cli_version="0.144.1",
            app_server_version="0.145.0-alpha.18",
            socket_path=socket_path,
        )

    resolved = resolve_compatible_daemon_cli(
        "codex",
        home=tmp_path / "home",
        which_command=lambda _name: str(path_cli),
        probe=probe,
    )

    assert resolved.path == path_cli
    assert resolved.source == "path"
    assert resolved.fallback_used is False
    assert resolved.info.cli_version == "0.144.1"
    assert resolved.info.app_server_version == "0.145.0-alpha.18"
    assert resolved.info.socket_path == socket_path
    assert calls == [str(path_cli)]


def test_daemon_cli_does_not_mask_socket_failure_with_fallback(tmp_path):
    path_cli = tmp_path / "path-codex"
    path_cli.write_text("path", encoding="utf-8")
    path_cli.chmod(0o700)
    managed_cli = (
        tmp_path
        / "home"
        / ".codex"
        / "packages"
        / "standalone"
        / "current"
        / "codex"
    )
    managed_cli.parent.mkdir(parents=True)
    managed_cli.write_text("managed", encoding="utf-8")
    managed_cli.chmod(0o700)
    calls = []

    def probe(candidate):
        calls.append(candidate)
        raise DaemonSocketError("unsafe daemon socket")

    with pytest.raises(DaemonSocketError, match="unsafe daemon socket"):
        resolve_compatible_daemon_cli(
            "codex",
            home=tmp_path / "home",
            which_command=lambda _name: str(path_cli),
            probe=probe,
        )

    assert calls == [str(path_cli)]


def test_daemon_cli_falls_back_after_candidate_specific_probe_failure(tmp_path):
    path_cli = tmp_path / "path-codex"
    path_cli.write_text("path", encoding="utf-8")
    path_cli.chmod(0o700)
    managed_cli = (
        tmp_path
        / "home"
        / ".codex"
        / "packages"
        / "standalone"
        / "current"
        / "codex"
    )
    managed_cli.parent.mkdir(parents=True)
    managed_cli.write_text("managed", encoding="utf-8")
    managed_cli.chmod(0o700)
    calls = []

    def probe(candidate):
        calls.append(candidate)
        if candidate == str(path_cli):
            raise DaemonVersionError(
                "daemon version output did not contain JSON"
            )
        return DaemonVersionInfo(
            cli_version="0.145.0",
            app_server_version="0.145.0",
            socket_path=tmp_path / "daemon.sock",
        )

    resolved = resolve_compatible_daemon_cli(
        "codex",
        home=tmp_path / "home",
        which_command=lambda _name: str(path_cli),
        probe=probe,
    )

    assert resolved.path == managed_cli
    assert resolved.source == "managed_standalone"
    assert resolved.fallback_used is True
    assert calls == [str(path_cli), str(managed_cli)]


def test_daemon_cli_rejects_app_bundle_symlink_escape(tmp_path):
    outside_cli = tmp_path / "outside-codex"
    outside_cli.write_text("outside", encoding="utf-8")
    outside_cli.chmod(0o700)
    app_path = tmp_path / "ChatGPT.app"
    app_cli = app_path / "Contents" / "Resources" / "codex"
    app_cli.parent.mkdir(parents=True)
    app_cli.symlink_to(outside_cli)
    with pytest.raises(
        DaemonProbeError,
        match="no Codex CLI executable",
    ):
        resolve_compatible_daemon_cli(
            "codex",
            app_path=app_path,
            home=tmp_path / "home-without-managed-codex",
            which_command=lambda _name: None,
        )


def test_auto_transport_classifies_malformed_daemon_version_response(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(
            path=tmp_path / "ChatGPT.app",
            log_root=tmp_path,
            pid=123,
        ),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        codex_rpc,
        "resolve_compatible_daemon_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DaemonVersionError(
                "daemon version output did not contain JSON"
            )
        ),
    )

    with pytest.raises(CodexRpcError) as caught:
        resolve_app_server_transport("codex")

    assert caught.value.error_code == "CODEX_DAEMON_VERSION_INVALID"
    assert caught.value.retryable is True


def test_auto_transport_uses_stdio_when_app_is_not_verified(monkeypatch):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: (_ for _ in ()).throw(
            codex_rpc.AppRuntimeError(
                "CODEX_APP_NOT_RUNNING",
                "not running",
            )
        ),
    )

    assert resolve_app_server_transport("codex") == ("stdio", None)


def test_auto_transport_fails_closed_when_running_app_runtime_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: (_ for _ in ()).throw(
            codex_rpc.AppRuntimeError(
                "CODEX_APP_PROCESS_UNAVAILABLE",
                "running App process unavailable",
                retryable=True,
                app_running=True,
            )
        ),
    )

    with pytest.raises(CodexRpcError) as caught:
        resolve_app_server_transport("codex")

    assert caught.value.error_code == "CODEX_APP_TRANSPORT_UNOBSERVED"
    assert caught.value.retryable is True
    assert caught.value.details["cause_code"] == (
        "CODEX_APP_PROCESS_UNAVAILABLE"
    )


def test_auto_transport_uses_observed_stdio_for_unknown_app_build(monkeypatch):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(
            log_root="/logs",
            pid=123,
            version="99.0.0",
            build="9999",
        ),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="stdio",
            connected=True,
            state="connected",
        ),
    )

    assert resolve_app_server_transport("codex") == ("stdio", None)


def test_explicit_stdio_does_not_inspect_the_app(monkeypatch):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: pytest.fail("explicit stdio must not inspect the App"),
    )

    assert resolve_app_server_transport(
        "codex",
        requested="stdio",
    ) == ("stdio", None)


def test_auto_transport_fails_closed_when_running_app_transport_is_unobserved(
    monkeypatch,
):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root="/logs", pid=123),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(CodexRpcError) as caught:
        resolve_app_server_transport("codex")

    assert caught.value.error_code == "CODEX_APP_TRANSPORT_UNOBSERVED"
    assert caught.value.retryable is True


def test_auto_transport_requires_verified_websocket_app(monkeypatch, tmp_path):
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root=tmp_path, pid=123),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(transport="websocket"),
    )
    monkeypatch.setattr(
        codex_rpc,
        "probe_shared_daemon",
        lambda _codex_bin: SimpleNamespace(socket_path=socket_path),
    )

    assert resolve_app_server_transport(_fake_codex(tmp_path)) == (
        "daemon",
        socket_path,
    )


def test_current_app_build_and_daemon_version_resolve_together(
    monkeypatch,
    tmp_path,
):
    app = tmp_path / "ChatGPT.app"
    app.mkdir()
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "codex-desktop-a-123-t0-i1-0.log").write_text(
        "2026-07-16T02:00:00.000Z info [AppServerConnection] "
        "app_server_connection.state_changed hostId=local "
        "hasConnection=true initialized=true next=connected "
        "transport=websocket\n",
        encoding="utf-8",
    )
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setattr(app_runtime.sys, "platform", "darwin")
    monkeypatch.setattr(app_runtime, "CODEX_APP_LOG_ROOT", log_root)
    monkeypatch.setattr(
        app_runtime,
        "_running_codex_processes",
        lambda: [app_runtime._RunningCodexProcess(pid=123, app_path=app)],
    )
    monkeypatch.setattr(
        app_runtime,
        "_read_app_version",
        lambda _path: ("26.707.91948", "5440"),
    )
    monkeypatch.setattr(
        codex_rpc,
        "probe_shared_daemon",
        lambda _codex_bin: SimpleNamespace(
            socket_path=socket_path,
            app_server_version="0.144.5",
        ),
    )

    assert resolve_app_server_transport(_fake_codex(tmp_path)) == (
        "daemon",
        socket_path,
    )


def test_auto_transport_accepts_daemon_after_capability_probe(
    monkeypatch,
    tmp_path,
):
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root=tmp_path, pid=123),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        codex_rpc,
        "probe_shared_daemon",
        lambda _codex_bin: SimpleNamespace(
            socket_path=socket_path,
            app_server_version="99.0.0",
        ),
    )

    assert resolve_app_server_transport(_fake_codex(tmp_path)) == (
        "daemon",
        socket_path,
    )


def test_auto_transport_fails_closed_for_disconnected_websocket_app(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root=tmp_path, pid=123),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=False,
            state="disconnected",
        ),
    )

    with pytest.raises(CodexRpcError) as caught:
        resolve_app_server_transport("codex")

    assert caught.value.error_code == "CODEX_APP_SHARED_DAEMON_DISCONNECTED"
    assert caught.value.retryable is True
    assert caught.value.details == {
        "observed_app_transport": "websocket",
        "observed_app_state": "disconnected",
    }


def test_explicit_daemon_rejects_stdio_app(monkeypatch, tmp_path):
    monkeypatch.setattr(
        codex_rpc,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root=tmp_path, pid=123),
    )
    monkeypatch.setattr(
        codex_rpc,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(transport="stdio"),
    )

    with pytest.raises(CodexRpcError) as caught:
        resolve_app_server_transport("codex", requested="daemon")

    assert caught.value.error_code == "CODEX_APP_NOT_ON_SHARED_DAEMON"


def test_daemon_transport_rejects_per_command_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        codex_rpc,
        "resolve_app_server_transport",
        lambda *_args, **_kwargs: ("daemon", tmp_path / "daemon.sock"),
    )

    with pytest.raises(CodexRpcError) as caught:
        CodexAppServer(profile="work")

    assert caught.value.error_code == "CODEX_DAEMON_RUNTIME_MISMATCH"


class FakeProc:
    def poll(self):
        return None


class FakeCodexAppServer(CodexAppServer):
    def __init__(self):
        self.requests = []
        self.transport = "stdio"
        self._server_request_lock = threading.Lock()
        self._owned_turn_ids = set()
        self._deferred_server_requests = {}
        self._notifications = queue.Queue()
        self._notifications.put(
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}
        )
        self._proc = FakeProc()
        self._stderr = []

    def request(self, method, params=None, *, timeout=30.0):
        self.requests.append((method, params or {}))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}


class FakeDaemonCodexAppServer(FakeCodexAppServer):
    def __init__(self):
        super().__init__()
        self.transport = "daemon"

    def request(self, method, params=None, *, timeout=30.0):
        result = super().request(method, params, timeout=timeout)
        if method == "turn/interrupt":
            self._notifications.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "interrupted",
                        },
                    },
                }
            )
        return result


def bare_client():
    client = object.__new__(CodexAppServer)
    client._next_id = 0
    client._request_id_lock = threading.Lock()
    client._pending_lock = threading.Lock()
    client._server_request_lock = threading.Lock()
    client._pending_responses = {}
    client._owned_turn_ids = set()
    client._deferred_server_requests = {}
    client._notifications = queue.Queue()
    client._stderr = []
    client._closed = False
    client._reader_error = None
    client._websocket = None
    client._proc = FakeProc()
    return client
