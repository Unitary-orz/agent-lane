import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
import agent_lane.doctor as doctor
from agent_lane.codex_rpc import CodexRpcError, CompatibleDaemonCli
from agent_lane.daemon_transport import DaemonVersionInfo
from agent_lane.app_runtime import AppRuntimeError
from agent_lane.state import load_alias, save_alias
from agent_lane.workspace import WorkspaceError


def test_parser_exposes_v1_runtime_and_session_options(tmp_path):
    parser = build_parser()
    run = parser.parse_args(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-1",
            "--cwd",
            str(tmp_path),
            "--mode",
            "app-sync",
            "--model",
            "gpt-test",
            "--profile",
            "work",
            "--add-dir",
            str(tmp_path),
            "--effort",
            "high",
            "--config",
            "features.example=true",
            "--worktree",
            "auto",
            "--goal-objective",
            "Ship the lane workflow",
            "--prompt",
            "hello",
        ]
    )
    assert run.model == "gpt-test"
    assert run.profile == "work"
    assert run.add_dir == [str(tmp_path)]
    assert run.effort == "high"
    assert run.config_overrides == ["features.example=true"]
    assert run.worktree == "auto"
    assert run.mode == "app-sync"
    assert run.goal_objective == "Ship the lane workflow"
    assert run.timeout is None
    assert run.sandbox is None

    send = parser.parse_args(
        [
            "codex",
            "send",
            "--lane-id",
            "lane-1",
            "--prompt",
            "hello",
        ]
    )
    assert send.timeout is None
    assert send.sandbox is None

    doctor_args = parser.parse_args(["doctor", "--mode", "app-sync", "--probe"])
    assert doctor_args.probe is True
    assert doctor_args.mode == "app-sync"
    assert parser.parse_args(["doctor", "--verbose"]).verbose is True
    assert parser.parse_args(["codex", "wait", "--lane-id", "lane-1"]).timeout == 600
    assert parser.parse_args(["codex", "watch", "--lane-id", "lane-1"]).jsonl_output
    checkpoint = parser.parse_args(["codex", "checkpoint", "--lane-id", "lane-1"])
    monitor = parser.parse_args(
        ["codex", "checkpoint", "--lane-id", "lane-1", "--after", "0"]
    )
    assert checkpoint.after_seconds == 300
    assert monitor.after_seconds == 0
    session_list = parser.parse_args(
        [
            "codex",
            "session",
            "list",
            "--scope",
            "lanes",
            "--threads",
            "all",
            "--observe",
            "live",
            "--detail",
            "metadata",
        ]
    )
    assert session_list.scope == "lanes"
    assert session_list.threads == "all"
    assert session_list.observe == "live"
    assert session_list.detail == "metadata"
    assert (
        parser.parse_args(
            ["codex", "closeout", "--lane-id", "lane-1"]
        ).command
        == "closeout"
    )
    assert (
        parser.parse_args(["codex", "cleanup", "--lane-id", "lane-1"]).command
        == "cleanup"
    )
    adopt = parser.parse_args(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "lane-1",
            "--thread-id",
            "thread-1",
        ]
    )
    assert adopt.thread_id == "thread-1"
    name_get = parser.parse_args(
        ["codex", "session", "name", "get", "--lane-id", "lane-1"]
    )
    name_set = parser.parse_args(
        [
            "codex", "session", "name",
            "set",
            "--lane-id",
            "lane-1",
            "--title",
            "New title",
            "--expected-title",
            "Old title",
        ]
    )
    assert name_get.name_command == "get"
    assert name_set.title == "New title"
    assert name_set.expected_title == "Old title"
    custom_title_set = parser.parse_args(
        [
            "codex",
            "custom-title",
            "set",
            "--lane-id",
            "lane-1",
            "--title",
            "Pinned local title",
        ]
    )
    assert custom_title_set.custom_title_command == "set"
    assert custom_title_set.title == "Pinned local title"
    goal_run = parser.parse_args(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "lane-1",
            "--turn-timeout",
            "30",
            "--max-runtime",
            "120",
            "--max-turns",
            "4",
        ]
    )
    assert goal_run.turn_timeout == 30
    assert goal_run.max_runtime == 120
    assert goal_run.max_turns == 4

    unbounded_goal_run = parser.parse_args(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "lane-1",
        ]
    )
    assert unbounded_goal_run.turn_timeout is None
    assert unbounded_goal_run.max_runtime is None
    assert unbounded_goal_run.max_turns is None


@pytest.mark.parametrize(
    "option", ["--app-refresh", "--no-app-refresh", "--ephemeral", "--brief"]
)
def test_cli_rejects_removed_options_with_json_migration_error(option, capsys):
    rc = main(
        ["codex", "run", "--lane-id", "lane-1", "--prompt", "hello", option]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 2
    assert result["error_code"] == "CLI_REMOVED"
    assert result["removed"] == option


def test_worktree_request_requires_a_source_checkout(tmp_path, monkeypatch, capsys):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid worktree request must not start app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--worktree",
            "auto",
            "--prompt",
            "hello",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["ok"] is False
    assert result["error_code"] == "GIT_WORKTREE_SOURCE_REQUIRED"
    assert "--cwd" in result["error"]


def test_run_explicit_cwd_rebind_replaces_not_loaded_thread(
    tmp_path, monkeypatch, capsys
):
    stored_cwd = tmp_path / "main"
    requested_cwd = tmp_path / "feature"
    stored_cwd.mkdir()
    requested_cwd.mkdir()

    class RebindCodex:
        instances = []
        transport = "daemon"

        def __init__(self, *_args, **_kwargs):
            self.started_cwd = None
            self.resumed = False
            self.additional_context = None
            self.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read_thread(self, _thread_id, include_turns=False):
            del include_turns
            raise CodexRpcError(
                "thread/read failed: thread not loaded",
                rpc_code=-32600,
            )

        def start_thread(self, cwd, **_kwargs):
            self.started_cwd = cwd
            return "thread-rebound"

        def set_thread_name(self, _thread_id, _title):
            return None

        def update_git_info(self, _thread_id, _git_info):
            return None

        def resume_thread(self, *_args, **_kwargs):
            self.resumed = True

        def run_turn(
            self,
            thread_id,
            _prompt,
            *,
            on_started=None,
            additional_context=None,
            **_kwargs,
        ):
            self.additional_context = additional_context
            if on_started:
                on_started("turn-rebound")
            return SimpleNamespace(
                thread_id=thread_id,
                turn_id="turn-rebound",
                status="completed",
                final_text="continued",
                events=["turn/completed"],
            )

    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-origin",
            "cwd": str(stored_cwd),
            "custom_title": "Lane",
            "sandbox": "danger-full-access",
            "commit_signing": {"mode": "off"},
            "last_completed_final_text": "Previous result",
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", RebindCodex)
    monkeypatch.setattr(cli, "workspace_binding_changed", lambda *_args: True)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--cwd",
            str(requested_cwd),
            "--commit-signing",
            "off",
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)
    instance = RebindCodex.instances[0]

    assert rc == 0
    assert result["thread_replaced"] is True
    assert result["origin_thread_id"] == "thread-origin"
    assert result["codex_thread_id"] == "thread-rebound"
    assert result["handoff_reason"] == "workspace_binding_changed"
    assert result["previous_cwd"] == str(stored_cwd)
    assert instance.started_cwd == str(requested_cwd)
    assert instance.resumed is False
    assert "agent_lane_workspace_rebind" in instance.additional_context
    assert alias["codex_thread_id"] == "thread-rebound"
    assert alias["cwd"] == str(requested_cwd)
    assert alias["thread_replacement"]["reason"] == "workspace_binding_changed"
    assert alias["binding"]["generation"] == 2
    assert alias["binding"]["thread_id"] == "thread-rebound"
    assert alias["binding"]["predecessor_thread_id"] == "thread-origin"
    assert alias["binding_history"][-1]["thread_id"] == "thread-origin"
    assert alias["binding_history"][-1]["unbound_reason"] == (
        "workspace_binding_changed"
    )


