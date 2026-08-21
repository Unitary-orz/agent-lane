"""Durable Codex task control plane used by the V1 command layers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .app_sync import (
    app_sync_disable,
    app_sync_enable,
    app_sync_login,
    app_sync_status,
)

from .codex_rpc import (
    CodexAppServer,
    CodexRpcError,
    normalize_sandbox_mode,
)
from .doctor import doctor_report
from .output import success_envelope
from .rollout import read_rollout_closeout
from .signing import (
    init_signing,
    prepare_agent_signing,
    signing_metadata,
    signing_smoke_test,
    signing_status,
    stop_agent,
    thread_signing_probe,
)
from .state import (
    DEFAULT_ALIAS_ROOT,
    alias_path,
    list_aliases,
    load_alias,
    safe_lane_id,
    save_alias as _save_alias_file,
)
from .settings import (
    UserConfigError,
    clear_default_effort,
    normalize_effort,
    read_default_effort,
    set_default_effort,
    user_config_path,
)
from .workspace import (
    WorkspaceError,
    cleanup_managed_worktree,
    create_managed_worktree,
    operation_lock,
    sibling_worktree_drift,
    workspace_binding_changed,
    workspace_snapshot,
)

CODEX_PROVIDER = "codex"
CODEX_ALIAS_SCHEMA_VERSION = 4
REMOVED_CODEX_TITLE_FIELDS = frozenset(
    {
        "title",
        "title_source",
        "lane_label",
        "lane_title",
        "lane_title_source",
    }
)
EXECUTION_MODES = ("independent", "app-sync")
COMMIT_SIGNING_MODES = ("off", "agent")
SENSITIVE_CONFIG_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "password",
        "secret",
        "token",
    }
)
READ_ONLY_STDIO_FALLBACK_ERRORS = frozenset(
    {
        "CODEX_APP_TRANSPORT_UNOBSERVED",
        "CODEX_APP_SHARED_DAEMON_DISCONNECTED",
        "CODEX_DAEMON_SOCKET_INVALID",
        "CODEX_DAEMON_VERSION_INVALID",
        "CODEX_DAEMON_UNAVAILABLE",
    }
)
SESSION_LIST_MAX_PAGES = 100
STEER_LOCK_ROOT = DEFAULT_ALIAS_ROOT

LANE_TARGET_HANDLERS = frozenset(
    {
        "codex.run",
        "codex.send",
        "codex.steer",
        "codex.status",
        "codex.closeout",
        "codex.cleanup",
        "codex.wait",
        "codex.watch",
        "codex.checkpoint",
        "codex.custom-title.get",
        "codex.custom-title.set",
        "codex.custom-title.clear",
        "codex.session.name.get",
        "codex.session.name.set",
        "codex.goal.set",
        "codex.goal.run",
        "codex.goal.get",
        "codex.goal.complete",
        "codex.goal.clear",
    }
)
CREATE_WITHOUT_TARGET_HANDLERS = frozenset({"codex.run", "codex.goal.set"})
READ_ONLY_UNBOUND_TARGET_HANDLERS = frozenset(
    {
        "codex.status",
        "codex.closeout",
        "codex.wait",
        "codex.watch",
        "codex.checkpoint",
        "codex.goal.get",
        "codex.session.name.get",
    }
)


def save_alias(
    provider: str,
    lane_id: str,
    data: dict[str, Any],
    root: Path | None = None,
) -> Path:
    """Persist an alias after applying the current Codex identity contract."""

    if provider == CODEX_PROVIDER:
        _prepare_codex_alias_for_save(data, lane_id=lane_id)
    return _save_alias_file(provider, lane_id, data, root)



def cmd_doctor_v1(args: argparse.Namespace) -> dict[str, Any]:
    result = cmd_codex_doctor(args)
    result["requested_mode"] = args.mode
    if args.mode == "app-sync":
        daemon = result.get("shared_daemon")
        if not isinstance(daemon, dict) or not daemon.get("ready"):
            result.update(
                {
                    "ok": False,
                    "error_code": "APP_SYNC_NOT_READY",
                    "error": "App Sync readiness requirements are not satisfied",
                    "retryable": True,
                }
            )
    return result


def cmd_app_sync_enable(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **app_sync_enable(codex_bin=args.codex_bin)}


def cmd_app_sync_status(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **app_sync_status(codex_bin=args.codex_bin)}


def cmd_app_sync_disable(_args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **app_sync_disable()}


def cmd_app_sync_login(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **app_sync_login(codex_bin=args.codex_bin)}


def cmd_effort_set(args: argparse.Namespace) -> dict[str, Any]:
    setting = _user_effort_operation(set_default_effort, args.value)
    return {"ok": True, "operation": "effort.set", **setting}


def cmd_effort_status(_args: argparse.Namespace) -> dict[str, Any]:
    setting = _user_effort_operation(read_default_effort)
    return {"ok": True, "operation": "effort.status", **setting}


def cmd_effort_clear(_args: argparse.Namespace) -> dict[str, Any]:
    setting = _user_effort_operation(clear_default_effort)
    return {"ok": True, "operation": "effort.clear", **setting}


def _user_effort_operation(operation: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        result = operation(*args)
    except UserConfigError as exc:
        raise WorkspaceError(
            "USER_CONFIG_INVALID",
            str(exc),
            config_path=str(user_config_path()),
            retryable=False,
        ) from exc
    except OSError as exc:
        raise WorkspaceError(
            "USER_CONFIG_WRITE_FAILED",
            f"could not update user config: {exc}",
            config_path=str(user_config_path()),
            retryable=False,
        ) from exc
    return {
        "effective_effort": result["value"],
        "effective_effort_source": result["source"],
        "config_path": str(result["path"]),
    }


def cmd_status_v1(args: argparse.Namespace) -> dict[str, Any]:
    args.include_turns = args.detail == "turns"
    args.brief = args.detail == "summary"
    return cmd_codex_status(args)


def _adapt_session_query_args(args: argparse.Namespace) -> None:
    args.aliases_only = args.scope == "lanes"
    args.include_unaliased = args.scope == "all"
    args.include_subagents = args.threads == "all"
    args.include_last_turn = args.detail == "summary"
    args.refresh = args.observe != "stored"
    args.brief = False


def cmd_session_list_v1(args: argparse.Namespace) -> dict[str, Any]:
    _adapt_session_query_args(args)
    return cmd_codex_recent(args)


def cmd_session_find_v1(args: argparse.Namespace) -> dict[str, Any]:
    _adapt_session_query_args(args)
    return cmd_codex_find(args)


def cmd_session_name_get_v1(args: argparse.Namespace) -> dict[str, Any]:
    alias, thread_id = _resolve_thread_target(args)
    if isinstance(alias, dict):
        args.lane_id = alias.get("lane_id")
        return cmd_codex_name_get(args)
    if not thread_id:
        raise WorkspaceError(
            "CODEX_TARGET_NOT_FOUND",
            "the selected task does not expose a Codex thread id",
            retryable=False,
        )
    codex, transport = _open_read_only_codex(args.observe)
    with codex:
        result = codex.read_thread(thread_id, include_turns=False)
    thread = result.get("thread") or {}
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": None,
        "codex_thread_id": thread_id,
        **_title_contract({}, thread=thread),
        **transport,
    }



def _command_locks(args: argparse.Namespace) -> ExitStack:
    stack = ExitStack()
    try:
        if getattr(args, "provider", None) != CODEX_PROVIDER:
            return stack
        _prepare_command_target(args)
        handler = str(getattr(args, "handler", ""))
        lane_id = getattr(args, "lane_id", None)
        if not lane_id:
            return stack
        alias_root = Path(args.alias_root).expanduser()
        if handler == "codex.session.attach":
            stack.enter_context(
                operation_lock(alias_root, "adopt-registry", namespace="global")
            )
        if handler in {
            "codex.run",
            "codex.send",
            "codex.cleanup",
            "codex.session.attach",
            "codex.session.name.get",
            "codex.session.name.set",
            "codex.custom-title.set",
            "codex.custom-title.clear",
            "codex.goal.set",
            "codex.goal.run",
            "codex.goal.complete",
            "codex.goal.clear",
        }:
            stack.enter_context(operation_lock(alias_root, str(lane_id)))
        expected_thread_id = _nonempty_text(
            getattr(args, "_target_expected_thread_id", None)
        )
        if expected_thread_id is not None:
            current = load_alias(CODEX_PROVIDER, str(lane_id), alias_root)
            observed_thread_id = _nonempty_text(
                (current or {}).get("codex_thread_id")
            )
            if observed_thread_id != expected_thread_id:
                raise WorkspaceError(
                    "CODEX_TARGET_CHANGED",
                    "the resolved task binding changed before the command began",
                    lane_id=lane_id,
                    expected_thread_id=expected_thread_id,
                    observed_thread_id=observed_thread_id,
                    retryable=False,
                )
        return stack
    except Exception:
        stack.close()
        raise


def _prepare_command_target(args: argparse.Namespace) -> None:
    if getattr(args, "_target_prepared", False):
        return
    args._target_prepared = True
    handler = str(getattr(args, "handler", ""))
    if handler == "codex.session.attach":
        _prepare_attach_target(args)
        return
    if handler not in LANE_TARGET_HANDLERS:
        return

    alias_root = Path(args.alias_root).expanduser()
    requested = _requested_target(args)
    if requested is None:
        if handler not in CREATE_WITHOUT_TARGET_HANDLERS:
            return
        lane_id = _new_internal_lane_id(alias_root)
        args.lane_id = lane_id
        args._target_resolution = {
            "requested": {"kind": "new", "value": None},
            "source": "generated_internal",
            "user_supplied_lane_id": False,
            "resolved": {"lane_id": lane_id, "thread_id": None},
        }
        return

    kind = str(requested["kind"])
    value = requested["value"]
    if kind == "lane_id":
        lane_id = str(value)
        alias = _load_target_alias(alias_root, lane_id)
        thread_id = _nonempty_text((alias or {}).get("codex_thread_id"))
        args._target_resolution = {
            "requested": requested,
            "source": "explicit_lane_id",
            "user_supplied_lane_id": True,
            "resolved": {
                "lane_id": lane_id,
                "thread_id": thread_id,
            },
        }
        args._target_expected_thread_id = thread_id
        return

    matches = _target_alias_matches(
        alias_root,
        kind=kind,
        value=str(value),
        allow_invalid_registry=(
            kind == "thread_id" and handler in READ_ONLY_UNBOUND_TARGET_HANDLERS
        ),
    )
    if len(matches) > 1:
        raise _ambiguous_target_error(
            requested,
            matches,
            alias_root=alias_root,
        )
    if len(matches) == 1:
        alias = matches[0]
        lane_id = str(alias["lane_id"])
        thread_id = _nonempty_text(alias.get("codex_thread_id"))
        args.lane_id = lane_id
        args._target_resolution = {
            "requested": requested,
            "source": {
                "thread_id": "thread_binding",
                "title": "exact_title",
                "current": "current_cwd",
            }[kind],
            "user_supplied_lane_id": False,
            "resolved": {"lane_id": lane_id, "thread_id": thread_id},
        }
        args._target_expected_thread_id = thread_id
        return

    if kind == "thread_id" and handler in READ_ONLY_UNBOUND_TARGET_HANDLERS:
        thread_id = str(value)
        args.lane_id = None
        args._direct_thread_id = thread_id
        args._target_resolution = {
            "requested": requested,
            "source": "unbound_thread_read_only",
            "user_supplied_lane_id": False,
            "resolved": {"lane_id": None, "thread_id": thread_id},
        }
        return
    if kind == "thread_id":
        raise _attach_required_error(args, handler, str(value))
    raise WorkspaceError(
        "CODEX_TARGET_NOT_FOUND",
        "no attached Codex task matches the requested target",
        requested_target=requested,
        discover_argv=["codex", "session", "list", "--scope", "all"],
        retryable=False,
    )


def _prepare_attach_target(args: argparse.Namespace) -> None:
    alias_root = Path(args.alias_root).expanduser()
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not thread_id:
        return
    requested_lane_id = _nonempty_text(getattr(args, "lane_id", None))
    if requested_lane_id is not None:
        _load_target_alias(alias_root, requested_lane_id)
        args.lane_id = requested_lane_id
        args._target_resolution = {
            "requested": {"kind": "thread_id", "value": thread_id},
            "source": "explicit_lane_id",
            "user_supplied_lane_id": True,
            "resolved": {
                "lane_id": requested_lane_id,
                "thread_id": thread_id,
            },
        }
        return
    matches = _target_alias_matches(
        alias_root,
        kind="thread_id",
        value=thread_id,
    )
    if len(matches) > 1:
        raise _ambiguous_target_error(
            {"kind": "thread_id", "value": thread_id},
            matches,
            alias_root=alias_root,
        )
    if matches:
        lane_id = str(matches[0]["lane_id"])
        source = "thread_binding"
    else:
        lane_id = _internal_lane_id_for_thread(thread_id)
        source = "generated_for_attach"
    args.lane_id = lane_id
    args._target_resolution = {
        "requested": {"kind": "thread_id", "value": thread_id},
        "source": source,
        "user_supplied_lane_id": False,
        "resolved": {"lane_id": lane_id, "thread_id": thread_id},
    }


def _requested_target(args: argparse.Namespace) -> dict[str, Any] | None:
    prepared = getattr(args, "_target_resolution", None)
    if isinstance(prepared, dict):
        requested = prepared.get("requested")
        if isinstance(requested, dict) and requested.get("kind") not in {
            None,
            "new",
        }:
            return dict(requested)
    lane_id = _nonempty_text(getattr(args, "lane_id", None))
    if lane_id is not None:
        return {"kind": "lane_id", "value": lane_id}
    thread_id = _nonempty_text(getattr(args, "thread_id", None))
    if thread_id is not None:
        return {"kind": "thread_id", "value": thread_id}
    title = _nonempty_text(getattr(args, "target_title", None))
    if title is not None:
        return {"kind": "title", "value": title}
    if bool(getattr(args, "current", False)):
        return {"kind": "current", "value": str(Path.cwd().resolve())}
    return None


def _target_alias_matches(
    alias_root: Path,
    *,
    kind: str,
    value: str,
    allow_invalid_registry: bool = False,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    requested_title = value.casefold()
    requested_cwd = (
        Path(value).expanduser().resolve(strict=False)
        if kind == "current"
        else None
    )
    for raw in _target_alias_registry(
        alias_root,
        allow_invalid=allow_invalid_registry,
    ):
        alias = dict(raw)
        alias.pop("_path", None)
        lane_id = _nonempty_text(alias.get("lane_id"))
        if lane_id is None:
            continue
        matched = False
        if kind == "thread_id":
            matched = _nonempty_text(alias.get("codex_thread_id")) == value
        elif kind == "title":
            titles = {
                title.casefold()
                for title in (
                    _nonempty_text(alias.get("codex_title")),
                    _nonempty_text(alias.get("custom_title")),
                )
                if title is not None
            }
            matched = requested_title in titles
        elif kind == "current":
            raw_cwd = _nonempty_text(alias.get("cwd"))
            matched = bool(
                raw_cwd
                and Path(raw_cwd).expanduser().resolve(strict=False)
                == requested_cwd
            )
        if matched:
            matches.append(alias)
    return sorted(matches, key=lambda item: str(item.get("lane_id") or ""))


def _target_alias_registry(
    alias_root: Path,
    *,
    allow_invalid: bool = False,
) -> list[dict[str, Any]]:
    items = list_aliases(CODEX_PROVIDER, alias_root)
    invalid_entries = [
        {
            "path": str(item.get("_path") or ""),
            "error": str(item.get("error") or "invalid alias entry"),
        }
        for item in items
        if item.get("error")
    ]
    if invalid_entries and not allow_invalid:
        raise WorkspaceError(
            "CODEX_TARGET_REGISTRY_INVALID",
            "cannot safely resolve a task while the lane registry contains "
            "unreadable entries",
            invalid_entries=invalid_entries,
            retryable=False,
        )

    aliases: list[dict[str, Any]] = []
    for raw in items:
        alias = _project_codex_alias(raw)
        if _nonempty_text(alias.get("lane_id")) is None:
            raw_path = _nonempty_text(alias.get("_path"))
            if raw_path is not None:
                alias["lane_id"] = Path(raw_path).stem
        aliases.append(alias)
    return aliases


def _load_target_alias(
    alias_root: Path,
    lane_id: str,
) -> dict[str, Any] | None:
    try:
        alias = load_alias(CODEX_PROVIDER, lane_id, alias_root)
        return _project_codex_alias(alias) if alias is not None else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "CODEX_TARGET_REGISTRY_INVALID",
            "cannot safely resolve a task because its lane alias is unreadable",
            invalid_entries=[
                {
                    "path": str(alias_path(CODEX_PROVIDER, lane_id, alias_root)),
                    "error": str(exc),
                }
            ],
            retryable=False,
        ) from exc


def _target_choice(
    alias: dict[str, Any],
    *,
    alias_root: Path,
) -> dict[str, Any]:
    thread_id = _nonempty_text(alias.get("codex_thread_id"))
    lane_id = str(alias.get("lane_id") or "")
    target_argv = (
        ["--thread-id", thread_id]
        if thread_id is not None
        else ["--lane-id", lane_id]
    )
    target_argv.extend(_alias_root_argv(alias_root))
    return {
        "lane_id": lane_id,
        "thread_id": thread_id,
        "lane_title": _title_contract(alias, lane_id=lane_id)[
            "lane_title"
        ],
        "target_argv": target_argv,
    }


def _ambiguous_target_error(
    requested: dict[str, Any],
    matches: list[dict[str, Any]],
    *,
    alias_root: Path,
) -> WorkspaceError:
    return WorkspaceError(
        "CODEX_TARGET_AMBIGUOUS",
        "the requested target matches more than one attached Codex task",
        requested_target=requested,
        choices=[
            _target_choice(alias, alias_root=alias_root) for alias in matches
        ],
        retryable=False,
    )


def _alias_root_argv(alias_root: str | Path | None) -> list[str]:
    if alias_root is None:
        return []
    resolved = Path(alias_root).expanduser().resolve(strict=False)
    default = DEFAULT_ALIAS_ROOT.expanduser().resolve(strict=False)
    if resolved == default:
        return []
    return ["--alias-root", str(resolved)]


def _attach_required_error(
    args: argparse.Namespace,
    handler: str,
    thread_id: str,
) -> WorkspaceError:
    raw_argv = getattr(args, "_raw_argv", None)
    after_attach_argv = (
        [str(value) for value in raw_argv]
        if isinstance(raw_argv, list) and raw_argv
        else [*handler.split("."), "--thread-id", thread_id]
    )
    attach_argv = [
        "codex",
        "session",
        "attach",
        "--thread-id",
        thread_id,
    ]
    if handler == "codex.steer":
        attach_argv.extend(["--mode", "app-sync"])
    attach_argv.extend(_alias_root_argv(getattr(args, "alias_root", None)))
    return WorkspaceError(
        "CODEX_TARGET_ATTACH_REQUIRED",
        "the selected Codex task is read-only until it is explicitly attached",
        requested_target={"kind": "thread_id", "value": thread_id},
        control_created=False,
        attach_argv=attach_argv,
        after_attach_argv=after_attach_argv,
        retryable=False,
    )


def _new_internal_lane_id(alias_root: Path) -> str:
    while True:
        lane_id = f"task-{uuid.uuid4().hex}"
        if load_alias(CODEX_PROVIDER, lane_id, alias_root) is None:
            return lane_id


def _internal_lane_id_for_thread(thread_id: str) -> str:
    try:
        prefix = safe_lane_id(thread_id).casefold()[:48]
    except ValueError:
        prefix = "session"
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:12]
    return f"session-{prefix}-{digest}"



def cmd_codex_run(args: argparse.Namespace) -> dict[str, Any]:
    goal_objective = _normalize_goal_objective(
        getattr(args, "goal_objective", None)
    )
    requested_custom_title = _nonempty_text(getattr(args, "title", None))
    if getattr(args, "title", None) is not None and requested_custom_title is None:
        raise WorkspaceError(
            "LANE_CUSTOM_TITLE_INVALID",
            "--title requires a non-empty value",
            lane_id=args.lane_id,
            retryable=False,
        )
    alias_root = Path(args.alias_root).expanduser()
    existing = load_alias(CODEX_PROVIDER, args.lane_id, alias_root)
    initial_title_cwd = _resolve_cwd(args.cwd, existing)
    execution_mode, resolved_mode_source = _resolve_execution_mode(
        getattr(args, "mode", None),
        existing,
    )
    execution_mode_source = str(
        (existing or {}).get("execution_mode_source") or resolved_mode_source
    )
    if execution_mode == "app-sync":
        # Verify the shared control plane before worktree or alias side effects.
        with CodexAppServer(transport="daemon"):
            pass
    if existing is not None and getattr(args, "worktree", None) is None:
        _refresh_adopted_alias_cwd(existing, args.lane_id, alias_root)
    prompt = _read_prompt(args)
    cwd, existing, workspace = _resolve_run_workspace(
        args,
        existing,
        alias_root,
    )
    custom_title = (
        requested_custom_title
        if getattr(args, "title", None) is not None
        else _custom_title(existing or {})
    )
    codex_title = (
        _stored_codex_title(existing or {})
        if existing and existing.get("codex_thread_id")
        else _new_codex_title(
            requested_title=custom_title,
            cwd=initial_title_cwd,
            lane_id=args.lane_id,
        )
    ) or args.lane_id
    sandbox = _resolve_sandbox(args.sandbox, existing)
    request_echo = _resolve_turn_request(args, existing)
    model = request_echo["requested_model"]
    profile = _resolve_runtime_value(args.profile, existing, "profile")
    add_dirs = _resolve_add_dirs(args.add_dir, existing)
    workspace_roots = _runtime_workspace_roots(cwd, add_dirs)
    commit_signing_mode = _resolve_commit_signing(args.commit_signing, existing)
    commit_signing = _prepare_commit_signing(commit_signing_mode)
    user_config_overrides = _validated_config_overrides(args.config_overrides)
    effort = request_echo["requested_effort"]
    config_overrides = [
        *user_config_overrides,
        *commit_signing["config_overrides"],
    ]
    resumed = existing is not None and bool(existing.get("codex_thread_id"))
    alias = dict(existing or {})
    thread_replacement: dict[str, Any] | None = None
    written_codex_title: str | None = None
    additional_context: dict[str, dict[str, str]] | None = None
    replacement_goal: dict[str, Any] | None = None
    stored_cwd = (
        _resolve_cwd(None, existing)
        if resumed and existing is not None
        else None
    )
    explicit_workspace_rebind = bool(
        resumed
        and getattr(args, "cwd", None) is not None
        and stored_cwd is not None
        and cwd is not None
        and workspace_binding_changed(stored_cwd, cwd)
    )
    if explicit_workspace_rebind:
        stored_goal = alias.get("goal")
        stored_goal_status = (
            stored_goal.get("status")
            if isinstance(stored_goal, dict)
            else alias.get("goal_status")
        )
        if stored_goal_status == "active" and goal_objective is None:
            raise WorkspaceError(
                "CODEX_WORKSPACE_REBIND_ACTIVE_GOAL_REQUIRES_OBJECTIVE",
                "rebinding an active goal lane requires --goal-objective",
                lane_id=args.lane_id,
                configured_cwd=stored_cwd,
                requested_cwd=cwd,
                required_option="--goal-objective",
                retryable=False,
            )
        if (
            workspace
            and workspace.get("managed_by") == "agent-lane"
            and workspace.get("status") == "active"
        ):
            raise WorkspaceError(
                "GIT_WORKTREE_REBIND_MANAGED",
                "refusing to move a lane away from its active managed worktree",
                lane_id=args.lane_id,
                configured_cwd=stored_cwd,
                requested_cwd=cwd,
            )
        workspace = _workspace_status(cwd, None)
    with CodexAppServer(
        transport=_transport_for_mode(execution_mode),
        profile=profile,
        extra_env=commit_signing["extra_env"],
        config_overrides=config_overrides,
    ) as codex:
        if resumed:
            thread_id = str(existing["codex_thread_id"])
            try:
                snapshot = codex.read_thread(thread_id, include_turns=False)
            except CodexRpcError as exc:
                if not (
                    explicit_workspace_rebind
                    and _is_thread_not_loaded_error(exc)
                ):
                    raise
            else:
                _update_thread_alias(alias, snapshot.get("thread") or {})
                codex_title = _stored_codex_title(alias) or args.lane_id
                _require_thread_inactive_for_turn(
                    snapshot.get("thread") or {},
                    lane_id=args.lane_id,
                    thread_id=thread_id,
                )
            if explicit_workspace_rebind:
                assert cwd is not None
                origin_thread_id = thread_id
                thread_id = codex.start_thread(
                    cwd,
                    sandbox=sandbox,
                    model=model,
                    runtime_workspace_roots=workspace_roots,
                )
                codex.set_thread_name(thread_id, codex_title)
                written_codex_title = codex_title
                codex.update_git_info(thread_id, _git_info(cwd))
                additional_context = _workspace_rebind_additional_context(
                    alias,
                    origin_thread_id=origin_thread_id,
                    origin_cwd=stored_cwd,
                    execution_cwd=cwd,
                )
                thread_replacement = {
                    "reason": "workspace_binding_changed",
                    "origin_thread_id": origin_thread_id,
                    "execution_thread_id": thread_id,
                    "origin_cwd": stored_cwd,
                    "execution_cwd": cwd,
                }
                _record_workspace_thread_replacement(
                    alias,
                    thread_replacement,
                )
                if (
                    getattr(codex, "transport", "stdio") == "daemon"
                    and commit_signing_mode == "agent"
                ):
                    _require_codex_thread_signing(
                        codex,
                        thread_id=thread_id,
                        commit_signing=commit_signing,
                    )
                resumed = False
            else:
                prepared_thread = _prepare_existing_thread_for_turn(
                    codex,
                    alias=alias,
                    thread_id=thread_id,
                    cwd=cwd,
                    title=codex_title,
                    sandbox=sandbox,
                    model=model,
                    workspace_roots=workspace_roots,
                    commit_signing_mode=commit_signing_mode,
                    commit_signing=commit_signing,
                    allow_signing_replacement=args.allow_signing_replacement,
                    replacement_goal_objective=goal_objective,
                    replacement_origin_goal=None,
                    lane_id=args.lane_id,
                    alias_root=alias_root,
                )
                thread_id = str(prepared_thread["thread_id"])
                if prepared_thread["replaced"]:
                    resumed = False
                    thread_replacement = prepared_thread
                    additional_context = prepared_thread["additional_context"]
                    codex_title = str(prepared_thread["codex_title"])
                    candidate = prepared_thread.get("goal")
                    replacement_goal = (
                        candidate if isinstance(candidate, dict) else None
                    )
        else:
            if not cwd:
                raise ValueError("--cwd is required when creating a new lane")
            thread_id = codex.start_thread(
                cwd,
                sandbox=sandbox,
                model=model,
                runtime_workspace_roots=workspace_roots,
            )
            codex.set_thread_name(thread_id, codex_title)
            written_codex_title = codex_title
            codex.update_git_info(thread_id, _git_info(cwd))
            if (
                getattr(codex, "transport", "stdio") == "daemon"
                and commit_signing_mode == "agent"
            ):
                _require_codex_thread_signing(
                    codex,
                    thread_id=thread_id,
                    commit_signing=commit_signing,
                )

        now = time.time()
        alias.update(
            {
                "lane_id": args.lane_id,
                "codex_thread_id": thread_id,
                "codex_url": f"codex://threads/{thread_id}",
                "cwd": cwd,
                "sandbox": sandbox,
                "model": model,
                **request_echo,
                "profile": profile,
                "add_dirs": add_dirs,
                "commit_signing": commit_signing["metadata"],
                "created_at": alias.get("created_at") or now,
            }
        )
        if custom_title is None:
            alias.pop("custom_title", None)
        else:
            alias["custom_title"] = custom_title
        if not isinstance(alias.get("binding"), dict):
            _initialize_codex_binding(
                alias,
                thread_id=thread_id,
                origin="created",
                bound_at=now,
            )
        _record_execution_mode(
            alias,
            mode=execution_mode,
            source=execution_mode_source,
        )
        if written_codex_title is not None:
            _record_written_codex_title(
                alias,
                thread_id=thread_id,
                title=written_codex_title,
            )
        if workspace:
            alias["workspace"] = workspace
        goal: dict[str, Any] | None = replacement_goal
        goal_tracking = (
            goal_objective is not None or replacement_goal is not None
        )
        if goal_objective is not None:
            if replacement_goal is not None:
                goal = replacement_goal
            else:
                goal_result = codex.set_goal(
                    thread_id,
                    objective=goal_objective,
                    status="active",
                )
                candidate = goal_result.get("goal")
                goal = candidate if isinstance(candidate, dict) else None
            alias["mode"] = "goal"
            if goal is not None:
                _update_goal_alias(alias, goal)
        try:
            turn = _run_tracked_turn(
                codex,
                alias=alias,
                alias_root=alias_root,
                lane_id=args.lane_id,
                thread_id=thread_id,
                prompt=prompt,
                sandbox=sandbox,
                model=model,
                effort=effort,
                workspace_cwd=cwd,
                workspace_roots=workspace_roots,
                additional_context=additional_context,
                timeout=args.timeout,
            )
        except TimeoutError as exc:
            return _turn_timeout_result(
                codex=codex,
                alias=alias,
                alias_root=alias_root,
                lane_id=args.lane_id,
                thread_id=thread_id,
                cwd=cwd,
                error=str(exc),
            )
        if goal_tracking:
            try:
                goal = codex.get_goal(thread_id)
                alias.pop("goal_refresh_error", None)
            except Exception as exc:
                alias["goal_refresh_error"] = str(exc)
    _update_turn_alias(alias, args.lane_id, thread_id, cwd, sandbox, turn)
    if goal_tracking:
        _update_goal_alias(alias, goal)
    runner = _runner_state(
        alias,
        goal if goal_tracking else alias.get("goal"),
        thread_active=False,
        thread_observed=True,
        observed_turn={
            "turn_id": turn.turn_id,
            "status": turn.status,
            "final_text": turn.final_text,
        },
    )
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    result = {
        "ok": True,
        "provider": "codex",
        "lane_id": args.lane_id,
        "resumed": resumed,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "app_server_transport": getattr(codex, "transport", "stdio"),
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "alias_path": str(path),
        "cwd": cwd,
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
        "sandbox": sandbox,
        "model": model,
        "effort": effort,
        **request_echo,
        "profile": profile,
        "add_dirs": add_dirs,
        "workspace": _workspace_status(cwd, workspace),
        "config_override_count": len(user_config_overrides),
        "commit_signing": commit_signing["metadata"],
        "turn_id": turn.turn_id,
        "status": turn.status,
        **_execution_fields(runner),
        "final_text": turn.final_text,
        "events": turn.events[-20:],
    }
    if thread_replacement is not None:
        result.update(
            {
                "thread_replaced": True,
                "origin_thread_id": thread_replacement["origin_thread_id"],
                "handoff_reason": thread_replacement["reason"],
            }
        )
        if "origin_cwd" in thread_replacement:
            result["previous_cwd"] = thread_replacement["origin_cwd"]
    if goal_tracking:
        result["goal"] = goal
    return result


def cmd_codex_send(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "worktree", None) is not None:
        raise WorkspaceError(
            "GIT_WORKTREE_CREATE_ONLY",
            "--worktree is only valid with `codex run` when creating a lane",
            option="--worktree",
        )
    return _cmd_codex_send_lane(args)


def cmd_codex_steer(args: argparse.Namespace) -> dict[str, Any]:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise WorkspaceError(
            "CODEX_STEER_INVALID_ARGUMENT",
            "--timeout must be a finite number greater than zero",
            option="--timeout",
            retryable=False,
        )

    try:
        alias, thread_id = _resolve_thread_target(args)
    except (OSError, ValueError) as exc:
        target_option = (
            "--lane-id"
            if getattr(args, "lane_id", None) is not None
            else "--thread-id"
        )
        raise WorkspaceError(
            "CODEX_STEER_INVALID_ARGUMENT",
            str(exc),
            option=target_option,
            retryable=False,
        ) from exc
    if not thread_id:
        raise WorkspaceError(
            "CODEX_STEER_INVALID_ARGUMENT",
            "lane alias does not contain a Codex thread id",
            option="--lane-id",
            lane_id=getattr(args, "lane_id", None),
            retryable=False,
        )
    execution_mode, execution_mode_source = _resolve_execution_mode(None, alias)
    if execution_mode != "app-sync":
        raise WorkspaceError(
            "LANE_APP_SYNC_REQUIRED",
            "steer requires a lane created or attached with --mode app-sync",
            lane_id=getattr(args, "lane_id", None),
            execution_mode=execution_mode,
            retryable=False,
        )
    requested_target = _requested_target(args) or {}
    target_source = {
        "lane_id": "lane",
        "thread_id": "thread",
        "title": "title",
        "current": "current",
    }.get(str(requested_target.get("kind")), "lane")
    expected_lane_id = _nonempty_text((alias or {}).get("lane_id"))
    try:
        prompt = _read_prompt(args)
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(
            "CODEX_STEER_INVALID_ARGUMENT",
            f"could not read steering prompt: {exc}",
            option="--prompt-file",
            prompt_file=str(getattr(args, "prompt_file", "") or ""),
            retryable=False,
        ) from exc
    if not prompt.strip():
        raise WorkspaceError(
            "CODEX_STEER_INVALID_ARGUMENT",
            "steering prompt must not be empty",
            option="--prompt" if args.prompt is not None else "--prompt-file",
            retryable=False,
        )

    with CodexAppServer(transport="daemon") as codex:
        snapshot = codex.read_thread(thread_id, include_turns=True)
        thread = snapshot.get("thread")
        if not isinstance(thread, dict):
            raise WorkspaceError(
                "CODEX_STEER_THREAD_NOT_FOUND",
                "Codex did not return the requested thread",
                codex_thread_id=thread_id,
                retryable=False,
            )
        live_turn_id = _steer_active_turn_id(thread, thread_id=thread_id)
        requested_turn_id = str(getattr(args, "turn_id", None) or "").strip()
        if getattr(args, "turn_id", None) is not None and not requested_turn_id:
            raise WorkspaceError(
                "CODEX_STEER_INVALID_ARGUMENT",
                "--turn-id requires a non-empty value",
                option="--turn-id",
                retryable=False,
            )
        if requested_turn_id and requested_turn_id != live_turn_id:
            raise WorkspaceError(
                "CODEX_STEER_TURN_MISMATCH",
                "the requested turn is not the live active turn",
                codex_thread_id=thread_id,
                expected_turn_id=requested_turn_id,
                active_turn_id=live_turn_id,
                retryable=False,
            )
        expected_turn_id = requested_turn_id or live_turn_id

        with operation_lock(
            STEER_LOCK_ROOT,
            thread_id,
            namespace="steer",
            wait_timeout=args.timeout,
        ):
            current_alias, current_thread_id = _resolve_thread_target(args)
            observed_lane_id = _nonempty_text(
                (current_alias or {}).get("lane_id")
            )
            if (
                current_thread_id != thread_id
                or observed_lane_id != expected_lane_id
            ):
                raise WorkspaceError(
                    "CODEX_STEER_TARGET_CHANGED",
                    "task binding changed before steering",
                    expected_lane_id=expected_lane_id,
                    observed_lane_id=observed_lane_id,
                    expected_thread_id=thread_id,
                    observed_thread_id=current_thread_id,
                    retryable=False,
                )
            alias = current_alias

            latest_snapshot = codex.read_thread(thread_id, include_turns=True)
            latest_thread = latest_snapshot.get("thread")
            if not isinstance(latest_thread, dict):
                raise WorkspaceError(
                    "CODEX_STEER_THREAD_NOT_FOUND",
                    "Codex did not return the requested thread",
                    codex_thread_id=thread_id,
                    retryable=False,
                )
            try:
                latest_turn_id = _steer_active_turn_id(
                    latest_thread,
                    thread_id=thread_id,
                )
            except WorkspaceError as exc:
                raise WorkspaceError(
                    "CODEX_STEER_TURN_CHANGED",
                    "the active turn changed while waiting to steer",
                    codex_thread_id=thread_id,
                    expected_turn_id=expected_turn_id,
                    active_turn_id=None,
                    cause_code=exc.error_code,
                    retryable=False,
                ) from exc
            if latest_turn_id != expected_turn_id:
                raise WorkspaceError(
                    "CODEX_STEER_TURN_CHANGED",
                    "the active turn changed while waiting to steer",
                    codex_thread_id=thread_id,
                    expected_turn_id=expected_turn_id,
                    active_turn_id=latest_turn_id,
                    retryable=False,
                )
            result = codex.steer_turn(
                thread_id,
                prompt,
                expected_turn_id=expected_turn_id,
                timeout=args.timeout,
            )

    lane_id = alias.get("lane_id") if isinstance(alias, dict) else None
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "operation": "steer",
        "target_source": target_source,
        "lane_id": lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "expected_turn_id": expected_turn_id,
        "turn_id": result.turn_id,
        "client_user_message_id": result.client_message_id,
        "steer_status": "accepted",
        "app_server_transport": getattr(codex, "transport", "daemon"),
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
    }


def _cmd_codex_send_lane(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    existing = _require_command_alias(args, alias_root)
    execution_mode, resolved_mode_source = _resolve_execution_mode(None, existing)
    execution_mode_source = str(
        existing.get("execution_mode_source") or resolved_mode_source
    )
    prompt = _read_prompt(args)
    codex_title = _stored_codex_title(existing) or args.lane_id
    sandbox = _resolve_sandbox(args.sandbox, existing)
    request_echo = _resolve_turn_request(args, existing)
    model = request_echo["requested_model"]
    profile = _resolve_runtime_value(args.profile, existing, "profile")
    add_dirs = _resolve_add_dirs(args.add_dir, existing)
    commit_signing_mode = _resolve_commit_signing(args.commit_signing, existing)
    commit_signing = _prepare_commit_signing(commit_signing_mode)
    user_config_overrides = _validated_config_overrides(args.config_overrides)
    effort = request_echo["requested_effort"]
    config_overrides = [
        *user_config_overrides,
        *commit_signing["config_overrides"],
    ]
    alias = dict(existing)
    thread_replacement: dict[str, Any] | None = None
    additional_context: dict[str, dict[str, str]] | None = None

    with CodexAppServer(
        transport=_transport_for_mode(execution_mode),
        profile=profile,
        extra_env=commit_signing["extra_env"],
        config_overrides=config_overrides,
    ) as codex:
        thread_id = str(existing["codex_thread_id"])
        thread = codex.read_thread(thread_id, include_turns=False)
        _require_thread_inactive_for_turn(
            thread.get("thread") or {},
            lane_id=args.lane_id,
            thread_id=thread_id,
        )
        _sync_adopted_thread_cwd(alias, thread.get("thread") or {})
        _update_thread_alias(alias, thread.get("thread") or {})
        codex_title = _stored_codex_title(alias) or args.lane_id
        cwd = _resolve_cwd(None, alias)
        workspace_roots = _runtime_workspace_roots(cwd, add_dirs)
        prepared_thread = _prepare_existing_thread_for_turn(
            codex,
            alias=alias,
            thread_id=thread_id,
            cwd=cwd,
            title=codex_title,
            sandbox=sandbox,
            model=model,
            workspace_roots=workspace_roots,
            commit_signing_mode=commit_signing_mode,
            commit_signing=commit_signing,
            allow_signing_replacement=args.allow_signing_replacement,
            replacement_goal_objective=None,
            replacement_origin_goal=None,
            lane_id=args.lane_id,
            alias_root=alias_root,
        )
        thread_id = str(prepared_thread["thread_id"])
        if prepared_thread["replaced"]:
            thread_replacement = prepared_thread
            additional_context = prepared_thread["additional_context"]
            codex_title = str(prepared_thread["codex_title"])
        alias.update(
            {
                "model": model,
                **request_echo,
                "profile": profile,
                "add_dirs": add_dirs,
                "commit_signing": commit_signing["metadata"],
            }
        )
        _record_execution_mode(
            alias,
            mode=execution_mode,
            source=execution_mode_source,
        )
        try:
            turn = _run_tracked_turn(
                codex,
                alias=alias,
                alias_root=alias_root,
                lane_id=args.lane_id,
                thread_id=thread_id,
                prompt=prompt,
                sandbox=sandbox,
                model=model,
                effort=effort,
                workspace_cwd=cwd,
                workspace_roots=workspace_roots,
                additional_context=additional_context,
                timeout=args.timeout,
            )
        except TimeoutError as exc:
            return _turn_timeout_result(
                codex=codex,
                alias=alias,
                alias_root=alias_root,
                lane_id=args.lane_id,
                thread_id=thread_id,
                cwd=cwd,
                error=str(exc),
            )
        _update_turn_alias(
            alias,
            args.lane_id,
            thread_id,
            cwd,
            sandbox,
            turn,
        )
        save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
        try:
            goal = codex.get_goal(thread_id)
            alias.pop("goal_refresh_error", None)
        except Exception as exc:
            stored_goal = alias.get("goal")
            goal = stored_goal if isinstance(stored_goal, dict) else None
            alias["goal_refresh_error"] = str(exc)

    alias["commit_signing"] = commit_signing["metadata"]
    if goal is not None or "goal_refresh_error" not in alias:
        _update_goal_alias(alias, goal)
    runner = _runner_state(
        alias,
        goal,
        thread_active=False,
        thread_observed=True,
        observed_turn={
            "turn_id": turn.turn_id,
            "status": turn.status,
            "final_text": turn.final_text,
        },
    )
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    result = {
        "ok": True,
        "provider": "codex",
        "lane_id": args.lane_id,
        "resumed": thread_replacement is None,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "app_server_transport": getattr(codex, "transport", "stdio"),
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "alias_path": str(path),
        "cwd": cwd,
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
        "sandbox": sandbox,
        "model": model,
        "effort": effort,
        **request_echo,
        "profile": profile,
        "add_dirs": add_dirs,
        "workspace": _workspace_status(cwd, alias.get("workspace")),
        "config_override_count": len(user_config_overrides),
        "commit_signing": commit_signing["metadata"],
        "turn_id": turn.turn_id,
        "status": turn.status,
        **_execution_fields(runner),
        "goal": goal,
        "final_text": turn.final_text,
        "events": turn.events[-20:],
    }
    if thread_replacement is not None:
        result.update(
            {
                "thread_replaced": True,
                "origin_thread_id": thread_replacement["origin_thread_id"],
                "handoff_reason": "loaded_thread_resume_config_not_effective",
            }
        )
    return result


def cmd_codex_status(args: argparse.Namespace) -> dict[str, Any]:
    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        return _direct_thread_status(
            direct_thread_id,
            include_turns=bool(args.include_turns),
            brief=bool(getattr(args, "brief", False)),
            alias_root=Path(args.alias_root).expanduser(),
        )
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    thread_id = str(alias["codex_thread_id"])
    execution_mode, execution_mode_source = _resolve_execution_mode(None, alias)
    with _codex_for_alias(alias) as codex:
        thread = codex.read_thread(thread_id, include_turns=args.include_turns)
        goal = codex.get_goal(thread_id)
    thread_obj = thread.get("thread") or {}
    thread_status = thread_obj.get("status") or {}
    _sync_adopted_thread_cwd(alias, thread_obj)
    _update_thread_alias(alias, thread_obj)
    alias["thread_status"] = thread_status
    fallback = _apply_rollout_status_fallback(alias, thread_obj, goal)
    goal = fallback["goal"]
    _update_goal_alias(alias, goal)
    runner = _runner_state(
        alias,
        goal,
        thread=thread_obj,
        goal_source=fallback["goal_status_source"],
    )
    result = {
        "ok": True,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "alias_path": str(alias_path(CODEX_PROVIDER, args.lane_id, alias_root)),
        **_title_contract(alias, thread=thread_obj, lane_id=args.lane_id),
        **_binding_contract(alias),
        "thread_status": thread_status,
        "goal_status": goal.get("status") if isinstance(goal, dict) else None,
        "goal_status_source": fallback["goal_status_source"],
        "goal_tokens_used": _goal_value(goal, "tokensUsed"),
        "goal_time_used_seconds": _goal_value(goal, "timeUsedSeconds"),
        **_turn_request_echo(alias),
        **_execution_fields(runner),
        "last_status": alias.get("last_status"),
        "last_completed_final_lead": _last_completed_final_lead(alias),
        "current_turn_final_lead": _current_turn_final_lead(alias, runner),
        "rollout_fallback_used": fallback["rollout_fallback_used"],
        "runner": runner,
        "goal": goal,
        "workspace": _workspace_status(alias.get("cwd"), alias.get("workspace")),
        "alias": alias,
        "thread": thread,
    }
    if getattr(args, "brief", False):
        return _status_brief(alias, runner, result)
    return result


def cmd_codex_closeout(args: argparse.Namespace) -> dict[str, Any]:
    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        return _direct_thread_closeout(
            direct_thread_id,
            alias_root=Path(args.alias_root).expanduser(),
        )
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    thread_id = str(alias["codex_thread_id"])
    execution_mode, execution_mode_source = _resolve_execution_mode(None, alias)
    with _codex_for_alias(alias) as codex:
        thread = codex.read_thread(thread_id, include_turns=True)
        goal = codex.get_goal(thread_id)
    thread_obj = thread.get("thread") or {}
    _sync_adopted_thread_cwd(alias, thread_obj)
    _update_thread_alias(alias, thread_obj)
    fallback = _apply_rollout_status_fallback(alias, thread_obj, goal)
    goal = fallback["goal"]
    _update_goal_alias(alias, goal)
    runner = _runner_state(
        alias,
        goal,
        thread=thread_obj,
        goal_source=fallback["goal_status_source"],
    )
    cwd = alias.get("cwd")
    git = _git_snapshot(str(cwd) if cwd else None, include_details=True)
    goal_status = goal.get("status") if isinstance(goal, dict) else None
    last_status = alias.get("last_status")
    completed = goal_status == "complete" or (
        goal_status is None and last_status == "completed"
    )
    if runner["execution_active"]:
        summary = (
            "runner_active"
            if runner["execution_source"] in {"runner", "runner_and_thread"}
            else "thread_active"
        )
    elif runner["needs_resume"]:
        summary = "needs_resume"
    elif not git["is_repo"]:
        summary = "not_git_repo"
    elif completed and git["dirty"] is True:
        summary = "complete_dirty"
    elif completed and git["dirty"] is False:
        summary = "complete_and_clean"
    else:
        summary = "unknown"
    return {
        "ok": True,
        "lane_id": args.lane_id,
        **_title_contract(alias, thread=thread_obj, lane_id=args.lane_id),
        **_binding_contract(alias),
        "cwd": cwd,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "goal_status": goal_status,
        "goal_status_source": fallback["goal_status_source"],
        "goal_tokens_used": _goal_value(goal, "tokensUsed"),
        "goal_time_used_seconds": _goal_value(goal, "timeUsedSeconds"),
        **_turn_request_echo(alias),
        **_execution_fields(runner),
        "last_status": last_status,
        "last_completed_final_lead": _last_completed_final_lead(alias),
        "current_turn_final_lead": _current_turn_final_lead(alias, runner),
        "rollout_fallback_used": fallback["rollout_fallback_used"],
        "git": git,
        "workspace": workspace_snapshot(
            str(cwd) if cwd else None,
            alias.get("workspace"),
            branch=git.get("branch"),
            dirty=git.get("dirty"),
        ),
        "summary": summary,
    }


def _direct_thread_snapshot(
    thread_id: str,
    *,
    include_turns: bool,
    alias_root: Path,
) -> dict[str, Any]:
    codex, transport = _open_read_only_codex("auto")
    with codex:
        result = codex.read_thread(thread_id, include_turns=include_turns)
        try:
            goal = codex.get_goal(thread_id)
            goal_source = "thread_goal_get"
            goal_error = None
        except Exception as exc:
            goal = None
            goal_source = "unavailable"
            goal_error = str(exc)
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise WorkspaceError(
            "CODEX_TARGET_NOT_FOUND",
            "Codex did not return the selected thread",
            requested_target={"kind": "thread_id", "value": thread_id},
            retryable=False,
        )
    alias = {
        "codex_thread_id": thread_id,
        "cwd": _nonempty_text(thread.get("cwd")),
    }
    runner = _runner_state(
        alias,
        goal if isinstance(goal, dict) else None,
        thread=thread,
        goal_source=goal_source,
        thread_authoritative=bool(transport["live_status_authoritative"]),
        observed_at=transport.get("observed_at"),
        observation_mode=str(transport.get("observation_mode") or "unknown"),
    )
    snapshot = {
        "ok": True,
        "lane_id": None,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": None,
        "execution_mode": None,
        "execution_mode_source": "unattached",
        **_title_contract({}, thread=thread),
        "thread_status": thread.get("status") or {},
        "goal_status": goal.get("status") if isinstance(goal, dict) else None,
        "goal_status_source": goal_source,
        "goal_tokens_used": _goal_value(goal, "tokensUsed"),
        "goal_time_used_seconds": _goal_value(goal, "timeUsedSeconds"),
        **_turn_request_echo(None),
        **_execution_fields(runner),
        "last_status": None,
        "workspace": _workspace_status(alias.get("cwd"), None),
        "alias": None,
        "thread": result,
        "goal": goal,
        "runner": runner,
        "control": _control_contract(
            None,
            thread_id,
            thread=thread,
            attach_mode=_execution_mode_from_transport(transport),
            alias_root=alias_root,
        ),
        **transport,
    }
    if goal_error is not None:
        snapshot["goal_refresh_error"] = goal_error
    return snapshot


def _direct_thread_status(
    thread_id: str,
    *,
    include_turns: bool,
    brief: bool,
    alias_root: Path,
) -> dict[str, Any]:
    snapshot = _direct_thread_snapshot(
        thread_id,
        include_turns=include_turns,
        alias_root=alias_root,
    )
    if not brief:
        return snapshot
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"alias", "thread", "goal"}
    }


def _direct_thread_closeout(thread_id: str, *, alias_root: Path) -> dict[str, Any]:
    snapshot = _direct_thread_snapshot(
        thread_id,
        include_turns=True,
        alias_root=alias_root,
    )
    thread = (snapshot.get("thread") or {}).get("thread") or {}
    cwd = _nonempty_text(thread.get("cwd"))
    git = _git_snapshot(cwd, include_details=True)
    goal_status = snapshot.get("goal_status")
    last_status = (snapshot.get("last_turn") or {}).get("status")
    completed = goal_status == "complete" or (
        goal_status is None and last_status == "completed"
    )
    if snapshot.get("execution_active"):
        summary = "thread_active"
    elif snapshot.get("needs_resume"):
        summary = "needs_resume"
    elif not git["is_repo"]:
        summary = "not_git_repo"
    elif completed and git["dirty"] is True:
        summary = "complete_dirty"
    elif completed and git["dirty"] is False:
        summary = "complete_and_clean"
    else:
        summary = "unknown"
    snapshot.pop("alias", None)
    snapshot.pop("thread", None)
    snapshot.pop("goal", None)
    snapshot.update(
        {
            "cwd": cwd,
            "last_status": last_status,
            "git": git,
            "summary": summary,
        }
    )
    return snapshot


def cmd_codex_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    stored = load_alias(CODEX_PROVIDER, args.lane_id, alias_root)
    if not stored:
        raise WorkspaceError(
            "LANE_ALIAS_NOT_FOUND",
            "lane alias was not found",
            lane_id=args.lane_id,
        )
    alias = dict(stored)
    runner = _runner_state(alias, alias.get("goal"))
    if runner["alive"]:
        raise WorkspaceError(
            "GIT_WORKTREE_RUNNER_ACTIVE",
            "refusing to clean up a worktree while its lane runner is active",
            lane_id=args.lane_id,
        )
    thread_id = alias.get("codex_thread_id")
    if thread_id:
        with _codex_for_alias(alias) as codex:
            thread = codex.read_thread(str(thread_id), include_turns=True)
        thread_obj = thread.get("thread") or {}
        if _thread_has_active_turn(thread_obj):
            raise WorkspaceError(
                "GIT_WORKTREE_THREAD_ACTIVE",
                "refusing to clean up a worktree while its Codex thread is active",
                lane_id=args.lane_id,
                codex_thread_id=thread_id,
            )
        if not args.confirm_thread_inactive:
            raise WorkspaceError(
                "GIT_WORKTREE_THREAD_STATE_UNVERIFIABLE",
                "confirm that no parent thread, subagent, or other client is using "
                "the workspace before cleanup",
                lane_id=args.lane_id,
                codex_thread_id=thread_id,
                option="--confirm-thread-inactive",
            )
    workspace = alias.get("workspace")
    if not isinstance(workspace, dict):
        raise WorkspaceError(
            "GIT_WORKTREE_NOT_MANAGED",
            "lane has no agent-lane managed Git worktree",
            lane_id=args.lane_id,
        )
    try:
        cleanup = cleanup_managed_worktree(
            workspace,
            expected_lane_id=args.lane_id,
            delete_branch=bool(args.delete_branch),
        )
    except WorkspaceError as exc:
        if exc.details.get("worktree_removed"):
            workspace = dict(workspace)
            workspace.update({"status": "removed", "removed_at": time.time()})
            alias["workspace"] = workspace
            save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
        raise

    workspace = dict(workspace)
    workspace.update(
        {
            "status": "removed",
            "removed_at": time.time(),
            "branch_deleted": cleanup["branch_deleted"],
        }
    )
    alias["workspace"] = workspace
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": alias.get("codex_url"),
        "alias_path": str(path),
        "cleanup": cleanup,
        "workspace": workspace_snapshot(alias.get("cwd"), workspace),
    }


def _status_brief(
    alias: dict[str, Any],
    runner: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    cwd = alias.get("cwd")
    git = _git_snapshot(str(cwd) if cwd else None, include_details=False)
    workspace = workspace_snapshot(
        str(cwd) if cwd else None,
        alias.get("workspace"),
        branch=git.get("branch"),
        dirty=git.get("dirty"),
    )
    result = {
        "ok": True,
        "lane_id": status["lane_id"],
        **{
            key: status.get(key)
            for key in (
                "lane_title",
                "lane_title_source",
                "codex_title",
                "codex_title_observation",
                "codex_title_observed_at",
                "custom_title",
            )
        },
        "cwd": cwd,
        "codex_thread_id": status["codex_thread_id"],
        "codex_url": status["codex_url"],
        "goal_status": status.get("goal_status"),
        "goal_status_source": status.get("goal_status_source"),
        "goal_tokens_used": status.get("goal_tokens_used"),
        "goal_time_used_seconds": status.get("goal_time_used_seconds"),
        **_turn_request_echo(alias),
        **_execution_fields(runner),
        "last_status": alias.get("last_status"),
        "last_completed_final_lead": _last_completed_final_lead(alias),
        "current_turn_final_lead": _current_turn_final_lead(alias, runner),
        "rollout_fallback_used": status.get("rollout_fallback_used", False),
        "branch": git["branch"],
        "git_dirty": git["dirty"],
        "workspace_kind": workspace["kind"],
        "app_native_handoff": workspace["app_native_handoff"],
    }
    if alias.get("last_error_code") is not None:
        result["last_error_code"] = alias["last_error_code"]
    return result


def _last_completed_final_lead(alias: dict[str, Any]) -> str | None:
    if "last_completed_final_text" in alias:
        raw_text = alias.get("last_completed_final_text")
    elif alias.get("last_status") not in {"failed", "interrupted"}:
        raw_text = alias.get("last_final_text")
    else:
        raw_text = None
    final_text = _clean_agent_text(str(raw_text or ""))
    return _clip(_first_paragraph(final_text), 200)


def _current_turn_final_lead(
    alias: dict[str, Any],
    runner: dict[str, Any],
) -> str | None:
    if runner.get("execution_active") or runner.get("status") != "completed":
        return None
    final_text = _clean_agent_text(str(alias.get("last_final_text") or ""))
    return _clip(_first_paragraph(final_text), 200)


def _preserve_legacy_completed_final(alias: dict[str, Any]) -> None:
    if "last_completed_final_text" in alias:
        return
    if alias.get("last_status") in {"failed", "interrupted"}:
        alias.pop("last_final_text", None)
        return
    final_text = _clean_agent_text(str(alias.get("last_final_text") or ""))
    if final_text:
        alias["last_completed_final_text"] = final_text


def _apply_rollout_status_fallback(
    alias: dict[str, Any],
    thread: dict[str, Any],
    goal: dict[str, Any] | None,
) -> dict[str, Any]:
    thread_id = str(alias.get("codex_thread_id") or thread.get("id") or "")
    session_path = thread.get("path") or alias.get("codex_session_path")
    rollout = read_rollout_closeout(thread_id, session_path=session_path)
    rollout_used = False
    rollout_goal = rollout.get("goal") if isinstance(rollout, dict) else None
    resolved_goal = dict(goal) if isinstance(goal, dict) else None
    goal_status_source = (
        "thread_goal_get"
        if isinstance(resolved_goal, dict) and resolved_goal.get("status")
        else "unavailable"
    )

    if isinstance(rollout_goal, dict):
        if resolved_goal is None or not resolved_goal.get("status"):
            resolved_goal = dict(rollout_goal)
            goal_status_source = "rollout"
            rollout_used = True
        elif resolved_goal.get("status") == rollout_goal.get("status"):
            for key in (
                "objective",
                "tokensUsed",
                "tokenBudget",
                "timeUsedSeconds",
                "createdAt",
                "updatedAt",
            ):
                if resolved_goal.get(key) is None and rollout_goal.get(key) is not None:
                    resolved_goal[key] = rollout_goal[key]
                    rollout_used = True

    if (
        isinstance(rollout, dict)
        and rollout.get("status") == "completed"
        and not _thread_has_active_turn(thread)
    ):
        alias["last_status"] = "completed"
        if rollout.get("turn_id"):
            alias["last_turn_id"] = rollout["turn_id"]
        rollout_used = True

        for key in ("task_complete_message", "assistant_message"):
            final_text = _clean_agent_text(str(rollout.get(key) or ""))
            if final_text:
                alias["last_final_text"] = final_text
                alias["last_completed_final_text"] = final_text
                break

    return {
        "goal": resolved_goal,
        "goal_status_source": goal_status_source,
        "rollout_fallback_used": rollout_used,
    }


def _goal_value(goal: dict[str, Any] | None, key: str) -> Any:
    return goal.get(key) if isinstance(goal, dict) else None


def _thread_has_active_turn(thread: dict[str, Any]) -> bool:
    status = thread.get("status") or {}
    if isinstance(status, dict) and status.get("type") == "active":
        return True
    for turn in thread.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        turn_status = str(turn.get("status") or "").casefold()
        if turn_status in {"inprogress", "in_progress", "running", "started"}:
            return True
    return False


def _steer_active_turn_id(thread: dict[str, Any], *, thread_id: str) -> str:
    status = thread.get("status") or {}
    status_type = status.get("type") if isinstance(status, dict) else None
    if status_type != "active":
        raise WorkspaceError(
            "CODEX_STEER_NO_ACTIVE_TURN",
            "steer requires an active Codex turn and never starts a new turn",
            codex_thread_id=thread_id,
            thread_status=status_type,
            retryable=False,
        )

    active_turn_ids: list[str] = []
    for turn in thread.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        turn_status = str(turn.get("status") or "").casefold()
        turn_id = str(turn.get("id") or "").strip()
        if turn_status in {"inprogress", "in_progress", "running", "started"}:
            if turn_id:
                active_turn_ids.append(turn_id)
    active_turn_ids = list(dict.fromkeys(active_turn_ids))
    if len(active_turn_ids) != 1:
        raise WorkspaceError(
            "CODEX_STEER_ACTIVE_TURN_UNRESOLVED",
            "could not resolve exactly one live active turn for steering",
            codex_thread_id=thread_id,
            active_turn_ids=active_turn_ids,
            retryable=True,
        )
    return active_turn_ids[0]


def _require_thread_inactive_for_turn(
    thread: dict[str, Any],
    *,
    lane_id: str,
    thread_id: str,
) -> None:
    if not _thread_has_active_turn(thread):
        return
    raise WorkspaceError(
        "CODEX_THREAD_ACTIVE",
        "refusing to start another turn while the Codex task is active",
        lane_id=lane_id,
        codex_thread_id=thread_id,
        retryable=True,
    )


def _is_thread_not_loaded_error(exc: CodexRpcError) -> bool:
    return (
        exc.details.get("rpc_code") == -32600
        and "thread not loaded" in str(exc).casefold()
    )


def cmd_codex_wait(args: argparse.Namespace) -> dict[str, Any]:
    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        return _wait_for_thread(
            thread_id=direct_thread_id,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            alias_root=Path(args.alias_root).expanduser(),
        )
    return _wait_for_lane(
        lane_id=args.lane_id,
        alias_root=Path(args.alias_root).expanduser(),
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        expected_thread_id=_nonempty_text(
            getattr(args, "_target_expected_thread_id", None)
        ),
    )


def cmd_codex_watch(args: argparse.Namespace) -> dict[str, Any]:
    def emit(snapshot: dict[str, Any]) -> None:
        event = success_envelope(
            "codex.watch",
            {
                "event": "snapshot",
                "diagnostic": True,
                "stream": "polling_snapshots",
                **snapshot,
            },
        )
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)

    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        result = _wait_for_thread(
            thread_id=direct_thread_id,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            alias_root=Path(args.alias_root).expanduser(),
            emit=emit,
        )
    else:
        result = _wait_for_lane(
            lane_id=args.lane_id,
            alias_root=Path(args.alias_root).expanduser(),
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            emit=emit,
            expected_thread_id=_nonempty_text(
                getattr(args, "_target_expected_thread_id", None)
            ),
        )
    return {
        "event": "completed",
        "diagnostic": True,
        "stream": "polling_snapshots",
        **result,
    }


def cmd_codex_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    delay = max(float(args.after_seconds), 0.0)
    scheduled_at = time.time()
    started_at = time.monotonic()
    if delay:
        time.sleep(delay)
    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        codex, _transport = _open_read_only_codex("auto")
        with codex:
            observation = _thread_observation(
                codex,
                direct_thread_id,
                alias_root=Path(args.alias_root).expanduser(),
            )
    else:
        alias_root = Path(args.alias_root).expanduser()
        alias = _require_command_alias(args, alias_root)
        with _codex_for_alias(alias) as codex:
            observation = _lane_observation(
                codex,
                args.lane_id,
                alias_root,
                expected_thread_id=_nonempty_text(
                    getattr(args, "_target_expected_thread_id", None)
                ),
            )

    ok = "observation_error" not in observation
    result = {
        "ok": ok,
        "kind": "checkpoint",
        "scheduled_at": scheduled_at,
        "checked_at": time.time(),
        "delay_seconds": delay,
        "waited_seconds": round(time.monotonic() - started_at, 3),
        **observation,
    }
    if not ok:
        result.update(
            {
                "error_code": "LANE_CHECKPOINT_OBSERVATION_FAILED",
                "error": observation["observation_error"],
                "retryable": True,
            }
        )
    return result


def cmd_codex_doctor(args: argparse.Namespace) -> dict[str, Any]:
    return doctor_report(
        alias_root=Path(args.alias_root).expanduser(),
        run_probe=bool(args.probe),
        verbose=bool(args.verbose),
    )


def _non_authoritative_session_warning(
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    warning = {
        "code": "CODEX_SESSION_STATE_NON_AUTHORITATIVE",
        "message": (
            "live App Sync state was not observed; persisted session data is "
            "historical evidence and current execution state is unknown"
        ),
    }
    if fallback_reason is not None:
        warning["fallback_reason"] = fallback_reason
    return warning


def _stored_alias_observation() -> dict[str, Any]:
    return {
        "app_server_transport": None,
        "transport_degraded": False,
        "transport_fallback_reason": None,
        "observation_mode": "stored_alias",
        "live_status_authoritative": False,
        "observed_at": time.time(),
        "warnings": [_non_authoritative_session_warning()],
    }


def _annotate_alias_observation(
    aliases: list[dict[str, Any]],
    transport: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "_session_observation": {
                "transport": transport,
                "thread": None,
            },
        }
        for item in aliases
    ]


def _open_read_only_codex(
    observe: str = "auto",
) -> tuple[CodexAppServer, dict[str, Any]]:
    observed_at = time.time()
    if observe == "stored":
        codex = CodexAppServer(transport="stdio")
        return codex, {
            "app_server_transport": "stdio",
            "transport_degraded": False,
            "transport_fallback_reason": None,
            "observation_mode": "stored",
            "live_status_authoritative": False,
            "observed_at": observed_at,
            "warnings": [_non_authoritative_session_warning()],
        }
    if observe == "live":
        codex = CodexAppServer(transport="daemon")
        return codex, {
            "app_server_transport": "daemon",
            "transport_degraded": False,
            "transport_fallback_reason": None,
            "observation_mode": "live",
            "live_status_authoritative": True,
            "observed_at": observed_at,
        }
    if observe != "auto":
        raise WorkspaceError(
            "SESSION_OBSERVATION_MODE_INVALID",
            "session observation must be auto, stored, or live",
            observation_mode=observe,
            retryable=False,
        )
    fallback_error: CodexRpcError | None = None
    try:
        codex = CodexAppServer()
    except CodexRpcError as exc:
        requested = os.environ.get("AGENT_LANE_CODEX_TRANSPORT", "auto")
        if (
            str(requested).strip().casefold() != "auto"
            or not exc.retryable
            or exc.error_code not in READ_ONLY_STDIO_FALLBACK_ERRORS
        ):
            raise
        fallback_error = exc
        codex = CodexAppServer(transport="stdio")

    transport = str(getattr(codex, "transport", "stdio"))
    live_status_authoritative = transport == "daemon"
    return codex, {
        "app_server_transport": transport,
        "transport_degraded": fallback_error is not None,
        "transport_fallback_reason": (
            fallback_error.error_code if fallback_error is not None else None
        ),
        "observation_mode": (
            "shared_daemon" if live_status_authoritative else "persisted_stdio"
        ),
        "live_status_authoritative": live_status_authoritative,
        "observed_at": observed_at,
        "warnings": []
        if live_status_authoritative
        else [
            _non_authoritative_session_warning(
                fallback_error.error_code if fallback_error is not None else None
            )
        ],
    }


def cmd_codex_recent(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    limit = max(args.limit, 0)
    detail = str(getattr(args, "detail", "summary"))
    if args.aliases_only:
        aliases = _sorted_aliases(alias_root)
        transport: dict[str, Any] = {}
        if args.refresh:
            aliases = _refresh_aliases_from_codex(
                aliases,
                alias_root,
                observe=getattr(args, "observe", "auto"),
                transport_out=transport,
            )
            aliases = _sort_aliases(aliases)
        else:
            transport = _stored_alias_observation()
            aliases = _annotate_alias_observation(aliases, transport)
        return {
            "ok": True,
            "source": "lane_aliases",
            "view": "aliases",
            "aliases_only": True,
            "include_unaliased": False,
            "refreshed": bool(args.refresh),
            "include_last_turn": False,
            "detail": detail,
            **transport,
            **_project_session_output(
                [
                    _alias_summary(item, alias_root=alias_root)
                    for item in aliases[:limit]
                ],
                detail=detail,
            ),
        }

    alias_by_thread_id = _alias_by_thread_id(alias_root)
    codex, transport = _open_read_only_codex(getattr(args, "observe", "auto"))
    with codex:
        goal_observations: dict[str, dict[str, Any]] = {}
        raw_items, pagination = _collect_session_pages(
            codex,
            page_limit=_session_fetch_limit(
                limit,
                include_subagents=args.include_subagents,
            ),
            enough=lambda candidates: len(
                _session_summaries(
                    candidates,
                    alias_by_thread_id,
                    include_subagents=args.include_subagents,
                    limit=limit,
                )
            )
            >= limit,
        )
        natural_items = _session_summaries(
            raw_items,
            alias_by_thread_id,
            include_subagents=args.include_subagents,
            limit=max(limit, len(raw_items)),
        )
        natural_thread_ids = {
            str(item.get("id") or "")
            for item in natural_items[:limit]
            if item.get("id")
        }
        active_goal_thread_ids, active_goal_scan = _scan_aliased_active_goals(
            codex,
            alias_by_thread_id,
            goal_observations=goal_observations,
        )
        items, thread_read_errors = _merge_active_goal_sessions(
            codex,
            natural_items,
            raw_items,
            alias_by_thread_id,
            active_goal_thread_ids=active_goal_thread_ids,
        )
        active_goal_scan["thread_read_errors"] = thread_read_errors
        rollout_facts = _load_session_rollout_facts(items, alias_by_thread_id)
        items = _merge_completed_rollout_sessions(
            items,
            alias_by_thread_id,
            goal_observations=goal_observations,
            rollout_facts=rollout_facts,
        )
        items = _enrich_session_summaries_with_goals(
            codex,
            items,
            goal_observations=goal_observations,
            thread_authoritative=bool(transport["live_status_authoritative"]),
            observed_at=transport.get("observed_at"),
            observation_mode=str(transport.get("observation_mode") or "unknown"),
        )
        for item in items:
            if item.get("goal_status") == "active":
                item["hidden_active_goal"] = str(item.get("id") or "") not in (
                    natural_thread_ids
                )
        if args.include_last_turn:
            items = _enrich_session_summaries_with_last_turns(
                codex,
                items,
                thread_authoritative=bool(
                    transport["live_status_authoritative"]
                ),
                observed_at=transport.get("observed_at"),
                observation_mode=str(
                    transport.get("observation_mode") or "unknown"
                ),
            )
        items = _enrich_session_summaries_with_active_turns(
            items,
            rollout_facts=rollout_facts,
        )
        items = _enrich_session_summaries_with_recency(
            items,
            rollout_facts=rollout_facts,
        )
        items = sorted(
            items,
            key=_active_goal_session_sort_value,
            reverse=True,
        )[:limit]
        items = _strip_session_internal_fields(items)
        items = _apply_control_context(items, transport, alias_root=alias_root)
        project_output = _project_session_output(items, detail=detail)
    hidden_active_goal_count = len(
        active_goal_thread_ids.difference(natural_thread_ids)
    )
    visible_active_goal_count = len(
        [item for item in items if item.get("goal_status") == "active"]
    )
    return {
        "ok": True,
        "source": "codex_app",
        "view": "raw" if args.include_subagents else "main",
        "aliases_only": False,
        "include_unaliased": True,
        "include_subagents": bool(args.include_subagents),
        "include_last_turn": bool(args.include_last_turn),
        "detail": detail,
        "sort": {
            "key": "active_goal_then_recency_at",
            "direction": "desc",
            "semantics": "active_goals_first_then_completion_aware_activity_at",
        },
        "pagination": pagination,
        "active_goal_scan": active_goal_scan,
        "active_goal_count": len(active_goal_thread_ids),
        "hidden_active_goal_count": hidden_active_goal_count,
        "visible_active_goal_count": visible_active_goal_count,
        **transport,
        **project_output,
    }


def _find_page_has_enough_matches(
    codex: CodexAppServer,
    candidates: list[dict[str, Any]],
    *,
    alias_by_thread_id: dict[str, dict[str, Any]],
    goal_observations: dict[str, dict[str, Any]],
    include_subagents: bool,
    query: str,
    limit: int,
    thread_authoritative: bool,
    observed_at: float | None,
    observation_mode: str,
) -> bool:
    summaries = _session_summaries(
        candidates,
        alias_by_thread_id,
        include_subagents=include_subagents,
        limit=len(candidates),
    )
    summaries = _enrich_session_summaries_with_goals(
        codex,
        summaries,
        goal_observations=goal_observations,
        thread_authoritative=thread_authoritative,
        observed_at=observed_at,
        observation_mode=observation_mode,
    )
    return (
        len([item for item in summaries if _matches_session_summary(item, query)])
        >= limit
    )


def cmd_codex_find(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    limit = max(args.limit, 0)
    query = str(args.query)
    detail = str(getattr(args, "detail", "summary"))
    if args.aliases_only:
        candidates = _sorted_aliases(alias_root)
        transport: dict[str, Any] = {}
        if args.refresh:
            candidates = _refresh_aliases_from_codex(
                candidates,
                alias_root,
                observe=getattr(args, "observe", "auto"),
                transport_out=transport,
            )
        else:
            transport = _stored_alias_observation()
            candidates = _annotate_alias_observation(candidates, transport)
        matches = [item for item in candidates if _matches_alias(item, query)]
        matches = _sort_aliases(matches)[:limit]
        return {
            "ok": True,
            "source": "lane_aliases",
            "view": "aliases",
            "aliases_only": True,
            "query": query,
            "refreshed": bool(args.refresh),
            "include_last_turn": False,
            "detail": detail,
            **transport,
            **_project_session_output(
                [
                    _alias_summary(item, alias_root=alias_root)
                    for item in matches
                ],
                detail=detail,
            ),
        }

    alias_by_thread_id = _alias_by_thread_id(alias_root)
    codex, transport = _open_read_only_codex(getattr(args, "observe", "auto"))
    with codex:
        fetch_limit = _find_fetch_limit(limit)
        goal_observations: dict[str, dict[str, Any]] = {}

        def enough(candidates: list[dict[str, Any]]) -> bool:
            return _find_page_has_enough_matches(
                codex,
                candidates,
                alias_by_thread_id=alias_by_thread_id,
                goal_observations=goal_observations,
                include_subagents=args.include_subagents,
                query=query,
                limit=limit,
                thread_authoritative=bool(
                    transport["live_status_authoritative"]
                ),
                observed_at=transport.get("observed_at"),
                observation_mode=str(
                    transport.get("observation_mode") or "unknown"
                ),
            )

        searched, searched_pagination = _collect_session_pages(
            codex,
            page_limit=fetch_limit,
            search_term=query,
            enough=enough,
        )
        recent, recent_pagination = _collect_session_pages(
            codex,
            page_limit=fetch_limit,
            enough=enough,
        )
        summaries = _session_summaries(
            _merge_thread_items(searched, recent),
            alias_by_thread_id,
            include_subagents=args.include_subagents,
            limit=max(
                limit,
                len(searched) + len(recent),
            ),
        )
        summaries = _enrich_session_summaries_with_goals(
            codex,
            summaries,
            goal_observations=goal_observations,
            thread_authoritative=bool(transport["live_status_authoritative"]),
            observed_at=transport.get("observed_at"),
            observation_mode=str(transport.get("observation_mode") or "unknown"),
        )
        matches = [
            item for item in summaries if _matches_session_summary(item, query)
        ]
        rollout_facts = _load_session_rollout_facts(matches, alias_by_thread_id)
        if args.include_last_turn:
            matches = _enrich_session_summaries_with_last_turns(
                codex,
                matches,
                thread_authoritative=bool(
                    transport["live_status_authoritative"]
                ),
                observed_at=transport.get("observed_at"),
                observation_mode=str(
                    transport.get("observation_mode") or "unknown"
                ),
            )
        matches = _enrich_session_summaries_with_active_turns(
            matches,
            rollout_facts=rollout_facts,
        )
        matches = _enrich_session_summaries_with_recency(
            matches,
            rollout_facts=rollout_facts,
        )
        matches = sorted(
            matches,
            key=lambda item: _recency_sort_value(item.get("recency_at")),
            reverse=True,
        )[:limit]
        matches = _strip_session_internal_fields(matches)
        matches = _apply_control_context(
            matches,
            transport,
            alias_root=alias_root,
        )
        project_output = _project_session_output(matches, detail=detail)
    return {
        "ok": True,
        "source": "codex_app",
        "view": "raw" if args.include_subagents else "main",
        "aliases_only": False,
        "query": query,
        "include_subagents": bool(args.include_subagents),
        "include_last_turn": bool(args.include_last_turn),
        "detail": detail,
        "sort": {
            "key": "recency_at",
            "direction": "desc",
            "semantics": "completion_aware_activity_at",
        },
        "pagination": {
            "search": searched_pagination,
            "recent": recent_pagination,
        },
        **transport,
        **project_output,
    }


def _latest_command_cwd(thread: dict[str, Any]) -> str | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("type") != "commandExecution":
                continue
            raw_cwd = _nonempty_text(item.get("cwd"))
            if raw_cwd is not None:
                return str(Path(raw_cwd).expanduser().resolve(strict=False))
    return None


def _recommended_attach_argv(
    *,
    thread_id: str,
    lane_id: str,
    alias_root: Path,
    execution_mode: str,
    cwd: str,
) -> list[str]:
    return [
        "codex",
        "session",
        "attach",
        "--thread-id",
        thread_id,
        "--lane-id",
        lane_id,
        "--mode",
        execution_mode,
        "--alias-root",
        str(alias_root),
        "--cwd",
        cwd,
    ]


def _attach_workspace_preflight(
    *,
    thread: dict[str, Any],
    thread_id: str,
    lane_id: str,
    alias_root: Path,
    execution_mode: str,
    requested_cwd: str | None,
    existing: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any], str]:
    explicit_cwd = _nonempty_text(requested_cwd)
    raw_thread_cwd = _nonempty_text(thread.get("cwd"))
    thread_cwd = (
        str(Path(raw_thread_cwd).expanduser().resolve(strict=False))
        if raw_thread_cwd is not None
        else None
    )
    observed_cwd = _latest_command_cwd(thread)
    configured_cwd = _resolve_cwd(explicit_cwd or thread_cwd, None)
    if configured_cwd is None and observed_cwd is not None:
        configured_cwd = _resolve_cwd(observed_cwd, None)

    evidence_cwd = observed_cwd or thread_cwd
    evidence_source = (
        "recent_command"
        if observed_cwd is not None
        else "thread"
        if thread_cwd is not None
        else "unavailable"
    )

    source = (
        "explicit_attach"
        if explicit_cwd is not None
        else "recent_command"
        if observed_cwd is not None
        else "thread"
    )
    preflight = {
        "status": "unavailable" if evidence_cwd is None else "matched",
        "configured_cwd": configured_cwd,
        "thread_cwd": thread_cwd,
        "observed_cwd": observed_cwd,
        "observed_worktree": None,
        "source": evidence_source,
    }
    existing_cwd = _nonempty_text((existing or {}).get("cwd"))
    managed_lane = bool(
        existing is not None and existing.get("adopted_from") != "codex-app"
    )
    replacement_cwd = evidence_cwd or configured_cwd
    managed_drift = (
        sibling_worktree_drift(existing_cwd, replacement_cwd)
        if existing_cwd is not None and replacement_cwd is not None
        else None
    )
    if (
        managed_lane
        and existing_cwd is not None
        and replacement_cwd is not None
        and workspace_binding_changed(existing_cwd, replacement_cwd)
    ):
        raise WorkspaceError(
            "CODEX_ATTACH_WORKSPACE_DRIFT",
            "the task's observed workspace differs from its managed lane; "
            "replace the Codex task through run instead of moving the binding",
            control_created=False,
            lane_id=lane_id,
            codex_thread_id=thread_id,
            configured_cwd=existing_cwd,
            thread_cwd=thread_cwd,
            observed_cwd=observed_cwd,
            workspace_evidence_source=evidence_source,
            observed_worktree=(
                managed_drift.get("observed_worktree")
                if isinstance(managed_drift, dict)
                else replacement_cwd
            ),
            git_common_dir=(
                managed_drift.get("git_common_dir")
                if isinstance(managed_drift, dict)
                else None
            ),
            recommended_cwd=replacement_cwd,
            replacement_required=True,
            required_action="run_workspace_rebind",
            recommended_attach_argv=None,
            recovery={
                "command": "run",
                "lane_id": lane_id,
                "cwd": replacement_cwd,
                "thread_action": "replace",
            },
            retryable=False,
        )
    if configured_cwd is None or evidence_cwd is None:
        return configured_cwd, preflight, source

    drift = sibling_worktree_drift(configured_cwd, evidence_cwd)
    binding_changed = workspace_binding_changed(configured_cwd, evidence_cwd)
    if drift is None and not binding_changed:
        return configured_cwd, preflight, source

    observed_worktree = (
        drift.get("observed_worktree") if isinstance(drift, dict) else evidence_cwd
    )
    raise WorkspaceError(
        "CODEX_ATTACH_WORKSPACE_DRIFT",
        "the task's latest known workspace differs from the requested cwd; "
        "retry attach with the reported cwd",
        control_created=False,
        lane_id=lane_id,
        codex_thread_id=thread_id,
        configured_cwd=configured_cwd,
        thread_cwd=thread_cwd,
        observed_cwd=observed_cwd,
        workspace_evidence_source=evidence_source,
        observed_worktree=observed_worktree,
        git_common_dir=(
            drift.get("git_common_dir") if isinstance(drift, dict) else None
        ),
        recommended_cwd=evidence_cwd,
        replacement_required=False,
        recommended_attach_argv=_recommended_attach_argv(
            thread_id=thread_id,
            lane_id=lane_id,
            alias_root=alias_root,
            execution_mode=execution_mode,
            cwd=evidence_cwd,
        ),
        retryable=False,
    )


def cmd_codex_adopt(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    thread_id = str(args.thread_id).strip()
    if not thread_id:
        raise ValueError("--thread-id requires a non-empty value")
    requested_custom_title = _nonempty_text(getattr(args, "title", None))
    if getattr(args, "title", None) is not None and requested_custom_title is None:
        raise WorkspaceError(
            "LANE_CUSTOM_TITLE_INVALID",
            "--title requires a non-empty value",
            lane_id=args.lane_id,
            retryable=False,
        )

    existing = load_alias(CODEX_PROVIDER, args.lane_id, alias_root)
    requested_mode = getattr(args, "mode", None)
    execution_mode, resolved_mode_source = _resolve_execution_mode(
        requested_mode,
        existing,
        allow_rebind=True,
    )
    execution_mode_source = (
        resolved_mode_source
        if requested_mode is not None
        else str(
            (existing or {}).get("execution_mode_source")
            or resolved_mode_source
        )
    )
    if existing and str(existing.get("codex_thread_id") or "") != thread_id:
        raise WorkspaceError(
            "LANE_ALIAS_EXISTS",
            "lane-id is already bound to a different Codex thread",
            lane_id=args.lane_id,
            codex_thread_id=existing.get("codex_thread_id"),
        )
    for item in _target_alias_registry(alias_root):
        if (
            str(item.get("codex_thread_id") or "") == thread_id
            and str(item.get("lane_id") or "") != args.lane_id
        ):
            raise WorkspaceError(
                "CODEX_THREAD_ALREADY_ALIASED",
                "Codex thread is already bound to another lane-id",
                codex_thread_id=thread_id,
                lane_id=item.get("lane_id"),
            )

    with CodexAppServer(transport=_transport_for_mode(execution_mode)) as codex:
        result = codex.read_thread(thread_id, include_turns=True)
    thread = result.get("thread") or {}
    cwd, workspace_preflight, workspace_binding_source = (
        _attach_workspace_preflight(
            thread=thread,
            thread_id=thread_id,
            lane_id=str(args.lane_id),
            alias_root=alias_root,
            execution_mode=execution_mode,
            requested_cwd=getattr(args, "cwd", None),
            existing=existing,
        )
    )

    if existing is not None:
        alias = dict(existing)
        if alias.get("adopted_from") == "codex-app":
            if cwd:
                alias["cwd"] = cwd
                alias["workspace"] = _workspace_status(cwd, None)
                alias["workspace_binding_source"] = workspace_binding_source
        _update_thread_alias(alias, thread)
        if getattr(args, "title", None) is not None:
            alias["custom_title"] = requested_custom_title
        if args.sandbox:
            alias["sandbox"] = _resolve_sandbox(args.sandbox, alias)
        _record_execution_mode(
            alias,
            mode=execution_mode,
            source=execution_mode_source,
        )
        path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
        cwd = alias.get("cwd")
        workspace = _workspace_status(cwd, alias.get("workspace"))
        return {
            "ok": True,
            "provider": "codex",
            "lane_id": args.lane_id,
            "codex_thread_id": thread_id,
            "codex_url": f"codex://threads/{thread_id}",
            "alias_path": str(path),
            "adopted": False,
            "cwd": cwd,
            **_title_contract(alias, thread=thread, lane_id=args.lane_id),
            **_binding_contract(alias),
            "sandbox": alias.get("sandbox"),
            "workspace": workspace,
            "execution_mode": execution_mode,
            "execution_mode_source": execution_mode_source,
            "workspace_preflight": workspace_preflight,
            "control": _control_contract(
                alias,
                thread_id,
                thread=thread,
                alias_root=alias_root,
            ),
        }

    if not cwd:
        raise WorkspaceError(
            "CODEX_THREAD_CWD_REQUIRED",
            "thread does not expose a usable cwd; pass --cwd explicitly",
            codex_thread_id=thread_id,
            control_created=False,
            required_action="retry_explicit_attach_with_cwd",
            required_option="--cwd",
            retryable=False,
        )

    now = time.time()
    alias: dict[str, Any] = {}
    sandbox = _resolve_sandbox(args.sandbox, alias)
    git = _git_snapshot(cwd, include_details=False)
    workspace = workspace_snapshot(
        cwd,
        branch=git.get("branch"),
        dirty=git.get("dirty"),
    )
    alias.update(
        {
            "lane_id": args.lane_id,
            "codex_thread_id": thread_id,
            "codex_url": f"codex://threads/{thread_id}",
            "cwd": cwd,
            "sandbox": sandbox,
            "workspace": workspace,
            "workspace_binding_source": workspace_binding_source,
            "adopted_from": "codex-app",
            "adopted_at": alias.get("adopted_at") or now,
            "created_at": alias.get("created_at") or now,
        }
    )
    if not isinstance(alias.get("binding"), dict):
        _initialize_codex_binding(
            alias,
            thread_id=thread_id,
            origin="adopted",
            bound_at=now,
        )
    _record_execution_mode(
        alias,
        mode=execution_mode,
        source=execution_mode_source,
    )
    _update_thread_alias(alias, thread)
    if requested_custom_title is not None:
        alias["custom_title"] = requested_custom_title
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "provider": "codex",
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        "adopted": True,
        "cwd": cwd,
        **_title_contract(alias, thread=thread, lane_id=args.lane_id),
        **_binding_contract(alias),
        "sandbox": sandbox,
        "workspace": workspace,
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "workspace_preflight": workspace_preflight,
        "control": _control_contract(
            alias,
            thread_id,
            thread=thread,
            alias_root=alias_root,
        ),
    }


def cmd_codex_name_get(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    thread_id = str(alias["codex_thread_id"])
    codex, transport = _open_read_only_codex(args.observe)
    with codex:
        result = codex.read_thread(thread_id, include_turns=False)
    thread = result.get("thread") or {}
    observed_thread_id = _nonempty_text(thread.get("id")) or thread_id
    if observed_thread_id != thread_id:
        raise WorkspaceError(
            "LANE_BINDING_CHANGED",
            "Codex returned a different thread while reading its name",
            lane_id=args.lane_id,
            expected_thread_id=thread_id,
            observed_thread_id=observed_thread_id,
            retryable=False,
        )
    _update_thread_alias(alias, thread)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        **_title_contract(alias, thread=thread, lane_id=args.lane_id),
        **_binding_contract(alias),
        "binding": alias.get("binding"),
        **transport,
    }


def cmd_codex_name_set(args: argparse.Namespace) -> dict[str, Any]:
    requested_title = _nonempty_text(args.title)
    if requested_title is None:
        raise WorkspaceError(
            "CODEX_NAME_INVALID",
            "--title requires a non-empty value",
            lane_id=args.lane_id,
            retryable=False,
        )
    expected_title = (
        _nonempty_text(args.expected_title)
        if getattr(args, "expected_title", None) is not None
        else None
    )
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    thread_id = str(alias["codex_thread_id"])
    with _codex_for_alias(alias) as codex:
        before_result = codex.read_thread(thread_id, include_turns=False)
        before = before_result.get("thread") or {}
        before_thread_id = _nonempty_text(before.get("id")) or thread_id
        if before_thread_id != thread_id:
            raise WorkspaceError(
                "LANE_BINDING_CHANGED",
                "Codex returned a different thread before renaming",
                lane_id=args.lane_id,
                expected_thread_id=thread_id,
                observed_thread_id=before_thread_id,
                retryable=False,
            )
        observed_title = _nonempty_text(before.get("name"))
        if getattr(args, "expected_title", None) is not None and (
            observed_title != expected_title
        ):
            _update_thread_alias(alias, before)
            path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
            raise WorkspaceError(
                "CODEX_NAME_CONFLICT",
                "the live Codex thread name does not match --expected-title",
                lane_id=args.lane_id,
                codex_thread_id=thread_id,
                expected_title=expected_title,
                observed_title=observed_title,
                alias_path=str(path),
                retryable=False,
            )
        codex.set_thread_name(thread_id, requested_title)
        after_result = codex.read_thread(thread_id, include_turns=False)
    after = after_result.get("thread") or {}
    after_thread_id = _nonempty_text(after.get("id")) or thread_id
    if after_thread_id != thread_id:
        raise WorkspaceError(
            "LANE_BINDING_CHANGED",
            "Codex returned a different thread while confirming its new name",
            lane_id=args.lane_id,
            expected_thread_id=thread_id,
            observed_thread_id=after_thread_id,
            retryable=False,
        )
    readback_title = _nonempty_text(after.get("name"))
    _update_thread_alias(alias, after)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    if readback_title != requested_title:
        raise WorkspaceError(
            "CODEX_NAME_READBACK_MISMATCH",
            "Codex did not confirm the requested thread name",
            lane_id=args.lane_id,
            codex_thread_id=thread_id,
            requested_title=requested_title,
            observed_title=readback_title,
            alias_path=str(path),
            retryable=True,
        )
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        **_title_contract(alias, thread=after, lane_id=args.lane_id),
        **_binding_contract(alias),
        "binding": alias.get("binding"),
        "renamed": observed_title != requested_title,
        "previous_codex_title": observed_title,
    }


def cmd_codex_custom_title_get(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    thread_id = str(alias["codex_thread_id"])
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(alias_path(CODEX_PROVIDER, args.lane_id, alias_root)),
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
    }


def cmd_codex_custom_title_set(args: argparse.Namespace) -> dict[str, Any]:
    requested_title = _nonempty_text(args.title)
    if requested_title is None:
        raise WorkspaceError(
            "LANE_CUSTOM_TITLE_INVALID",
            "--title requires a non-empty value",
            lane_id=args.lane_id,
            retryable=False,
        )
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    previous_title = _custom_title(alias)
    alias["custom_title"] = requested_title
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    thread_id = str(alias["codex_thread_id"])
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
        "updated": previous_title != requested_title,
        "previous_custom_title": previous_title,
    }


def cmd_codex_custom_title_clear(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    alias = dict(_require_command_alias(args, alias_root))
    previous_title = _custom_title(alias)
    alias.pop("custom_title", None)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    thread_id = str(alias["codex_thread_id"])
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
        "cleared": previous_title is not None,
        "previous_custom_title": previous_title,
    }


def cmd_codex_outline(args: argparse.Namespace) -> dict[str, Any]:
    alias, thread_id = _resolve_thread_target(args)
    if not thread_id:
        raise ValueError("lane alias does not contain a Codex thread id")
    codex, transport = _open_read_only_codex(getattr(args, "observe", "auto"))
    with codex:
        result = codex.read_thread(thread_id, include_turns=True)
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise ValueError(f"Codex thread {thread_id!r} was not returned")
    return {
        **_thread_outline(thread, alias, fallback_thread_id=thread_id),
        **transport,
    }


def cmd_codex_read(args: argparse.Namespace) -> dict[str, Any]:
    alias, thread_id = _resolve_thread_target(args)
    turn_id = getattr(args, "turn_id", None)
    turn_index = getattr(args, "turn_index", None)
    include_turns = bool(getattr(args, "include_turns", False))
    selecting_turn = turn_id is not None or turn_index is not None

    if not thread_id:
        if selecting_turn:
            raise ValueError("lane alias does not contain a Codex thread id")
        return {"ok": True, "alias": alias, "thread": None}

    codex, transport = _open_read_only_codex(getattr(args, "observe", "auto"))
    with codex:
        result = codex.read_thread(
            thread_id,
            include_turns=include_turns or selecting_turn,
        )
        try:
            goal = codex.get_goal(thread_id)
            goal_source = "thread_goal_get"
        except Exception as exc:
            goal = None
            goal_source = "unavailable"
            goal_error = str(exc)
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise ValueError(f"Codex thread {thread_id!r} was not returned")
    runner = _runner_state(
        dict(alias or {}),
        goal if isinstance(goal, dict) else None,
        thread=thread,
        goal_source=goal_source,
        thread_authoritative=bool(transport["live_status_authoritative"]),
        observed_at=transport.get("observed_at"),
        observation_mode=str(transport.get("observation_mode") or "unknown"),
    )
    common = {
        "goal_status": goal.get("status") if isinstance(goal, dict) else None,
        "goal_status_source": goal_source,
        **_execution_fields(runner),
        "control": _control_contract(
            alias,
            thread_id,
            thread=thread,
            attach_mode=_execution_mode_from_transport(transport),
            alias_root=Path(args.alias_root).expanduser(),
        ),
    }
    if goal_source == "unavailable":
        common["goal_refresh_error"] = goal_error
    if not selecting_turn:
        return {
            "ok": True,
            "alias": alias,
            "thread": result,
            **common,
            **transport,
        }

    turns = thread.get("turns")
    if not isinstance(turns, list):
        turns = []
    selected, selected_index = _select_turn(
        turns,
        turn_id=str(turn_id) if turn_id is not None else None,
        turn_index=turn_index,
    )
    metadata = dict(thread)
    metadata.pop("turns", None)
    return {
        "ok": True,
        "alias": alias,
        "thread": metadata,
        "selection": {
            "turn_id": selected.get("id"),
            "turn_index": selected_index,
        },
        "turn": selected,
        **common,
        **transport,
    }


def _resolve_thread_target(
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, str | None]:
    alias_root = Path(args.alias_root).expanduser()
    requested = _requested_target(args)
    if requested is None:
        raise ValueError("one task target is required")
    kind = str(requested["kind"])
    value = str(requested["value"])
    if kind == "lane_id":
        alias = _load_target_alias(alias_root, value)
        if not alias:
            raise ValueError(f"no alias found for lane-id {value!r}")
        raw_thread_id = alias.get("codex_thread_id")
        thread_id = str(raw_thread_id).strip() if raw_thread_id else None
        args._target_resolution = {
            "requested": requested,
            "source": "explicit_lane_id",
            "user_supplied_lane_id": True,
            "resolved": {"lane_id": value, "thread_id": thread_id},
        }
        args._target_expected_thread_id = thread_id
        return alias, thread_id

    matches = _target_alias_matches(
        alias_root,
        kind=kind,
        value=value,
        allow_invalid_registry=kind == "thread_id",
    )
    if len(matches) > 1:
        raise _ambiguous_target_error(
            requested,
            matches,
            alias_root=alias_root,
        )
    if matches:
        alias = matches[0]
        thread_id = _nonempty_text(alias.get("codex_thread_id"))
        args._target_resolution = {
            "requested": requested,
            "source": {
                "thread_id": "thread_binding",
                "title": "exact_title",
                "current": "current_cwd",
            }[kind],
            "user_supplied_lane_id": False,
            "resolved": {
                "lane_id": alias.get("lane_id"),
                "thread_id": thread_id,
            },
        }
        args._target_expected_thread_id = thread_id
        return alias, thread_id
    if kind == "thread_id":
        args._target_resolution = {
            "requested": requested,
            "source": "unbound_thread_read_only",
            "user_supplied_lane_id": False,
            "resolved": {"lane_id": None, "thread_id": value},
        }
        return None, value
    raise WorkspaceError(
        "CODEX_TARGET_NOT_FOUND",
        "no attached Codex task matches the requested target",
        requested_target=requested,
        discover_argv=["codex", "session", "list", "--scope", "all"],
        retryable=False,
    )


def _control_contract(
    alias: dict[str, Any] | None,
    thread_id: str,
    *,
    thread: dict[str, Any] | None = None,
    attach_mode: str | None = None,
    alias_root: str | Path | None = None,
) -> dict[str, Any]:
    lane_id = _nonempty_text((alias or {}).get("lane_id"))
    if lane_id is not None:
        alias_root_argv = _alias_root_argv(alias_root)
        return {
            "binding_status": "attached",
            "control_ready": True,
            "requires_explicit_attach": False,
            "lane_id": lane_id,
            "thread_id": thread_id,
            "suggested_lane_id": None,
            "attach_argv": None,
            "target_argv": ["--thread-id", thread_id, *alias_root_argv],
            "lane_target_argv": ["--lane-id", lane_id, *alias_root_argv],
            "send_target_argv": [
                "codex",
                "send",
                "--thread-id",
                thread_id,
                *alias_root_argv,
            ],
        }

    suggested_lane_id = _suggested_lane_id(thread or {}, thread_id)
    attach_argv = [
        "codex",
        "session",
        "attach",
        "--thread-id",
        thread_id,
    ]
    if attach_mode is not None:
        attach_argv.extend(["--mode", attach_mode])
    attach_argv.extend(_alias_root_argv(alias_root))
    return {
        "binding_status": "unattached",
        "control_ready": False,
        "requires_explicit_attach": True,
        "lane_id": None,
        "thread_id": thread_id,
        "suggested_lane_id": suggested_lane_id,
        "target_argv": ["--thread-id", thread_id],
        "lane_target_argv": None,
        "attach_argv": attach_argv,
        "send_target_argv": None,
        "after_attach_argv": None,
    }


def _execution_mode_from_transport(transport: dict[str, Any]) -> str | None:
    observed = _nonempty_text(transport.get("app_server_transport"))
    if observed == "daemon":
        return "app-sync"
    if observed == "stdio":
        return "independent"
    return None


def _apply_control_context(
    items: list[dict[str, Any]],
    transport: dict[str, Any],
    *,
    alias_root: Path,
) -> list[dict[str, Any]]:
    attach_mode = _execution_mode_from_transport(transport)
    for item in items:
        control = item.get("control")
        if not isinstance(control, dict):
            continue
        thread_id = _nonempty_text(control.get("thread_id"))
        if thread_id is None:
            continue
        if control.get("binding_status") == "attached":
            lane_id = _nonempty_text(control.get("lane_id"))
            if lane_id is not None:
                item["control"] = _control_contract(
                    {"lane_id": lane_id},
                    thread_id,
                    alias_root=alias_root,
                )
            continue
        if (
            control.get("binding_status") != "unattached"
            or attach_mode is None
        ):
            continue
        item["control"] = _control_contract(
            None,
            thread_id,
            attach_mode=attach_mode,
            alias_root=alias_root,
        )
    return items


def _suggested_lane_id(thread: dict[str, Any], thread_id: str) -> str:
    raw = thread.get("name") or thread.get("preview")
    if raw:
        try:
            candidate = safe_lane_id(str(raw)).casefold()[:64].rstrip(".-")
        except ValueError:
            candidate = ""
        if candidate:
            return candidate
    suffix = safe_lane_id(thread_id)[:12].casefold() if thread_id else "new"
    return f"lane-{suffix}"


def _select_turn(
    turns: list[Any],
    *,
    turn_id: str | None,
    turn_index: int | None,
) -> tuple[dict[str, Any], int]:
    valid_turns = [turn for turn in turns if isinstance(turn, dict)]
    if turn_index is not None:
        if turn_index < 1:
            raise ValueError("--turn-index must be at least 1")
        if turn_index > len(valid_turns):
            raise ValueError(
                f"--turn-index {turn_index} is out of range; "
                f"thread contains {len(valid_turns)} turns"
            )
        return valid_turns[turn_index - 1], turn_index

    assert turn_id is not None
    for index, turn in enumerate(valid_turns, start=1):
        if str(turn.get("id") or "") == turn_id:
            return turn, index
    raise ValueError(f"turn-id {turn_id!r} was not found in this thread")


def cmd_codex_goal_set(args: argparse.Namespace) -> dict[str, Any]:
    requested_custom_title = _nonempty_text(getattr(args, "title", None))
    if getattr(args, "title", None) is not None and requested_custom_title is None:
        raise WorkspaceError(
            "LANE_CUSTOM_TITLE_INVALID",
            "--title requires a non-empty value",
            lane_id=args.lane_id,
            retryable=False,
        )
    alias_root = Path(args.alias_root).expanduser()
    existing = load_alias(CODEX_PROVIDER, args.lane_id, alias_root)
    execution_mode, resolved_mode_source = _resolve_execution_mode(None, existing)
    execution_mode_source = str(
        (existing or {}).get("execution_mode_source") or resolved_mode_source
    )
    if existing is not None:
        _refresh_adopted_alias_cwd(existing, args.lane_id, alias_root)
    cwd = _resolve_cwd(args.cwd, existing)
    custom_title = (
        requested_custom_title
        if getattr(args, "title", None) is not None
        else _custom_title(existing or {})
    )
    codex_title = (
        _stored_codex_title(existing or {})
        if existing and existing.get("codex_thread_id")
        else _new_codex_title(
            requested_title=custom_title,
            cwd=cwd,
            lane_id=args.lane_id,
        )
    ) or args.lane_id
    sandbox = _resolve_sandbox(args.sandbox, existing)
    commit_signing_mode = _resolve_commit_signing(args.commit_signing, existing)
    commit_signing = _prepare_commit_signing(commit_signing_mode)
    resumed = existing is not None and bool(existing.get("codex_thread_id"))
    written_codex_title: str | None = None

    with CodexAppServer(
        transport=_transport_for_mode(execution_mode),
        extra_env=commit_signing["extra_env"],
        config_overrides=commit_signing["config_overrides"],
    ) as codex:
        if resumed:
            thread_id = str(existing["codex_thread_id"])
            snapshot = codex.read_thread(thread_id, include_turns=False)
            _update_thread_alias(existing, snapshot.get("thread") or {})
            codex_title = _stored_codex_title(existing) or args.lane_id
            codex.resume_thread(thread_id, cwd=cwd, sandbox=sandbox)
        else:
            if not cwd:
                raise ValueError("--cwd is required when creating a new goal lane")
            thread_id = codex.start_thread(cwd, sandbox=sandbox)
            codex.set_thread_name(thread_id, codex_title)
            written_codex_title = codex_title
            codex.update_git_info(thread_id, _git_info(cwd))
        result = codex.set_goal(
            thread_id,
            objective=args.objective,
            status=args.status,
            token_budget=args.token_budget,
        )
        goal = result.get("goal")

    now = time.time()
    alias = dict(existing or {})
    alias.update(
        {
            "lane_id": args.lane_id,
            "codex_thread_id": thread_id,
            "codex_url": f"codex://threads/{thread_id}",
            "cwd": cwd,
            "sandbox": sandbox,
            "commit_signing": commit_signing["metadata"],
            "mode": "goal",
            "objective": args.objective,
            "created_at": alias.get("created_at") or now,
        }
    )
    if custom_title is None:
        alias.pop("custom_title", None)
    else:
        alias["custom_title"] = custom_title
    if not isinstance(alias.get("binding"), dict):
        _initialize_codex_binding(
            alias,
            thread_id=thread_id,
            origin="created",
            bound_at=now,
        )
    _record_execution_mode(
        alias,
        mode=execution_mode,
        source=execution_mode_source,
    )
    if written_codex_title is not None:
        _record_written_codex_title(
            alias,
            thread_id=thread_id,
            title=written_codex_title,
        )
    _update_goal_alias(alias, goal if isinstance(goal, dict) else None)
    runner = _runner_state(alias, goal if isinstance(goal, dict) else None)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "lane_id": args.lane_id,
        "resumed": resumed,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "app_server_transport": getattr(codex, "transport", "stdio"),
        "alias_path": str(path),
        **_title_contract(alias, lane_id=args.lane_id),
        **_binding_contract(alias),
        "sandbox": sandbox,
        "commit_signing": commit_signing["metadata"],
        "runner_alive": runner["alive"],
        "needs_resume": runner["needs_resume"],
        "goal": goal,
    }


def cmd_codex_goal_run(args: argparse.Namespace) -> dict[str, Any]:
    if args.turn_timeout is not None and args.turn_timeout <= 0:
        raise ValueError("--turn-timeout must be greater than zero")
    if args.max_runtime is not None and args.max_runtime <= 0:
        raise ValueError("--max-runtime must be greater than zero")
    if args.max_turns is not None and args.max_turns <= 0:
        raise ValueError("--max-turns must be greater than zero")

    alias_root = Path(args.alias_root).expanduser()
    existing = _require_command_alias(args, alias_root)
    execution_mode, resolved_mode_source = _resolve_execution_mode(None, existing)
    execution_mode_source = str(
        existing.get("execution_mode_source") or resolved_mode_source
    )
    _refresh_adopted_alias_cwd(existing, args.lane_id, alias_root)
    cwd = _resolve_cwd(None, existing)
    codex_title = _stored_codex_title(existing) or args.lane_id
    sandbox = _resolve_sandbox(args.sandbox, existing)
    request_echo = _resolve_turn_request(args, existing)
    model = request_echo["requested_model"]
    profile = _resolve_runtime_value(args.profile, existing, "profile")
    add_dirs = _resolve_add_dirs(args.add_dir, existing)
    workspace_roots = _runtime_workspace_roots(cwd, add_dirs)
    commit_signing_mode = _resolve_commit_signing(args.commit_signing, existing)
    commit_signing = _prepare_commit_signing(commit_signing_mode)
    user_config_overrides = _validated_config_overrides(args.config_overrides)
    effort = request_echo["requested_effort"]
    config_overrides = [
        *user_config_overrides,
        *commit_signing["config_overrides"],
    ]
    thread_id = str(existing["codex_thread_id"])
    alias = dict(existing)
    alias.update(
        {
            "model": model,
            "profile": profile,
            "add_dirs": add_dirs,
        }
    )
    turns: list[dict[str, Any]] = []
    started_at = time.monotonic()
    started_at_wall = time.time()
    deadline = (
        started_at + args.max_runtime
        if args.max_runtime is not None
        else None
    )
    stored_goal = alias.get("goal")
    goal = stored_goal if isinstance(stored_goal, dict) else None
    app_server_transport: str | None = None
    thread_replacement: dict[str, Any] | None = None
    additional_context: dict[str, dict[str, str]] | None = None

    def finish(
        stop_condition: str,
        reason: str,
        *,
        ok: bool,
        error: str | None = None,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        runner = _runner_state(alias, goal)
        elapsed_seconds = round(time.monotonic() - started_at, 3)
        goal_status = goal.get("status") if isinstance(goal, dict) else None
        limits = {
            "turn_timeout_seconds": args.turn_timeout,
            "max_runtime_seconds": args.max_runtime,
            "max_turns": args.max_turns,
        }
        receipt = {
            "started_at": started_at_wall,
            "finished_at": time.time(),
            "elapsed_seconds": elapsed_seconds,
            "turn_count": len(turns),
            "turns": [
                {
                    key: turn.get(key)
                    for key in ("turn_id", "status", "elapsed_seconds")
                }
                for turn in turns
            ],
            "goal_status": goal_status,
            "stop_condition": stop_condition,
            "limits": limits,
        }
        alias["last_goal_run_receipt"] = receipt
        path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
        result: dict[str, Any] = {
            "ok": ok,
            "provider": "codex",
            "lane_id": args.lane_id,
            "resumed": thread_replacement is None,
            "codex_thread_id": thread_id,
            "codex_url": f"codex://threads/{thread_id}",
            "app_server_transport": app_server_transport,
            "execution_mode": execution_mode,
            "execution_mode_source": execution_mode_source,
            "alias_path": str(path),
            **_title_contract(alias, lane_id=args.lane_id),
            **_binding_contract(alias),
            "cwd": cwd,
            "add_dirs": add_dirs,
            **request_echo,
            "commit_signing": commit_signing["metadata"],
            "goal": goal,
            "goal_status": goal_status,
            "completed": goal_status == "complete",
            **_execution_fields(runner),
            "turn_count": len(turns),
            "turns": turns,
            "goal_run_receipt": receipt,
            "last_final_text": alias.get("last_final_text"),
            "stop_condition": stop_condition,
            "reason": reason,
            "retryable": retryable,
            "elapsed_seconds": elapsed_seconds,
        }
        if error is not None:
            result["error"] = error
        if error_code is not None:
            result["error_code"] = error_code
        if thread_replacement is not None:
            result.update(
                {
                    "thread_replaced": True,
                    "origin_thread_id": thread_replacement["origin_thread_id"],
                    "handoff_reason": (
                        "loaded_thread_resume_config_not_effective"
                    ),
                }
            )
        return result

    initial_runner = _runner_state(alias, goal)
    if initial_runner["alive"]:
        return finish(
            "runner_already_active",
            "lane already has a live runner; wait for it instead",
            ok=False,
            error_code="RUNNER_ALREADY_ACTIVE",
            retryable=True,
        )

    with CodexAppServer(
        transport=_transport_for_mode(execution_mode),
        profile=profile,
        extra_env=commit_signing["extra_env"],
        config_overrides=config_overrides,
    ) as codex:
        app_server_transport = getattr(codex, "transport", "stdio")
        snapshot = codex.read_thread(thread_id, include_turns=False)
        _update_thread_alias(alias, snapshot.get("thread") or {})
        codex_title = _stored_codex_title(alias) or args.lane_id
        _require_thread_inactive_for_turn(
            snapshot.get("thread") or {},
            lane_id=args.lane_id,
            thread_id=thread_id,
        )
        goal = codex.get_goal(thread_id)
        _update_goal_alias(alias, goal)
        if goal is None:
            return finish(
                "goal_missing",
                "lane has no active Codex goal",
                ok=False,
                error_code="GOAL_MISSING",
            )
        goal_status = str(goal.get("status") or "unknown")
        if goal_status != "active":
            return finish(
                GOAL_STOP_CONDITIONS.get(goal_status, "goal_unknown"),
                f"goal stopped with status {goal_status}",
                ok=True,
            )
        prepared_thread = _prepare_existing_thread_for_turn(
            codex,
            alias=alias,
            thread_id=thread_id,
            cwd=cwd,
            title=codex_title,
            sandbox=sandbox,
            model=model,
            workspace_roots=workspace_roots,
            commit_signing_mode=commit_signing_mode,
            commit_signing=commit_signing,
            allow_signing_replacement=args.allow_signing_replacement,
            replacement_goal_objective=None,
            replacement_origin_goal=goal,
            lane_id=args.lane_id,
            alias_root=alias_root,
        )
        thread_id = str(prepared_thread["thread_id"])
        if prepared_thread["replaced"]:
            thread_replacement = prepared_thread
            additional_context = prepared_thread["additional_context"]
            codex_title = str(prepared_thread["codex_title"])
            candidate = prepared_thread.get("goal")
            goal = candidate if isinstance(candidate, dict) else None
        alias.update(
            {
                "codex_thread_id": thread_id,
                "codex_url": f"codex://threads/{thread_id}",
                "commit_signing": commit_signing["metadata"],
            }
        )
        _record_execution_mode(
            alias,
            mode=execution_mode,
            source=execution_mode_source,
        )
        _update_goal_alias(alias, goal)
        _runner_state(alias, goal)
        save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)

        if goal is None:
            return finish(
                "goal_missing",
                "lane has no active Codex goal",
                ok=False,
                error_code="GOAL_MISSING",
            )

        while goal.get("status") == "active":
            if args.max_turns is not None and len(turns) >= args.max_turns:
                return finish(
                    "max_turns",
                    "maximum continuation turns reached while goal remains active",
                    ok=True,
                    retryable=True,
                )
            remaining = (
                deadline - time.monotonic() if deadline is not None else None
            )
            if remaining is not None and remaining <= 0:
                return finish(
                    "max_runtime",
                    "maximum goal-run time reached while goal remains active",
                    ok=True,
                    retryable=True,
                )
            if remaining is None:
                turn_timeout = args.turn_timeout
                limited_by_runtime = False
            elif args.turn_timeout is None:
                turn_timeout = remaining
                limited_by_runtime = True
            else:
                turn_timeout = min(args.turn_timeout, remaining)
                limited_by_runtime = turn_timeout < args.turn_timeout
            turn_started_at = time.monotonic()
            try:
                alias.update(request_echo)
                turn = _run_tracked_turn(
                    codex,
                    alias=alias,
                    alias_root=alias_root,
                    lane_id=args.lane_id,
                    thread_id=thread_id,
                    prompt=GOAL_CONTINUATION_PROMPT,
                    sandbox=sandbox,
                    model=model,
                    effort=effort,
                    workspace_cwd=cwd,
                    workspace_roots=workspace_roots,
                    additional_context=additional_context,
                    timeout=turn_timeout,
                )
            except TimeoutError as exc:
                turns.append(
                    {
                        "turn_id": alias.get("last_turn_id"),
                        "status": "timed_out",
                        "final_text": None,
                        "error": str(exc),
                        "elapsed_seconds": round(
                            time.monotonic() - turn_started_at,
                            3,
                        ),
                    }
                )
                _update_goal_alias(alias, goal)
                stop_condition = (
                    "max_runtime" if limited_by_runtime else "turn_timeout"
                )
                reason = (
                    "maximum goal-run time reached during a continuation turn"
                    if limited_by_runtime
                    else "continuation turn timed out; goal can be resumed"
                )
                return finish(
                    stop_condition,
                    reason,
                    ok=limited_by_runtime,
                    error=str(exc),
                    error_code=None if limited_by_runtime else "TURN_TIMEOUT",
                    retryable=True,
                )
            except Exception as exc:
                turns.append(
                    {
                        "turn_id": alias.get("last_turn_id"),
                        "status": alias.get("last_status"),
                        "final_text": None,
                        "error": str(exc),
                        "elapsed_seconds": round(
                            time.monotonic() - turn_started_at,
                            3,
                        ),
                    }
                )
                _update_goal_alias(alias, goal)
                return finish(
                    "turn_failure",
                    "continuation turn failed; inspect error before resuming",
                    ok=False,
                    error=str(exc),
                    error_code="TURN_FAILED",
                    retryable=True,
                )

            _update_turn_alias(
                alias,
                args.lane_id,
                thread_id,
                cwd,
                sandbox,
                turn,
            )
            additional_context = None
            turns.append(
                {
                    "turn_id": turn.turn_id,
                    "status": turn.status,
                    "final_text": turn.final_text,
                    "elapsed_seconds": round(
                        time.monotonic() - turn_started_at,
                        3,
                    ),
                }
            )
            save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
            try:
                thread = codex.read_thread(thread_id, include_turns=False)
                _update_thread_alias(alias, thread.get("thread") or {})
                goal = codex.get_goal(thread_id)
            except Exception as exc:
                return finish(
                    "status_refresh_failure",
                    "turn completed but goal/status refresh failed",
                    ok=False,
                    error=str(exc),
                    error_code="GOAL_STATUS_REFRESH_FAILED",
                    retryable=True,
                )
            _update_goal_alias(alias, goal)
            _runner_state(alias, goal)
            save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)

            if turn.status != "completed":
                return finish(
                    f"turn_{turn.status}",
                    "continuation turn did not complete successfully",
                    ok=False,
                    error_code="TURN_NOT_COMPLETED",
                    retryable=goal is not None and goal.get("status") == "active",
                )

        goal_status = str(goal.get("status") or "unknown")
        return finish(
            GOAL_STOP_CONDITIONS.get(goal_status, "goal_unknown"),
            f"goal stopped with status {goal_status}",
            ok=True,
        )


def cmd_codex_signing_init(args: argparse.Namespace) -> dict[str, Any]:
    result = init_signing(generate=bool(args.generate))
    return {"ok": True, **result}


def cmd_codex_signing_status(_args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **signing_status()}


def cmd_codex_signing_test(_args: argparse.Namespace) -> dict[str, Any]:
    result = signing_smoke_test()
    return {"ok": bool(result.get("signed")), **result}


def cmd_codex_signing_stop(_args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **stop_agent()}


def cmd_codex_goal_get(args: argparse.Namespace) -> dict[str, Any]:
    direct_thread_id = _nonempty_text(
        getattr(args, "_direct_thread_id", None)
    )
    if direct_thread_id is not None:
        snapshot = _direct_thread_snapshot(
            direct_thread_id,
            include_turns=False,
            alias_root=Path(args.alias_root).expanduser(),
        )
        return {
            "ok": True,
            "lane_id": None,
            "codex_thread_id": direct_thread_id,
            "codex_url": f"codex://threads/{direct_thread_id}",
            "alias_path": None,
            **_execution_fields(snapshot["runner"]),
            "goal": snapshot.get("goal"),
            "control": snapshot["control"],
        }
    alias_root = Path(args.alias_root).expanduser()
    alias = _require_command_alias(args, alias_root)
    thread_id = str(alias["codex_thread_id"])
    with _codex_for_alias(alias) as codex:
        goal = codex.get_goal(thread_id)
    _update_goal_alias(alias, goal)
    runner = _runner_state(alias, goal)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        "runner_alive": runner["alive"],
        "needs_resume": runner["needs_resume"],
        "goal": goal,
    }


def cmd_codex_goal_complete(args: argparse.Namespace) -> dict[str, Any]:
    return _cmd_codex_goal_status(args, "complete")


def cmd_codex_goal_clear(args: argparse.Namespace) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    alias = _require_command_alias(args, alias_root)
    thread_id = str(alias["codex_thread_id"])
    with _codex_for_alias(alias) as codex:
        result = codex.clear_goal(thread_id)
        goal = codex.get_goal(thread_id)
    _update_goal_alias(alias, goal)
    runner = _runner_state(alias, goal)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        "runner_alive": runner["alive"],
        "needs_resume": runner["needs_resume"],
        "result": result,
        "goal": goal,
    }


def _cmd_codex_goal_status(args: argparse.Namespace, status: str) -> dict[str, Any]:
    alias_root = Path(args.alias_root).expanduser()
    alias = _require_command_alias(args, alias_root)
    thread_id = str(alias["codex_thread_id"])
    with _codex_for_alias(alias) as codex:
        result = codex.set_goal(thread_id, status=status)
        goal = result.get("goal")
    _update_goal_alias(alias, goal if isinstance(goal, dict) else None)
    runner = _runner_state(alias, goal if isinstance(goal, dict) else None)
    path = save_alias(CODEX_PROVIDER, args.lane_id, alias, alias_root)
    return {
        "ok": True,
        "lane_id": args.lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        "runner_alive": runner["alive"],
        "needs_resume": runner["needs_resume"],
        "goal": goal,
    }



TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}
STOPPED_RUNNER_STATUSES = {*TERMINAL_TURN_STATUSES, "timed_out", "stale"}
RESUMABLE_RUNNER_STATUSES = {
    "failed",
    "interrupted",
    "stale",
    "timed_out",
    "unknown",
}
GOAL_CONTINUATION_PROMPT = (
    "Continue the active goal from current state; do not narrow or rewrite it."
)
GOAL_STOP_CONDITIONS = {
    "blocked": "goal_blocked",
    "budgetLimited": "goal_budget_limited",
    "complete": "goal_complete",
    "paused": "goal_paused",
    "usageLimited": "goal_usage_limited",
}
OBSERVATION_LIMITATION = (
    "Cross-process app-server event subscription is unavailable; agent-lane "
    "polls thread/read(includeTurns=true) and never interrupts the Codex turn."
)


def _is_terminal_turn_status(value: Any) -> bool:
    return isinstance(value, str) and value in TERMINAL_TURN_STATUSES


def _is_stopped_runner_status(value: Any) -> bool:
    return isinstance(value, str) and value in STOPPED_RUNNER_STATUSES


def process_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clear_runner_tracking(alias: dict[str, Any]) -> None:
    alias["current_turn_id"] = None
    alias["runner_alive"] = False
    alias.pop("pending_turn_started_at", None)
    alias.pop("previous_turn_id", None)
    alias.pop("runner_pid", None)


def _mark_runner_stopped(
    alias: dict[str, Any],
    status: str,
    *,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    alias["last_status"] = status
    alias["needs_resume"] = True
    _clear_runner_tracking(alias)
    if error is not None:
        alias["last_error"] = error
    if error_code is not None:
        alias["last_error_code"] = error_code
    if status == "timed_out":
        alias["timed_out_at"] = time.time()


def _runner_state(
    alias: dict[str, Any],
    goal: dict[str, Any] | None = None,
    *,
    thread: dict[str, Any] | None = None,
    thread_active: bool | None = None,
    thread_observed: bool | None = None,
    observed_turn: dict[str, Any] | None = None,
    goal_source: str | None = None,
    thread_authoritative: bool = True,
    observed_at: float | None = None,
    observation_mode: str | None = None,
) -> dict[str, Any]:
    if thread is not None:
        thread_observed = True
        thread_active = _thread_has_active_turn(thread)
    elif thread_observed is None:
        thread_observed = thread_active is not None
    evidence_thread_observed = bool(thread_observed)
    observed_thread_active = thread_active if evidence_thread_observed else None
    current_thread_observed = evidence_thread_observed and thread_authoritative
    thread_active = observed_thread_active if current_thread_observed else None

    pid = alias.get("runner_pid")
    runner_alive = process_running(pid)
    if pid is None and isinstance(alias.get("owner_running"), bool):
        runner_alive = bool(alias.get("owner_running"))
    last_status = str(
        alias.get("local_runner_status") or alias.get("last_status") or "idle"
    )
    last_error = str(alias.get("last_error") or "")
    if (
        not runner_alive
        and last_status == "unknown"
        and "timed out" in last_error.casefold()
        and alias.get("last_error_code") in {None, "TURN_TIMEOUT"}
    ):
        _mark_runner_stopped(
            alias,
            "timed_out",
            error=last_error,
            error_code="TURN_TIMEOUT",
        )
        last_status = "timed_out"
    elif (
        thread_active is not True
        and pid is not None
        and not runner_alive
        and last_status in {
            "starting",
            "inProgress",
        }
    ):
        _mark_runner_stopped(alias, "stale", error_code="STALE_RUNNER")
        alias["stale_detected_at"] = time.time()
        last_status = "stale"
    elif not runner_alive and pid is not None:
        alias.pop("runner_pid", None)

    goal_status = None
    if isinstance(goal, dict):
        goal_status = goal.get("status")
    if goal_status is None:
        goal_status = alias.get("goal_status")

    execution_active = runner_alive or thread_active is True
    if runner_alive and thread_active is True:
        execution_source = "runner_and_thread"
    elif runner_alive:
        execution_source = "runner"
    elif thread_active is True:
        execution_source = "thread"
    else:
        execution_source = "none" if current_thread_observed else "unknown"

    terminal_evidence_authoritative = thread_authoritative or isinstance(
        observed_turn,
        dict,
    )
    alias_proves_inactive = terminal_evidence_authoritative and (
        last_status in STOPPED_RUNNER_STATUSES
        or ("last_status" in alias and last_status == "idle")
    )
    if execution_active:
        needs_resume = False
    elif not current_thread_observed and not alias_proves_inactive:
        needs_resume = False
    elif goal_status == "active":
        needs_resume = True
    elif goal_status is not None:
        needs_resume = False
    else:
        needs_resume = last_status in RESUMABLE_RUNNER_STATUSES

    alias["runner_alive"] = runner_alive
    alias["needs_resume"] = needs_resume
    raw_last_turn = _observed_last_turn(thread, alias, observed_turn)
    if (
        not thread_authoritative
        and raw_last_turn.get("source") == "app_server"
    ):
        raw_last_turn = {**raw_last_turn, "source": "persisted_app_server"}
    canonical_last_turn = _canonical_last_turn(
        alias,
        active=execution_active,
        thread=thread,
        observed_turn=observed_turn,
        raw_last_turn=raw_last_turn,
        execution_source=execution_source,
    )
    if (
        not execution_active
        and current_thread_observed
        and str(canonical_last_turn.get("status") or "").casefold()
        in {"active", "inprogress", "in_progress", "running", "started", "starting"}
    ):
        canonical_last_turn = {
            **canonical_last_turn,
            "status": None,
            "source": "thread_inactive",
        }
    if execution_active:
        state = "active"
    elif current_thread_observed or (
        terminal_evidence_authoritative
        and _is_terminal_turn_status(canonical_last_turn.get("status"))
    ) or (
        terminal_evidence_authoritative
        and last_status in STOPPED_RUNNER_STATUSES
    ):
        state = "inactive"
    elif (
        terminal_evidence_authoritative
        and "last_status" in alias
        and last_status == "idle"
    ):
        state = "inactive"
    else:
        state = "unknown"
    if state == "unknown":
        effective_status = "unknown"
    elif execution_active:
        effective_status = "inProgress"
    elif last_status in {"stale", "timed_out"}:
        effective_status = last_status
    else:
        effective_status = str(
            canonical_last_turn.get("status") or last_status or "unknown"
        )
    conflicts = _execution_conflicts(
        active=execution_active,
        thread_active=thread_active,
        thread_observed=current_thread_observed,
        runner_alive=runner_alive,
        local_status=last_status,
        raw_last_turn=raw_last_turn,
        goal_status=goal_status,
    )
    if execution_active:
        decision_source = execution_source
    elif isinstance(observed_turn, dict):
        decision_source = "turn_result"
    elif current_thread_observed:
        decision_source = "thread"
    elif evidence_thread_observed:
        decision_source = "persisted_thread"
    elif "last_status" in alias:
        decision_source = "alias"
    else:
        decision_source = "unknown"
    execution_authoritative = state != "unknown"
    execution = {
        "state": state,
        "active": execution_active if state != "unknown" else None,
        "effective_turn_status": effective_status,
        "source": decision_source,
        "authoritative": execution_authoritative,
        "stale": not execution_authoritative,
        "observed_at": observed_at if observed_at is not None else time.time(),
        "observation_mode": observation_mode,
        "needs_resume": needs_resume,
        "evidence": {
            "thread": {
                "observed": evidence_thread_observed,
                "authoritative": (
                    thread_authoritative and evidence_thread_observed
                ),
                "status": _thread_status_type(thread),
                "active": thread_active if current_thread_observed else None,
                "observed_active": observed_thread_active,
                "active_turn_id": canonical_last_turn.get("turn_id")
                if thread_active is True
                else None,
            },
            "runner": {
                "pid": int(pid) if runner_alive and pid is not None else None,
                "alive": runner_alive,
                "status": last_status,
            },
            "last_turn": raw_last_turn,
            "goal": {
                "status": goal_status,
                "source": goal_source
                or ("thread_goal_get" if isinstance(goal, dict) else "alias"),
            },
        },
        "conflicts": conflicts,
    }
    return {
        "status": effective_status,
        "local_status": last_status,
        "alive": runner_alive,
        "thread_active": thread_active,
        "execution_active": execution["active"],
        "execution_source": execution_source,
        "needs_resume": needs_resume,
        "pid": int(pid) if runner_alive and pid is not None else None,
        "last_turn": canonical_last_turn,
        "execution": execution,
    }


def _thread_status_type(thread: dict[str, Any] | None) -> str | None:
    if not isinstance(thread, dict):
        return None
    status = thread.get("status")
    if isinstance(status, dict):
        value = status.get("type")
    else:
        value = status
    text = str(value or "").strip()
    return text or None


def _observed_last_turn(
    thread: dict[str, Any] | None,
    alias: dict[str, Any],
    observed_turn: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(observed_turn, dict):
        return _turn_result_summary(observed_turn, source="turn_result")
    if isinstance(thread, dict) and isinstance(thread.get("turns"), list):
        summary = _last_turn_summary(thread)
        if summary.get("turn_id") is not None:
            return {**summary, "source": "app_server"}
    if alias.get("last_turn_id") is not None or alias.get("last_status") is not None:
        return {
            "turn_id": alias.get("last_turn_id") or alias.get("current_turn_id"),
            "status": alias.get("last_status"),
            "started_at": alias.get("pending_turn_started_at"),
            "completed_at": None,
            "user_request": None,
            "assistant_final_lead": _last_completed_final_lead(alias),
            "assistant_final_excerpt": _clip(alias.get("last_final_text"), 800),
            "source": "alias",
        }
    return {**_empty_last_turn(), "source": "unavailable"}


def _canonical_last_turn(
    alias: dict[str, Any],
    *,
    active: bool,
    thread: dict[str, Any] | None,
    observed_turn: dict[str, Any] | None,
    raw_last_turn: dict[str, Any],
    execution_source: str,
) -> dict[str, Any]:
    if active:
        active_summary = _active_turn_summary(thread or {})
        if isinstance(active_summary, dict):
            return {
                "turn_id": active_summary.get("turn_id"),
                "status": "inProgress",
                "started_at": active_summary.get("started_at"),
                "completed_at": None,
                "user_request": active_summary.get("user_request"),
                "assistant_final_lead": None,
                "assistant_final_excerpt": None,
                "source": active_summary.get("source") or "app_server",
            }
        return {
            "turn_id": alias.get("current_turn_id")
            or alias.get("last_turn_id")
            or raw_last_turn.get("turn_id"),
            "status": "inProgress",
            "started_at": alias.get("pending_turn_started_at"),
            "completed_at": None,
            "user_request": None,
            "assistant_final_lead": None,
            "assistant_final_excerpt": None,
            "source": (
                "thread_status" if execution_source == "thread" else execution_source
            ),
        }
    if isinstance(observed_turn, dict):
        return _turn_result_summary(observed_turn, source="turn_result")
    return dict(raw_last_turn)


def _turn_result_summary(turn: dict[str, Any], *, source: str) -> dict[str, Any]:
    final_text = _clean_agent_text(str(turn.get("final_text") or ""))
    return {
        "turn_id": turn.get("turn_id") or turn.get("id"),
        "status": turn.get("status"),
        "started_at": turn.get("started_at"),
        "completed_at": turn.get("completed_at"),
        "user_request": None,
        "assistant_final_lead": _clip(_first_paragraph(final_text), 160),
        "assistant_final_excerpt": _clip(final_text, 800),
        "source": source,
    }


def _execution_conflicts(
    *,
    active: bool,
    thread_active: bool | None,
    thread_observed: bool,
    runner_alive: bool,
    local_status: str,
    raw_last_turn: dict[str, Any],
    goal_status: Any,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if active and goal_status in {
        "blocked",
        "budgetLimited",
        "cancelled",
        "complete",
        "completed",
        "failed",
        "paused",
        "usageLimited",
    }:
        conflicts.append(
            {
                "code": "EXECUTION_ACTIVE_GOAL_STOPPED",
                "fields": ["execution.active", "goal_status"],
            }
        )
    if active and local_status not in {
        "active",
        "inProgress",
        "in_progress",
        "running",
        "started",
        "starting",
    }:
        conflicts.append(
            {
                "code": "EXECUTION_ACTIVE_LOCAL_STATUS_STALE",
                "fields": ["execution.active", "local_runner_status"],
            }
        )
    if thread_active is True and _is_terminal_turn_status(
        raw_last_turn.get("status")
    ):
        conflicts.append(
            {
                "code": "THREAD_ACTIVE_LAST_TURN_TERMINAL",
                "fields": ["thread_active", "execution.evidence.last_turn.status"],
            }
        )
    if runner_alive and thread_observed and thread_active is False:
        conflicts.append(
            {
                "code": "RUNNER_ACTIVE_THREAD_INACTIVE",
                "fields": ["runner_alive", "thread_active"],
            }
        )
    return conflicts


def _execution_fields(runner: dict[str, Any]) -> dict[str, Any]:
    return {
        "runner_status": runner["status"],
        "local_runner_status": runner["local_status"],
        "runner_alive": runner["alive"],
        "thread_active": runner["thread_active"],
        "execution_active": runner["execution_active"],
        "execution_source": runner["execution_source"],
        "needs_resume": runner["needs_resume"],
        "last_turn": runner["last_turn"],
        "execution": runner["execution"],
    }


def _turn_timeout_result(
    *,
    codex: CodexAppServer,
    alias: dict[str, Any],
    alias_root: Path,
    lane_id: str,
    thread_id: str,
    cwd: str | None,
    error: str,
) -> dict[str, Any]:
    thread: dict[str, Any] | None = None
    observation_error: str | None = None
    try:
        thread = codex.read_thread(thread_id, include_turns=True).get("thread") or {}
    except Exception as exc:
        observation_error = str(exc)
    try:
        observed_goal = codex.get_goal(thread_id)
    except Exception:
        stored_goal = alias.get("goal")
        goal = stored_goal if isinstance(stored_goal, dict) else None
        goal_source = "alias"
    else:
        goal = observed_goal if isinstance(observed_goal, dict) else None
        goal_source = "thread_goal_get"
    runner = _runner_state(
        alias,
        goal,
        thread=thread if observation_error is None else None,
        thread_observed=observation_error is None,
        goal_source=goal_source,
    )
    path = save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)
    active = runner["execution_active"]
    result = {
        "ok": False,
        "provider": "codex",
        "lane_id": lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "alias_path": str(path),
        "cwd": cwd,
        "status": "timed_out",
        **_turn_request_echo(alias),
        "goal_status": goal.get("status") if goal else alias.get("goal_status"),
        "goal_status_source": goal_source,
        **_execution_fields(runner),
        "recommended_action": "observe" if active is not False else "resume",
        "retryable": active is False,
        "error_code": "TURN_TIMEOUT",
        "error": error,
    }
    if observation_error is not None:
        result["execution_observation_error"] = observation_error
    return result


def _run_tracked_turn(
    codex: CodexAppServer,
    *,
    alias: dict[str, Any],
    alias_root: Path,
    lane_id: str,
    thread_id: str,
    prompt: str,
    sandbox: str | None,
    model: str | None,
    effort: str | None,
    workspace_cwd: str | None,
    workspace_roots: list[str] | None,
    additional_context: dict[str, dict[str, str]] | None,
    timeout: float | None,
) -> Any:
    _preserve_legacy_completed_final(alias)
    previous_turn_id = alias.get("last_turn_id")
    alias.update(
        {
            "current_turn_id": None,
            "last_turn_id": None,
            "last_status": "starting",
            "pending_turn_started_at": time.time(),
            "runner_pid": os.getpid(),
            "runner_alive": True,
            "needs_resume": False,
        }
    )
    if previous_turn_id:
        alias["previous_turn_id"] = previous_turn_id
    alias.pop("last_error", None)
    alias.pop("last_error_code", None)
    alias.pop("timed_out_at", None)
    alias.pop("stale_detected_at", None)
    save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)
    started = False

    def on_started(turn_id: str | None) -> None:
        nonlocal started
        started = True
        alias.update(
            {
                "current_turn_id": turn_id,
                "last_turn_id": turn_id,
                "last_status": "inProgress",
                "runner_alive": True,
                "needs_resume": False,
            }
        )
        save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)

    try:
        return codex.run_turn(
            thread_id,
            prompt,
            sandbox=sandbox,
            model=model,
            effort=effort,
            workspace_cwd=workspace_cwd,
            runtime_workspace_roots=workspace_roots,
            additional_context=additional_context,
            timeout=timeout,
            on_started=on_started,
        )
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            _mark_runner_stopped(
                alias,
                "timed_out",
                error=str(exc),
                error_code="TURN_TIMEOUT",
            )
        elif isinstance(exc, CodexRpcError):
            if exc.error_code == "CODEX_WORKSPACE_BINDING_DRIFT":
                exc.details.setdefault("lane_id", lane_id)
                exc.details.setdefault("codex_thread_id", thread_id)
                exc.details.setdefault(
                    "recovery",
                    {
                        "command": "run",
                        "lane_id": lane_id,
                        "cwd": exc.details.get("observed_worktree"),
                        "thread_action": "replace",
                    },
                )
            _mark_runner_stopped(
                alias,
                (
                    "interrupted"
                    if exc.error_code
                    in {
                        "CODEX_INTERACTION_REQUIRED",
                        "CODEX_WORKSPACE_BINDING_DRIFT",
                    }
                    else (
                        "unknown"
                        if exc.error_code
                        == "CODEX_DAEMON_TURN_STATE_UNCERTAIN"
                        else ("unknown" if started else "failed")
                    )
                ),
                error=str(exc),
                error_code=exc.error_code,
            )
            if exc.error_code == "CODEX_DAEMON_TURN_STATE_UNCERTAIN":
                observed_thread: dict[str, Any] | None = None
                observation_error: str | None = None
                try:
                    observed_thread = (
                        codex.read_thread(thread_id, include_turns=True).get(
                            "thread"
                        )
                        or {}
                    )
                except Exception as observation_exc:
                    observation_error = str(observation_exc)
                try:
                    observed_goal = codex.get_goal(thread_id)
                except Exception:
                    stored_goal = alias.get("goal")
                    goal = (
                        stored_goal
                        if isinstance(stored_goal, dict)
                        else None
                    )
                    goal_source = "alias"
                else:
                    goal = (
                        observed_goal
                        if isinstance(observed_goal, dict)
                        else None
                    )
                    goal_source = "thread_goal_get"
                runner = _runner_state(
                    alias,
                    goal,
                    thread=(
                        observed_thread
                        if observation_error is None
                        else None
                    ),
                    thread_observed=observation_error is None,
                    goal_source=goal_source,
                )
                active = runner["execution_active"]
                exc.retryable = active is False
                exc.details.update(
                    {
                        "lane_id": lane_id,
                        "codex_thread_id": thread_id,
                        **_turn_request_echo(alias),
                        "goal_status": (
                            goal.get("status")
                            if goal
                            else alias.get("goal_status")
                        ),
                        "goal_status_source": goal_source,
                        **_execution_fields(runner),
                        "recommended_action": (
                            "observe" if active is not False else "resume"
                        ),
                    }
                )
                if observation_error is not None:
                    exc.details["execution_observation_error"] = (
                        observation_error
                    )
        else:
            _mark_runner_stopped(
                alias,
                "unknown" if started else "failed",
                error=str(exc),
                error_code="TURN_FAILED",
            )
        save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)
        raise


def _resolve_runtime_value(
    value: Any,
    alias: dict[str, Any] | None,
    key: str,
) -> str | None:
    raw = value if value is not None else (alias or {}).get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _resolve_turn_request(
    args: argparse.Namespace,
    alias: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_model = getattr(args, "model", None)
    model = _resolve_runtime_value(raw_model, alias, "model")
    if raw_model is not None:
        model_source = "explicit"
    elif model is not None:
        model_source = "alias"
    else:
        model_source = "default-or-unset"

    raw_effort = getattr(args, "effort", None)
    if raw_effort is not None:
        effort = _validated_effort(raw_effort)
        effort_source = "explicit"
    else:
        try:
            configured = read_default_effort()
        except UserConfigError as exc:
            raise WorkspaceError(
                "USER_CONFIG_INVALID",
                str(exc),
                config_path=str(user_config_path()),
                retryable=False,
            ) from exc
        effort = configured["value"]
        effort_source = str(configured["source"])
    return {
        "requested_model": model,
        "requested_model_source": model_source,
        "requested_effort": effort,
        "requested_effort_source": effort_source,
        "effective_effort": effort,
        "effective_effort_source": effort_source,
    }


def _turn_request_echo(alias: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(alias, dict):
        return {
            "requested_model": None,
            "requested_model_source": "unknown",
            "requested_effort": None,
            "requested_effort_source": "unknown",
            "effective_effort": None,
            "effective_effort_source": "unknown",
        }

    model = (
        alias.get("requested_model")
        if "requested_model" in alias
        else _resolve_runtime_value(None, alias, "model")
    )
    model_source = alias.get("requested_model_source")
    effort = alias.get("requested_effort")
    effort_source = alias.get("requested_effort_source")
    effective_effort = alias.get("effective_effort", effort)
    effective_effort_source = alias.get("effective_effort_source")
    if not isinstance(effective_effort_source, str):
        if effort_source in {"explicit", "user_config", "user_config_legacy"}:
            effective_effort_source = effort_source
        elif effort_source == "default-or-unset" and effort is None:
            effective_effort_source = "unset"
        else:
            effective_effort_source = "unknown"
    known_sources = {
        "explicit",
        "alias",
        "default-or-unset",
        "unset",
        "user_config",
        "user_config_legacy",
        "unknown",
    }
    return {
        "requested_model": model,
        "requested_model_source": (
            model_source
            if isinstance(model_source, str) and model_source in known_sources
            else "unknown"
        ),
        "requested_effort": effort,
        "requested_effort_source": (
            effort_source
            if isinstance(effort_source, str) and effort_source in known_sources
            else "unknown"
        ),
        "effective_effort": effective_effort,
        "effective_effort_source": (
            effective_effort_source
            if effective_effort_source in known_sources
            else "unknown"
        ),
    }


def _normalize_goal_objective(value: Any) -> str | None:
    if value is None:
        return None
    objective = str(value).strip()
    if not objective:
        raise ValueError("--goal-objective requires a non-empty value")
    return objective


def _resolve_run_workspace(
    args: argparse.Namespace,
    existing: dict[str, Any] | None,
    alias_root: Path,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    cwd = _resolve_cwd(args.cwd, existing)
    requested = getattr(args, "worktree", None)
    stored = (existing or {}).get("workspace")
    workspace = dict(stored) if isinstance(stored, dict) else None
    if requested is None:
        return cwd, existing, workspace
    if str(requested).strip().casefold() != "auto":
        raise WorkspaceError(
            "GIT_WORKTREE_MODE_UNSUPPORTED",
            "--worktree currently supports only MODE=auto",
            requested=requested,
        )
    if existing is not None:
        if (
            workspace
            and workspace.get("managed_by") == "agent-lane"
            and workspace.get("status") == "active"
        ):
            return (
                _resolve_cwd(str(workspace.get("cwd") or ""), None),
                existing,
                workspace,
            )
        raise WorkspaceError(
            "GIT_WORKTREE_LANE_EXISTS",
            "refusing to attach a new managed worktree to an existing lane",
            lane_id=args.lane_id,
        )
    if not cwd:
        raise WorkspaceError(
            "GIT_WORKTREE_SOURCE_REQUIRED",
            "--cwd is required with --worktree when creating a lane",
        )

    workspace = create_managed_worktree(cwd, args.lane_id)
    cwd = str(workspace["cwd"])
    existing = {
        "lane_id": args.lane_id,
        "cwd": cwd,
        "workspace": workspace,
        "created_at": time.time(),
    }
    requested_custom_title = _nonempty_text(getattr(args, "title", None))
    if requested_custom_title is not None:
        existing["custom_title"] = requested_custom_title
    save_alias(CODEX_PROVIDER, args.lane_id, existing, alias_root)
    return cwd, existing, workspace


def _workspace_rebind_additional_context(
    alias: dict[str, Any],
    *,
    origin_thread_id: str,
    origin_cwd: str | None,
    execution_cwd: str,
) -> dict[str, dict[str, str]]:
    context = str(
        alias.get("last_completed_final_text")
        or alias.get("last_final_text")
        or alias.get("codex_preview")
        or ""
    ).strip()
    objective = str(alias.get("objective") or "").strip()
    lines = [
        (
            "Agent-lane replaced the underlying Codex task because the lane "
            "workspace binding changed explicitly."
        ),
        f"Original Codex thread: {origin_thread_id}",
        f"Previous cwd: {origin_cwd or '(unknown)'}",
        f"Effective cwd: {execution_cwd}",
        (
            "Continue the same business lane in the effective cwd. Treat its "
            "filesystem and Git state as authoritative, preserve existing work, "
            "and do not operate in sibling worktrees."
        ),
    ]
    if objective:
        lines.extend(["", f"Existing objective:\n{objective[:2000]}"])
    if context:
        lines.extend(["", f"Latest completed context:\n{context[:8000]}"])
    return {
        "agent_lane_workspace_rebind": {
            "kind": "application",
            "value": "\n".join(lines),
        }
    }


def _record_workspace_thread_replacement(
    alias: dict[str, Any],
    replacement: dict[str, Any],
) -> None:
    origin_thread_id = str(replacement["origin_thread_id"])
    execution_thread_id = str(replacement["execution_thread_id"])
    _advance_codex_binding(
        alias,
        origin_thread_id=origin_thread_id,
        execution_thread_id=execution_thread_id,
        reason="workspace_binding_changed",
    )
    for key in (
        "adopted_at",
        "adopted_cwd_refreshed_at",
        "adopted_from",
        "codex_preview",
        "codex_recency_at",
        "codex_session_path",
        "codex_title",
        "codex_title_binding_generation",
        "codex_title_observation",
        "codex_title_observed_at",
        "codex_title_thread_id",
        "thread_status",
    ):
        alias.pop(key, None)
    alias.update(
        {
            "origin_codex_thread_id": (
                alias.get("origin_codex_thread_id") or origin_thread_id
            ),
            "previous_codex_thread_id": origin_thread_id,
            "thread_replacement": {
                "reason": "workspace_binding_changed",
                "origin_thread_id": origin_thread_id,
                "execution_thread_id": execution_thread_id,
                "origin_cwd": replacement.get("origin_cwd"),
                "execution_cwd": replacement.get("execution_cwd"),
                "created_at": time.time(),
            },
        }
    )


def _validated_config_overrides(values: list[str] | None) -> list[str]:
    overrides: list[str] = []
    for raw in values or []:
        value = str(raw).strip()
        if "=" not in value:
            raise ValueError("--config requires KEY=VALUE")
        key = value.split("=", 1)[0].strip().casefold()
        if _is_sensitive_config_key(key):
            raise ValueError(
                f"refusing potentially sensitive --config key {key!r}; "
                "use Codex config or environment-based credential storage"
            )
        overrides.append(value)
    return overrides


def _is_sensitive_config_key(key: str) -> bool:
    segments = [
        segment.replace("-", "_")
        for segment in key.replace("[", ".").replace("]", ".").split(".")
        if segment
    ]
    sensitive_suffixes = tuple(f"_{part}" for part in SENSITIVE_CONFIG_KEY_PARTS)
    return any(
        segment in SENSITIVE_CONFIG_KEY_PARTS
        or segment.endswith(sensitive_suffixes)
        for segment in segments
    )


def _validated_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    try:
        return normalize_effort(effort, label="--effort")
    except UserConfigError as exc:
        raise ValueError(str(exc)) from exc


def _resolve_add_dirs(
    values: list[str] | None,
    alias: dict[str, Any] | None,
) -> list[str]:
    raw_values: Any = values if values is not None else (alias or {}).get("add_dirs")
    if raw_values is None:
        return []
    if isinstance(raw_values, str):
        raw_values = [raw_values]
    if not isinstance(raw_values, list):
        raise ValueError("add_dirs must be a list of directory paths")
    resolved: list[str] = []
    for raw in raw_values:
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"add-dir is not a directory: {path}")
        value = str(path)
        if value not in resolved:
            resolved.append(value)
    return resolved


def _runtime_workspace_roots(
    cwd: str | None,
    add_dirs: list[str],
) -> list[str] | None:
    if not add_dirs:
        return None
    roots: list[str] = []
    for value in [cwd, *add_dirs]:
        if value and value not in roots:
            roots.append(value)
    return roots


def _wait_for_lane(
    *,
    lane_id: str,
    alias_root: Path,
    timeout: float,
    poll_interval: float,
    emit: Any = None,
    expected_thread_id: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout, 0.0)
    started_at = time.monotonic()
    polls = 0
    last_signature: str | None = None
    latest: dict[str, Any] | None = None
    alias = _require_alias_identity(
        lane_id,
        alias_root,
        expected_thread_id=expected_thread_id,
    )
    with _codex_for_alias(alias) as codex:
        while True:
            latest = _lane_observation(
                codex,
                lane_id,
                alias_root,
                expected_thread_id=expected_thread_id,
            )
            polls += 1
            signature = json.dumps(
                {
                    "turn_id": latest.get("turn_id"),
                    "status": latest.get("status"),
                    "final_lead": latest.get("final_lead"),
                    "error": latest.get("observation_error"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if emit and signature != last_signature and not latest["terminal"]:
                emit(latest)
            last_signature = signature
            if latest["terminal"]:
                return {
                    "ok": latest["status"] == "completed",
                    **latest,
                    "polls": polls,
                    "waited_seconds": round(time.monotonic() - started_at, 3),
                }
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    **latest,
                    "error_code": "LANE_WAIT_TIMEOUT",
                    "error": f"lane wait timed out after {timeout}s",
                    "polls": polls,
                    "waited_seconds": round(time.monotonic() - started_at, 3),
                }
            time.sleep(max(poll_interval, 0.05))


def _wait_for_thread(
    *,
    thread_id: str,
    timeout: float,
    poll_interval: float,
    alias_root: Path,
    emit: Any = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout, 0.0)
    started_at = time.monotonic()
    polls = 0
    last_signature: str | None = None
    codex, transport = _open_read_only_codex("auto")
    with codex:
        while True:
            latest = _thread_observation(
                codex,
                thread_id,
                alias_root=alias_root,
            )
            latest.update(transport)
            polls += 1
            signature = json.dumps(
                {
                    "turn_id": latest.get("turn_id"),
                    "status": latest.get("status"),
                    "final_lead": latest.get("final_lead"),
                    "error": latest.get("observation_error"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if emit and signature != last_signature and not latest["terminal"]:
                emit(latest)
            last_signature = signature
            if latest["terminal"]:
                return {
                    "ok": latest["status"] == "completed",
                    **latest,
                    "polls": polls,
                    "waited_seconds": round(time.monotonic() - started_at, 3),
                }
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    **latest,
                    "error_code": "THREAD_WAIT_TIMEOUT",
                    "error": f"thread wait timed out after {timeout}s",
                    "polls": polls,
                    "waited_seconds": round(time.monotonic() - started_at, 3),
                }
            time.sleep(max(poll_interval, 0.05))


def _thread_observation(
    codex: CodexAppServer,
    thread_id: str,
    *,
    alias_root: Path,
) -> dict[str, Any]:
    thread: dict[str, Any] = {}
    observation_error: str | None = None
    try:
        thread = codex.read_thread(thread_id, include_turns=True).get("thread") or {}
    except Exception as exc:
        observation_error = str(exc)
    runner = _runner_state(
        {"codex_thread_id": thread_id},
        thread=thread if observation_error is None else None,
        thread_observed=observation_error is None,
    )
    turn = runner["last_turn"]
    status = str(runner["status"] or "unknown")
    terminal = (
        runner["execution"]["state"] == "inactive"
        and _is_stopped_runner_status(status)
    )
    result: dict[str, Any] = {
        "lane_id": None,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "turn_id": turn.get("turn_id"),
        "status": status,
        "terminal": terminal,
        **_execution_fields(runner),
        "last_user": turn.get("user_request"),
        "final_lead": turn.get("assistant_final_lead"),
        "final_text": turn.get("assistant_final_excerpt"),
        "observation_mode": "thread_read_poll",
        "confidence": "high" if observation_error is None else "low",
        "limitation": OBSERVATION_LIMITATION,
        "control": _control_contract(
            None,
            thread_id,
            thread=thread,
            attach_mode=(
                "app-sync"
                if getattr(codex, "transport", "stdio") == "daemon"
                else "independent"
            ),
            alias_root=alias_root,
        ),
    }
    if observation_error is not None:
        result["observation_error"] = observation_error
    return result


def _lane_observation(
    codex: CodexAppServer,
    lane_id: str,
    alias_root: Path,
    *,
    expected_thread_id: str | None = None,
) -> dict[str, Any]:
    alias = _require_alias_identity(
        lane_id,
        alias_root,
        expected_thread_id=expected_thread_id,
    )
    thread_id = str(alias["codex_thread_id"])
    expected_turn_id = alias.get("current_turn_id")
    if not expected_turn_id and alias.get("last_status") != "starting":
        expected_turn_id = alias.get("last_turn_id")
    thread: dict[str, Any] = {}
    observation_error: str | None = None
    try:
        thread = codex.read_thread(thread_id, include_turns=True).get("thread") or {}
    except Exception as exc:
        observation_error = str(exc)
    runner = _runner_state(
        alias,
        thread=thread if observation_error is None else None,
        thread_observed=observation_error is None,
    )
    alias_status = str(runner["status"] or "unknown")
    execution_active = bool(runner["execution_active"])
    turns = [item for item in (thread.get("turns") or []) if isinstance(item, dict)]
    selected: dict[str, Any] | None = None
    if expected_turn_id:
        selected = next(
            (item for item in turns if str(item.get("id")) == str(expected_turn_id)),
            None,
        )
    if selected is None and turns and alias.get("last_status") != "starting":
        selected = max(turns, key=_turn_sort_value)

    if not execution_active and alias_status in {"timed_out", "stale"}:
        turn = _last_turn_summary({"turns": [selected]}) if selected else _empty_last_turn()
        status = alias_status
        confidence = "high"
    elif selected:
        turn = _last_turn_summary({"turns": [selected]})
        observed_status = str(turn.get("status") or "unknown")
        exact = bool(expected_turn_id) and str(selected.get("id")) == str(
            expected_turn_id
        )
        if execution_active and alias_status in {"starting", "inProgress"}:
            status = alias_status
            confidence = "medium" if exact else "low"
        elif _is_terminal_turn_status(alias_status) and (
            not expected_turn_id
            or str(alias.get("last_turn_id")) == str(selected.get("id"))
        ):
            status = alias_status
            confidence = "high" if observed_status == alias_status else "medium"
        else:
            status = observed_status
            confidence = "high" if exact else "medium"
    else:
        turn = _empty_last_turn()
        status = alias_status
        confidence = "medium" if _is_stopped_runner_status(status) else "low"

    terminal = _is_stopped_runner_status(status)
    final_lead = None
    final_text = None
    if _is_terminal_turn_status(status):
        final_lead = turn.get("assistant_final_lead") or _first_paragraph(
            _clean_agent_text(str(alias.get("last_final_text") or ""))
        )
        final_text = turn.get("assistant_final_excerpt") or alias.get(
            "last_final_text"
        )
    result: dict[str, Any] = {
        "lane_id": lane_id,
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        "turn_id": turn.get("turn_id") or expected_turn_id,
        "status": status,
        "terminal": terminal,
        **_execution_fields(runner),
        "last_user": turn.get("user_request"),
        "final_lead": final_lead,
        "final_text": final_text,
        "observation_mode": "thread_read_poll",
        "confidence": confidence,
        "limitation": OBSERVATION_LIMITATION,
    }
    if observation_error:
        result["observation_error"] = observation_error
        result["confidence"] = "low"

    if (
        selected
        and not execution_active
        and alias_status not in {"timed_out", "stale"}
    ):
        alias["last_turn_id"] = turn.get("turn_id")
        alias["last_status"] = status
        alias["current_turn_id"] = None if terminal else turn.get("turn_id")
        if turn.get("assistant_final_excerpt"):
            alias["last_final_text"] = turn.get("assistant_final_excerpt")
            alias["last_completed_final_text"] = turn.get(
                "assistant_final_excerpt"
            )
        _runner_state(alias)
    return result



def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return str(args.prompt)
    path = Path(args.prompt_file).expanduser()
    return path.read_text(encoding="utf-8")


def _resolve_cwd(cwd_arg: str | None, state: dict[str, Any] | None) -> str | None:
    raw = cwd_arg or (state or {}).get("cwd")
    if not raw:
        return None
    path = Path(str(raw)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"cwd is not a directory: {path}")
    return str(path)


def _workspace_status(
    cwd: Any,
    workspace: Any,
) -> dict[str, Any]:
    resolved_cwd = str(cwd) if cwd else None
    metadata = workspace if isinstance(workspace, dict) else None
    git = _git_snapshot(resolved_cwd, include_details=False)
    return workspace_snapshot(
        resolved_cwd,
        metadata,
        branch=git.get("branch"),
        dirty=git.get("dirty"),
    )


def _sync_adopted_thread_cwd(
    alias: dict[str, Any],
    thread: dict[str, Any],
) -> None:
    if alias.get("adopted_from") != "codex-app":
        return
    if alias.get("workspace_binding_source") in {
        "explicit_attach",
        "recent_command",
    }:
        return
    raw_cwd = thread.get("cwd")
    if not raw_cwd:
        return
    path = Path(str(raw_cwd)).expanduser().resolve()
    if not path.is_dir():
        raise WorkspaceError(
            "CODEX_ADOPTED_CWD_MISSING",
            "adopted Codex thread points to a missing cwd",
            codex_thread_id=alias.get("codex_thread_id"),
            cwd=str(path),
        )
    changed = str(alias.get("cwd") or "") != str(path)
    alias["cwd"] = str(path)
    alias["workspace"] = _workspace_status(str(path), None)
    if changed:
        alias["adopted_cwd_refreshed_at"] = time.time()


def _refresh_adopted_alias_cwd(
    alias: dict[str, Any],
    lane_id: str,
    alias_root: Path,
) -> None:
    if alias.get("adopted_from") != "codex-app":
        return
    thread_id = alias.get("codex_thread_id")
    if not thread_id:
        return
    with _codex_for_alias(alias) as codex:
        result = codex.read_thread(str(thread_id), include_turns=False)
    _sync_adopted_thread_cwd(alias, result.get("thread") or {})
    save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)


def _resolve_sandbox(
    sandbox_arg: str | None,
    alias: dict[str, Any] | None,
) -> str | None:
    raw = sandbox_arg or (alias or {}).get("sandbox")
    if raw is None:
        return None
    return normalize_sandbox_mode(str(raw))


def _resolve_commit_signing(
    signing_arg: str | None,
    alias: dict[str, Any] | None,
) -> str:
    raw = signing_arg
    if raw is None:
        commit_signing = (alias or {}).get("commit_signing")
        if isinstance(commit_signing, dict):
            raw = commit_signing.get("mode")
    value = str(raw or "off").strip().casefold()
    if value not in COMMIT_SIGNING_MODES:
        choices = ", ".join(COMMIT_SIGNING_MODES)
        raise ValueError(f"unsupported commit signing mode {raw!r}; choose {choices}")
    return value


def _resolve_execution_mode(
    requested: str | None,
    alias: dict[str, Any] | None,
    *,
    source_when_defaulted: str = "default",
    allow_rebind: bool = False,
) -> tuple[str, str]:
    explicit = _nonempty_text(requested)
    if explicit is not None and explicit not in EXECUTION_MODES:
        raise WorkspaceError(
            "LANE_EXECUTION_MODE_INVALID",
            "unsupported lane execution mode",
            execution_mode=explicit,
            choices=list(EXECUTION_MODES),
            retryable=False,
        )
    try:
        stored = _stored_execution_mode(alias)
    except WorkspaceError:
        if explicit is not None and allow_rebind:
            return explicit, "explicit"
        raise
    if explicit is not None and stored is not None and explicit != stored:
        if allow_rebind:
            return explicit, "explicit"
        raise WorkspaceError(
            "LANE_EXECUTION_MODE_CONFLICT",
            "a lane execution mode cannot be changed in place",
            requested_mode=explicit,
            stored_mode=stored,
            recovery=(
                "re-attach the same task with codex session attach and the "
                "requested --mode"
            ),
            retryable=False,
        )
    if explicit is not None:
        return explicit, "explicit"
    if stored is not None:
        return stored, "binding"
    return "independent", source_when_defaulted


def _stored_execution_mode(alias: dict[str, Any] | None) -> str | None:
    if not isinstance(alias, dict):
        return None
    binding = alias.get("binding")
    candidates: list[tuple[str, Any]] = []
    if isinstance(binding, dict) and "execution_mode" in binding:
        candidates.append(("binding", binding.get("execution_mode")))
    if "execution_mode" in alias:
        candidates.append(("alias", alias.get("execution_mode")))
    if not candidates:
        return None

    observed: list[tuple[str, str]] = []
    for source, candidate in candidates:
        value = _nonempty_text(candidate)
        if value not in EXECUTION_MODES:
            raise WorkspaceError(
                "LANE_EXECUTION_MODE_INVALID",
                "the stored lane execution mode is invalid",
                stored_mode=candidate,
                stored_mode_source=source,
                choices=list(EXECUTION_MODES),
                recovery=(
                    "repair or reattach the lane binding before executing it"
                ),
                retryable=False,
            )
        observed.append((source, value))

    distinct = {value for _source, value in observed}
    if len(distinct) != 1:
        raise WorkspaceError(
            "LANE_EXECUTION_MODE_INVALID",
            "the stored lane execution mode fields disagree",
            stored_modes={source: value for source, value in observed},
            recovery="repair or reattach the lane binding before executing it",
            retryable=False,
        )
    return observed[0][1]


def _record_execution_mode(
    alias: dict[str, Any],
    *,
    mode: str,
    source: str,
) -> None:
    alias["execution_mode"] = mode
    alias["execution_mode_source"] = source
    binding = alias.get("binding")
    if isinstance(binding, dict):
        binding["execution_mode"] = mode
        binding["execution_mode_source"] = source


def _transport_for_mode(mode: str) -> str:
    return "daemon" if mode == "app-sync" else "stdio"


def _codex_for_alias(
    alias: dict[str, Any] | None,
    **kwargs: Any,
) -> CodexAppServer:
    mode, _source = _resolve_execution_mode(None, alias)
    return CodexAppServer(transport=_transport_for_mode(mode), **kwargs)


def _prepare_commit_signing(mode: str) -> dict[str, Any]:
    if mode == "off":
        return {
            "metadata": signing_metadata(mode="off"),
            "extra_env": {},
            "config_overrides": [],
        }
    prepared = prepare_agent_signing()
    return {
        "metadata": prepared.metadata,
        "extra_env": prepared.extra_env,
        "config_overrides": prepared.config_overrides,
    }


def _codex_thread_signing_probe(
    codex: CodexAppServer,
    thread_id: str,
) -> dict[str, Any]:
    probe = thread_signing_probe()
    receipt_temp_path = Path(f"{probe.receipt_path}.tmp")
    try:
        try:
            result = codex.run_thread_shell_command(
                thread_id,
                probe.command,
                timeout=30.0,
                success_receipt=(probe.receipt_path, probe.marker),
            )
        except TimeoutError as exc:
            raise WorkspaceError(
                "CODEX_AGENT_SIGNING_PROBE_TIMEOUT",
                "Codex thread shell did not produce an observable signing "
                "probe completion",
                codex_thread_id=thread_id,
                retryable=True,
            ) from exc
        try:
            codex.wait_thread_idle(thread_id, timeout=5.0)
        except TimeoutError as exc:
            raise WorkspaceError(
                "CODEX_AGENT_SIGNING_PROBE_NOT_QUIESCENT",
                "Codex thread shell produced signing evidence but its turn did "
                "not finish before the next user turn",
                codex_thread_id=thread_id,
                retryable=True,
            ) from exc
        receipt_observed = bool(
            getattr(result, "receipt_observed", False)
        )
        return {
            "ok": (
                result.status == "completed"
                and result.exit_code == 0
                and probe.marker in result.output
            ),
            "status": result.status,
            "exit_code": result.exit_code,
            "turn_id": result.turn_id,
            "item_id": result.item_id,
            "output_tail": result.output[-1000:],
            "verification": (
                "thread_shell_probe_receipt_idle"
                if receipt_observed
                else "thread_shell_probe_idle"
            ),
        }
    finally:
        probe.receipt_path.unlink(missing_ok=True)
        receipt_temp_path.unlink(missing_ok=True)


def _mark_commit_signing_effective(
    commit_signing: dict[str, Any],
    thread_id: str,
    receipt: dict[str, Any],
) -> None:
    metadata = dict(commit_signing["metadata"])
    metadata.update(
        {
            "effective": True,
            "effective_thread_id": thread_id,
            "verification": receipt.get(
                "verification",
                "thread_shell_probe_idle",
            ),
            "verified_at": time.time(),
            "verification_turn_id": receipt.get("turn_id"),
        }
    )
    commit_signing["metadata"] = metadata


def _require_codex_thread_signing(
    codex: CodexAppServer,
    *,
    thread_id: str,
    commit_signing: dict[str, Any],
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = receipt or _codex_thread_signing_probe(codex, thread_id)
    if not receipt["ok"]:
        raise WorkspaceError(
            "CODEX_AGENT_SIGNING_ENV_UNAVAILABLE",
            "Codex thread shell did not receive the agent-lane signing environment",
            codex_thread_id=thread_id,
            signing_probe={
                "status": receipt.get("status"),
                "exit_code": receipt.get("exit_code"),
                "output_tail": receipt.get("output_tail"),
            },
            retryable=False,
        )
    _mark_commit_signing_effective(commit_signing, thread_id, receipt)
    return receipt


def _replacement_additional_context(
    alias: dict[str, Any],
    *,
    origin_thread_id: str,
    objective_override: str | None = None,
) -> dict[str, dict[str, str]]:
    context = str(
        alias.get("last_completed_final_text")
        or alias.get("last_final_text")
        or alias.get("codex_preview")
        or ""
    ).strip()
    objective = (
        str(alias.get("objective") or "").strip()
        if objective_override is None
        else objective_override
    )
    lines = [
        (
            "Agent-lane moved execution from an already-loaded Codex task "
            "because that task could not receive the managed commit-signing "
            "environment."
        ),
        f"Original Codex thread: {origin_thread_id}",
        (
            "Continue in the unchanged repository cwd. Treat current filesystem "
            "and Git state as authoritative, preserve existing work, and do not "
            "repeat completed expensive checks unless current evidence requires it."
        ),
    ]
    if objective:
        lines.extend(["", f"Existing objective:\n{objective[:2000]}"])
    if context:
        lines.extend(["", f"Latest completed context:\n{context[:8000]}"])
    return {
        "agent_lane_signing_handoff": {
            "kind": "application",
            "value": "\n".join(lines),
        }
    }


def _clear_replaced_thread_cache(alias: dict[str, Any]) -> None:
    for key in (
        "adopted_at",
        "adopted_cwd_refreshed_at",
        "adopted_from",
        "codex_preview",
        "codex_recency_at",
        "codex_session_path",
        "codex_title",
        "codex_title_binding_generation",
        "codex_title_observation",
        "codex_title_observed_at",
        "codex_title_thread_id",
        "thread_status",
    ):
        alias.pop(key, None)


def _replacement_goal_plan(
    *,
    origin_goal: dict[str, Any] | None,
    origin_thread_id: str,
    objective_override: str | None,
) -> dict[str, Any] | None:
    if objective_override is not None:
        objective = objective_override
        token_budget = None
    else:
        goal = origin_goal or {}
        status = str(goal.get("status") or "")
        if status != "active":
            return None
        objective = str(goal.get("objective") or "").strip()
        raw_budget = goal.get("tokenBudget")
        raw_used = goal.get("tokensUsed")
        if raw_budget is None:
            token_budget = None
        elif (
            not isinstance(raw_budget, int)
            or isinstance(raw_budget, bool)
            or raw_budget <= 0
            or (
                raw_used is not None
                and (
                    not isinstance(raw_used, int)
                    or isinstance(raw_used, bool)
                    or raw_used < 0
                )
            )
        ):
            raise WorkspaceError(
                "CODEX_SIGNING_REPLACEMENT_GOAL_BUDGET_INVALID",
                "the original task has invalid active-goal token usage",
                codex_thread_id=origin_thread_id,
                token_budget=raw_budget,
                tokens_used=raw_used,
                retryable=False,
            )
        else:
            token_budget = raw_budget - int(raw_used or 0)
            if token_budget <= 0:
                raise WorkspaceError(
                    "CODEX_SIGNING_REPLACEMENT_GOAL_BUDGET_EXHAUSTED",
                    "the original task has no remaining active-goal token budget",
                    codex_thread_id=origin_thread_id,
                    token_budget=raw_budget,
                    tokens_used=raw_used or 0,
                    retryable=False,
                )
    if not objective:
        raise WorkspaceError(
            "CODEX_SIGNING_REPLACEMENT_GOAL_INVALID",
            "the original task has an active goal without an objective",
            codex_thread_id=origin_thread_id,
            retryable=False,
        )
    return {
        "objective": objective,
        "token_budget": token_budget,
    }


def _signing_replacement_titles(title: str) -> tuple[str, str]:
    suffix = " [agent-lane]"
    logical_title = str(title).strip()
    while logical_title.endswith(suffix):
        logical_title = logical_title[: -len(suffix)].rstrip()
    if not logical_title:
        logical_title = "Codex task"
    return logical_title, f"{logical_title}{suffix}"


def _replace_thread_for_agent_signing(
    codex: CodexAppServer,
    *,
    alias: dict[str, Any],
    origin_thread_id: str,
    cwd: str,
    title: str,
    sandbox: str | None,
    model: str | None,
    workspace_roots: list[str] | None,
    commit_signing: dict[str, Any],
    allow_signing_replacement: bool,
    replacement_goal_objective: str | None,
    replacement_origin_goal: dict[str, Any] | None,
    lane_id: str,
    alias_root: Path,
) -> dict[str, Any]:
    _, replacement_title = _signing_replacement_titles(title)
    if not allow_signing_replacement:
        raise WorkspaceError(
            "CODEX_SIGNING_REPLACEMENT_AUTHORIZATION_REQUIRED",
            "creating a new App-visible Codex task for managed commit signing "
            "requires explicit user authorization",
            codex_thread_id=origin_thread_id,
            cwd=cwd,
            replacement_title=replacement_title,
            required_option="--allow-signing-replacement",
            authorization_scope="single_command",
            original_task_preserved=True,
            lane_rebound_after_verification=True,
            side_effects={
                "creates_app_visible_task": True,
                "keeps_original_task_visible": True,
                "rebinds_lane_after_verification": True,
                "copies_live_active_goal_to_replacement": True,
                "keeps_origin_goal_unchanged": True,
                "uses_bounded_context_handoff": True,
                "adds_signing_shell_turn": True,
            },
            retryable=False,
        )
    origin_goal = (
        None
        if replacement_goal_objective is not None
        else (
            replacement_origin_goal
            if replacement_origin_goal is not None
            else codex.get_goal(origin_thread_id)
        )
    )
    goal_plan = _replacement_goal_plan(
        origin_goal=origin_goal,
        origin_thread_id=origin_thread_id,
        objective_override=replacement_goal_objective,
    )
    additional_context = _replacement_additional_context(
        alias,
        origin_thread_id=origin_thread_id,
        objective_override=(
            str(goal_plan["objective"]) if goal_plan is not None else ""
        ),
    )
    thread_id = codex.start_thread(
        cwd,
        sandbox=sandbox,
        model=model,
        runtime_workspace_roots=workspace_roots,
    )
    replacement_goal: dict[str, Any] | None = None
    original_alias = dict(alias)
    try:
        codex.set_thread_name(thread_id, replacement_title)
        codex.update_git_info(thread_id, _git_info(cwd))
        _require_codex_thread_signing(
            codex,
            thread_id=thread_id,
            commit_signing=commit_signing,
        )
        if goal_plan is not None:
            goal_result = codex.set_goal(
                thread_id,
                objective=str(goal_plan["objective"]),
                status="active",
                token_budget=goal_plan["token_budget"],
            )
            candidate = goal_result.get("goal")
            if not isinstance(candidate, dict):
                raise WorkspaceError(
                    "CODEX_SIGNING_REPLACEMENT_GOAL_COPY_FAILED",
                    "Codex did not confirm the goal on the replacement task",
                    codex_thread_id=origin_thread_id,
                    replacement_thread_id=thread_id,
                    retryable=True,
                )
            replacement_goal = candidate

        _advance_codex_binding(
            alias,
            origin_thread_id=origin_thread_id,
            execution_thread_id=thread_id,
            reason="loaded_thread_resume_config_not_effective",
        )
        _clear_replaced_thread_cache(alias)
        alias.update(
            {
                "codex_thread_id": thread_id,
                "codex_url": f"codex://threads/{thread_id}",
                "commit_signing": commit_signing["metadata"],
                "origin_codex_thread_id": (
                    alias.get("origin_codex_thread_id") or origin_thread_id
                ),
                "previous_codex_thread_id": origin_thread_id,
                "thread_replacement": {
                    "reason": "loaded_thread_resume_config_not_effective",
                    "origin_thread_id": origin_thread_id,
                    "execution_thread_id": thread_id,
                    "created_at": time.time(),
                },
            }
        )
        _record_written_codex_title(
            alias,
            thread_id=thread_id,
            title=replacement_title,
        )
        if replacement_goal is not None:
            _update_goal_alias(alias, replacement_goal)
            alias["mode"] = "goal"
        else:
            _update_goal_alias(alias, None)
        try:
            save_alias(CODEX_PROVIDER, lane_id, alias, alias_root)
        except Exception as exc:
            raise WorkspaceError(
                "CODEX_SIGNING_REPLACEMENT_ALIAS_SAVE_FAILED",
                "the verified replacement task could not be durably bound "
                "to its lane",
                lane_id=lane_id,
                alias_path=str(
                    alias_path(CODEX_PROVIDER, lane_id, alias_root)
                ),
                retryable=True,
            ) from exc
    except Exception as exc:
        alias.clear()
        alias.update(original_alias)
        cleanup_error: Exception | None = None
        try:
            codex.archive_thread(thread_id)
        except Exception as archive_exc:
            cleanup_error = archive_exc
        if isinstance(exc, (WorkspaceError, CodexRpcError)):
            exc.details.update(
                {
                    "replacement_thread_id": thread_id,
                    "replacement_thread_archived": cleanup_error is None,
                }
            )
            if cleanup_error is not None:
                exc.details["replacement_cleanup_error"] = str(cleanup_error)
            raise
        details: dict[str, Any] = {
            "replacement_thread_id": thread_id,
            "replacement_thread_archived": cleanup_error is None,
            "replacement_error": str(exc),
            "retryable": False,
        }
        if cleanup_error is not None:
            details["replacement_cleanup_error"] = str(cleanup_error)
        raise WorkspaceError(
            "CODEX_SIGNING_REPLACEMENT_SETUP_FAILED",
            "Codex signing replacement setup failed",
            **details,
        ) from exc
    return {
        "thread_id": thread_id,
        "replaced": True,
        "reason": "loaded_thread_resume_config_not_effective",
        "origin_thread_id": origin_thread_id,
        "codex_title": replacement_title,
        "additional_context": additional_context,
        "goal": replacement_goal,
    }


def _prepare_existing_thread_for_turn(
    codex: CodexAppServer,
    *,
    alias: dict[str, Any],
    thread_id: str,
    cwd: str,
    title: str,
    sandbox: str | None,
    model: str | None,
    workspace_roots: list[str] | None,
    commit_signing_mode: str,
    commit_signing: dict[str, Any],
    allow_signing_replacement: bool,
    replacement_goal_objective: str | None,
    replacement_origin_goal: dict[str, Any] | None,
    lane_id: str,
    alias_root: Path,
) -> dict[str, Any]:
    if (
        getattr(codex, "transport", "stdio") != "daemon"
        or commit_signing_mode != "agent"
    ):
        codex.resume_thread(
            thread_id,
            cwd=cwd,
            sandbox=sandbox,
            model=model,
            runtime_workspace_roots=workspace_roots,
        )
        return {
            "thread_id": thread_id,
            "replaced": False,
            "origin_thread_id": None,
            "additional_context": None,
        }

    loaded = thread_id in codex.list_loaded_thread_ids()
    if not loaded:
        codex.resume_thread(
            thread_id,
            cwd=cwd,
            sandbox=sandbox,
            model=model,
            runtime_workspace_roots=workspace_roots,
        )
        receipt = _codex_thread_signing_probe(codex, thread_id)
        if not receipt["ok"]:
            return _replace_thread_for_agent_signing(
                codex,
                alias=alias,
                origin_thread_id=thread_id,
                cwd=cwd,
                title=title,
                sandbox=sandbox,
                model=model,
                workspace_roots=workspace_roots,
                commit_signing=commit_signing,
                allow_signing_replacement=allow_signing_replacement,
                replacement_goal_objective=replacement_goal_objective,
                replacement_origin_goal=replacement_origin_goal,
                lane_id=lane_id,
                alias_root=alias_root,
            )
        _require_codex_thread_signing(
            codex,
            thread_id=thread_id,
            commit_signing=commit_signing,
            receipt=receipt,
        )
        return {
            "thread_id": thread_id,
            "replaced": False,
            "origin_thread_id": None,
            "additional_context": None,
        }

    if alias.get("adopted_from") != "codex-app":
        receipt = _codex_thread_signing_probe(codex, thread_id)
        if receipt["ok"]:
            _mark_commit_signing_effective(
                commit_signing,
                thread_id,
                receipt,
            )
            codex.resume_thread(
                thread_id,
                apply_config=False,
            )
            return {
                "thread_id": thread_id,
                "replaced": False,
                "origin_thread_id": None,
                "additional_context": None,
            }

    return _replace_thread_for_agent_signing(
        codex,
        alias=alias,
        origin_thread_id=thread_id,
        cwd=cwd,
        title=title,
        sandbox=sandbox,
        model=model,
        workspace_roots=workspace_roots,
        commit_signing=commit_signing,
        allow_signing_replacement=allow_signing_replacement,
        replacement_goal_objective=replacement_goal_objective,
        replacement_origin_goal=replacement_origin_goal,
        lane_id=lane_id,
        alias_root=alias_root,
    )


def _require_alias(lane_id: str, alias_root: Path) -> dict[str, Any]:
    alias = load_alias(CODEX_PROVIDER, lane_id, alias_root)
    if not alias or not alias.get("codex_thread_id"):
        raise WorkspaceError(
            "LANE_ALIAS_NOT_FOUND",
            "lane-id is not attached to a Codex task",
            lane_id=lane_id,
            control_requires_explicit_attach=True,
            recovery={
                "discover_argv": ["codex", "session", "list", "--scope", "all"],
                "attach_argv": [
                    "codex",
                    "session",
                    "attach",
                    "--lane-id",
                    lane_id,
                    "--thread-id",
                    "<thread-id>",
                    "--mode",
                    "independent",
                ],
            },
            retryable=False,
        )
    return _project_codex_alias(alias)


def _require_alias_identity(
    lane_id: str,
    alias_root: Path,
    *,
    expected_thread_id: str | None,
) -> dict[str, Any]:
    alias = _require_alias(lane_id, alias_root)
    observed_thread_id = _nonempty_text(alias.get("codex_thread_id"))
    if (
        expected_thread_id is not None
        and observed_thread_id != expected_thread_id
    ):
        raise WorkspaceError(
            "CODEX_TARGET_CHANGED",
            "the resolved task binding changed before the command began",
            lane_id=lane_id,
            expected_thread_id=expected_thread_id,
            observed_thread_id=observed_thread_id,
            retryable=False,
        )
    return alias


def _require_command_alias(
    args: argparse.Namespace,
    alias_root: Path,
) -> dict[str, Any]:
    return _require_alias_identity(
        str(args.lane_id),
        alias_root,
        expected_thread_id=_nonempty_text(
            getattr(args, "_target_expected_thread_id", None)
        ),
    )


def _nonempty_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _project_codex_alias(alias: dict[str, Any]) -> dict[str, Any]:
    projected = dict(alias)
    for field in REMOVED_CODEX_TITLE_FIELDS:
        projected.pop(field, None)
    return projected


def _custom_title(alias: dict[str, Any]) -> str | None:
    return _nonempty_text(alias.get("custom_title"))


def _stored_codex_title(alias: dict[str, Any]) -> str | None:
    return _nonempty_text(alias.get("codex_title"))


def _new_codex_title(
    *,
    requested_title: str | None,
    cwd: str | None,
    lane_id: str,
) -> str:
    explicit = _nonempty_text(requested_title)
    if explicit is not None:
        return explicit
    if cwd:
        cwd_name = Path(cwd).expanduser().name
        if cwd_name:
            return cwd_name
    return lane_id


def _title_contract(
    alias: dict[str, Any],
    *,
    thread: dict[str, Any] | None = None,
    lane_id: str | None = None,
) -> dict[str, Any]:
    live_name_present = isinstance(thread, dict) and "name" in thread
    raw_codex_title = (
        thread.get("name")
        if live_name_present and thread is not None
        else alias.get("codex_title")
    )
    codex_title = _nonempty_text(raw_codex_title)
    custom_title = _custom_title(alias)
    resolved_lane_id = _nonempty_text(lane_id or alias.get("lane_id"))
    if custom_title is not None:
        lane_title = custom_title
        lane_title_source = "custom_title"
    elif codex_title is not None:
        lane_title = codex_title
        lane_title_source = "codex_title"
    else:
        lane_title = resolved_lane_id
        lane_title_source = "lane_id"
    if live_name_present:
        observation = "live"
    elif "codex_title" in alias:
        observation = "cached"
    else:
        observation = "unknown"
    return {
        "lane_title": lane_title,
        "lane_title_source": lane_title_source,
        "codex_title": codex_title,
        "codex_title_observation": observation,
        "codex_title_observed_at": alias.get("codex_title_observed_at"),
        "custom_title": custom_title,
    }


def _binding_contract(alias: dict[str, Any]) -> dict[str, Any]:
    binding = alias.get("binding")
    if not isinstance(binding, dict):
        return {
            "binding_generation": None,
            "binding_origin": None,
            "lineage_complete": alias.get("lineage_complete"),
            "execution_mode": _stored_execution_mode(alias),
            "execution_mode_source": alias.get("execution_mode_source"),
        }
    return {
        "binding_generation": binding.get("generation"),
        "binding_origin": binding.get("origin"),
        "lineage_complete": alias.get("lineage_complete"),
        "execution_mode": _stored_execution_mode(alias),
        "execution_mode_source": (
            binding.get("execution_mode_source")
            or alias.get("execution_mode_source")
        ),
    }


def _initialize_codex_binding(
    alias: dict[str, Any],
    *,
    thread_id: str,
    origin: str,
    bound_at: float | None = None,
    lineage_complete: bool = True,
) -> None:
    now = bound_at if bound_at is not None else time.time()
    alias["schema_version"] = CODEX_ALIAS_SCHEMA_VERSION
    alias["codex_thread_id"] = thread_id
    alias["binding"] = {
        "generation": 1,
        "thread_id": thread_id,
        "bound_at": now,
        "origin": origin,
    }
    alias["binding_history"] = []
    alias["lineage_complete"] = bool(lineage_complete)


def _advance_codex_binding(
    alias: dict[str, Any],
    *,
    origin_thread_id: str,
    execution_thread_id: str,
    reason: str,
    origin: str = "replacement",
    transitioned_at: float | None = None,
) -> None:
    _prepare_codex_alias_for_save(alias, lane_id=str(alias.get("lane_id") or ""))
    binding = alias.get("binding")
    if not isinstance(binding, dict):
        raise WorkspaceError(
            "LANE_BINDING_MISSING",
            "lane alias does not contain a current Codex binding",
            lane_id=alias.get("lane_id"),
            retryable=False,
        )
    current_thread_id = str(binding.get("thread_id") or "")
    if current_thread_id != origin_thread_id:
        raise WorkspaceError(
            "LANE_BINDING_CHANGED",
            "lane binding changed before the replacement could be committed",
            lane_id=alias.get("lane_id"),
            expected_thread_id=origin_thread_id,
            observed_thread_id=current_thread_id or None,
            retryable=False,
        )
    now = transitioned_at if transitioned_at is not None else time.time()
    displaced = dict(binding)
    displaced.update({"unbound_at": now, "unbound_reason": reason})
    history = alias.get("binding_history")
    if not isinstance(history, list):
        history = []
    history = [*history, displaced]
    generation = int(binding.get("generation") or 0) + 1
    alias["binding_history"] = history
    alias["binding"] = {
        "generation": generation,
        "thread_id": execution_thread_id,
        "bound_at": now,
        "origin": origin,
        "predecessor_thread_id": origin_thread_id,
        "transition_reason": reason,
        "execution_mode": (
            binding.get("execution_mode")
            or alias.get("execution_mode")
            or "independent"
        ),
        "execution_mode_source": (
            binding.get("execution_mode_source")
            or alias.get("execution_mode_source")
            or "legacy-default"
        ),
    }
    alias["codex_thread_id"] = execution_thread_id


def _prepare_codex_alias_for_save(alias: dict[str, Any], *, lane_id: str) -> None:
    alias["schema_version"] = CODEX_ALIAS_SCHEMA_VERSION
    for field in REMOVED_CODEX_TITLE_FIELDS:
        alias.pop(field, None)
    custom_title = _custom_title(alias)
    if custom_title is None:
        alias.pop("custom_title", None)
    else:
        alias["custom_title"] = custom_title
    thread_id = _nonempty_text(alias.get("codex_thread_id"))
    if thread_id is None:
        return
    binding = alias.get("binding")
    if not isinstance(binding, dict):
        _initialize_codex_binding(
            alias,
            thread_id=thread_id,
            origin="legacy",
            bound_at=float(alias.get("created_at") or time.time()),
            lineage_complete=False,
        )
        _record_execution_mode(
            alias,
            mode=_stored_execution_mode(alias) or "independent",
            source=str(alias.get("execution_mode_source") or "legacy-default"),
        )
        return
    binding_thread_id = _nonempty_text(binding.get("thread_id"))
    if binding_thread_id != thread_id:
        raise WorkspaceError(
            "LANE_BINDING_INTEGRITY_ERROR",
            "top-level Codex thread id does not match the canonical binding",
            lane_id=lane_id,
            codex_thread_id=thread_id,
            binding_thread_id=binding_thread_id,
            retryable=False,
        )
    generation = binding.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise WorkspaceError(
            "LANE_BINDING_INTEGRITY_ERROR",
            "binding generation must be a positive integer",
            lane_id=lane_id,
            binding_generation=generation,
            retryable=False,
        )
    _record_execution_mode(
        alias,
        mode=_stored_execution_mode(alias) or "independent",
        source=str(alias.get("execution_mode_source") or "legacy-default"),
    )
    history = alias.get("binding_history")
    if history is None:
        alias["binding_history"] = []
    elif not isinstance(history, list):
        raise WorkspaceError(
            "LANE_BINDING_INTEGRITY_ERROR",
            "binding history must be an array",
            lane_id=lane_id,
            retryable=False,
        )


def _update_turn_alias(
    alias: dict[str, Any],
    lane_id: str,
    thread_id: str,
    cwd: str | None,
    sandbox: str | None,
    turn: Any,
) -> None:
    _preserve_legacy_completed_final(alias)
    alias.update(
        {
            "lane_id": lane_id,
            "codex_thread_id": thread_id,
            "codex_url": f"codex://threads/{thread_id}",
            "cwd": cwd,
            "sandbox": sandbox,
            "current_turn_id": None,
            "last_turn_id": turn.turn_id,
            "last_status": turn.status,
            "last_final_text": turn.final_text,
            "last_events": turn.events[-20:],
            "runner_alive": False,
            "needs_resume": turn.status != "completed",
            "created_at": alias.get("created_at") or time.time(),
        }
    )
    if turn.status == "completed":
        alias["last_completed_final_text"] = turn.final_text or None
    _clear_runner_tracking(alias)
    alias.pop("last_error", None)
    alias.pop("last_error_code", None)
    alias.pop("timed_out_at", None)
    alias.pop("stale_detected_at", None)


def _update_goal_alias(alias: dict[str, Any], goal: dict[str, Any] | None) -> None:
    if goal:
        alias["goal"] = goal
        alias["objective"] = goal.get("objective")
        alias["goal_status"] = goal.get("status")
        alias["goal_tokens_used"] = goal.get("tokensUsed")
        alias["goal_token_budget"] = goal.get("tokenBudget")
        alias["goal_time_used_seconds"] = goal.get("timeUsedSeconds")
    else:
        alias.pop("goal", None)
        alias.pop("objective", None)
        alias.pop("goal_status", None)
        alias.pop("goal_tokens_used", None)
        alias.pop("goal_token_budget", None)
        alias.pop("goal_time_used_seconds", None)


def _update_thread_alias(alias: dict[str, Any], thread: dict[str, Any]) -> None:
    if "name" in thread:
        alias["codex_title"] = _nonempty_text(thread.get("name"))
        alias["codex_title_observed_at"] = time.time()
        alias["codex_title_observation"] = "live"
        observed_thread_id = _nonempty_text(thread.get("id")) or _nonempty_text(
            alias.get("codex_thread_id")
        )
        if observed_thread_id is not None:
            alias["codex_title_thread_id"] = observed_thread_id
        binding = alias.get("binding")
        if isinstance(binding, dict) and observed_thread_id == _nonempty_text(
            binding.get("thread_id")
        ):
            alias["codex_title_binding_generation"] = binding.get("generation")
    if thread.get("preview") is not None:
        alias["codex_preview"] = thread.get("preview")
    if thread.get("recencyAt") is not None:
        alias["codex_recency_at"] = thread.get("recencyAt")
    if thread.get("status") is not None:
        alias["thread_status"] = thread.get("status")
    if thread.get("path") is not None:
        alias["codex_session_path"] = thread.get("path")


def _record_written_codex_title(
    alias: dict[str, Any],
    *,
    thread_id: str,
    title: str,
) -> None:
    alias["codex_title"] = _nonempty_text(title)
    alias["codex_title_observed_at"] = time.time()
    alias["codex_title_observation"] = "write_ack"
    alias["codex_title_thread_id"] = thread_id
    binding = alias.get("binding")
    if isinstance(binding, dict) and thread_id == _nonempty_text(
        binding.get("thread_id")
    ):
        alias["codex_title_binding_generation"] = binding.get("generation")


def _sorted_aliases(alias_root: Path) -> list[dict[str, Any]]:
    return _sort_aliases(
        [
            _project_codex_alias(alias)
            for alias in list_aliases(CODEX_PROVIDER, alias_root)
        ]
    )


def _sort_aliases(aliases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        aliases,
        key=lambda item: float(
            item.get("codex_recency_at")
            or item.get("updated_at")
            or item.get("created_at")
            or 0
        ),
        reverse=True,
    )


def _alias_by_thread_id(alias_root: Path) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for raw in list_aliases(CODEX_PROVIDER, alias_root):
        alias = _project_codex_alias(raw)
        thread_id = alias.get("codex_thread_id")
        if thread_id:
            aliases[str(thread_id)] = alias
    return aliases


def _refresh_aliases_from_codex(
    aliases: list[dict[str, Any]],
    alias_root: Path,
    *,
    observe: str = "auto",
    transport_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    refreshed: list[dict[str, Any]] = []
    codex, transport = _open_read_only_codex(observe)
    if transport_out is not None:
        transport_out.update(transport)
    with codex:
        for alias in aliases:
            item = _project_codex_alias(alias)
            observed_thread: dict[str, Any] | None = None
            thread_id = item.get("codex_thread_id")
            if not thread_id:
                item["_session_observation"] = {
                    "transport": transport,
                    "thread": None,
                }
                refreshed.append(item)
                continue
            try:
                thread = codex.read_thread(str(thread_id), include_turns=False)
                candidate = thread.get("thread")
                if isinstance(candidate, dict):
                    observed_thread = candidate
                lane_id = str(item.get("lane_id") or "")
                with operation_lock(alias_root, lane_id):
                    current = load_alias(CODEX_PROVIDER, lane_id, alias_root)
                    item = _project_codex_alias(current or item)
                    observed_thread_id = _nonempty_text(
                        item.get("codex_thread_id")
                    )
                    if observed_thread_id != str(thread_id):
                        raise WorkspaceError(
                            "CODEX_TARGET_CHANGED",
                            "the task binding changed while refreshing the lane",
                            lane_id=lane_id,
                            expected_thread_id=str(thread_id),
                            observed_thread_id=observed_thread_id,
                            retryable=False,
                        )
                    _sync_adopted_thread_cwd(item, observed_thread or {})
                    _update_thread_alias(item, observed_thread or {})
                    save_alias(CODEX_PROVIDER, lane_id, item, alias_root)
            except Exception as exc:
                observed_thread = None
                item["refresh_error"] = str(exc)
            item["_session_observation"] = {
                "transport": transport,
                "thread": (
                    {"status": observed_thread.get("status")}
                    if observed_thread is not None
                    else None
                ),
            }
            refreshed.append(item)
    return refreshed


def _alias_summary(
    item: dict[str, Any],
    *,
    alias_root: Path,
) -> dict[str, Any]:
    stored_goal = item.get("goal")
    goal = stored_goal if isinstance(stored_goal, dict) else None
    observation = item.get("_session_observation")
    transport = (
        observation.get("transport")
        if isinstance(observation, dict)
        and isinstance(observation.get("transport"), dict)
        else None
    )
    observed_thread = (
        observation.get("thread") if isinstance(observation, dict) else None
    )
    runner = _runner_state(
        dict(item),
        goal,
        thread=observed_thread if isinstance(observed_thread, dict) else None,
        thread_authoritative=(
            bool(transport.get("live_status_authoritative"))
            and isinstance(observed_thread, dict)
            if transport is not None
            else True
        ),
        observed_at=transport.get("observed_at") if transport is not None else None,
        observation_mode=(
            str(transport.get("observation_mode") or "unknown")
            if transport is not None
            else None
        ),
    )
    return {
        "kind": "lane_alias",
        "aliased": True,
        "lane_id": item.get("lane_id"),
        **_title_contract(item),
        **_binding_contract(item),
        "cwd": item.get("cwd"),
        "codex_thread_id": item.get("codex_thread_id"),
        "codex_url": item.get("codex_url"),
        "sandbox": item.get("sandbox"),
        "model": item.get("model"),
        **_turn_request_echo(item),
        "profile": item.get("profile"),
        "add_dirs": item.get("add_dirs"),
        "commit_signing": item.get("commit_signing"),
        "workspace": workspace_snapshot(item.get("cwd"), item.get("workspace")),
        "last_status": item.get("last_status"),
        **_execution_fields(runner),
        "goal_status": item.get("goal_status"),
        "objective": item.get("objective"),
        "updated_at": item.get("updated_at"),
        "codex_recency_at": item.get("codex_recency_at"),
        "refresh_error": item.get("refresh_error"),
        "last_final_text": _clip(item.get("last_final_text"), 500),
        "control": _control_contract(
            item,
            str(item.get("codex_thread_id") or ""),
            alias_root=alias_root,
        ),
    }


LAST_TURN_USER_LIMIT = 500
LAST_TURN_LEAD_LIMIT = 160
LAST_TURN_EXCERPT_LIMIT = 800
OUTLINE_PROMPT_LIMIT = 500


def _thread_outline(
    thread: dict[str, Any],
    alias: dict[str, Any] | None,
    *,
    fallback_thread_id: str,
) -> dict[str, Any]:
    raw_turns = thread.get("turns")
    history_complete = isinstance(raw_turns, list)
    turn_items: list[dict[str, Any]] = []
    incomplete_turn_ids: list[str] = []
    prompt_index = 0

    for raw_turn in raw_turns if isinstance(raw_turns, list) else []:
        if not isinstance(raw_turn, dict):
            history_complete = False
            continue
        turn_index = len(turn_items) + 1
        turn_outline, prompt_index = _turn_outline(
            raw_turn,
            turn_index=turn_index,
            prompt_index=prompt_index,
        )
        if turn_outline["items_view"] != "full":
            history_complete = False
            if turn_outline["turn_id"]:
                incomplete_turn_ids.append(str(turn_outline["turn_id"]))
        turn_items.append(turn_outline)

    thread_id = str(
        thread.get("id") or thread.get("sessionId") or fallback_thread_id
    )
    title_fields = _title_contract(
        alias or {},
        thread=thread,
        lane_id=(alias or {}).get("lane_id"),
    )
    if title_fields["lane_title"] is None:
        title_fields["lane_title"] = thread.get("preview")
        title_fields["lane_title_source"] = "thread_preview"
    return {
        "ok": True,
        "provider": CODEX_PROVIDER,
        "lane_id": (alias or {}).get("lane_id"),
        "codex_thread_id": thread_id,
        "codex_url": f"codex://threads/{thread_id}",
        **title_fields,
        "cwd": thread.get("cwd") or (alias or {}).get("cwd"),
        "turn_count": len(turn_items),
        "prompt_count": prompt_index,
        "history_complete": history_complete,
        "incomplete_turn_ids": incomplete_turn_ids,
        "outline": turn_items,
    }


def _turn_outline(
    turn: dict[str, Any],
    *,
    turn_index: int,
    prompt_index: int,
) -> tuple[dict[str, Any], int]:
    prompts: list[dict[str, Any]] = []
    for item in turn.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        prompt_index += 1
        prompts.append(_prompt_outline(item, prompt_index=prompt_index))

    assistant_final = _assistant_final_text(turn)
    started_at = turn.get("startedAt")
    if started_at is None:
        started_at = turn.get("started_at")
    completed_at = turn.get("completedAt")
    if completed_at is None:
        completed_at = turn.get("completed_at")
    duration_ms = turn.get("durationMs")
    if duration_ms is None:
        duration_ms = turn.get("duration_ms")
    return (
        {
            "index": turn_index,
            "turn_id": turn.get("id"),
            "status": turn.get("status"),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "items_view": turn.get("itemsView")
            or turn.get("items_view")
            or "full",
            "error": turn.get("error"),
            "prompts": prompts,
            "assistant_final_lead": _clip(
                _first_paragraph(assistant_final),
                LAST_TURN_LEAD_LIMIT,
            ),
        },
        prompt_index,
    )


def _prompt_outline(
    item: dict[str, Any],
    *,
    prompt_index: int,
) -> dict[str, Any]:
    text_parts: list[str] = []
    input_types: list[str] = []
    fallback_labels: list[str] = []
    for content in item.get("content") or []:
        if not isinstance(content, dict):
            continue
        input_type = str(content.get("type") or "unknown")
        if input_type not in input_types:
            input_types.append(input_type)
        if input_type == "text":
            text = str(content.get("text") or "").strip()
            if text:
                text_parts.append(text)
        elif input_type in {"image", "localImage"}:
            fallback_labels.append("Image")
        elif input_type == "skill":
            name = _safe_input_name(content.get("name"))
            fallback_labels.append(f"Skill: {name}" if name else "Skill")
        elif input_type == "mention":
            name = _safe_input_name(content.get("name"))
            fallback_labels.append(f"Mention: {name}" if name else "Mention")
        else:
            fallback_labels.append(input_type)

    text = "\n".join(text_parts).strip()
    heading = _first_paragraph(text)
    if not heading:
        heading = ", ".join(dict.fromkeys(fallback_labels)) or "(No content)"
    return {
        "prompt_index": prompt_index,
        "item_id": item.get("id"),
        "heading": _clip(heading, LAST_TURN_LEAD_LIMIT),
        "text_excerpt": _clip(text or None, OUTLINE_PROMPT_LIMIT),
        "input_types": input_types,
    }


def _safe_input_name(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    return name.replace("\\", "/").rsplit("/", 1)[-1] or None


def _assistant_final_text(turn: dict[str, Any]) -> str | None:
    if turn.get("status") != "completed":
        return None
    phase_aware = False
    explicit_final_seen = False
    explicit_final: str | None = None
    legacy_final: str | None = None
    for item in turn.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        phase = item.get("phase")
        if phase is not None:
            phase_aware = True
        if phase == "final_answer":
            explicit_final_seen = True
            explicit_final = _clean_agent_text(_message_text(item))
        elif phase is None and (text := _clean_agent_text(_message_text(item))):
            legacy_final = text
    if explicit_final_seen:
        return explicit_final
    return None if phase_aware else legacy_final


def _enrich_session_summaries_with_last_turns(
    codex: CodexAppServer,
    items: list[dict[str, Any]],
    *,
    thread_authoritative: bool = True,
    observed_at: float | None = None,
    observation_mode: str | None = None,
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        thread_id = str(item.get("id") or "")
        parent_thread: dict[str, Any] = {}
        if thread_id:
            try:
                parent_thread = (
                    codex.read_thread(thread_id, include_turns=True).get("thread") or {}
                )
            except Exception as exc:
                enriched["last_turn_error"] = str(exc)

        enriched["locations"] = _thread_locations(parent_thread, item)
        workspace_metadata = (
            None if item.get("adopted_from") == "codex-app" else item.get("workspace")
        )
        enriched["workspace"] = workspace_snapshot(
            enriched["locations"].get("cwd"),
            workspace_metadata,
        )
        enriched["last_turn"] = (
            _last_turn_summary(parent_thread) if parent_thread else _empty_last_turn()
        )
        enriched["active_turn"] = (
            _active_turn_summary(parent_thread) if parent_thread else None
        )
        enriched["latest_activity"] = _latest_activity_summary(
            codex,
            item,
            parent_thread,
        )
        enriched_items.append(
            _apply_session_execution_state(
                enriched,
                thread=parent_thread or None,
                thread_authoritative=thread_authoritative,
                observed_at=observed_at,
                observation_mode=observation_mode,
            )
        )
    return enriched_items


def _load_session_rollout_facts(
    items: list[dict[str, Any]],
    alias_by_thread_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    session_paths = {
        str(item.get("id") or ""): item.get("_session_path")
        for item in items
        if item.get("id")
    }
    for thread_id, alias in alias_by_thread_id.items():
        if not session_paths.get(thread_id):
            session_paths[thread_id] = alias.get("codex_session_path")

    facts: dict[str, dict[str, Any]] = {}
    for thread_id, session_path in session_paths.items():
        rollout = read_rollout_closeout(
            thread_id,
            session_path=str(session_path) if session_path else None,
        )
        if isinstance(rollout, dict):
            facts[thread_id] = rollout
    return facts


def _enrich_session_summaries_with_active_turns(
    items: list[dict[str, Any]],
    *,
    rollout_facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        if not _session_has_active_context(enriched):
            enriched["active_turn"] = None
            enriched_items.append(enriched)
            continue

        active_turn = enriched.get("active_turn")
        summary = dict(active_turn) if isinstance(active_turn, dict) else {}
        rollout = rollout_facts.get(str(enriched.get("id") or ""))
        if not isinstance(rollout, dict):
            rollout = {}

        rollout_turn_id = rollout.get("active_turn_id")
        if not summary and (
            rollout_turn_id
            or rollout.get("active_turn_user_message")
            or rollout.get("active_turn_agent_message")
        ):
            summary = {
                "turn_id": rollout_turn_id,
                "status": "inProgress",
                "started_at": rollout.get("active_turn_started_at"),
                "user_request": None,
                "user_request_source": None,
                "progress_lead": None,
                "progress_excerpt": None,
                "progress_source": None,
                "items_view": None,
                "items_complete": False,
                "source": "rollout",
            }

        summary_turn_id = summary.get("turn_id") if summary else None
        rollout_matches = (
            not summary_turn_id
            or not rollout_turn_id
            or str(summary_turn_id) == str(rollout_turn_id)
        )
        app_items_incomplete = (
            summary.get("source") == "app_server"
            and summary.get("items_complete") is False
        )
        partial_same_turn = (
            app_items_incomplete
            and bool(summary_turn_id)
            and bool(rollout_turn_id)
            and str(summary_turn_id) == str(rollout_turn_id)
        )
        if (
            summary
            and rollout_matches
            and (partial_same_turn or not summary.get("user_request"))
        ):
            user_message = _visible_user_request(
                rollout.get("active_turn_user_message")
            )
            if user_message:
                summary["user_request"] = _clip(
                    user_message,
                    LAST_TURN_USER_LIMIT,
                )
                summary["user_request_source"] = "rollout_user_message"

        if (
            summary
            and rollout_matches
            and (partial_same_turn or not summary.get("progress_lead"))
        ):
            agent_message = _clean_agent_text(
                str(rollout.get("active_turn_agent_message") or "")
            )
            if agent_message:
                summary["progress_lead"] = _clip(
                    _first_paragraph(agent_message),
                    LAST_TURN_LEAD_LIMIT,
                )
                summary["progress_excerpt"] = _clip(
                    agent_message,
                    LAST_TURN_EXCERPT_LIMIT,
                )
                summary["progress_source"] = "rollout_agent_message"

        if summary:
            visible_request = _visible_user_request(summary.get("user_request"))
            summary["user_request"] = visible_request
            if not visible_request:
                summary["user_request_source"] = None
                fallback, fallback_source = _active_user_request_fallback(enriched)
                summary["user_request"] = fallback
                summary["user_request_source"] = fallback_source

        enriched["active_turn"] = summary or None
        enriched_items.append(enriched)
    return enriched_items


def _active_user_request_fallback(
    item: dict[str, Any],
) -> tuple[str | None, str | None]:
    for key, source in (
        ("objective", "goal_objective"),
        ("preview", "thread_preview"),
    ):
        text = _visible_user_request(item.get(key))
        lead = _first_paragraph(text)
        if lead:
            return _clip(lead, LAST_TURN_LEAD_LIMIT), source
    return None, None


def _merge_completed_rollout_sessions(
    items: list[dict[str, Any]],
    alias_by_thread_id: dict[str, dict[str, Any]],
    *,
    goal_observations: dict[str, dict[str, Any]],
    rollout_facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries = {
        str(item.get("id") or ""): item for item in items if item.get("id")
    }
    for thread_id, alias in alias_by_thread_id.items():
        if thread_id in summaries:
            continue
        if (
            goal_observations.get(thread_id, {}).get("goal_status_source")
            == "unavailable"
        ):
            continue
        rollout = rollout_facts.get(thread_id)
        if not isinstance(rollout, dict) or rollout.get("status") != "completed":
            continue
        summaries[thread_id] = _thread_summary(
            {
                "id": thread_id,
                "name": alias.get("codex_title"),
                "preview": alias.get("codex_preview"),
                "cwd": alias.get("cwd"),
                "recencyAt": alias.get("codex_recency_at"),
                "path": alias.get("codex_session_path"),
                "status": alias.get("thread_status"),
            },
            alias,
        )
    return list(summaries.values())


def _enrich_session_summaries_with_recency(
    items: list[dict[str, Any]],
    *,
    rollout_facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        thread_id = str(item.get("id") or "")
        recency_at, recency_source = _effective_session_recency(
            enriched,
            rollout_facts.get(thread_id),
        )
        enriched["recency_at"] = recency_at
        enriched["recency_source"] = recency_source
        enriched_items.append(enriched)
    return enriched_items


def _effective_session_recency(
    item: dict[str, Any],
    rollout: dict[str, Any] | None,
) -> tuple[Any, str]:
    raw_recency = item.get("thread_recency_at")
    if raw_recency is None:
        raw_recency = item.get("recency_at")

    if item.get("execution_active"):
        live_candidates = [
            ("thread_recency_at", raw_recency),
            (
                "live_rollout_mtime",
                rollout.get("rollout_mtime")
                if isinstance(rollout, dict)
                else None,
            ),
            ("live_runner_updated_at", item.get("_alias_updated_at")),
        ]
        source, value = max(
            live_candidates,
            key=lambda candidate: _recency_sort_value(candidate[1]),
        )
        return _normalized_recency(value, fallback=raw_recency), source

    last_turn = item.get("last_turn")
    if isinstance(last_turn, dict) and last_turn.get("completed_at") is not None:
        return (
            _normalized_recency(last_turn.get("completed_at"), fallback=raw_recency),
            "last_turn_completed_at",
        )

    if isinstance(rollout, dict) and rollout.get("status") == "completed":
        if rollout.get("task_complete_at") is not None:
            return (
                _normalized_recency(
                    rollout.get("task_complete_at"),
                    fallback=raw_recency,
                ),
                "rollout_task_complete_at",
            )

    terminal = _session_is_terminal(item, rollout)
    if terminal and isinstance(rollout, dict):
        if rollout.get("assistant_message_at") is not None:
            return (
                _normalized_recency(
                    rollout.get("assistant_message_at"),
                    fallback=raw_recency,
                ),
                "rollout_assistant_message_at",
            )
        if rollout.get("rollout_mtime") is not None:
            return (
                _normalized_recency(
                    rollout.get("rollout_mtime"),
                    fallback=raw_recency,
                ),
                "rollout_mtime",
            )

    return _normalized_recency(raw_recency, fallback=raw_recency), "thread_recency_at"


def _session_is_terminal(
    item: dict[str, Any],
    rollout: dict[str, Any] | None,
) -> bool:
    last_turn = item.get("last_turn")
    if isinstance(last_turn, dict) and _is_terminal_turn_status(
        last_turn.get("status")
    ):
        return True
    if _is_terminal_turn_status(item.get("last_status")):
        return True
    if item.get("goal_status") in {"complete", "completed", "cancelled", "failed"}:
        return True
    return isinstance(rollout, dict) and rollout.get("status") == "completed"


def _normalized_recency(value: Any, *, fallback: Any) -> Any:
    normalized = _recency_sort_value(value)
    if normalized:
        return normalized
    fallback_normalized = _recency_sort_value(fallback)
    return fallback_normalized if fallback_normalized else fallback


def _strip_session_internal_fields(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stripped_items: list[dict[str, Any]] = []
    for item in items:
        stripped = dict(item)
        stripped.pop("_session_path", None)
        stripped.pop("_alias_updated_at", None)
        stripped_items.append(stripped)
    return stripped_items


def _project_session_output(
    items: list[dict[str, Any]],
    *,
    detail: str,
) -> dict[str, Any]:
    if detail != "compact":
        return _project_grouped_output(items)
    return {"items": [_compact_session_summary(item) for item in items]}


def _compact_session_summary(item: dict[str, Any]) -> dict[str, Any]:
    execution = item.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    authoritative = bool(execution.get("authoritative"))
    state = str(execution.get("state") or "unknown")
    status = str(execution.get("effective_turn_status") or "unknown")
    if not authoritative:
        state = "unknown"
        status = "unknown"

    locations = item.get("locations")
    cwd = locations.get("cwd") if isinstance(locations, dict) else None
    if cwd is None:
        cwd = item.get("cwd")
    control = item.get("control")
    requires_attach = (
        bool(control.get("requires_explicit_attach"))
        if isinstance(control, dict)
        else True
    )
    last_turn = item.get("last_turn")
    final_lead = (
        last_turn.get("assistant_final_lead")
        if isinstance(last_turn, dict)
        else None
    )
    if not final_lead:
        final_lead = _clip(_first_paragraph(item.get("last_final_text")), 160)
    raw_title = item.get("lane_title") or item.get("name") or item.get("preview")
    title = _clip(_first_paragraph(str(raw_title)) if raw_title else None, 160)

    return {
        "thread_id": item.get("id") or item.get("codex_thread_id"),
        "title": title,
        "cwd": cwd,
        "updated_at": _normalized_recency(
            item.get("recency_at"),
            fallback=item.get("thread_recency_at"),
        ),
        "execution": {
            "state": state,
            "status": status,
            "authoritative": authoritative,
            "stale": bool(execution.get("stale", not authoritative)),
            "observed_at": execution.get("observed_at"),
        },
        "final_lead": final_lead,
        "requires_attach": requires_attach,
    }


def _project_grouped_output(
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped_items = [dict(item) for item in items]
    grouped_indexes: dict[str, list[int]] = {}
    group_cwds: dict[str, str] = {}

    for index, item in enumerate(grouped_items):
        cwd = _project_group_cwd(item)
        key = _project_group_key(cwd)
        if key is None or cwd is None:
            item["project_group"] = None
            continue
        grouped_indexes.setdefault(key, []).append(index)
        group_cwds.setdefault(key, cwd)

    project_groups: list[dict[str, Any]] = []
    for key, indexes in grouped_indexes.items():
        cwd = group_cwds[key]
        lane_ids = _unique_nonempty(
            grouped_items[index].get("lane_id") for index in indexes
        )
        thread_ids = _unique_nonempty(
            _project_group_thread_id(grouped_items[index]) for index in indexes
        )
        group = {
            "key": key,
            "name": Path(cwd).name or cwd,
            "cwd": cwd,
            "visible_session_count": len(indexes),
            "visible_lane_count": len(lane_ids),
            "visible_lane_ids": lane_ids,
            "visible_thread_ids": thread_ids,
        }
        project_groups.append(group)
        for position, item_index in enumerate(indexes, start=1):
            item = grouped_items[item_index]
            lane_id = str(item.get("lane_id") or "")
            thread_id = _project_group_thread_id(item)
            item["project_group"] = {
                "key": key,
                "name": group["name"],
                "cwd": cwd,
                "visible_session_count": len(indexes),
                "position": position,
                "related_lane_ids": [
                    value for value in lane_ids if value != lane_id
                ],
                "related_thread_ids": [
                    value for value in thread_ids if value != thread_id
                ],
            }

    return {"project_groups": project_groups, "items": grouped_items}


def _project_group_cwd(item: dict[str, Any]) -> str | None:
    locations = item.get("locations")
    cwd = locations.get("cwd") if isinstance(locations, dict) else None
    if cwd is None:
        cwd = item.get("cwd")
    text = str(cwd or "").strip()
    return text or None


def _project_group_key(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    return str(Path(cwd).expanduser().resolve(strict=False))


def _project_group_thread_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("codex_thread_id") or "")


def _unique_nonempty(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _enrich_session_summaries_with_goals(
    codex: CodexAppServer,
    items: list[dict[str, Any]],
    *,
    goal_observations: dict[str, dict[str, Any]] | None = None,
    thread_authoritative: bool = True,
    observed_at: float | None = None,
    observation_mode: str | None = None,
) -> list[dict[str, Any]]:
    observations = goal_observations if goal_observations is not None else {}
    enriched_items: list[dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        thread_id = str(item.get("id") or "")
        if thread_id not in observations:
            try:
                goal = codex.get_goal(thread_id) if thread_id else None
            except Exception as exc:
                observations[thread_id] = {
                    "goal_status": None,
                    "objective": None,
                    "goal_status_source": "unavailable",
                    "goal_refresh_error": str(exc),
                }
            else:
                observations[thread_id] = {
                    "goal_status": (
                        goal.get("status") if isinstance(goal, dict) else None
                    ),
                    "objective": (
                        goal.get("objective") if isinstance(goal, dict) else None
                    ),
                    "goal_status_source": "thread_goal_get",
                }
        observation = observations[thread_id]
        enriched.update(observation)
        if "goal_refresh_error" not in observation:
            enriched.pop("goal_refresh_error", None)
        enriched_items.append(
            _apply_session_execution_state(
                enriched,
                thread_authoritative=thread_authoritative,
                observed_at=observed_at,
                observation_mode=observation_mode,
            )
        )
    return enriched_items


def _scan_aliased_active_goals(
    codex: CodexAppServer,
    alias_by_thread_id: dict[str, dict[str, Any]],
    *,
    goal_observations: dict[str, dict[str, Any]],
) -> tuple[set[str], dict[str, Any]]:
    probes = [{"id": thread_id} for thread_id in alias_by_thread_id]
    _enrich_session_summaries_with_goals(
        codex,
        probes,
        goal_observations=goal_observations,
    )
    active_thread_ids = {
        thread_id
        for thread_id in alias_by_thread_id
        if goal_observations.get(thread_id, {}).get("goal_status") == "active"
    }
    errors = sum(
        1
        for thread_id in alias_by_thread_id
        if goal_observations.get(thread_id, {}).get("goal_status_source")
        == "unavailable"
    )
    return active_thread_ids, {
        "scope": "aliased_threads",
        "scanned": len(alias_by_thread_id),
        "errors": errors,
    }


def _merge_active_goal_sessions(
    codex: CodexAppServer,
    natural_items: list[dict[str, Any]],
    raw_items: list[dict[str, Any]],
    alias_by_thread_id: dict[str, dict[str, Any]],
    *,
    active_goal_thread_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    summaries = {
        str(item.get("id") or ""): item
        for item in natural_items
        if item.get("id")
    }
    raw_by_thread_id = {
        _thread_id(item): item for item in raw_items if _thread_id(item)
    }
    thread_read_errors = 0
    for thread_id in active_goal_thread_ids:
        if thread_id in summaries:
            continue
        thread = raw_by_thread_id.get(thread_id)
        thread_read_error: str | None = None
        if thread is None:
            try:
                thread = (
                    codex.read_thread(thread_id, include_turns=False).get("thread")
                    or {}
                )
            except Exception as exc:
                thread_read_errors += 1
                thread_read_error = str(exc)
                thread = {}
        alias = alias_by_thread_id[thread_id]
        if not thread:
            thread = {
                "id": thread_id,
                "name": alias.get("codex_title"),
                "preview": alias.get("codex_preview"),
                "cwd": alias.get("cwd"),
                "recencyAt": alias.get("codex_recency_at"),
            }
        summary = _thread_summary(thread, alias)
        if thread_read_error:
            summary["active_goal_thread_error"] = thread_read_error
            summary["cached_thread_status"] = alias.get("thread_status")
        summaries[thread_id] = summary
    return list(summaries.values()), thread_read_errors


def _active_goal_session_sort_value(item: dict[str, Any]) -> tuple[int, float]:
    return (
        1 if item.get("goal_status") == "active" else 0,
        _recency_sort_value(item.get("recency_at")),
    )


def _apply_session_execution_state(
    item: dict[str, Any],
    *,
    thread: dict[str, Any] | None = None,
    thread_authoritative: bool = True,
    observed_at: float | None = None,
    observation_mode: str | None = None,
) -> dict[str, Any]:
    enriched = dict(item)
    observed_thread = thread or {"status": enriched.get("status")}
    goal_status = enriched.get("goal_status")
    goal = {"status": goal_status} if goal_status is not None else None
    runner = _runner_state(
        enriched,
        goal,
        thread=observed_thread,
        goal_source=str(enriched.get("goal_status_source") or "unavailable"),
        thread_authoritative=thread_authoritative,
        observed_at=observed_at,
        observation_mode=observation_mode,
    )
    enriched.update(_execution_fields(runner))
    return enriched


def _session_has_active_context(item: dict[str, Any]) -> bool:
    if item.get("execution_active") or item.get("thread_active"):
        return True
    if item.get("goal_status") == "active":
        return True
    if _thread_has_active_turn({"status": item.get("status")}):
        return True
    effective_status = item.get("runner_status") or item.get("last_status")
    return str(effective_status or "").casefold() in {
        "active",
        "inprogress",
        "in_progress",
        "running",
        "started",
        "starting",
    }


def _active_turn_summary(thread: dict[str, Any]) -> dict[str, Any] | None:
    turns = [item for item in thread.get("turns") or [] if isinstance(item, dict)]
    active_turns = [
        turn
        for turn in turns
        if str(turn.get("status") or "").casefold()
        in {"active", "inprogress", "in_progress", "running", "started", "starting"}
    ]
    if not active_turns and _thread_has_active_turn(thread):
        active_turns = [
            turn
            for turn in turns
            if not _is_terminal_turn_status(turn.get("status"))
        ]
    turn = max(
        enumerate(active_turns),
        key=lambda pair: (_turn_sort_value(pair[1]), pair[0]),
        default=(0, None),
    )[1]
    if turn is None:
        return None

    items_view = turn.get("itemsView") or turn.get("items_view") or "full"
    user_request, user_turn_id = _latest_visible_user_request(thread)
    progress: str | None = None
    for item in turn.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agentMessage":
            text = _clean_agent_text(_message_text(item))
            if text:
                progress = text

    current_turn_id = turn.get("id")
    user_request_source = None
    if user_request:
        user_request_source = (
            "app_server"
            if str(user_turn_id or "") == str(current_turn_id or "")
            else "app_server_prior_user_message"
        )

    return {
        "turn_id": current_turn_id,
        "status": turn.get("status"),
        "started_at": turn.get("startedAt") or turn.get("started_at"),
        "user_request": _clip(user_request, LAST_TURN_USER_LIMIT),
        "user_request_source": user_request_source,
        "progress_lead": _clip(
            _first_paragraph(progress),
            LAST_TURN_LEAD_LIMIT,
        ),
        "progress_excerpt": _clip(progress, LAST_TURN_EXCERPT_LIMIT),
        "progress_source": "app_server_agent_message" if progress else None,
        "items_view": items_view,
        "items_complete": items_view == "full",
        "source": "app_server",
    }


def _latest_visible_user_request(
    thread: dict[str, Any],
) -> tuple[str | None, Any]:
    turns = [item for item in thread.get("turns") or [] if isinstance(item, dict)]
    ordered = sorted(
        enumerate(turns),
        key=lambda pair: (_turn_sort_value(pair[1]), pair[0]),
        reverse=True,
    )
    for _, turn in ordered:
        items = [item for item in turn.get("items") or [] if isinstance(item, dict)]
        for item in reversed(items):
            if item.get("type") != "userMessage":
                continue
            text = _visible_user_request(_message_text(item))
            if text:
                return text, turn.get("id")
    return None, None


def _visible_user_request(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.casefold()
    if normalized.startswith("<codex_internal_context"):
        return None
    internal_continuation_prefixes = (
        GOAL_CONTINUATION_PROMPT.casefold(),
        "continue working toward the active thread goal.",
    )
    if normalized.startswith(internal_continuation_prefixes):
        return None
    return text


def _last_turn_summary(thread: dict[str, Any]) -> dict[str, Any]:
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return _empty_last_turn()
    turn = max(
        (item for item in turns if isinstance(item, dict)),
        key=_turn_sort_value,
        default=None,
    )
    if not turn:
        return _empty_last_turn()

    user_request: str | None = None
    for item in turn.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "userMessage":
            text = _message_text(item)
            if text:
                user_request = text
    assistant_final = _assistant_final_text(turn)

    return {
        "turn_id": turn.get("id"),
        "status": turn.get("status"),
        "started_at": turn.get("startedAt") or turn.get("started_at"),
        "completed_at": turn.get("completedAt") or turn.get("completed_at"),
        "user_request": _clip(user_request, LAST_TURN_USER_LIMIT),
        "assistant_final_lead": _clip(
            _first_paragraph(assistant_final),
            LAST_TURN_LEAD_LIMIT,
        ),
        "assistant_final_excerpt": _clip(assistant_final, LAST_TURN_EXCERPT_LIMIT),
    }


def _empty_last_turn() -> dict[str, Any]:
    return {
        "turn_id": None,
        "status": None,
        "started_at": None,
        "completed_at": None,
        "user_request": None,
        "assistant_final_lead": None,
        "assistant_final_excerpt": None,
    }


def _thread_locations(
    thread: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    thread_id = str(
        thread.get("id")
        or thread.get("sessionId")
        or fallback.get("id")
        or fallback.get("thread_id")
        or ""
    )
    return {
        "thread_id": thread_id or None,
        "codex_url": f"codex://threads/{thread_id}" if thread_id else None,
        "session_path": thread.get("path"),
        "cwd": thread.get("cwd") or fallback.get("cwd"),
    }


def _latest_activity_summary(
    codex: CodexAppServer,
    item: dict[str, Any],
    parent_thread: dict[str, Any],
) -> dict[str, Any]:
    latest_child_id = item.get("latest_subagent_thread_id")
    if latest_child_id:
        child_thread: dict[str, Any] = {}
        child_fallback = {
            "id": latest_child_id,
            "cwd": item.get("cwd"),
        }
        try:
            child_thread = (
                codex.read_thread(str(latest_child_id), include_turns=False).get(
                    "thread"
                )
                or {}
            )
        except Exception as exc:
            child_fallback["latest_activity_error"] = str(exc)
        return _activity_from_thread(
            "subagent",
            child_thread,
            child_fallback,
            parent_thread_id=item.get("id"),
            name=item.get("latest_subagent_name"),
            recency_at=item.get("latest_subagent_recency_at"),
            agent_nickname=item.get("latest_subagent_nickname"),
            agent_role=item.get("latest_subagent_role"),
        )

    if item.get("parent_thread_id"):
        return _activity_from_thread(
            "subagent",
            parent_thread,
            item,
            parent_thread_id=item.get("parent_thread_id"),
            name=item.get("name"),
            recency_at=item.get("recency_at"),
            agent_nickname=item.get("agent_nickname"),
            agent_role=item.get("agent_role"),
        )

    return _activity_from_thread(
        "parent",
        parent_thread,
        item,
        parent_thread_id=None,
        name=item.get("name"),
        recency_at=item.get("recency_at"),
        agent_nickname=item.get("agent_nickname"),
        agent_role=item.get("agent_role"),
    )


def _activity_from_thread(
    kind: str,
    thread: dict[str, Any],
    fallback: dict[str, Any],
    *,
    parent_thread_id: Any,
    name: Any,
    recency_at: Any,
    agent_nickname: Any,
    agent_role: Any,
) -> dict[str, Any]:
    activity = {
        "kind": kind,
        **_thread_locations(thread, fallback),
        "parent_thread_id": parent_thread_id,
        "name": thread.get("name") or name,
        "recency_at": _thread_recency(thread) or recency_at,
        "agent_nickname": thread.get("agentNickname")
        or thread.get("agent_nickname")
        or agent_nickname,
        "agent_role": thread.get("agentRole") or thread.get("agent_role") or agent_role,
    }
    if fallback.get("latest_activity_error"):
        activity["latest_activity_error"] = fallback.get("latest_activity_error")
    return activity


def _turn_sort_value(turn: dict[str, Any]) -> float:
    return _recency_sort_value(
        turn.get("completedAt")
        or turn.get("completed_at")
        or turn.get("startedAt")
        or turn.get("started_at")
    )


def _message_text(item: dict[str, Any]) -> str:
    if item.get("type") == "userMessage":
        parts: list[str] = []
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content.get("text")))
        return "\n".join(parts).strip()
    if item.get("type") == "agentMessage":
        return str(item.get("text") or "").strip()
    return ""


def _clean_agent_text(text: str) -> str | None:
    lines: list[str] = []
    in_memory_citation = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "<oai-mem-citation>":
            in_memory_citation = True
            continue
        if line == "</oai-mem-citation>":
            in_memory_citation = False
            continue
        if in_memory_citation or not line:
            continue
        if line.startswith("::") and "{" in line and line.endswith("}"):
            continue
        lines.append(line)
    return "\n".join(lines).strip() or None


def _first_paragraph(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _thread_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("sessionId") or "")


def _session_fetch_limit(limit: int, *, include_subagents: bool) -> int:
    if include_subagents or limit <= 0:
        return limit
    return max(limit * 5, 50)


def _find_fetch_limit(limit: int) -> int:
    if limit <= 0:
        return 0
    return max(limit * 5, 50)


def _collect_session_pages(
    codex: CodexAppServer,
    *,
    page_limit: int,
    enough: Callable[[list[dict[str, Any]]], bool],
    search_term: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    fetched = 0
    scan_exhausted = False
    page_cap_reached = False
    cursor_stalled = False

    if page_limit <= 0:
        return items, {
            "pages": 0,
            "fetched": 0,
            "unique": 0,
            "limit_satisfied": enough(items),
            "scan_exhausted": False,
            "page_cap_reached": False,
            "cursor_stalled": False,
        }

    while pages < SESSION_LIST_MAX_PAGES:
        page = codex.list_threads(
            limit=page_limit,
            search_term=search_term,
            cursor=cursor,
        )
        raw_page = page.get("data")
        page_items = (
            [item for item in raw_page if isinstance(item, dict)]
            if isinstance(raw_page, list)
            else []
        )
        pages += 1
        fetched += len(page_items)
        items = _merge_thread_items(items, page_items)

        next_cursor = page.get("nextCursor") or page.get("next_cursor")
        scan_exhausted = not bool(next_cursor)
        if enough(items):
            break
        if not next_cursor:
            break
        next_cursor = str(next_cursor)
        if next_cursor == cursor or next_cursor in seen_cursors:
            cursor_stalled = True
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        page_cap_reached = True

    return items, {
        "pages": pages,
        "fetched": fetched,
        "unique": len(items),
        "limit_satisfied": enough(items),
        "scan_exhausted": scan_exhausted,
        "page_cap_reached": page_cap_reached,
        "cursor_stalled": cursor_stalled,
    }


def _session_summaries(
    items: list[dict[str, Any]],
    alias_by_thread_id: dict[str, dict[str, Any]],
    *,
    include_subagents: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if include_subagents:
        summaries = [
            _thread_summary(item, alias_by_thread_id.get(_thread_id(item)))
            for item in items
        ]
    else:
        summaries = _main_session_summaries(items, alias_by_thread_id)
    summaries = sorted(
        summaries,
        key=lambda item: _recency_sort_value(item.get("recency_at")),
        reverse=True,
    )
    return summaries[:limit]


def _merge_thread_items(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in groups:
        for item in group:
            thread_id = _thread_id(item)
            if not thread_id:
                continue
            if thread_id not in merged:
                order.append(thread_id)
            merged[thread_id] = item
    return [merged[thread_id] for thread_id in order]


def _main_session_summaries(
    items: list[dict[str, Any]],
    alias_by_thread_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _thread_summary(item, alias_by_thread_id.get(_thread_id(item)))
        for item in items
        if _thread_id(item) and not _is_subagent_thread(item)
    ]


def _parent_thread_id(item: dict[str, Any]) -> str | None:
    direct = item.get("parentThreadId") or item.get("parent_thread_id")
    if direct:
        return str(direct)

    source = item.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subAgent") or source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn")
    if not isinstance(spawn, dict):
        return None
    nested = spawn.get("parent_thread_id") or spawn.get("parentThreadId")
    return str(nested) if nested else None


def _subagent_spawn(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source")
    if not isinstance(source, dict):
        return {}
    subagent = source.get("subAgent") or source.get("subagent")
    if not isinstance(subagent, dict):
        return {}
    spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn")
    return spawn if isinstance(spawn, dict) else {}


def _subagent_nickname(item: dict[str, Any]) -> Any:
    return (
        item.get("agentNickname")
        or item.get("agent_nickname")
        or _subagent_spawn(item).get("agent_nickname")
        or _subagent_spawn(item).get("agentNickname")
    )


def _subagent_role(item: dict[str, Any]) -> Any:
    return (
        item.get("agentRole")
        or item.get("agent_role")
        or _subagent_spawn(item).get("agent_role")
        or _subagent_spawn(item).get("agentRole")
    )


def _is_subagent_thread(item: dict[str, Any]) -> bool:
    if _parent_thread_id(item):
        return True
    for key in ("threadSource", "thread_source", "sourceKind", "source_kind"):
        if _is_subagent_source_label(item.get(key)):
            return True
    source = item.get("source")
    if isinstance(source, dict):
        return isinstance(source.get("subAgent"), dict) or isinstance(
            source.get("subagent"), dict
        ) or "subAgent" in source or "subagent" in source
    return _is_subagent_source_label(source)


def _is_subagent_source_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.replace("_", "").replace("-", "").casefold()
    return normalized.startswith("subagent") or normalized.startswith("guardian")


def _thread_summary(
    item: dict[str, Any],
    alias: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thread_id = _thread_id(item)
    thread_recency = _thread_recency(item)
    title_fields = _title_contract(
        alias or {},
        thread=item,
        lane_id=(alias or {}).get("lane_id"),
    )
    if title_fields["lane_title"] is None:
        title_fields["lane_title"] = item.get("preview")
        title_fields["lane_title_source"] = "thread_preview"
    summary: dict[str, Any] = {
        "kind": "codex_thread",
        "aliased": alias is not None,
        "id": thread_id,
        "name": item.get("name"),
        **title_fields,
        "preview": item.get("preview"),
        "cwd": item.get("cwd"),
        "source": item.get("source"),
        "status": item.get("status"),
        "thread_recency_at": thread_recency,
        "recency_at": thread_recency,
        "codex_url": f"codex://threads/{thread_id}",
        "control": _control_contract(alias, thread_id, thread=item),
        **_turn_request_echo(alias),
        "_session_path": item.get("path")
        or (alias.get("codex_session_path") if alias else None),
    }
    thread_source = item.get("threadSource") or item.get("thread_source")
    parent_thread_id = _parent_thread_id(item)
    agent_nickname = _subagent_nickname(item)
    agent_role = _subagent_role(item)
    if thread_source is not None:
        summary["thread_source"] = thread_source
    if parent_thread_id is not None:
        summary["parent_thread_id"] = parent_thread_id
    if agent_nickname is not None:
        summary["agent_nickname"] = agent_nickname
    if agent_role is not None:
        summary["agent_role"] = agent_role
    if alias:
        stored_goal = alias.get("goal")
        runner = _runner_state(
            dict(alias),
            stored_goal if isinstance(stored_goal, dict) else None,
        )
        workspace_metadata = (
            None
            if alias.get("adopted_from") == "codex-app"
            else alias.get("workspace")
        )
        summary.update(
            {
                "lane_id": alias.get("lane_id"),
                "last_status": alias.get("last_status"),
                "local_runner_status": runner["local_status"],
                "runner_alive": runner["alive"],
                "needs_resume": runner["needs_resume"],
                "last_final_text": _clip(alias.get("last_final_text"), 500),
                "sandbox": alias.get("sandbox"),
                "model": alias.get("model"),
                "profile": alias.get("profile"),
                "add_dirs": alias.get("add_dirs"),
                "commit_signing": alias.get("commit_signing"),
                "goal_status": alias.get("goal_status"),
                "objective": alias.get("objective"),
                "workspace": workspace_snapshot(
                    item.get("cwd") or alias.get("cwd"),
                    workspace_metadata,
                ),
                "adopted_from": alias.get("adopted_from"),
                "owner_running": runner["alive"],
                "_alias_updated_at": alias.get("updated_at"),
            }
        )
    return summary


def _thread_recency(item: dict[str, Any]) -> Any:
    return (
        item.get("recencyAt")
        or item.get("recency_at")
        or item.get("updatedAt")
        or item.get("updated_at")
    )


def _recency_sort_value(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _matches_alias(item: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    haystack = "\n".join(
        str(item.get(key) or "")
        for key in (
            "lane_id",
            "custom_title",
            "codex_title",
            "cwd",
            "codex_thread_id",
            "codex_preview",
            "objective",
            "last_final_text",
        )
    ).casefold()
    return needle in haystack


def _matches_session_summary(item: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    haystack = "\n".join(
        _summary_match_text(item.get(key))
        for key in (
            "id",
            "name",
            "preview",
            "cwd",
            "source",
            "lane_id",
            "lane_title",
            "objective",
            "parent_thread_id",
            "agent_nickname",
            "agent_role",
        )
    ).casefold()
    return needle in haystack


def _summary_match_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _git_info(cwd: str | None) -> dict[str, str]:
    if not cwd:
        return {}

    def run(*parts: str) -> str | None:
        try:
            value = subprocess.check_output(
                ["git", *parts],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            return value or None
        except Exception:
            return None

    info = {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "sha": run("rev-parse", "HEAD"),
        "originUrl": run("config", "--get", "remote.origin.url"),
    }
    return {k: v for k, v in info.items() if v}


def _git_snapshot(
    cwd: str | None,
    *,
    include_details: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_repo": False,
        "branch": None,
        "dirty": None,
    }
    if include_details:
        result.update(
            {
                "status_short": [],
                "status_truncated": False,
                "recent_commits": [],
            }
        )
    if not cwd or not Path(cwd).is_dir():
        return result

    def run(*parts: str) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                ["git", *parts],
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except Exception:
            return False, ""
        return completed.returncode == 0, completed.stdout.rstrip("\n")

    is_repo_ok, is_repo = run("rev-parse", "--is-inside-work-tree")
    if not is_repo_ok or is_repo != "true":
        return result
    result["is_repo"] = True

    branch_ok, branch = run("symbolic-ref", "--short", "-q", "HEAD")
    if not branch_ok or not branch:
        head_ok, head = run("rev-parse", "--short", "HEAD")
        branch = f"HEAD@{head}" if head_ok and head else None
    result["branch"] = branch

    status_ok, status = run("status", "--short", "--untracked-files=all")
    status_lines = status.splitlines() if status_ok and status else []
    result["dirty"] = bool(status_lines) if status_ok else None
    if not include_details:
        return result

    result["status_short"] = status_lines[:100]
    result["status_truncated"] = len(status_lines) > 100
    log_ok, log = run("log", "-n", "3", "--format=%h%x09%s")
    if log_ok and log:
        commits = []
        for line in log.splitlines():
            sha, separator, subject = line.partition("\t")
            commits.append(
                {
                    "sha": sha,
                    "subject": subject if separator else "",
                }
            )
        result["recent_commits"] = commits
    return result
