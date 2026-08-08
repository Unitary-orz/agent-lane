"""Git workspace lifecycle and diagnostics for agent lanes."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .state import safe_lane_id


class WorkspaceError(RuntimeError):
    """A stable, machine-readable workspace operation failure."""

    def __init__(self, error_code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "error": str(self),
            **self.details,
        }


def git_worktree_identity(path_value: str | os.PathLike[str]) -> dict[str, str] | None:
    """Return the registered worktree and common Git directory for one path."""
    path = Path(path_value).expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return None

    worktree_root = _git_text(probe, "rev-parse", "--show-toplevel")
    common_dir = _git_text(
        probe,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if not worktree_root or not common_dir:
        return None
    return {
        "worktree_root": str(Path(worktree_root).resolve()),
        "git_common_dir": str(Path(common_dir).resolve()),
    }


def sibling_worktree_drift(
    configured_path: str | os.PathLike[str],
    observed_path: str | os.PathLike[str],
) -> dict[str, str] | None:
    """Describe an observed path in another worktree of the configured repo."""
    configured = git_worktree_identity(configured_path)
    observed = git_worktree_identity(observed_path)
    if configured is None or observed is None:
        return None
    if configured["git_common_dir"] != observed["git_common_dir"]:
        return None
    if configured["worktree_root"] == observed["worktree_root"]:
        return None
    return {
        "configured_worktree": configured["worktree_root"],
        "observed_worktree": observed["worktree_root"],
        "git_common_dir": configured["git_common_dir"],
    }


def workspace_binding_changed(
    stored_cwd: str | os.PathLike[str],
    requested_cwd: str | os.PathLike[str],
) -> bool:
    """Return whether an explicit cwd moves a lane to another workspace."""
    stored = Path(stored_cwd).expanduser().resolve()
    requested = Path(requested_cwd).expanduser().resolve()
    if stored == requested:
        return False

    stored_git = git_worktree_identity(stored)
    requested_git = git_worktree_identity(requested)
    if stored_git is not None and requested_git is not None:
        return (
            stored_git["git_common_dir"] != requested_git["git_common_dir"]
            or stored_git["worktree_root"] != requested_git["worktree_root"]
        )
    return True


def create_managed_worktree(source_cwd: str, lane_id: str) -> dict[str, Any]:
    """Create a clean, branch-backed Git worktree for a new lane."""
    source = Path(source_cwd).expanduser().resolve()
    if not source.is_dir():
        raise WorkspaceError(
            "GIT_WORKTREE_SOURCE_MISSING",
            f"worktree source is not a directory: {source}",
            source_cwd=str(source),
        )

    repo_root = _git_text(source, "rev-parse", "--show-toplevel")
    if not repo_root:
        raise WorkspaceError(
            "GIT_WORKTREE_SOURCE_NOT_REPO",
            "--worktree requires --cwd inside a Git repository",
            source_cwd=str(source),
        )
    repo = Path(repo_root).resolve()
    try:
        relative_cwd = source.relative_to(repo)
    except ValueError as exc:
        raise WorkspaceError(
            "GIT_WORKTREE_SOURCE_INVALID",
            "resolved cwd is outside the Git worktree root",
            source_cwd=str(source),
            repo_root=str(repo),
        ) from exc

    status = _git_text(repo, "status", "--porcelain", "--untracked-files=all") or ""
    if status:
        raise WorkspaceError(
            "GIT_WORKTREE_SOURCE_DIRTY",
            "refusing to create a worktree from a dirty checkout",
            source_cwd=str(source),
            repo_root=str(repo),
            status_short=status.splitlines()[:100],
        )

    base_sha = _required_git_text(repo, "rev-parse", "HEAD")
    base_branch = _git_text(repo, "symbolic-ref", "--short", "-q", "HEAD")
    git_common_dir = _required_git_text(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    safe_id = safe_lane_id(lane_id)
    branch = f"codex/{_git_branch_component(safe_id)}"
    target_root = repo.parent / f".{repo.name}-agent-lane-worktrees" / safe_id

    if target_root.exists():
        raise WorkspaceError(
            "GIT_WORKTREE_PATH_EXISTS",
            f"managed worktree path already exists: {target_root}",
            worktree_path=str(target_root),
        )
    if _git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        raise WorkspaceError(
            "GIT_WORKTREE_BRANCH_EXISTS",
            f"managed worktree branch already exists: {branch}",
            branch=branch,
        )

    target_root.parent.mkdir(parents=True, exist_ok=True)
    completed = _run_git(
        repo,
        "worktree",
        "add",
        "-b",
        branch,
        str(target_root),
        base_sha,
        timeout=30,
    )
    if completed.returncode != 0:
        raise WorkspaceError(
            "GIT_WORKTREE_CREATE_FAILED",
            completed.stderr.strip() or "git worktree add failed",
            repo_root=str(repo),
            worktree_path=str(target_root),
            branch=branch,
        )

    work_cwd = (target_root / relative_cwd).resolve()
    if not work_cwd.is_dir():
        removed = _run_git(repo, "worktree", "remove", str(target_root), timeout=30)
        branch_removed = _run_git(repo, "branch", "-d", branch, timeout=10)
        raise WorkspaceError(
            "GIT_WORKTREE_CWD_MISSING",
            "source cwd is not materialized in the new worktree",
            source_cwd=str(source),
            worktree_path=str(target_root),
            worktree_cwd=str(work_cwd),
            branch=branch,
            worktree_removed=removed.returncode == 0,
            branch_removed=branch_removed.returncode == 0,
        )
    return {
        "kind": "git-worktree",
        "managed_by": "agent-lane",
        "status": "active",
        "path": str(target_root),
        "cwd": str(work_cwd),
        "source_cwd": str(source),
        "source_repo_root": str(repo),
        "git_common_dir": git_common_dir,
        "lane_id": lane_id,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "branch": branch,
        "created_at": time.time(),
        "app_native_handoff": False,
    }


def cleanup_managed_worktree(
    workspace: dict[str, Any],
    *,
    expected_lane_id: str,
    delete_branch: bool = False,
) -> dict[str, Any]:
    """Remove a clean, merged agent-lane worktree without forcing Git state."""
    if (
        workspace.get("managed_by") != "agent-lane"
        or workspace.get("kind") != "git-worktree"
    ):
        raise WorkspaceError(
            "GIT_WORKTREE_NOT_MANAGED",
            "cleanup only supports agent-lane managed Git worktrees",
        )
    raw_path = workspace.get("path")
    raw_repo = workspace.get("source_repo_root")
    raw_common_dir = workspace.get("git_common_dir")
    lane_id = workspace.get("lane_id")
    if not raw_path or not raw_repo or not raw_common_dir or not lane_id:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree metadata is incomplete",
        )
    if str(lane_id) != expected_lane_id:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree belongs to a different lane",
            recorded_lane_id=str(lane_id),
            expected_lane_id=expected_lane_id,
        )
    path = Path(str(raw_path)).expanduser().resolve()
    repo = Path(str(raw_repo)).expanduser().resolve()
    common_dir = Path(str(raw_common_dir)).expanduser().resolve()
    expected_root = repo.parent / f".{repo.name}-agent-lane-worktrees"
    expected_path = expected_root / safe_lane_id(str(lane_id))
    try:
        relative_path = path.relative_to(expected_root)
    except ValueError as exc:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree path is outside the expected agent-lane directory",
            worktree_path=str(path),
        ) from exc
    if relative_path == Path(".") or path != expected_path:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree metadata points to the container directory",
            worktree_path=str(path),
        )
    branch = str(workspace.get("branch") or "")
    base_branch = workspace.get("base_branch")
    base_sha = str(workspace.get("base_sha") or "")

    if not path.is_dir():
        raise WorkspaceError(
            "GIT_WORKTREE_PATH_MISSING",
            f"managed worktree directory is missing: {path}",
            worktree_path=str(path),
        )
    actual_root = _git_text(path, "rev-parse", "--show-toplevel")
    if not actual_root or Path(actual_root).resolve() != path:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree path is not a registered Git worktree root",
            worktree_path=str(path),
        )
    actual_common_dir = _git_text(
        path,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if not actual_common_dir or Path(actual_common_dir).resolve() != common_dir:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree common Git directory does not match its metadata",
            worktree_path=str(path),
        )
    expected_branch = f"codex/{_git_branch_component(safe_lane_id(str(lane_id)))}"
    actual_branch = _git_text(path, "symbolic-ref", "--short", "-q", "HEAD")
    if not actual_branch or branch != expected_branch or actual_branch != branch:
        raise WorkspaceError(
            "GIT_WORKTREE_METADATA_INVALID",
            "managed worktree branch does not match its metadata",
            worktree_path=str(path),
            recorded_branch=branch or None,
            actual_branch=actual_branch,
            expected_branch=expected_branch,
        )
    status = _git_text(path, "status", "--porcelain", "--untracked-files=all") or ""
    if status:
        raise WorkspaceError(
            "GIT_WORKTREE_DIRTY",
            "refusing to remove a dirty managed worktree",
            worktree_path=str(path),
            status_short=status.splitlines()[:100],
        )
    ignored = (
        _git_text(path, "ls-files", "--others", "--ignored", "--exclude-standard")
        or ""
    )
    if ignored:
        raise WorkspaceError(
            "GIT_WORKTREE_IGNORED_FILES",
            "refusing to remove a managed worktree that contains ignored files",
            worktree_path=str(path),
            ignored_files=ignored.splitlines()[:100],
        )

    head_sha = _required_git_text(path, "rev-parse", "HEAD")
    merged = head_sha == base_sha
    merge_target = str(base_branch or base_sha)
    if not merged and merge_target:
        merged = _git_dir_ok(
            common_dir,
            "merge-base",
            "--is-ancestor",
            head_sha,
            merge_target,
        )
    if not merged:
        raise WorkspaceError(
            "GIT_WORKTREE_UNMERGED",
            "refusing to remove a managed worktree with unmerged commits",
            worktree_path=str(path),
            branch=branch or None,
            head_sha=head_sha,
            merge_target=merge_target or None,
        )

    completed = _run_git_dir(
        common_dir,
        "worktree",
        "remove",
        str(path),
        timeout=30,
    )
    if completed.returncode != 0:
        raise WorkspaceError(
            "GIT_WORKTREE_REMOVE_FAILED",
            completed.stderr.strip() or "git worktree remove failed",
            worktree_path=str(path),
        )

    branch_deleted = False
    if delete_branch:
        completed = _run_git_dir(
            common_dir,
            "branch",
            "-d",
            actual_branch,
            timeout=10,
        )
        if completed.returncode != 0:
            raise WorkspaceError(
                "GIT_WORKTREE_BRANCH_DELETE_FAILED",
                completed.stderr.strip() or "git branch -d failed",
                branch=actual_branch,
                worktree_removed=True,
            )
        branch_deleted = True

    return {
        "removed": True,
        "worktree_path": str(path),
        "branch": actual_branch,
        "branch_deleted": branch_deleted,
        "head_sha": head_sha,
    }


def workspace_snapshot(
    cwd: str | None,
    workspace: dict[str, Any] | None = None,
    *,
    branch: str | None = None,
    dirty: bool | None = None,
) -> dict[str, Any]:
    """Classify a cwd without claiming unsupported App lifecycle ownership."""
    metadata = dict(workspace or {})
    raw_cwd = cwd or metadata.get("cwd") or metadata.get("path")
    path = Path(str(raw_cwd)).expanduser().resolve() if raw_cwd else None
    exists = bool(path and path.is_dir())
    managed_by = metadata.get("managed_by")
    status = str(metadata.get("status") or ("active" if exists else "missing"))

    linked_worktree = False
    if exists and path is not None and managed_by not in {"agent-lane", "codex-app"}:
        git_dir = _git_text(path, "rev-parse", "--absolute-git-dir")
        common_dir = _git_text(
            path, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if git_dir and common_dir:
            linked_worktree = Path(git_dir).resolve() != Path(common_dir).resolve()

    if managed_by == "agent-lane":
        kind = "git-worktree"
    elif managed_by == "codex-app":
        kind = "app-worktree"
    elif exists and path is not None and linked_worktree:
        if _is_codex_app_worktree(path):
            kind = "app-worktree"
            managed_by = "codex-app"
        else:
            kind = "git-worktree"
    elif exists:
        kind = "local"
    elif metadata.get("kind"):
        kind = str(metadata["kind"])
    else:
        kind = "unknown"

    if kind == "app-worktree":
        app_handoff: bool | None = True
    elif managed_by == "agent-lane":
        app_handoff = False
    else:
        app_handoff = None

    return {
        "kind": kind,
        "managed_by": managed_by,
        "status": status,
        "path": metadata.get("path") or (str(path) if path else None),
        "cwd": str(path) if path else None,
        "exists": exists,
        "branch": branch or metadata.get("branch"),
        "dirty": dirty,
        "app_native_handoff": app_handoff,
    }


def _is_codex_app_worktree(path: Path) -> bool:
    root = (Path.home() / ".codex" / "worktrees").resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _git_branch_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    component = re.sub(r"\.{2,}", "-", component)
    component = component.replace("@{", "-").strip(".-")
    while component.endswith(".lock"):
        component = component[: -len(".lock")].rstrip(".-")
    if not component:
        raise WorkspaceError(
            "GIT_WORKTREE_BRANCH_INVALID",
            "lane-id cannot produce a valid managed Git branch name",
            lane_id=value,
        )
    return component


def _required_git_text(cwd: Path, *parts: str) -> str:
    value = _git_text(cwd, *parts)
    if value is None:
        raise WorkspaceError(
            "GIT_COMMAND_FAILED",
            f"git {' '.join(parts)} failed in {cwd}",
        )
    return value


def _git_text(cwd: Path, *parts: str) -> str | None:
    completed = _run_git(cwd, *parts)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _git_ok(cwd: Path, *parts: str) -> bool:
    return _run_git(cwd, *parts).returncode == 0


def _git_dir_ok(git_dir: Path, *parts: str) -> bool:
    return _run_git_dir(git_dir, *parts).returncode == 0


def _run_git(
    cwd: Path, *parts: str, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *parts],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(
            "GIT_COMMAND_FAILED",
            f"git {' '.join(parts)} failed: {exc}",
        ) from exc


def _run_git_dir(
    git_dir: Path, *parts: str, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    cwd = git_dir.parent if git_dir.parent.is_dir() else Path("/")
    try:
        return subprocess.run(
            ["git", "--git-dir", str(git_dir), *parts],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(
            "GIT_COMMAND_FAILED",
            f"git --git-dir {git_dir} {' '.join(parts)} failed: {exc}",
        ) from exc


@contextmanager
def operation_lock(
    alias_root: Path,
    key: str,
    *,
    namespace: str = "lanes",
    wait_timeout: float = 0.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Serialize destructive or identity-changing lane operations."""
    if wait_timeout < 0:
        raise ValueError("wait_timeout must be non-negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")
    lock_dir = alias_root / "codex" / ".locks" / safe_lane_id(namespace)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_lane_id(key)}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        deadline = time.monotonic() + wait_timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError(
                        "LANE_OPERATION_BUSY",
                        "another agent-lane operation holds this lock",
                        lock_key=key,
                        wait_timeout=wait_timeout,
                        retryable=True,
                    ) from exc
                time.sleep(min(poll_interval, remaining))
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
