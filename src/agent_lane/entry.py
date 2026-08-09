"""Thin V1 CLI entrypoint and output boundary."""

from __future__ import annotations

import json
import sys

from .codex_rpc import CodexRpcError
from .commands import command_handlers
from .control_plane import _command_locks
from .output import CliUsageError, failure_envelope, success_envelope, usage_failure
from .parser import V1ArgumentParser, build_parser
from .workspace import WorkspaceError


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:1] == ["_app-sync-login"]:
        return _run_internal_login(raw_args[1:])

    removed = _removed_cli_request(raw_args)
    if removed is not None:
        _print(removed)
        return 2

    try:
        args = build_parser().parse_args(raw_args)
    except CliUsageError as exc:
        _print(usage_failure(_command_from_argv(raw_args), str(exc)))
        return 2
    args._raw_argv = raw_args

    command = str(getattr(args, "command_name", _command_from_argv(raw_args)))
    handler = command_handlers().get(str(args.handler))
    if handler is None:
        _print(
            failure_envelope(
                command,
                {
                    "ok": False,
                    "error_code": "CLI_HANDLER_MISSING",
                    "error": "the parsed command has no registered handler",
                    "retryable": False,
                    "handler": getattr(args, "handler", None),
                },
            )
        )
        return 1
    try:
        with _command_locks(args):
            result = handler(args)
    except WorkspaceError as exc:
        target_resolution = getattr(args, "_target_resolution", None)
        if isinstance(target_resolution, dict):
            exc.details.setdefault("target_resolution", target_resolution)
        _print(failure_envelope(command, exc.as_dict()))
        return 1
    except CodexRpcError as exc:
        target_resolution = getattr(args, "_target_resolution", None)
        if isinstance(target_resolution, dict):
            exc.details.setdefault("target_resolution", target_resolution)
        _print(failure_envelope(command, exc.as_dict()))
        return 1
    except Exception as exc:
        _print(
            failure_envelope(
                command,
                {
                    "ok": False,
                    "error_code": "AGENT_LANE_UNEXPECTED_ERROR",
                    "error": str(exc),
                    "retryable": False,
                    "exception_type": type(exc).__name__,
                },
            )
        )
        return 1

    target_resolution = getattr(args, "_target_resolution", None)
    if isinstance(result, dict) and isinstance(target_resolution, dict):
        result["target_resolution"] = _final_target_resolution(
            target_resolution,
            result,
        )
    envelope = (
        failure_envelope(command, result)
        if isinstance(result, dict) and not result.get("ok", True)
        else success_envelope(command, result)
    )
    _print(envelope, compact=bool(getattr(args, "jsonl_output", False)))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


def _run_internal_login(argv: list[str]) -> int:
    parser = V1ArgumentParser(prog="agent-lane _app-sync-login", add_help=False)
    parser.add_argument("--codex-bin", required=True)
    try:
        args = parser.parse_args(argv)
        args.handler = "internal.app-sync-login"
        result = command_handlers()[args.handler](args)
    except CliUsageError as exc:
        _print(usage_failure("internal.app-sync-login", str(exc)))
        return 2
    except (WorkspaceError, CodexRpcError) as exc:
        _print(failure_envelope("internal.app-sync-login", exc.as_dict()))
        return 1
    except Exception as exc:
        _print(
            failure_envelope(
                "internal.app-sync-login",
                {
                    "ok": False,
                    "error_code": "APP_SYNC_LOGIN_FAILED",
                    "error": str(exc),
                    "retryable": True,
                    "exception_type": type(exc).__name__,
                },
            )
        )
        return 1
    envelope = (
        failure_envelope("internal.app-sync-login", result)
        if not result.get("ok", True)
        else success_envelope("internal.app-sync-login", result)
    )
    _print(envelope)
    return 0 if result.get("ok", True) else 1


def _print(payload: dict[str, object], *, compact: bool = False) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
            sort_keys=True,
        )
    )


def _command_from_argv(argv: list[str]) -> str:
    words: list[str] = []
    for value in argv:
        if value.startswith("-"):
            break
        words.append(value)
        if len(words) == 4:
            break
    return ".".join(words) if words else "unknown"


def _removed_cli_request(argv: list[str]) -> dict[str, object] | None:
    if tuple(argv[:2]) in {("codex", "send"), ("codex", "steer")}:
        for option in ("--adopt-as",):
            if option in argv:
                return usage_failure(
                    ".".join(argv[:2]),
                    (
                        f"`{option}` was removed from `{' '.join(argv[:2])}` in V1; "
                        "attach the task first with `agent-lane codex session attach`"
                    ),
                    code="CLI_REMOVED",
                    removed=option,
                    replacement="codex session attach",
                    control_requires_explicit_attach=True,
                    thread_id=_option_value(argv, option),
                )

    command_map: dict[tuple[str, ...], str | None] = {
        ("codex", "doctor"): "doctor",
        ("codex", "signing"): "signing",
        ("codex", "recent"): "codex session list --scope all",
        ("codex", "find"): "codex session find",
        ("codex", "list"): "codex session list --scope lanes",
        ("codex", "adopt"): "codex session attach",
        ("codex", "name"): "codex session name",
        ("codex", "outline"): "codex session outline",
        ("codex", "read"): "codex session read",
        ("codex", "monitor"): "codex checkpoint",
        ("codex", "open"): None,
        ("codex", "refresh"): None,
    }
    for prefix, replacement in command_map.items():
        if tuple(argv[: len(prefix)]) == prefix:
            text = f"`{' '.join(prefix)}` was removed in V1"
            if replacement:
                text += f"; use `agent-lane {replacement}`"
            else:
                text += "; App page navigation is no longer part of agent-lane"
            return usage_failure(
                ".".join(prefix),
                text,
                code="CLI_REMOVED",
                removed=" ".join(prefix),
                replacement=replacement,
            )

    option_map = {
        "--git-worktree": "--worktree auto",
        "--raw": "--threads all",
        "--include-subagents": "--threads all",
        "--aliases-only": "--scope lanes",
        "--include-unaliased": "--scope all",
        "--brief": None,
        "--ephemeral": None,
        "--app-refresh": None,
        "--no-app-refresh": None,
    }
    for option, replacement in option_map.items():
        if option in argv:
            text = f"`{option}` was removed in V1"
            if replacement:
                text += f"; use `{replacement}`"
            return usage_failure(
                _command_from_argv(argv),
                text,
                code="CLI_REMOVED",
                removed=option,
                replacement=replacement,
            )
    return None


def _option_value(argv: list[str], option: str) -> str | None:
    try:
        index = argv.index(option)
    except ValueError:
        return None
    value_index = index + 1
    if value_index >= len(argv) or argv[value_index].startswith("-"):
        return None
    return argv[value_index]


def _final_target_resolution(
    resolution: dict[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    final = dict(resolution)
    resolved = dict(final.get("resolved") or {})
    if "lane_id" in result:
        resolved["lane_id"] = result.get("lane_id")
    if "codex_thread_id" in result:
        resolved["thread_id"] = result.get("codex_thread_id")
    final["resolved"] = resolved
    return final
