import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.codex_rpc import CodexRpcError
from agent_lane.state import save_alias
from agent_lane.workspace import WorkspaceError, operation_lock


class FakeSteerCodex:
    thread_status = {"type": "active", "activeFlags": []}
    turns = [{"id": "turn-live", "status": "inProgress", "items": []}]
    steer_error = None
    init_kwargs = None
    steer_calls = []
    read_count = 0
    initial_read = threading.Event()

    def __init__(self, *_args, **kwargs):
        type(self).init_kwargs = kwargs
        self.transport = "daemon"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        assert include_turns is True
        type(self).read_count += 1
        type(self).initial_read.set()
        return {
            "thread": {
                "id": thread_id,
                "status": type(self).thread_status,
                "turns": type(self).turns,
            }
        }

    def steer_turn(
        self,
        thread_id,
        prompt,
        *,
        expected_turn_id,
        timeout,
    ):
        type(self).steer_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "expected_turn_id": expected_turn_id,
                "timeout": timeout,
            }
        )
        if type(self).steer_error is not None:
            raise type(self).steer_error
        return SimpleNamespace(
            turn_id=expected_turn_id,
            client_message_id="agent-lane-steer-client",
        )


@pytest.fixture(autouse=True)
def reset_fake_steer(tmp_path, monkeypatch):
    FakeSteerCodex.thread_status = {"type": "active", "activeFlags": []}
    FakeSteerCodex.turns = [
        {"id": "turn-live", "status": "inProgress", "items": []}
    ]
    FakeSteerCodex.steer_error = None
    FakeSteerCodex.init_kwargs = None
    FakeSteerCodex.steer_calls = []
    FakeSteerCodex.read_count = 0
    FakeSteerCodex.initial_read = threading.Event()
    monkeypatch.setattr(cli, "STEER_LOCK_ROOT", tmp_path / "host-steer-locks")


def _save_lane(aliases, lane_id="lane-1", thread_id="thread-1"):
    return save_alias(
        "codex",
        lane_id,
        {
            "codex_thread_id": thread_id,
            "cwd": "/repo",
            "title": "Active lane",
            "current_turn_id": "turn-live",
            "last_status": "inProgress",
            "execution_mode": "app-sync",
            "execution_mode_source": "explicit",
        },
        aliases,
    )


