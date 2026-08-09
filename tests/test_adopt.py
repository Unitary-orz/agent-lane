from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.state import load_alias, save_alias
from agent_lane.workspace import WorkspaceError


class FakeAdoptCodex:
    cwd = None
    resumed_cwd = None
    transports = []

    def __init__(self, *_args, **_kwargs):
        type(self).transports.append(_kwargs.get("transport"))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        return {
            "thread": {
                "id": thread_id,
                "name": "App task",
                "cwd": str(self.cwd),
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def resume_thread(self, _thread_id, *, cwd=None, **_kwargs):
        type(self).resumed_cwd = cwd
        return {}

    def run_turn(
        self,
        thread_id,
        _prompt,
        *,
        sandbox=None,
        model=None,
        effort=None,
        workspace_cwd=None,
        runtime_workspace_roots=None,
        additional_context=None,
        timeout=600,
        on_started=None,
    ):
        del (
            sandbox,
            model,
            effort,
            workspace_cwd,
            runtime_workspace_roots,
            additional_context,
            timeout,
        )
        if on_started:
            on_started("turn-1")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-1",
            status="completed",
            final_text="continued",
            events=["turn/completed"],
        )

    def get_goal(self, _thread_id):
        return None


def test_adopt_binds_existing_app_thread_to_lane(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    fallback = tmp_path / "fallback"
    workspace.mkdir()
    fallback.mkdir()
    FakeAdoptCodex.cwd = workspace
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "app-task",
            "--thread-id",
            "thread-app",
            "--cwd",
            str(fallback),
            "--title",
            "Custom lane title",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "app-task", tmp_path / "aliases")

    assert rc == 0
    assert result["adopted"] is True
    assert result["workspace"]["kind"] == "local"
    assert alias["codex_thread_id"] == "thread-app"
    assert alias["execution_mode"] == "independent"
    assert alias["execution_mode_source"] == "explicit"
    assert alias["adopted_from"] == "codex-app"
    assert alias["cwd"] == str(workspace)
    assert alias["title"] == "Custom lane title"
    assert alias["lane_label"] == "Custom lane title"
    assert alias["codex_title"] == "App task"
    assert result["title"] == "App task"
    assert result["title_source"] == "codex_title"
    assert result["control"] == {
        "binding_status": "attached",
        "control_ready": True,
        "requires_explicit_attach": False,
        "lane_id": "app-task",
        "thread_id": "thread-app",
        "suggested_lane_id": None,
        "target_argv": [
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(tmp_path / "aliases"),
        ],
        "lane_target_argv": [
            "--lane-id",
            "app-task",
            "--alias-root",
            str(tmp_path / "aliases"),
        ],
        "attach_argv": None,
        "send_target_argv": [
            "codex",
            "send",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(tmp_path / "aliases"),
        ],
    }


def test_attach_defaults_to_independent_without_implicit_send(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    FakeAdoptCodex.cwd = workspace
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex",
            "session",
            "attach",
            "--lane-id",
            "app-task",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["adopted"] is True
    assert result["execution_mode"] == "independent"
    assert result["execution_mode_source"] == "default"
    assert result["control"]["binding_status"] == "attached"
    assert result["control"]["send_target_argv"] == [
        "codex",
        "send",
        "--thread-id",
        "thread-app",
        "--alias-root",
        str(tmp_path / "aliases"),
    ]


def test_attach_can_explicitly_rebind_existing_lane_to_app_sync(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "app-task",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "execution_mode": "independent",
            "execution_mode_source": "default",
        },
        aliases,
    )
    FakeAdoptCodex.cwd = workspace
    FakeAdoptCodex.transports = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex",
            "session",
            "attach",
            "--thread-id",
            "thread-app",
            "--mode",
            "app-sync",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "app-task", aliases)

    assert rc == 0, result
    assert result["lane_id"] == "app-task"
    assert result["execution_mode"] == "app-sync"
    assert result["execution_mode_source"] == "explicit"
    assert alias["execution_mode"] == "app-sync"
    assert alias["binding"]["execution_mode"] == "app-sync"
    assert FakeAdoptCodex.transports == ["daemon"]


def test_adopt_rejects_thread_bound_to_another_lane(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    FakeAdoptCodex.cwd = workspace
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "existing",
        {"codex_thread_id": "thread-app", "cwd": str(workspace)},
        aliases,
    )

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "new-lane",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_THREAD_ALREADY_ALIASED"
    assert result["lane_id"] == "existing"


def test_adopt_is_idempotent_without_reclassifying_managed_worktree(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    managed = {
        "kind": "git-worktree",
        "managed_by": "agent-lane",
        "status": "active",
        "path": str(workspace),
        "cwd": str(workspace),
        "branch": "codex/app-task",
    }
    save_alias(
        "codex",
        "app-task",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "workspace": managed,
        },
        aliases,
    )
    FakeAdoptCodex.cwd = workspace
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "app-task",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "app-task", aliases)

    assert rc == 0
    assert result["adopted"] is False
    assert alias["workspace"] == managed
    assert "adopted_from" not in alias


def test_send_refreshes_adopted_thread_cwd_before_resume(tmp_path, monkeypatch, capsys):
    old_workspace = tmp_path / "old"
    new_workspace = tmp_path / "new"
    old_workspace.mkdir()
    new_workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "app-task",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(old_workspace),
            "title": "App task",
            "sandbox": "workspace-write",
            "model": "gpt-alias",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeAdoptCodex.cwd = new_workspace
    FakeAdoptCodex.resumed_cwd = None
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "app-task",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "off",
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "app-task", aliases)

    assert rc == 0
    assert result["cwd"] == str(new_workspace)
    assert result["requested_model"] == "gpt-alias"
    assert result["requested_model_source"] == "alias"
    assert result["requested_effort"] is None
    assert result["requested_effort_source"] == "unset"
    assert result["effective_effort"] is None
    assert result["effective_effort_source"] == "unset"
    assert FakeAdoptCodex.resumed_cwd == str(new_workspace)
    assert alias["cwd"] == str(new_workspace)
    assert alias["workspace"]["kind"] == "local"
    assert alias["requested_model"] == "gpt-alias"
    assert alias["requested_model_source"] == "alias"
    assert alias["requested_effort"] is None
    assert alias["requested_effort_source"] == "unset"
    assert alias["effective_effort"] is None
    assert alias["effective_effort_source"] == "unset"


@pytest.mark.parametrize("option", ["--adopt-as"])
def test_send_rejects_removed_direct_attach_options(option, capsys):
    rc = main(["codex", "send", option, "thread-app", "--prompt", "continue"])
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 2
    assert result["error_code"] == "CLI_REMOVED"
    assert result["removed"] == option
    assert result["replacement"] == "codex session attach"
    assert result["control_requires_explicit_attach"] is True
    assert result["thread_id"] == "thread-app"


def test_adopt_registry_lock_serializes_thread_binding(tmp_path):
    parser = build_parser()
    first = parser.parse_args(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "lane-1",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    second = parser.parse_args(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "lane-2",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
        ]
    )

    with cli._command_locks(first):
        with pytest.raises(WorkspaceError) as caught:
            with cli._command_locks(second):
                pass

    assert caught.value.error_code == "LANE_OPERATION_BUSY"


def test_adopt_registry_lock_cannot_collide_with_lane_id(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id",
            "__adopt_registry__",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
        ]
    )

    with cli._command_locks(args):
        pass