def test_workspace_drift_error_marks_turn_interrupted_and_returns_recovery(
    tmp_path, monkeypatch, capsys
):
    cwd = tmp_path / "main"
    cwd.mkdir()

    class DriftCodex:
        transport = "daemon"

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
                    "status": {"type": "idle"},
                    "turns": [] if include_turns else None,
                }
            }

        def resume_thread(self, *_args, **_kwargs):
            return {}

        def run_turn(self, _thread_id, _prompt, *, on_started=None, **_kwargs):
            if on_started:
                on_started("turn-drift")
            raise CodexRpcError(
                "workspace drift",
                error_code="CODEX_WORKSPACE_BINDING_DRIFT",
                observed_worktree="/repo-sibling",
            )

    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(cwd),
            "custom_title": "Lane",
            "sandbox": "danger-full-access",
            "commit_signing": {"mode": "off"},
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", DriftCodex)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--commit-signing",
            "off",
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert rc == 1
    assert result["error_code"] == "CODEX_WORKSPACE_BINDING_DRIFT"
    assert result["lane_id"] == "lane-1"
    assert result["codex_thread_id"] == "thread-1"
    assert result["recovery"] == {
        "command": "run",
        "lane_id": "lane-1",
        "cwd": "/repo-sibling",
        "thread_action": "replace",
    }
    assert alias["last_status"] == "interrupted"
    assert alias["last_error_code"] == "CODEX_WORKSPACE_BINDING_DRIFT"


