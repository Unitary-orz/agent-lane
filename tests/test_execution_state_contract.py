from types import SimpleNamespace

import agent_lane.control_plane as cli
from agent_lane.cli import main
from agent_lane.codex_rpc import CodexRpcError
from agent_lane.state import save_alias
from cli_result import decode_cli_output


ACTIVE_WITH_CONFLICTS = {
    "id": "thread-1",
    "name": "Conflicting live task",
    "cwd": None,
    "status": {"type": "active"},
    "recencyAt": 30,
    "turns": [
        {
            "id": "turn-old",
            "status": "completed",
            "startedAt": 10,
            "completedAt": 20,
            "items": [],
        }
    ],
}


class ConflictingStateCodex:
    transport = "daemon"

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        thread = dict(ACTIVE_WITH_CONFLICTS)
        thread["id"] = thread_id
        if not include_turns:
            thread["turns"] = None
        return {"thread": thread}

    def list_threads(self, **_kwargs):
        thread = dict(ACTIVE_WITH_CONFLICTS)
        thread.pop("turns")
        return {"data": [thread]}

    def get_goal(self, _thread_id):
        return {"status": "blocked", "objective": "Waiting on access"}


def _save_conflicting_alias(tmp_path):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_turn_id": "turn-old",
            "last_status": "completed",
            "goal_status": "active",
            "execution_mode": "app-sync",
            "execution_mode_source": "explicit",
        },
        tmp_path,
    )


def _assert_active_contract(result):
    assert result["execution_active"] is True
    assert result["thread_active"] is True
    assert result["runner_status"] == "inProgress"
    assert result["needs_resume"] is False
    assert result["last_turn"]["status"] == "inProgress"
    assert result["execution"]["state"] == "active"
    assert result["execution"]["active"] is True
    assert result["execution"]["effective_turn_status"] == "inProgress"
    assert result["execution"]["evidence"]["goal"]["status"] == "blocked"
    conflict_codes = {conflict["code"] for conflict in result["execution"]["conflicts"]}
    assert "EXECUTION_ACTIVE_GOAL_STOPPED" in conflict_codes
    assert "EXECUTION_ACTIVE_LOCAL_STATUS_STALE" in conflict_codes


