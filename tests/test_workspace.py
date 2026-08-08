import subprocess
from pathlib import Path

import pytest

from agent_lane.workspace import (
    WorkspaceError,
    cleanup_managed_worktree,
    create_managed_worktree,
    git_worktree_identity,
    operation_lock,
    sibling_worktree_drift,
    workspace_binding_changed,
    workspace_snapshot,
)


def test_create_and_cleanup_managed_worktree_preserves_nested_cwd(tmp_path):
    repo = _repo(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add nested cwd")

    workspace = create_managed_worktree(str(nested), "lane-1")
    worktree = Path(workspace["path"])

    assert workspace["managed_by"] == "agent-lane"
    assert workspace["branch"] == "codex/lane-1"
    assert Path(workspace["cwd"]) == worktree / "src" / "pkg"
    assert worktree.is_dir()
    assert workspace_snapshot(
        workspace["cwd"], workspace, branch=workspace["branch"], dirty=False
    ) == {
        "kind": "git-worktree",
        "managed_by": "agent-lane",
        "status": "active",
        "path": str(worktree),
        "cwd": str(worktree / "src" / "pkg"),
        "exists": True,
        "branch": "codex/lane-1",
        "dirty": False,
        "app_native_handoff": False,
    }

    result = cleanup_managed_worktree(
        workspace, expected_lane_id="lane-1", delete_branch=True
    )

    assert result["removed"] is True
    assert result["branch_deleted"] is True
    assert not worktree.exists()
    assert not _git_ok(repo, "show-ref", "--verify", "refs/heads/codex/lane-1")


def test_create_managed_worktree_refuses_dirty_source(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorkspaceError) as caught:
        create_managed_worktree(str(repo), "lane-1")

    assert caught.value.error_code == "GIT_WORKTREE_SOURCE_DIRTY"


def test_create_managed_worktree_sanitizes_lane_id_for_git_ref(tmp_path):
    repo = _repo(tmp_path)

    workspace = create_managed_worktree(str(repo), "team:lane.lock")

    assert workspace["branch"] == "codex/team-lane"
    cleanup_managed_worktree(
        workspace, expected_lane_id="team:lane.lock", delete_branch=True
    )


def test_create_managed_worktree_rolls_back_when_nested_cwd_is_not_tracked(
    tmp_path,
):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore scratch directory")
    ignored = repo / "ignored"
    ignored.mkdir()

    with pytest.raises(WorkspaceError) as caught:
        create_managed_worktree(str(ignored), "lane-1")

    assert caught.value.error_code == "GIT_WORKTREE_CWD_MISSING"
    assert caught.value.details["worktree_removed"] is True
    assert caught.value.details["branch_removed"] is True
    assert not _git_ok(repo, "show-ref", "--verify", "refs/heads/codex/lane-1")


def test_cleanup_refuses_unmerged_worktree_commits(tmp_path):
    repo = _repo(tmp_path)
    workspace = create_managed_worktree(str(repo), "lane-1")
    worktree = Path(workspace["path"])
    (worktree / "lane.txt").write_text("lane\n", encoding="utf-8")
    _git(worktree, "add", "lane.txt")
    _git(worktree, "commit", "-qm", "lane commit")

    with pytest.raises(WorkspaceError) as caught:
        cleanup_managed_worktree(workspace, expected_lane_id="lane-1")

    assert caught.value.error_code == "GIT_WORKTREE_UNMERGED"
    assert worktree.is_dir()


def test_cleanup_refuses_ignored_files_that_git_would_remove(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore cache")
    workspace = create_managed_worktree(str(repo), "lane-1")
    worktree = Path(workspace["path"])
    cache = worktree / "cache"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"important local data")

    with pytest.raises(WorkspaceError) as caught:
        cleanup_managed_worktree(workspace, expected_lane_id="lane-1")

    assert caught.value.error_code == "GIT_WORKTREE_IGNORED_FILES"
    assert caught.value.details["ignored_files"] == ["cache/data.bin"]
    assert (cache / "data.bin").read_bytes() == b"important local data"


def test_cleanup_rejects_branch_metadata_that_does_not_match_worktree(tmp_path):
    repo = _repo(tmp_path)
    workspace = create_managed_worktree(str(repo), "lane-1")
    workspace["branch"] = "main"

    with pytest.raises(WorkspaceError) as caught:
        cleanup_managed_worktree(
            workspace, expected_lane_id="lane-1", delete_branch=True
        )

    assert caught.value.error_code == "GIT_WORKTREE_METADATA_INVALID"
    assert Path(workspace["path"]).is_dir()
    assert _git_ok(repo, "show-ref", "--verify", "refs/heads/main")


def test_cleanup_uses_common_git_dir_after_source_worktree_is_removed(tmp_path):
    repo = _repo(tmp_path)
    source = tmp_path / "source-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "source-branch", str(source))
    workspace = create_managed_worktree(str(source), "lane-1")
    _git(repo, "worktree", "remove", str(source))

    result = cleanup_managed_worktree(workspace, expected_lane_id="lane-1")

    assert result["removed"] is True
    assert not Path(workspace["path"]).exists()


def test_cleanup_rejects_incomplete_managed_metadata():
    with pytest.raises(WorkspaceError) as caught:
        cleanup_managed_worktree(
            {"kind": "git-worktree", "managed_by": "agent-lane"},
            expected_lane_id="lane-1",
        )

    assert caught.value.error_code == "GIT_WORKTREE_METADATA_INVALID"


def test_cleanup_rejects_workspace_owned_by_another_lane(tmp_path):
    repo = _repo(tmp_path)
    workspace = create_managed_worktree(str(repo), "lane-b")

    with pytest.raises(WorkspaceError) as caught:
        cleanup_managed_worktree(workspace, expected_lane_id="lane-a")

    assert caught.value.error_code == "GIT_WORKTREE_METADATA_INVALID"
    assert caught.value.details == {
        "recorded_lane_id": "lane-b",
        "expected_lane_id": "lane-a",
    }
    assert Path(workspace["path"]).is_dir()

    cleanup_managed_worktree(
        workspace, expected_lane_id="lane-b", delete_branch=True
    )


def test_workspace_snapshot_distinguishes_local_and_linked_worktree(tmp_path):
    repo = _repo(tmp_path)
    workspace = create_managed_worktree(str(repo), "lane-1")

    assert workspace_snapshot(str(repo))["kind"] == "local"
    detected = workspace_snapshot(workspace["cwd"])
    assert detected["kind"] == "git-worktree"
    assert detected["managed_by"] is None
    assert detected["app_native_handoff"] is None


def test_sibling_worktree_drift_distinguishes_same_repo_worktrees(tmp_path):
    repo = _repo(tmp_path)
    sibling = tmp_path / "repo-sibling"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(sibling))
    nested = repo / "nested"
    nested.mkdir()

    main_identity = git_worktree_identity(nested)
    sibling_identity = git_worktree_identity(sibling / "tracked.txt")
    drift = sibling_worktree_drift(nested, sibling)

    assert main_identity is not None
    assert sibling_identity is not None
    assert drift == {
        "configured_worktree": str(repo.resolve()),
        "observed_worktree": str(sibling.resolve()),
        "git_common_dir": main_identity["git_common_dir"],
    }
    assert sibling_worktree_drift(repo, nested) is None
    assert workspace_binding_changed(nested, repo) is False
    assert workspace_binding_changed(repo, sibling) is True


def test_sibling_worktree_drift_ignores_another_repository(tmp_path):
    repo = _repo(tmp_path)
    other_parent = tmp_path / "other"
    other_parent.mkdir()
    other = _repo(other_parent)

    assert sibling_worktree_drift(repo, other) is None
    assert workspace_binding_changed(repo, other) is True


def test_operation_lock_refuses_concurrent_lane_mutation(tmp_path):
    with operation_lock(tmp_path, "lane-1"):
        with pytest.raises(WorkspaceError) as caught:
            with operation_lock(tmp_path, "lane-1"):
                pass

    assert caught.value.error_code == "LANE_OPERATION_BUSY"
    assert caught.value.details["retryable"] is True


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "agent-lane@example.invalid")
    _git(repo, "config", "user.name", "Agent Lane Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial commit")
    return repo


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_ok(cwd: Path, *args: str) -> bool:
    return (
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).returncode
        == 0
    )