def test_workspace_rebind_requires_explicit_active_goal_objective(
    tmp_path, monkeypatch, capsys
):
    stored_cwd = tmp_path / "stored"
    requested_cwd = tmp_path / "requested"
    stored_cwd.mkdir()
    requested_cwd.mkdir()

    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("active-goal guard must run before app-server")

    save_alias(
        "codex",
        "goal-lane",
        {
            "codex_thread_id": "thread-origin",
            "cwd": str(stored_cwd),
            "custom_title": "Goal lane",
            "sandbox": "danger-full-access",
            "commit_signing": {"mode": "off"},
            "goal": {
                "objective": "Finish the goal",
                "status": "active",
                "tokensUsed": 100,
            },
            "goal_status": "active",
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    monkeypatch.setattr(cli, "workspace_binding_changed", lambda *_args: True)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "goal-lane",
            "--alias-root",
            str(tmp_path),
            "--cwd",
            str(requested_cwd),
            "--commit-signing",
            "off",
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert (
        result["error_code"]
        == "CODEX_WORKSPACE_REBIND_ACTIVE_GOAL_REQUIRES_OBJECTIVE"
    )
    assert result["required_option"] == "--goal-objective"


def test_doctor_auth_output_never_contains_tokens(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "acct-123",
                    "access_token": "SECRET_ACCESS",
                    "refresh_token": "SECRET_REFRESH",
                    "id_token": "SECRET_ID",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = doctor._auth_check()
    serialized = json.dumps(result, sort_keys=True)

    assert result == {
        "ok": True,
        "path": str(codex_dir / "auth.json"),
        "exists": True,
        "mode": "chatgpt",
        "account_id": "acct-123",
    }
    assert "SECRET_" not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "id_token" not in serialized


def test_doctor_config_has_python39_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        'model = "gpt-test" # current default\n[profiles.other]\nmodel = "wrong"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor, "tomllib", None)

    result = doctor._config_check()

    assert result["ok"] is True
    assert result["default_model"] == "gpt-test"
    assert result["parser"] == "minimal_top_level_fallback"


def test_doctor_report_is_compact_by_default_and_verbose_on_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        doctor,
        "_codex_check",
        lambda _bin: {
            "ok": True,
            "path": "/private/bin/codex",
            "version": "codex-cli 1.0",
        },
    )
    monkeypatch.setattr(
        doctor,
        "_config_check",
        lambda: {
            "ok": True,
            "path": "/private/config.toml",
            "default_model": "gpt-test",
        },
    )
    monkeypatch.setattr(
        doctor,
        "_auth_check",
        lambda: {
            "ok": True,
            "path": "/private/auth.json",
            "mode": "chatgpt",
            "account_id": "acct-private",
        },
    )
    monkeypatch.setattr(
        doctor,
        "_recent_check",
        lambda _root: {"ok": True, "codex_sessions_path": "/private/sessions"},
    )
    monkeypatch.setattr(
        doctor,
        "_probe_check",
        lambda _path, requested: {
            "requested": requested,
            "ok": True,
            "status": "passed",
            "operation": "detailed operation",
        },
    )
    monkeypatch.setattr(
        doctor,
        "_app_paths",
        lambda: {
            "codex": {
                "installed": False,
                "detected_path": None,
                "candidates": ["/private/Codex.app"],
            },
            "chatgpt": {
                "installed": True,
                "detected_path": "/private/ChatGPT.app",
                "candidates": ["/private/ChatGPT.app"],
            },
        },
    )
    monkeypatch.setattr(
        doctor,
        "_shared_daemon_check",
        lambda _path: {
            "ready": True,
            "ok": True,
            "required": False,
            "status": "ready",
            "app_transport": "websocket",
            "app_connected": True,
            "daemon_version": "1.0",
            "socket_path": "/private/daemon.sock",
        },
    )

    compact = doctor.doctor_report(alias_root=tmp_path, run_probe=True)
    verbose = doctor.doctor_report(
        alias_root=tmp_path, run_probe=True, verbose=True
    )
    compact_json = json.dumps(compact, sort_keys=True)

    assert compact["status"] == "ready"
    assert compact["issues"] == []
    assert compact["codex_cli"] == {
        "ok": True,
        "version": "codex-cli 1.0",
        "source": None,
        "fallback_used": None,
    }
    assert compact["shared_daemon"] == {
        "ready": True,
        "status": "ready",
        "app_transport": "websocket",
        "app_connected": True,
        "required": False,
        "probe_cli_source": None,
        "fallback_used": None,
        "version_mismatch": None,
        "warnings": None,
    }
    assert "acct-private" not in compact_json
    assert "/private/" not in compact_json
    assert "candidates" not in compact_json
    assert "operation" not in compact_json
    assert verbose["auth"]["account_id"] == "acct-private"
    assert verbose["shared_daemon"]["socket_path"] == "/private/daemon.sock"
    assert verbose["apps"]["chatgpt"]["detected_path"] == "/private/ChatGPT.app"

    monkeypatch.setattr(
        doctor,
        "_auth_check",
        lambda: {"ok": False, "error": "private diagnostic detail"},
    )
    failed = doctor.doctor_report(alias_root=tmp_path)

    assert failed["status"] == "issues"
    assert failed["issues"] == [
        {
            "check": "auth",
            "action": "Run `codex login`, then retry doctor.",
        }
    ]
    assert "private diagnostic detail" not in json.dumps(failed)


def test_doctor_reports_optional_daemon_failure_without_blocking_readiness(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        doctor,
        "_codex_check",
        lambda _bin: {"ok": True, "path": "/bin/codex", "version": "0.144.2"},
    )
    monkeypatch.setattr(
        doctor,
        "_config_check",
        lambda: {"ok": True, "default_model": "gpt-test"},
    )
    monkeypatch.setattr(
        doctor,
        "_auth_check",
        lambda: {"ok": True, "mode": "chatgpt"},
    )
    monkeypatch.setattr(doctor, "_recent_check", lambda _root: {"ok": True})
    monkeypatch.setattr(
        doctor,
        "_probe_check",
        lambda _path, requested: {
            "requested": requested,
            "ok": None,
            "status": "skipped",
        },
    )
    monkeypatch.setattr(doctor, "_app_paths", lambda: {})
    monkeypatch.setattr(
        doctor,
        "_shared_daemon_check",
        lambda _path: {
            "ready": False,
            "ok": False,
            "required": False,
            "status": "daemon_unavailable",
            "app_transport": "websocket",
            "app_connected": True,
        },
    )

    result = doctor.doctor_report(alias_root=tmp_path)

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["issues"] == []


def test_doctor_warns_on_version_difference_after_successful_handshake(
    tmp_path,
    monkeypatch,
):
    stale_cli = "/nvm/bin/codex"
    socket_path = tmp_path / "daemon.sock"
    resolution = CompatibleDaemonCli(
        path=Path(stale_cli),
        source="path",
        info=DaemonVersionInfo(
            cli_version="0.144.1",
            app_server_version="0.145.0-alpha.18",
            socket_path=socket_path,
        ),
        fallback_used=False,
    )
    handshakes = []

    class Handshake:
        def __init__(self, **kwargs):
            handshakes.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr(
        doctor,
        "_codex_check",
        lambda _bin: {
            "ok": True,
            "requested": "codex",
            "path": stale_cli,
            "version": "codex-cli 0.144.1",
        },
    )
    monkeypatch.setattr(
        doctor,
        "_config_check",
        lambda: {"ok": True, "default_model": "gpt-test"},
    )
    monkeypatch.setattr(
        doctor,
        "_auth_check",
        lambda: {"ok": True, "mode": "chatgpt"},
    )
    monkeypatch.setattr(doctor, "_recent_check", lambda _root: {"ok": True})
    monkeypatch.setattr(doctor, "_app_paths", lambda: {})
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(
            path=tmp_path / "ChatGPT.app",
            log_root="/logs",
            pid=123,
            version="26.test",
            build="1",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "resolve_compatible_daemon_cli",
        lambda *_args, **_kwargs: resolution,
    )
    monkeypatch.setattr(doctor, "CodexAppServer", Handshake)

    result = doctor.doctor_report(alias_root=tmp_path, verbose=True)

    assert result["ok"] is True
    assert result["codex_cli"]["path"] == stale_cli
    assert result["codex_cli"]["version"] == "codex-cli 0.144.1"
    assert result["codex_cli"]["source"] == "path"
    assert result["codex_cli"]["fallback_used"] is False
    assert result["shared_daemon"]["probe_cli_path"] == stale_cli
    assert result["shared_daemon"]["probe_cli_source"] == "path"
    assert result["shared_daemon"]["fallback_used"] is False
    assert result["shared_daemon"]["version_mismatch"] is True
    assert result["shared_daemon"]["warnings"] == [
        {
            "code": "CODEX_DAEMON_VERSION_MISMATCH",
            "message": (
                "daemon CLI and app-server versions differ; "
                "the live capability handshake succeeded"
            ),
            "cli_version": "0.144.1",
            "app_server_version": "0.145.0-alpha.18",
        }
    ]
    assert handshakes == [
        {
            "codex_bin": stale_cli,
            "transport": "daemon",
            "daemon_socket": socket_path,
        }
    ]


def test_doctor_classifies_malformed_daemon_version_response(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(
            path=tmp_path / "ChatGPT.app",
            log_root="/logs",
            pid=123,
        ),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "resolve_compatible_daemon_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            doctor.DaemonVersionError(
                "daemon version response omitted cliVersion"
            )
        ),
    )

    result = doctor._shared_daemon_check("codex")

    assert result["ok"] is False
    assert result["status"] == "daemon_version_invalid"
    assert result["error"] == "daemon version response omitted cliVersion"


