from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.state import load_alias, save_alias
from agent_lane.workspace import WorkspaceError


class FakeRunCodex:
    started_cwd = None

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def start_thread(self, cwd, **_kwargs):
        type(self).started_cwd = cwd
        return "thread-1"

    def set_thread_name(self, _thread_id, _title):
        return None

    def update_git_info(self, _thread_id, _git_info):
        return None

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
            final_text="done",
            events=["turn/completed"],
        )


class FakeActiveCodex:
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
                "status": {"type": "notLoaded"},
                "turns": [{"id": "turn-1", "status": "inProgress"}]
                if include_turns
                else None,
            }
        }


class FakeIdleCodex(FakeActiveCodex):
    def read_thread(self, thread_id, include_turns=False):
        return {
            "thread": {
                "id": thread_id,
                "status": {"type": "notLoaded"},
                "turns": [{"id": "turn-1", "status": "completed"}]
                if include_turns
                else None,
            }
        }


def test_run_creates_and_persists_managed_worktree_lane(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    workspace = {
        "kind": "git-worktree",
        "managed_by": "agent-lane",
        "status": "active",
        "path": str(target),
        "cwd": str(target),
        "source_cwd": str(source),
        "source_repo_root": str(source),
        "base_branch": "main",
        "base_sha": "abc123",
        "branch": "codex/lane-1",
        "app_native_handoff": False,
    }
    monkeypatch.setattr(cli, "CodexAppServer", FakeRunCodex)
    monkeypatch.setattr(cli, "create_managed_worktree", lambda *_args: workspace)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path / "aliases"),
            "--cwd",
            str(source),
            "--worktree",
            "auto",
            "--commit-signing",
            "off",
            "--prompt",
            "implement",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path / "aliases")

    assert rc == 0
    assert result["resumed"] is False
    assert result["cwd"] == str(target)
    assert result["workspace"]["kind"] == "git-worktree"
    assert result["workspace"]["app_native_handoff"] is False
    assert FakeRunCodex.started_cwd == str(target)
    assert alias["workspace"]["branch"] == "codex/lane-1"


def test_cleanup_refuses_an_active_codex_thread(tmp_path, monkeypatch, capsys):
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "last_status": "completed",
            "workspace": {
                "kind": "git-worktree",
                "managed_by": "agent-lane",
                "path": str(tmp_path / "worktree"),
                "source_repo_root": str(tmp_path / "repo"),
            },
        },
        aliases,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeActiveCodex)
    monkeypatch.setattr(
        cli,
        "cleanup_managed_worktree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cleanup must not run")
        ),
    )

    rc = main(
        [
            "codex",
            "cleanup",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "GIT_WORKTREE_THREAD_ACTIVE"


def test_cleanup_requires_explicit_cross_client_inactive_confirmation(
    tmp_path, monkeypatch, capsys
):
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "last_status": "completed",
            "workspace": {
                "kind": "git-worktree",
                "managed_by": "agent-lane",
                "path": str(tmp_path / "worktree"),
            },
        },
        aliases,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeIdleCodex)
    monkeypatch.setattr(
        cli,
        "cleanup_managed_worktree",
        lambda *_args, **_kwargs: {
            "removed": True,
            "worktree_path": str(tmp_path / "worktree"),
            "branch": "codex/lane-1",
            "branch_deleted": False,
            "head_sha": "abc123",
        },
    )

    rc = main(
        [
            "codex",
            "cleanup",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
        ]
    )
    unconfirmed = decode_cli_output(capsys.readouterr().out)
    confirmed_rc = main(
        [
            "codex",
            "cleanup",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(aliases),
            "--confirm-thread-inactive",
        ]
    )
    confirmed = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert unconfirmed["error_code"] == "GIT_WORKTREE_THREAD_STATE_UNVERIFIABLE"
    assert unconfirmed["option"] == "--confirm-thread-inactive"
    assert confirmed_rc == 0
    assert confirmed["cleanup"]["removed"] is True


def test_run_and_cleanup_share_the_same_lane_lock(tmp_path):
    parser = build_parser()
    run = parser.parse_args(
        [
            "codex",
            "run",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "run",
        ]
    )
    cleanup = parser.parse_args(
        [
            "codex",
            "cleanup",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )

    with cli._command_locks(run):
        with pytest.raises(WorkspaceError) as caught:
            with cli._command_locks(cleanup):
                pass

    assert caught.value.error_code == "LANE_OPERATION_BUSY"