def test_steer_lane_targets_live_turn_without_mutating_alias(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    alias_path = _save_lane(aliases)
    before = alias_path.read_bytes()
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Focus on the failing tests.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    target_resolution = result.pop("target_resolution")
    assert result == {
        "app_server_transport": "daemon",
        "client_user_message_id": "agent-lane-steer-client",
        "codex_thread_id": "thread-1",
        "codex_url": "codex://threads/thread-1",
        "expected_turn_id": "turn-live",
        "lane_id": "lane-1",
        "ok": True,
        "operation": "steer",
        "provider": "codex",
        "execution_mode": "app-sync",
        "execution_mode_source": "binding",
        "steer_status": "accepted",
        "target_source": "lane",
        "turn_id": "turn-live",
    }
    assert FakeSteerCodex.init_kwargs == {"transport": "daemon"}
    assert FakeSteerCodex.steer_calls == [
        {
            "thread_id": "thread-1",
            "prompt": "Focus on the failing tests.",
            "expected_turn_id": "turn-live",
            "timeout": 20.0,
        }
    ]
    assert alias_path.read_bytes() == before
    assert target_resolution["source"] == "explicit_lane_id"


def test_steer_requires_explicit_attach_for_unbound_thread(capsys):
    rc = main(
        ["codex", "steer", "--thread-id", "thread-app", "--prompt", "Continue."]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_ATTACH_REQUIRED"
    assert result["attach_argv"] == [
        "codex",
        "session",
        "attach",
        "--thread-id",
        "thread-app",
        "--mode",
        "app-sync",
    ]
    assert result["after_attach_argv"] == [
        "codex",
        "steer",
        "--thread-id",
        "thread-app",
        "--prompt",
        "Continue.",
    ]


def test_steer_optional_turn_id_is_a_strict_precondition(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--turn-id",
            "turn-old",
            "--prompt",
            "Do not spill into another turn.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_TURN_MISMATCH"
    assert result["expected_turn_id"] == "turn-old"
    assert result["active_turn_id"] == "turn-live"
    assert FakeSteerCodex.steer_calls == []


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
def test_steer_rejects_non_finite_or_non_positive_timeout(
    tmp_path, monkeypatch, capsys, timeout
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Keep this bounded.",
            f"--timeout={timeout}",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_INVALID_ARGUMENT"
    assert result["option"] == "--timeout"
    assert result["retryable"] is False
    assert FakeSteerCodex.init_kwargs is None


@pytest.mark.parametrize(
    ("target_args", "option"),
    [
        (["--lane-id", "missing-lane"], "--lane-id"),
        (["--lane-id", ""], "--lane-id"),
    ],
)
def test_steer_target_validation_returns_typed_machine_error(
    tmp_path, monkeypatch, capsys, target_args, option
):
    aliases = tmp_path / "aliases"
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            *target_args,
            "--alias-root",
            str(aliases),
            "--prompt",
            "This must not connect.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_INVALID_ARGUMENT"
    assert result["option"] == option
    assert result["retryable"] is False
    assert FakeSteerCodex.init_kwargs is None


def test_steer_prompt_file_failure_returns_typed_machine_error(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    missing_prompt = tmp_path / "missing-prompt.txt"
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt-file",
            str(missing_prompt),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_INVALID_ARGUMENT"
    assert result["option"] == "--prompt-file"
    assert result["prompt_file"] == str(missing_prompt)
    assert result["retryable"] is False
    assert FakeSteerCodex.init_kwargs is None


def test_steer_never_starts_a_turn_when_thread_is_idle(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    FakeSteerCodex.thread_status = {"type": "idle"}
    FakeSteerCodex.turns = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "This must not become send.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_NO_ACTIVE_TURN"
    assert result["retryable"] is False
    assert FakeSteerCodex.steer_calls == []


def test_steer_fails_when_active_turn_identity_is_ambiguous(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    FakeSteerCodex.turns = [
        {"id": "turn-a", "status": "inProgress"},
        {"id": "turn-b", "status": "inProgress"},
    ]
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Do not guess.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_ACTIVE_TURN_UNRESOLVED"
    assert result["active_turn_ids"] == ["turn-a", "turn-b"]
    assert FakeSteerCodex.steer_calls == []


def test_steer_bypasses_lane_execution_lock_but_serializes_by_thread(
    tmp_path, monkeypatch
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)
    lane_args = build_parser().parse_args(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Steer while the runner owns the lane.",
        ]
    )

    with operation_lock(aliases, "lane-1"):
        result = cli.cmd_codex_steer(lane_args)

    assert result["steer_status"] == "accepted"


def test_steer_serializes_same_thread_across_lane_alias_roots(
    tmp_path, monkeypatch
):
    aliases_a = tmp_path / "aliases-a"
    aliases_b = tmp_path / "aliases-b"
    _save_lane(aliases_a)
    _save_lane(aliases_b, lane_id="lane-2")

    class BlockingSteerCodex(FakeSteerCodex):
        first_rpc_started = threading.Event()
        release_first_rpc = threading.Event()

        def steer_turn(self, thread_id, prompt, **kwargs):
            if prompt == "first":
                type(self).first_rpc_started.set()
                assert type(self).release_first_rpc.wait(timeout=2)
            return super().steer_turn(thread_id, prompt, **kwargs)

    monkeypatch.setattr(cli, "CodexAppServer", BlockingSteerCodex)
    lane_args = build_parser().parse_args(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases_a),
            "--prompt",
            "first",
            "--timeout",
            "2",
        ]
    )
    second_lane_args = build_parser().parse_args(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-2",
            "--alias-root",
            str(aliases_b),
            "--prompt",
            "second",
            "--timeout",
            "2",
        ]
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cli.cmd_codex_steer, lane_args)
        assert BlockingSteerCodex.first_rpc_started.wait(timeout=1)
        second = pool.submit(cli.cmd_codex_steer, second_lane_args)
        time.sleep(0.1)
        assert second.done() is False
        BlockingSteerCodex.release_first_rpc.set()
        assert first.result(timeout=2)["steer_status"] == "accepted"
        assert second.result(timeout=2)["steer_status"] == "accepted"

    assert [call["prompt"] for call in BlockingSteerCodex.steer_calls] == [
        "first",
        "second",
    ]


def test_steer_rechecks_turn_after_waiting_for_thread_lock(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)
    args = build_parser().parse_args(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "must stay on the captured turn",
            "--timeout",
            "2",
        ]
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with operation_lock(cli.STEER_LOCK_ROOT, "thread-1", namespace="steer"):
            future = pool.submit(cli.cmd_codex_steer, args)
            assert FakeSteerCodex.initial_read.wait(timeout=1)
            FakeSteerCodex.turns = [
                {"id": "turn-next", "status": "inProgress", "items": []}
            ]
        with pytest.raises(WorkspaceError) as caught:
            future.result(timeout=2)

    assert caught.value.error_code == "CODEX_STEER_TURN_CHANGED"
    assert caught.value.details["expected_turn_id"] == "turn-live"
    assert caught.value.details["active_turn_id"] == "turn-next"
    assert FakeSteerCodex.steer_calls == []


def test_steer_rechecks_thread_binding_after_waiting_for_thread_lock(
    tmp_path, monkeypatch
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)
    args = build_parser().parse_args(
        [
            "codex",
            "steer",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "must retain attached control",
            "--timeout",
            "2",
        ]
    )
    cli._prepare_command_target(args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with operation_lock(cli.STEER_LOCK_ROOT, "thread-1", namespace="steer"):
            future = pool.submit(cli.cmd_codex_steer, args)
            assert FakeSteerCodex.initial_read.wait(timeout=1)
            _save_lane(aliases, thread_id="thread-2")
        with pytest.raises(WorkspaceError) as caught:
            future.result(timeout=2)

    assert caught.value.error_code == "CODEX_STEER_TARGET_CHANGED"
    assert caught.value.details["expected_thread_id"] == "thread-1"
    assert caught.value.details["observed_thread_id"] == "thread-1"
    assert caught.value.details["observed_lane_id"] is None
    assert FakeSteerCodex.steer_calls == []


def test_steer_preserves_server_rejection_semantics(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    FakeSteerCodex.steer_error = CodexRpcError(
        "review turns cannot be steered",
        error_code="CODEX_STEER_REJECTED",
        retryable=False,
        expected_turn_id="turn-live",
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Try steering review.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_REJECTED"
    assert result["retryable"] is False


def test_steer_uncertain_error_exposes_public_client_user_message_id(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    _save_lane(aliases)
    FakeSteerCodex.steer_error = CodexRpcError(
        "delivery uncertain",
        error_code="CODEX_STEER_STATE_UNCERTAIN",
        retryable=False,
        client_user_message_id="agent-lane-steer-receipt",
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeSteerCodex)

    rc = main(
        [
            "codex",
            "steer",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "Do not retry this automatically.",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_STEER_STATE_UNCERTAIN"
    assert result["retryable"] is False
    assert result["client_user_message_id"] == "agent-lane-steer-receipt"