def test_shared_daemon_check_reports_optional_websocket_app_disconnect(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root="/logs", pid=123),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=False,
            state="disconnected",
        ),
    )

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is False
    assert result["ok"] is False
    assert result["required"] is False
    assert result["status"] == "app_disconnected"
    assert result["app_transport"] == "websocket"
    assert result["app_connected"] is False


def test_shared_daemon_check_reports_optional_running_app_requirement(monkeypatch):
    def app_not_running():
        raise AppRuntimeError(
            "CODEX_APP_NOT_RUNNING",
            "no supported ChatGPT App installation is running",
            retryable=True,
        )

    monkeypatch.setattr(doctor, "detect_running_codex_app", app_not_running)

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is False
    assert result["ok"] is False
    assert result["required"] is False
    assert result["status"] == "app_not_running"
    assert result["setup_required"] is True


def test_shared_daemon_check_uses_transport_evidence_for_unknown_app_build(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(
            log_root="/logs",
            pid=123,
            version="99.0.0",
            build="9999",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="stdio",
            connected=True,
            state="connected",
        ),
    )

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is False
    assert result["ok"] is False
    assert result["required"] is False
    assert result["status"] == "app_stdio_sync_required"
    assert result["app_transport"] == "stdio"
    assert result["setup_required"] is True


def test_doctor_stdio_action_describes_the_one_time_app_switch():
    action = doctor._shared_daemon_action(
        {"status": "app_stdio_sync_required"}
    )

    assert "standalone managed daemon" in action
    assert "CODEX_APP_SERVER_USE_LOCAL_DAEMON=1" in action
    assert "reopen the App once" in action


def test_shared_daemon_check_reports_optional_unobserved_transport(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root="/logs", pid=123),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: None,
    )

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is False
    assert result["ok"] is False
    assert result["required"] is False
    assert result["status"] == "transport_unobserved"


def test_shared_daemon_check_accepts_current_version_pair(monkeypatch):
    calls = []

    class Handshake:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root="/logs", pid=123),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "probe_shared_daemon",
        lambda _path: SimpleNamespace(
            cli_version="0.144.5",
            app_server_version="0.144.5",
            socket_path="/private/daemon.sock",
        ),
    )
    monkeypatch.setattr(doctor, "CodexAppServer", Handshake)

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is True
    assert result["ok"] is True
    assert result["required"] is False
    assert result["status"] == "ready"
    assert result["daemon_version"] == "0.144.5"
    assert calls == [
        {
            "codex_bin": "/bin/codex",
            "transport": "daemon",
            "daemon_socket": "/private/daemon.sock",
        }
    ]


