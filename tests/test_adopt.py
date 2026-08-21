from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.state import load_alias, save_alias
from agent_lane.workspace import WorkspaceError


class FakeAdoptCodex:
    cwd = None
    turns = []
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
                "turns": list(self.turns) if include_turns else None,
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
    workspace.mkdir()
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
            str(workspace),
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
    assert alias["workspace_binding_source"] == "explicit_attach"
    assert alias["custom_title"] == "Custom lane title"
    assert "title" not in alias
    assert "lane_label" not in alias
    assert alias["codex_title"] == "App task"
    assert result["lane_title"] == "Custom lane title"
    assert result["lane_title_source"] == "custom_title"
    assert result["workspace_preflight"] == {
        "status": "matched",
        "configured_cwd": str(workspace),
        "thread_cwd": str(workspace),
        "observed_cwd": None,
        "observed_worktree": None,
        "source": "thread",
    }
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


def test_attach_rejects_explicit_cwd_different_from_thread_without_command_cwd(
    tmp_path, monkeypatch, capsys
):
    thread_workspace = tmp_path / "thread-workspace"
    requested_workspace = tmp_path / "requested-workspace"
    thread_workspace.mkdir()
    requested_workspace.mkdir()
    aliases = tmp_path / "aliases"
    FakeAdoptCodex.cwd = thread_workspace
    FakeAdoptCodex.turns = []
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex",
            "session",
            "attach",
            "--mode",
            "independent",
            "--thread-id",
            "thread-app",
            "--cwd",
            str(requested_workspace),
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_ATTACH_WORKSPACE_DRIFT"
    assert result["configured_cwd"] == str(requested_workspace)
    assert result["thread_cwd"] == str(thread_workspace)
    assert result["observed_cwd"] is None
    assert result["workspace_evidence_source"] == "thread"
    assert result["recommended_cwd"] == str(thread_workspace)
    assert result["recommended_attach_argv"][-2:] == [
        "--cwd",
        str(thread_workspace),
    ]
    assert load_alias("codex", result["lane_id"], aliases) is None


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


def test_attach_rejects_recent_command_in_sibling_worktree_before_binding(
    tmp_path, monkeypatch, capsys
):
    configured = tmp_path / "main"
    observed = tmp_path / "worktree"
    configured.mkdir()
    observed.mkdir()
    aliases = tmp_path / "aliases"
    FakeAdoptCodex.cwd = configured
    monkeypatch.setattr(
        FakeAdoptCodex,
        "turns",
        [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "id": "item-1",
                        "type": "commandExecution",
                        "cwd": str(observed),
                        "command": "git status",
                        "commandActions": [{"type": "read"}],
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)
    monkeypatch.setattr(
        cli,
        "sibling_worktree_drift",
        lambda candidate, actual: (
            None
            if str(candidate) == str(actual)
            else {
                "configured_worktree": str(configured),
                "observed_worktree": str(observed),
                "git_common_dir": str(tmp_path / ".git"),
            }
        ),
    )

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--thread-id", "thread-app",
            "--alias-root", str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_ATTACH_WORKSPACE_DRIFT"
    assert result["codex_thread_id"] == "thread-app"
    assert result["configured_cwd"] == str(configured)
    assert result["observed_cwd"] == str(observed)
    assert result["observed_worktree"] == str(observed)
    assert result["recommended_cwd"] == str(observed)
    assert result["replacement_required"] is False
    assert result["recommended_attach_argv"][-2:] == ["--cwd", str(observed)]
    assert load_alias("codex", result["lane_id"], aliases) is None


def test_attach_accepts_explicit_recent_command_worktree_and_preserves_it(
    tmp_path, monkeypatch, capsys
):
    configured = tmp_path / "main"
    observed = tmp_path / "worktree"
    configured.mkdir()
    observed.mkdir()
    aliases = tmp_path / "aliases"
    FakeAdoptCodex.cwd = configured
    monkeypatch.setattr(
        FakeAdoptCodex,
        "turns",
        [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "id": "item-1",
                        "type": "commandExecution",
                        "cwd": str(observed),
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)
    monkeypatch.setattr(
        cli,
        "sibling_worktree_drift",
        lambda candidate, actual: (
            None
            if str(candidate) == str(actual)
            else {
                "configured_worktree": str(configured),
                "observed_worktree": str(observed),
                "git_common_dir": str(tmp_path / ".git"),
            }
        ),
    )

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--thread-id", "thread-app",
            "--cwd", str(observed),
            "--alias-root", str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", result["lane_id"], aliases)

    assert rc == 0, result
    assert result["cwd"] == str(observed)
    assert result["workspace_preflight"]["status"] == "matched"
    assert result["workspace_preflight"]["observed_cwd"] == str(observed)
    assert alias["cwd"] == str(observed)
    assert alias["workspace_binding_source"] == "explicit_attach"


def test_attach_requires_run_replacement_for_managed_lane_workspace_change(
    tmp_path, monkeypatch, capsys
):
    configured = tmp_path / "main"
    observed = tmp_path / "worktree"
    configured.mkdir()
    observed.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(configured),
            "execution_mode": "independent",
            "execution_mode_source": "explicit",
        },
        aliases,
    )
    FakeAdoptCodex.cwd = observed
    monkeypatch.setattr(
        FakeAdoptCodex,
        "turns",
        [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "id": "item-1",
                        "type": "commandExecution",
                        "cwd": str(observed),
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeAdoptCodex)

    rc = main(
        [
            "codex", "session", "attach", "--mode", "independent",
            "--lane-id", "lane-1",
            "--thread-id", "thread-app",
            "--cwd", str(observed),
            "--alias-root", str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", aliases)

    assert rc == 1
    assert result["error_code"] == "CODEX_ATTACH_WORKSPACE_DRIFT"
    assert result["replacement_required"] is True
    assert result["required_action"] == "run_workspace_rebind"
    assert result["recommended_attach_argv"] is None
    assert result["recommended_cwd"] == str(observed)
    assert result["recovery"] == {
        "command": "run",
        "lane_id": "lane-1",
        "cwd": str(observed),
        "thread_action": "replace",
    }
    assert alias["cwd"] == str(configured)


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
            "custom_title": "App task",
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