def test_status_and_closeout_use_one_active_state_contract(
    tmp_path, monkeypatch, capsys
):
    _save_conflicting_alias(tmp_path)
    monkeypatch.setattr(cli, "CodexAppServer", ConflictingStateCodex)

    status_rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--detail",
            "turns",
        ]
    )
    status = decode_cli_output(capsys.readouterr().out)
    closeout_rc = main(
        [
            "codex",
            "closeout",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    closeout = decode_cli_output(capsys.readouterr().out)

    assert status_rc == closeout_rc == 0
    _assert_active_contract(status)
    _assert_active_contract(closeout)
    assert closeout["summary"] == "thread_active"


def test_session_list_and_read_project_same_state_and_explicit_control_boundary(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "CodexAppServer", ConflictingStateCodex)

    list_rc = main(
        [
            "codex",
            "session",
            "list",
            "--scope",
            "all",
            "--detail",
            "summary",
            "--alias-root",
            str(tmp_path),
        ]
    )
    listed = decode_cli_output(capsys.readouterr().out)["items"][0]
    read_rc = main(
        [
            "codex",
            "session",
            "read",
            "--thread-id",
            "thread-1",
            "--include-turns",
            "--alias-root",
            str(tmp_path),
        ]
    )
    read = decode_cli_output(capsys.readouterr().out)

    assert list_rc == read_rc == 0
    _assert_active_contract(listed)
    _assert_active_contract(read)
    assert listed["execution"]["evidence"]["last_turn"]["status"] == "completed"
    assert listed["execution"]["evidence"]["last_turn"]["source"] == "app_server"
    for result in (listed, read):
        control = result["control"]
        assert control["binding_status"] == "unattached"
        assert control["control_ready"] is False
        assert control["requires_explicit_attach"] is True
        assert control["target_argv"] == ["--thread-id", "thread-1"]
        assert control["attach_argv"] == [
            "codex",
            "session",
            "attach",
            "--thread-id",
            "thread-1",
            "--mode",
            "independent",
            "--alias-root",
            str(tmp_path),
        ]
        assert control["send_target_argv"] is None
        assert control["after_attach_argv"] is None


def test_live_session_observation_suggests_app_sync_attach(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "CodexAppServer", ConflictingStateCodex)

    rc = main(
        [
            "codex",
            "session",
            "list",
            "--scope",
            "all",
            "--observe",
            "live",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["items"][0]["control"]["attach_argv"] == [
        "codex",
        "session",
        "attach",
        "--thread-id",
        "thread-1",
        "--mode",
        "app-sync",
        "--alias-root",
        str(tmp_path),
    ]


class CompletedTurnCodex:
    transport = "stdio"

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        return {
            "thread": {
                "id": thread_id,
                "name": "Completed task",
                "cwd": None,
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def start_thread(self, _cwd, **_kwargs):
        return "thread-1"

    def set_thread_name(self, _thread_id, _title):
        return None

    def update_git_info(self, _thread_id, _git_info):
        return None

    def resume_thread(self, _thread_id, **_kwargs):
        return {}

    def run_turn(self, thread_id, _prompt, *, on_started=None, **_kwargs):
        if on_started:
            on_started("turn-new")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-new",
            status="completed",
            final_text="done",
            events=["turn/completed"],
        )

    def get_goal(self, _thread_id):
        return None


def test_send_completion_reports_the_same_inactive_execution_contract(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(workspace),
            "execution_mode": "independent",
            "execution_mode_source": "explicit",
            "commit_signing": {"mode": "off"},
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", CompletedTurnCodex)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["execution_active"] is False
    assert result["thread_active"] is False
    assert result["runner_status"] == "completed"
    assert result["last_turn"]["turn_id"] == "turn-new"
    assert result["last_turn"]["status"] == "completed"
    assert result["execution"]["state"] == "inactive"


def test_run_completion_reports_the_same_inactive_execution_contract(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli, "CodexAppServer", CompletedTurnCodex)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-new",
            "--cwd",
            str(workspace),
            "--alias-root",
            str(tmp_path / "aliases"),
            "--prompt",
            "start",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["execution_active"] is False
    assert result["thread_active"] is False
    assert result["runner_status"] == "completed"
    assert result["last_turn"]["status"] == "completed"
    assert result["execution"]["state"] == "inactive"


class ActiveAfterTimeoutCodex(CompletedTurnCodex):
    reads = 0

    def read_thread(self, thread_id, include_turns=False):
        type(self).reads += 1
        active = type(self).reads > 1
        return {
            "thread": {
                "id": thread_id,
                "name": "Timed out task",
                "cwd": None,
                "status": {"type": "active" if active else "idle"},
                "turns": (
                    [
                        {
                            "id": "turn-timeout",
                            "status": "inProgress",
                            "startedAt": 10,
                            "items": [],
                        }
                    ]
                    if include_turns and active
                    else ([] if include_turns else None)
                ),
            }
        }

    def run_turn(self, _thread_id, _prompt, *, on_started=None, **_kwargs):
        if on_started:
            on_started("turn-timeout")
        raise TimeoutError("observation window expired")

    def get_goal(self, _thread_id):
        return {"status": "blocked", "objective": "Waiting on input"}


def test_send_timeout_reports_observed_active_turn_without_retrying_control(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(workspace),
            "execution_mode": "independent",
            "execution_mode_source": "explicit",
            "commit_signing": {"mode": "off"},
        },
        tmp_path,
    )
    ActiveAfterTimeoutCodex.reads = 0
    monkeypatch.setattr(cli, "CodexAppServer", ActiveAfterTimeoutCodex)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "continue",
            "--timeout",
            "0.01",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "TURN_TIMEOUT"
    assert result["retryable"] is False
    assert result["recommended_action"] == "observe"
    _assert_active_contract(result)


class UncertainDaemonTurnCodex(ActiveAfterTimeoutCodex):
    transport = "daemon"

    def run_turn(self, _thread_id, _prompt, *, on_started=None, **_kwargs):
        if on_started:
            on_started("turn-uncertain")
        raise CodexRpcError(
            "turn/start timed out and state could not be identified",
            error_code="CODEX_DAEMON_TURN_STATE_UNCERTAIN",
            retryable=True,
            phase="turn/start",
        )


def test_send_uncertain_daemon_error_observes_execution_and_blocks_retry(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(workspace),
            "execution_mode": "app-sync",
            "execution_mode_source": "explicit",
            "commit_signing": {"mode": "off"},
        },
        tmp_path,
    )
    UncertainDaemonTurnCodex.reads = 0
    monkeypatch.setattr(cli, "CodexAppServer", UncertainDaemonTurnCodex)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_DAEMON_TURN_STATE_UNCERTAIN"
    assert result["retryable"] is False
    assert result["recommended_action"] == "observe"
    assert result["effective_effort"] is None
    assert result["effective_effort_source"] == "unset"
    _assert_active_contract(result)