def test_shared_daemon_check_rejects_failed_protocol_handshake(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "detect_running_codex_app",
        lambda: SimpleNamespace(log_root="/logs", pid=123),
    )
    monkeypatch.setattr(
        doctor,
        "detect_local_app_transport",
        lambda *_args, **_kwargs: SimpleNamespace(
            transport="websocket",
            connected=True,
            state="connected",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "probe_shared_daemon",
        lambda _path: SimpleNamespace(
            app_server_version="99.0.0",
            socket_path="/private/daemon.sock",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "CodexAppServer",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("initialize rejected")
        ),
    )

    result = doctor._shared_daemon_check("/bin/codex")

    assert result["ready"] is False
    assert result["ok"] is False
    assert result["required"] is False
    assert result["status"] == "daemon_handshake_failed"
    assert result["error"] == "initialize rejected"


def test_watch_marks_jsonl_as_polling_diagnostics(tmp_path, monkeypatch, capsys):
    def fake_wait_for_lane(**kwargs):
        kwargs["emit"](
            {
                "lane_id": "lane-1",
                "status": "inProgress",
                "terminal": False,
            }
        )
        return {
            "ok": True,
            "lane_id": "lane-1",
            "status": "completed",
            "terminal": True,
        }

    monkeypatch.setattr(cli, "_wait_for_lane", fake_wait_for_lane)

    rc = main(
        [
            "codex",
            "watch",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rc == 0
    assert [event["data"]["event"] for event in events] == [
        "snapshot",
        "completed",
    ]
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["ok"] is True for event in events)
    assert all(event["data"]["diagnostic"] is True for event in events)
    assert all(event["data"]["stream"] == "polling_snapshots" for event in events)


def test_checkpoint_waits_once_and_returns_machine_readable_snapshot(
    tmp_path, monkeypatch, capsys
):
    class FakeCheckpointCodex:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read_thread(self, thread_id, include_turns=False):
            assert thread_id == "thread-1"
            assert include_turns is True
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "completedAt": 10,
                            "items": [],
                        }
                    ],
                }
            }

    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "last_turn_id": "turn-1",
            "last_status": "completed",
        },
        tmp_path,
    )
    slept = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeCheckpointCodex)
    monkeypatch.setattr(cli.time, "sleep", slept.append)

    rc = main(
        [
            "codex", "checkpoint",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--after",
            "300",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert slept == [300.0]
    assert result["kind"] == "checkpoint"
    assert result["delay_seconds"] == 300.0
    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert result["runner_alive"] is False
    assert result["needs_resume"] is False
    assert isinstance(result["checked_at"], float)


def test_status_reports_dead_runner_as_stale_without_mutating_alias(
    tmp_path, monkeypatch, capsys
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_status": "inProgress",
            "current_turn_id": "turn-1",
            "pending_turn_started_at": 1,
            "runner_pid": 999999,
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeStatusCodex)
    monkeypatch.setattr(cli, "process_running", lambda _pid: False)

    rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert rc == 0
    assert result["goal_status"] == "active"
    assert result["runner_status"] == "stale"
    assert result["local_runner_status"] == "stale"
    assert result["runner_alive"] is False
    assert result["thread_active"] is False
    assert result["execution_active"] is False
    assert result["execution_source"] == "none"
    assert result["needs_resume"] is True
    assert alias["last_status"] == "inProgress"
    assert alias["current_turn_id"] == "turn-1"
    assert alias["runner_pid"] == 999999
    assert alias["pending_turn_started_at"] == 1


def test_status_reports_remote_thread_execution_without_needs_resume(
    tmp_path, monkeypatch, capsys
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_status": "idle",
            "last_final_text": "Previous turn completed.\n\nOld details.",
            "goal_status": "active",
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeActiveStatusCodex)

    rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert result["goal_status"] == "active"
    assert result["runner_status"] == "inProgress"
    assert result["local_runner_status"] == "idle"
    assert result["runner_alive"] is False
    assert result["thread_active"] is True
    assert result["execution_active"] is True
    assert result["execution_source"] == "thread"
    assert result["needs_resume"] is False
    assert result["requested_model"] is None
    assert result["requested_model_source"] == "unknown"
    assert result["requested_effort"] is None
    assert result["requested_effort_source"] == "unknown"
    assert result["last_completed_final_lead"] == "Previous turn completed."
    assert result["current_turn_final_lead"] is None
    assert "last_final_lead" not in result
def test_status_summary_is_compact_machine_readable_json(
    tmp_path, monkeypatch, capsys
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Compact lane",
            "cwd": str(tmp_path),
            "last_status": "timed_out",
            "last_error_code": "TURN_TIMEOUT",
            "last_final_text": "First useful paragraph.\n\nMore detail.",
            "goal_status": "active",
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeStatusCodex)
    monkeypatch.setattr(
        cli,
        "_git_snapshot",
        lambda _cwd, *, include_details: {
            "is_repo": True,
            "branch": "main",
            "dirty": True,
        },
    )

    rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    target_resolution = result.pop("target_resolution")
    execution = result.pop("execution")
    last_turn = result.pop("last_turn")
    assert result == {
        "ok": True,
        "lane_id": "lane-1",
        "lane_title": "Compact lane",
        "lane_title_source": "custom_title",
        "codex_title": None,
        "codex_title_observation": "unknown",
        "codex_title_observed_at": None,
        "custom_title": "Compact lane",
        "cwd": str(tmp_path),
        "codex_thread_id": "thread-1",
        "codex_url": "codex://threads/thread-1",
        "goal_status": "active",
        "goal_status_source": "thread_goal_get",
        "goal_tokens_used": None,
        "goal_time_used_seconds": None,
        "requested_model": None,
        "requested_model_source": "unknown",
        "requested_effort": None,
        "requested_effort_source": "unknown",
        "effective_effort": None,
        "effective_effort_source": "unknown",
        "runner_status": "timed_out",
        "local_runner_status": "timed_out",
        "runner_alive": False,
        "thread_active": False,
        "execution_active": False,
        "execution_source": "none",
        "needs_resume": True,
        "last_status": "timed_out",
        "last_error_code": "TURN_TIMEOUT",
        "last_completed_final_lead": "First useful paragraph.",
        "current_turn_final_lead": None,
        "rollout_fallback_used": False,
        "branch": "main",
        "git_dirty": True,
        "workspace_kind": "local",
        "app_native_handoff": None,
    }
    assert execution["state"] == "inactive"
    assert execution["active"] is False
    assert execution["effective_turn_status"] == "timed_out"
    assert execution["evidence"]["goal"]["status"] == "active"
    assert execution["conflicts"] == []
    assert last_turn["status"] == "timed_out"
    assert target_resolution["source"] == "explicit_lane_id"
    assert last_turn["source"] == "alias"
    assert "alias" not in result
    assert "thread" not in result
    assert "goal" not in result


def test_status_summary_and_closeout_split_running_turn_from_completed_lead(
    tmp_path, monkeypatch, capsys
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Running follow-up",
            "cwd": str(tmp_path),
            "last_status": "inProgress",
            "last_final_text": "Previous fix is complete.\n\nOld details.",
            "goal_status": "active",
            "requested_model": "gpt-test",
            "requested_model_source": "explicit",
            "requested_effort": "high",
            "requested_effort_source": "explicit",
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeActiveStatusCodex)

    status_rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
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

    assert status_rc == 0
    assert closeout_rc == 0
    for result in (status, closeout):
        assert result["runner_status"] == "inProgress"
        assert result["requested_model"] == "gpt-test"
        assert result["requested_model_source"] == "explicit"
        assert result["requested_effort"] == "high"
        assert result["requested_effort_source"] == "explicit"
        assert result["last_status"] == "inProgress"
        assert result["last_completed_final_lead"] == "Previous fix is complete."
        assert result["current_turn_final_lead"] is None
        assert "last_final_lead" not in result
    assert closeout["summary"] == "thread_active"


def test_turn_alias_keeps_completed_final_when_later_turn_is_interrupted(tmp_path):
    alias = {"last_completed_final_text": "Earlier completed result."}
    interrupted = SimpleNamespace(
        turn_id="turn-2",
        status="interrupted",
        final_text="Partial current output.",
        events=[],
    )

    cli._update_turn_alias(
        alias,
        "lane-1",
        "thread-1",
        str(tmp_path),
        "danger-full-access",
        interrupted,
    )

    assert alias["last_final_text"] == "Partial current output."
    assert alias["last_completed_final_text"] == "Earlier completed result."
    assert cli._last_completed_final_lead(alias) == "Earlier completed result."

    completed = SimpleNamespace(
        turn_id="turn-3",
        status="completed",
        final_text="New completed result.",
        events=[],
    )
    cli._update_turn_alias(
        alias,
        "lane-1",
        "thread-1",
        str(tmp_path),
        "danger-full-access",
        completed,
    )

    assert alias["last_completed_final_text"] == "New completed result."
    assert cli._last_completed_final_lead(alias) == "New completed result."


def test_interrupted_first_turn_does_not_create_completed_final_lead(tmp_path):
    alias = {}
    interrupted = SimpleNamespace(
        turn_id="turn-1",
        status="interrupted",
        final_text="Partial current output.",
        events=[],
    )

    cli._update_turn_alias(
        alias,
        "lane-1",
        "thread-1",
        str(tmp_path),
        "danger-full-access",
        interrupted,
    )

    assert alias["last_final_text"] == "Partial current output."
    assert "last_completed_final_text" not in alias
    assert cli._last_completed_final_lead(alias) is None

    cli._preserve_legacy_completed_final(alias)

    assert "last_final_text" not in alias
    assert "last_completed_final_text" not in alias


def test_completed_turn_without_final_does_not_reuse_previous_current_lead(tmp_path):
    alias = {
        "last_status": "completed",
        "last_final_text": "Earlier completed result.",
    }
    completed = SimpleNamespace(
        turn_id="turn-2",
        status="completed",
        final_text="",
        events=[],
    )

    cli._update_turn_alias(
        alias,
        "lane-1",
        "thread-1",
        str(tmp_path),
        "danger-full-access",
        completed,
    )
    runner = {
        "execution_active": False,
        "status": "completed",
    }

    assert alias["last_completed_final_text"] is None
    assert cli._last_completed_final_lead(alias) is None
    assert cli._current_turn_final_lead(alias, runner) is None


def test_closeout_summarizes_clean_and_dirty_git_repo(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "agent-lane@example.invalid")
    _git(repo, "config", "user.name", "Agent Lane Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial commit")
    monkeypatch.setattr(cli, "CodexAppServer", FakeCompleteCodex)
    alias_path = save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Closeout lane",
            "cwd": str(repo),
            "last_status": "timed_out",
            "last_final_text": "Delivered the requested change.\n\nDetails.",
            "goal_status": "complete",
        },
        tmp_path / "aliases",
    )
    alias_before = alias_path.read_text(encoding="utf-8")

    clean_rc = main(
        [
            "codex",
            "closeout",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    clean = decode_cli_output(capsys.readouterr().out)
    tracked.write_text("dirty\n", encoding="utf-8")
    dirty_rc = main(
        [
            "codex",
            "closeout",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    dirty = decode_cli_output(capsys.readouterr().out)

    assert clean_rc == 0
    assert clean["summary"] == "complete_and_clean"
    assert clean["git"]["is_repo"] is True
    assert clean["git"]["dirty"] is False
    assert clean["git"]["status_short"] == []
    assert len(clean["git"]["recent_commits"]) == 1
    assert clean["git"]["recent_commits"][0]["subject"] == "initial commit"
    assert clean["last_completed_final_lead"] == "Delivered the requested change."
    assert clean["current_turn_final_lead"] is None
    assert dirty_rc == 0
    assert dirty["summary"] == "complete_dirty"
    assert dirty["git"]["dirty"] is True
    assert " M tracked.txt" in dirty["git"]["status_short"]
    assert alias_path.read_text(encoding="utf-8") == alias_before


@pytest.mark.parametrize(
    ("task_complete_message", "assistant_message", "expected_lead"),
    [
        (
            "Delivered from task complete.\n\nDetails.",
            "Older assistant fallback.",
            "Delivered from task complete.",
        ),
        (
            "",
            "Delivered from the final assistant message.\n\nDetails.",
            "Delivered from the final assistant message.",
        ),
    ],
)
def test_status_and_closeout_fall_back_to_adopted_not_loaded_rollout(
    tmp_path,
    monkeypatch,
    capsys,
    task_complete_message,
    assistant_message,
    expected_lead,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "agent-lane@example.invalid")
    _git(repo, "config", "user.name", "Agent Lane Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial commit")

    class FakeAppStyleCodex:
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
                    "cwd": str(repo),
                    "status": {"type": "notLoaded"},
                    "turns": None,
                }
            }

        def get_goal(self, _thread_id):
            return None

    aliases = tmp_path / "aliases"
    alias_path = save_alias(
        "codex",
        "app-task",
        {
            "codex_thread_id": "thread-app",
            "custom_title": "Adopted App task",
            "cwd": str(repo),
            "adopted_from": "codex-app",
        },
        aliases,
    )
    alias_before = alias_path.read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "CodexAppServer", FakeAppStyleCodex)
    monkeypatch.setattr(
        cli,
        "read_rollout_closeout",
        lambda _thread_id, *, session_path=None: {
            "status": "completed",
            "turn_id": "turn-app",
            "task_complete_message": task_complete_message,
            "assistant_message": assistant_message,
            "goal": {
                "objective": "Finish App task",
                "status": "complete",
                "tokensUsed": 4321,
                "timeUsedSeconds": 98,
            },
            "source": "rollout",
        },
    )

    status_rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "app-task",
            "--alias-root",
            str(aliases),
        ]
    )
    status = decode_cli_output(capsys.readouterr().out)
    closeout_rc = main(
        [
            "codex",
            "closeout",
            "--lane-id",
            "app-task",
            "--alias-root",
            str(aliases),
        ]
    )
    closeout = decode_cli_output(capsys.readouterr().out)

    assert status_rc == 0
    assert status["goal_status"] == "complete"
    assert status["goal_status_source"] == "rollout"
    assert status["goal_tokens_used"] == 4321
    assert status["goal_time_used_seconds"] == 98
    assert status["runner_status"] == "completed"
    assert status["last_status"] == "completed"
    assert status["last_completed_final_lead"] == expected_lead
    assert status["current_turn_final_lead"] == expected_lead
    assert "last_final_lead" not in status
    assert status["rollout_fallback_used"] is True
    assert closeout_rc == 0
    assert closeout["goal_status"] == "complete"
    assert closeout["goal_status_source"] == "rollout"
    assert closeout["goal_tokens_used"] == 4321
    assert closeout["goal_time_used_seconds"] == 98
    assert closeout["last_status"] == "completed"
    assert closeout["last_completed_final_lead"] == expected_lead
    assert closeout["current_turn_final_lead"] == expected_lead
    assert "last_final_lead" not in closeout
    assert closeout["rollout_fallback_used"] is True
    assert closeout["summary"] == "complete_and_clean"
    assert alias_path.read_text(encoding="utf-8") == alias_before


def test_closeout_handles_non_git_directory(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli, "CodexAppServer", FakeCompleteCodex)
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Non Git lane",
            "cwd": str(workspace),
            "last_status": "completed",
            "goal_status": "complete",
        },
        tmp_path / "aliases",
    )

    rc = main(
        [
            "codex",
            "closeout",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert result["summary"] == "not_git_repo"
    assert result["git"] == {
        "is_repo": False,
        "branch": None,
        "dirty": None,
        "status_short": [],
        "status_truncated": False,
        "recent_commits": [],
    }


def test_runner_state_migrates_legacy_timeout_from_unknown(monkeypatch):
    alias = {
        "last_status": "unknown",
        "last_error": "turn timed out after 600s; events=[]",
        "runner_pid": 999999,
        "goal_status": "active",
    }
    monkeypatch.setattr(cli, "process_running", lambda _pid: False)

    runner = cli._runner_state(alias)

    assert runner["status"] == "timed_out"
    assert runner["alive"] is False
    assert runner["needs_resume"] is True
    assert alias["last_status"] == "timed_out"
    assert alias["last_error_code"] == "TURN_TIMEOUT"
    assert "runner_pid" not in alias


def test_runner_state_keeps_daemon_timeout_uncertain(monkeypatch):
    alias = {
        "last_status": "unknown",
        "last_error": "turn/start timed out and state could not be identified",
        "last_error_code": "CODEX_DAEMON_TURN_STATE_UNCERTAIN",
        "runner_pid": 999999,
    }
    monkeypatch.setattr(cli, "process_running", lambda _pid: False)

    runner = cli._runner_state(alias)

    assert runner["status"] == "unknown"
    assert runner["execution_active"] is None
    assert runner["execution"]["state"] == "unknown"
    assert runner["needs_resume"] is False
    assert alias["last_error_code"] == "CODEX_DAEMON_TURN_STATE_UNCERTAIN"


def test_runner_state_does_not_mark_dead_local_pid_stale_while_thread_is_active(
    monkeypatch,
):
    alias = {
        "last_status": "inProgress",
        "runner_pid": 999999,
        "goal_status": "active",
    }
    monkeypatch.setattr(cli, "process_running", lambda _pid: False)

    runner = cli._runner_state(alias, thread_active=True)

    assert runner["status"] == "inProgress"
    assert runner["local_status"] == "inProgress"
    assert runner["alive"] is False
    assert runner["thread_active"] is True
    assert runner["execution_active"] is True
    assert runner["execution_source"] == "thread"
    assert runner["needs_resume"] is False
    assert alias["last_status"] == "inProgress"
    assert alias.get("last_error_code") != "STALE_RUNNER"
    assert "runner_pid" not in alias


def test_turn_start_preflight_rejects_an_active_task():
    with pytest.raises(WorkspaceError) as caught:
        cli._require_thread_inactive_for_turn(
            {"status": {"type": "active"}},
            lane_id="lane-1",
            thread_id="thread-1",
        )

    assert caught.value.error_code == "CODEX_THREAD_ACTIVE"
    assert caught.value.details["retryable"] is True


def test_goal_runner_continues_active_goal_until_complete(
    tmp_path, monkeypatch, capsys
):
    FakeGoalCodex.goal_statuses = ["active", "active", "complete"]
    FakeGoalCodex.timeout = False
    FakeGoalCodex.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeGoalCodex)
    save_alias(
        "codex",
        "goal-lane",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "custom_title": "Goal lane",
            "sandbox": "workspace-write",
            "commit_signing": {"mode": "off"},
            "goal_status": "active",
        },
        tmp_path,
    )

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "goal-lane",
            "--alias-root",
            str(tmp_path),
            "--commit-signing",
            "off",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-lane", tmp_path)
    instance = FakeGoalCodex.instances[0]

    assert rc == 0
    assert result["stop_condition"] == "goal_complete"
    assert result["goal_status"] == "complete"
    assert result["completed"] is True
    assert result["turn_count"] == 2
    assert [turn["status"] for turn in result["turns"]] == [
        "completed",
        "completed",
    ]
    assert instance.prompts == [
        cli.GOAL_CONTINUATION_PROMPT,
        cli.GOAL_CONTINUATION_PROMPT,
    ]
    assert instance.timeouts == [None, None]
    receipt = result["goal_run_receipt"]
    assert receipt["limits"] == {
        "turn_timeout_seconds": None,
        "max_runtime_seconds": None,
        "max_turns": None,
    }
    assert receipt["stop_condition"] == "goal_complete"
    assert receipt["turn_count"] == 2
    assert [turn["status"] for turn in receipt["turns"]] == [
        "completed",
        "completed",
    ]
    assert all(turn["elapsed_seconds"] >= 0 for turn in receipt["turns"])
    assert alias["last_goal_run_receipt"] == receipt
    assert result["runner_alive"] is False
    assert result["needs_resume"] is False
    assert alias["goal_status"] == "complete"
    assert alias["runner_alive"] is False
    assert alias["needs_resume"] is False


def test_goal_runner_refuses_to_overlap_live_runner(tmp_path, monkeypatch, capsys):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("live-runner guard must run before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    save_alias(
        "codex",
        "goal-lane",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_status": "inProgress",
            "runner_pid": os.getpid(),
            "runner_alive": True,
            "goal": {"objective": "Finish the goal", "status": "active"},
            "goal_status": "active",
            "requested_model": "gpt-existing",
            "requested_model_source": "alias",
            "requested_effort": "high",
            "requested_effort_source": "explicit",
            "commit_signing": {"mode": "off"},
        },
        tmp_path,
    )

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "goal-lane",
            "--alias-root",
            str(tmp_path),
            "--commit-signing",
            "off",
            "--effort",
            "low",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-lane", tmp_path)

    assert rc == 1
    assert result["error_code"] == "RUNNER_ALREADY_ACTIVE"
    assert result["stop_condition"] == "runner_already_active"
    assert result["runner_alive"] is True
    assert result["needs_resume"] is False
    assert result["turn_count"] == 0
    assert result["requested_effort"] == "low"
    assert result["requested_effort_source"] == "explicit"
    assert alias["requested_model"] == "gpt-existing"
    assert alias["requested_model_source"] == "alias"
    assert alias["requested_effort"] == "high"
    assert alias["requested_effort_source"] == "explicit"


def test_goal_runner_timeout_returns_clean_recoverable_state(
    tmp_path, monkeypatch, capsys
):
    FakeGoalCodex.goal_statuses = ["active"]
    FakeGoalCodex.timeout = True
    FakeGoalCodex.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeGoalCodex)
    save_alias(
        "codex",
        "goal-lane",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "custom_title": "Goal lane",
            "sandbox": "workspace-write",
            "commit_signing": {"mode": "off"},
            "goal_status": "active",
        },
        tmp_path,
    )

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "goal-lane",
            "--alias-root",
            str(tmp_path),
            "--commit-signing",
            "off",
            "--turn-timeout",
            "1",
            "--max-runtime",
            "10",
            "--max-turns",
            "2",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-lane", tmp_path)

    assert rc == 1
    assert result["ok"] is False
    assert result["completed"] is False
    assert result["stop_condition"] == "turn_timeout"
    assert result["goal_status"] == "active"
    assert result["runner_alive"] is False
    assert result["needs_resume"] is True
    assert result["retryable"] is True
    assert result["turns"][0]["status"] == "timed_out"
    assert alias["last_status"] == "timed_out"
    assert alias["last_error_code"] == "TURN_TIMEOUT"
    assert alias["runner_alive"] is False
    assert alias["needs_resume"] is True
    assert alias["current_turn_id"] is None
    assert "runner_pid" not in alias
    assert "pending_turn_started_at" not in alias


def test_lane_observation_reports_polling_fallback_and_exact_turn(tmp_path):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "current_turn_id": "turn-1",
            "last_turn_id": "turn-1",
            "last_status": "inProgress",
        },
        tmp_path,
    )
    codex = FakeReadCodex(
        {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 1,
                    "completedAt": 2,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "last user"}],
                        },
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "final lead\nmore",
                        },
                    ],
                }
            ],
        }
    )

    before = load_alias("codex", "lane-1", tmp_path)
    result = cli._lane_observation(codex, "lane-1", tmp_path)
    after = load_alias("codex", "lane-1", tmp_path)

    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert result["confidence"] == "high"
    assert result["observation_mode"] == "thread_read_poll"
    assert "never interrupts" in result["limitation"]
    assert result["last_user"] == "last user"
    assert result["final_lead"] == "final lead"
    assert after == before


def test_lane_observation_keeps_remote_active_thread_nonterminal(tmp_path):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "current_turn_id": "turn-1",
            "last_turn_id": "turn-1",
            "last_status": "stale",
            "goal_status": "active",
        },
        tmp_path,
    )
    codex = FakeReadCodex(
        {
            "id": "thread-1",
            "status": {"type": "active"},
            "turns": [
                {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [],
                }
            ],
        }
    )

    result = cli._lane_observation(codex, "lane-1", tmp_path)

    assert result["status"] == "inProgress"
    assert result["terminal"] is False
    assert result["runner_status"] == "inProgress"
    assert result["local_runner_status"] == "stale"
    assert result["runner_alive"] is False
    assert result["thread_active"] is True
    assert result["execution_active"] is True
    assert result["execution_source"] == "thread"
    assert result["needs_resume"] is False


def test_lane_observation_hides_final_text_while_owner_is_running(
    tmp_path, monkeypatch
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "current_turn_id": "turn-1",
            "last_status": "inProgress",
            "runner_pid": 123,
        },
        tmp_path,
    )
    monkeypatch.setattr(cli, "process_running", lambda _pid: True)
    codex = FakeReadCodex(
        {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "not final until owner records completion",
                        }
                    ],
                }
            ],
        }
    )

    result = cli._lane_observation(codex, "lane-1", tmp_path)

    assert result["status"] == "inProgress"
    assert result["terminal"] is False
    assert result["final_lead"] is None
    assert result["final_text"] is None


def test_lane_observation_does_not_accept_transient_interrupted_owner_snapshot(
    tmp_path,
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "current_turn_id": "turn-1",
            "last_turn_id": "turn-1",
            "last_status": "inProgress",
            "runner_pid": os.getpid(),
        },
        tmp_path,
    )
    codex = FakeReadCodex(
        {
            "id": "thread-1",
            "turns": [{"id": "turn-1", "status": "interrupted", "items": []}],
        }
    )

    result = cli._lane_observation(codex, "lane-1", tmp_path)

    assert result["status"] == "inProgress"
    assert result["terminal"] is False
    assert result["confidence"] == "medium"


def test_session_list_json_includes_unaliased_main_sessions_by_default(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "CodexAppServer", FakeSessionCodex)
    save_alias(
        "codex",
        "main-lane",
        {"codex_thread_id": "thread-1", "custom_title": "Main lane"},
        tmp_path,
    )

    json_rc = main(
        [
            "codex", "session", "list",
            "--alias-root",
            str(tmp_path),
            "--limit",
            "2",
            "--detail", "metadata",
        ]
    )
    json_output = decode_cli_output(capsys.readouterr().out)
    assert json_rc == 0
    assert json_output["source"] == "codex_app"
    assert json_output["view"] == "main"
    assert [item["name"] for item in json_output["items"]] == [
        "Session title",
        "Scratch session",
    ]
    assert json_output["items"][0]["lane_title"] == "Main lane"
    assert json_output["items"][0]["lane_title_source"] == "custom_title"
    assert json_output["items"][0]["codex_title"] == "Session title"


class FakeReadCodex:
    def __init__(self, thread):
        self.thread = thread

    def read_thread(self, _thread_id, include_turns=False):
        assert include_turns is True
        return {"thread": self.thread}


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class FakeStatusCodex:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, _thread_id, include_turns=False):
        return {
            "thread": {
                "id": "thread-1",
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def get_goal(self, _thread_id):
        return {"objective": "Finish the goal", "status": "active"}


class FakeCompleteCodex(FakeStatusCodex):
    def read_thread(self, _thread_id, include_turns=False):
        return {
            "thread": {
                "id": "thread-1",
                "name": "Closeout lane",
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def get_goal(self, _thread_id):
        return {"objective": "Finish the goal", "status": "complete"}


class FakeActiveStatusCodex(FakeStatusCodex):
    def read_thread(self, _thread_id, include_turns=False):
        return {
            "thread": {
                "id": "thread-1",
                "status": {"type": "active"},
                "turns": [] if include_turns else None,
            }
        }


class FakeGoalCodex:
    goal_statuses = ["active", "complete"]
    timeout = False
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.remaining_goal_statuses = list(self.goal_statuses)
        self.prompts = []
        self.timeouts = []
        self.turn_number = 0
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, _thread_id, include_turns=False):
        return {
            "thread": {
                "id": "thread-1",
                "name": "Goal lane",
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def resume_thread(self, _thread_id, **_kwargs):
        return "thread-1"

    def get_goal(self, _thread_id):
        status = self.remaining_goal_statuses.pop(0)
        return {"objective": "Finish the goal", "status": status}

    def run_turn(
        self,
        thread_id,
        prompt,
        *,
        sandbox=None,
        model=None,
        effort=None,
        workspace_cwd=None,
        runtime_workspace_roots=None,
        additional_context=None,
        timeout=600.0,
        on_started=None,
    ):
        self.timeouts.append(timeout)
        del (
            sandbox,
            model,
            effort,
            workspace_cwd,
            runtime_workspace_roots,
            additional_context,
        )
        self.turn_number += 1
        turn_id = f"turn-{self.turn_number}"
        self.prompts.append(prompt)
        if on_started:
            on_started(turn_id)
        if self.timeout:
            raise TimeoutError("turn timed out after 1s")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            final_text=f"turn {self.turn_number} complete",
            events=["turn/completed"],
        )


class FakeSessionCodex:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def list_threads(self, **_kwargs):
        return {
            "data": [
                {
                    "id": "thread-1",
                    "name": "Session title",
                    "cwd": "/repo",
                    "status": {"type": "idle"},
                    "recencyAt": 1,
                },
                {
                    "id": "thread-2",
                    "name": "Scratch session",
                    "cwd": "/tmp/scratch",
                    "status": {"type": "idle"},
                    "recencyAt": 0,
                },
            ]
        }
