"""V1 command-line grammar.

The parser owns syntax only. Command handlers are selected by the stable
``handler`` key so parsing does not depend on implementation modules.
"""

from __future__ import annotations

import argparse

from . import __version__
from .codex_rpc import SANDBOX_MODES
from .output import CliUsageError
from .state import DEFAULT_ALIAS_ROOT


COMMIT_SIGNING_MODES = ("off", "agent")
EXECUTION_MODES = ("independent", "app-sync")


class V1ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = V1ArgumentParser(prog="agent-lane")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    root = parser.add_subparsers(
        dest="surface",
        required=True,
        parser_class=V1ArgumentParser,
    )

    _add_doctor(root)
    _add_config(root)
    _add_signing(root)
    codex = root.add_parser("codex", help="Run and inspect durable Codex tasks")
    codex_sub = codex.add_subparsers(
        dest="command",
        required=True,
        parser_class=V1ArgumentParser,
    )
    _add_execution_commands(codex_sub)
    _add_goal_commands(codex_sub)
    _add_session_commands(codex_sub)
    return parser


def _add_doctor(root: argparse._SubParsersAction) -> None:
    doctor = root.add_parser("doctor", help="Diagnose agent-lane readiness")
    _add_alias_root(doctor)
    doctor.add_argument(
        "--mode",
        choices=EXECUTION_MODES,
        default="independent",
        help="Execution mode whose readiness must be satisfied",
    )
    doctor.add_argument("--probe", action="store_true")
    doctor.add_argument("--verbose", action="store_true")
    doctor.set_defaults(
        handler="doctor",
        command_name="doctor",
        provider="agent-lane",
    )


def _add_config(root: argparse._SubParsersAction) -> None:
    config = root.add_parser("config", help="Manage agent-lane configuration")
    config_sub = config.add_subparsers(
        dest="config_group",
        required=True,
        parser_class=V1ArgumentParser,
    )
    app_sync = config_sub.add_parser(
        "app-sync",
        help="Manage optional Codex App Sync login integration",
    )
    app_sync_sub = app_sync.add_subparsers(
        dest="app_sync_command",
        required=True,
        parser_class=V1ArgumentParser,
    )
    for operation in ("enable", "status", "disable"):
        command = app_sync_sub.add_parser(operation)
        if operation in {"enable", "status"}:
            command.add_argument("--codex-bin", default="codex")
        command.set_defaults(
            handler=f"config.app-sync.{operation}",
            command_name=f"config.app-sync.{operation}",
            provider="agent-lane",
        )

    effort = config_sub.add_parser(
        "effort",
        help="Manage the per-user default Codex reasoning effort",
    )
    effort_sub = effort.add_subparsers(
        dest="effort_command",
        required=True,
        parser_class=V1ArgumentParser,
    )
    effort_set = effort_sub.add_parser("set")
    effort_set.add_argument("value")
    effort_set.set_defaults(
        handler="config.effort.set",
        command_name="config.effort.set",
        provider="agent-lane",
    )
    for operation in ("status", "clear"):
        command = effort_sub.add_parser(operation)
        command.set_defaults(
            handler=f"config.effort.{operation}",
            command_name=f"config.effort.{operation}",
            provider="agent-lane",
        )


def _add_signing(root: argparse._SubParsersAction) -> None:
    signing = root.add_parser(
        "signing",
        help="Manage beta agent-lane SSH commit signing",
    )
    signing_sub = signing.add_subparsers(
        dest="signing_command",
        required=True,
        parser_class=V1ArgumentParser,
    )
    init = signing_sub.add_parser("init")
    init.add_argument("--generate", action="store_true")
    init.set_defaults(
        handler="signing.init",
        command_name="signing.init",
        provider="agent-lane",
    )
    for operation in ("status", "test", "stop"):
        command = signing_sub.add_parser(operation)
        command.set_defaults(
            handler=f"signing.{operation}",
            command_name=f"signing.{operation}",
            provider="agent-lane",
        )


def _add_execution_commands(codex_sub: argparse._SubParsersAction) -> None:
    run = codex_sub.add_parser(
        "run", help="Create or resume a task and run one turn"
    )
    _add_lane_target(run, required=False)
    run.add_argument("--cwd")
    run.add_argument("--title")
    run.add_argument("--mode", choices=EXECUTION_MODES)
    _add_sandbox(run)
    _add_runtime_options(run)
    _add_worktree(run)
    _add_commit_signing(run)
    _add_signing_replacement_authorization(run)
    run.add_argument("--goal-objective")
    _add_prompt(run)
    run.add_argument("--timeout", type=float, default=None)
    _set(run, "codex.run")

    send = codex_sub.add_parser("send", help="Run one follow-up turn")
    _add_lane_target(send)
    _add_sandbox(send)
    _add_runtime_options(send)
    _add_commit_signing(send)
    _add_signing_replacement_authorization(send)
    _add_prompt(send)
    send.add_argument("--timeout", type=float, default=None)
    _set(send, "codex.send")

    steer = codex_sub.add_parser(
        "steer", help="Add input to an active App Sync turn"
    )
    _add_lane_target(steer)
    _add_prompt(steer)
    steer.add_argument("--turn-id")
    steer.add_argument("--timeout", type=float, default=20.0)
    _set(steer, "codex.steer")

    status = codex_sub.add_parser("status", help="Read task execution status")
    _add_lane_target(status)
    status.add_argument(
        "--detail",
        choices=("summary", "full", "turns"),
        default="summary",
    )
    _set(status, "codex.status")

    closeout = codex_sub.add_parser("closeout", help="Read completion and Git state")
    _add_lane_target(closeout)
    _set(closeout, "codex.closeout")

    cleanup = codex_sub.add_parser("cleanup", help="Remove a safe managed worktree")
    _add_lane_target(cleanup)
    cleanup.add_argument("--delete-branch", action="store_true")
    cleanup.add_argument("--confirm-thread-inactive", action="store_true")
    _set(cleanup, "codex.cleanup")

    wait = codex_sub.add_parser("wait", help="Wait for one task turn")
    _add_lane_target(wait)
    _add_observe_options(wait)
    _set(wait, "codex.wait")

    watch = codex_sub.add_parser("watch", help="Emit polling snapshots as JSONL")
    _add_lane_target(watch)
    _add_observe_options(watch)
    _set(watch, "codex.watch", jsonl_output=True)

    checkpoint = codex_sub.add_parser(
        "checkpoint", help="Wait once and return a lane snapshot"
    )
    _add_lane_target(checkpoint)
    checkpoint.add_argument("--after", dest="after_seconds", type=float, default=300.0)
    _set(checkpoint, "codex.checkpoint")


def _add_goal_commands(codex_sub: argparse._SubParsersAction) -> None:
    goal = codex_sub.add_parser("goal", help="Manage Codex task goals")
    goal_sub = goal.add_subparsers(
        dest="goal_command",
        required=True,
        parser_class=V1ArgumentParser,
    )

    set_command = goal_sub.add_parser("set")
    _add_lane_target(set_command, required=False)
    set_command.add_argument("--cwd")
    set_command.add_argument("--title")
    _add_sandbox(set_command)
    _add_commit_signing(set_command)
    set_command.add_argument("--objective", required=True)
    set_command.add_argument(
        "--status",
        choices=(
            "active",
            "paused",
            "blocked",
            "usageLimited",
            "budgetLimited",
            "complete",
        ),
        default="active",
    )
    set_command.add_argument("--token-budget", type=int)
    _set(set_command, "codex.goal.set")

    run = goal_sub.add_parser("run")
    _add_lane_target(run)
    _add_sandbox(run)
    _add_runtime_options(run)
    _add_commit_signing(run)
    _add_signing_replacement_authorization(run)
    run.add_argument("--turn-timeout", type=float)
    run.add_argument("--max-runtime", type=float)
    run.add_argument("--max-turns", type=int)
    _set(run, "codex.goal.run")

    for operation in ("get", "complete", "clear"):
        command = goal_sub.add_parser(operation)
        _add_lane_target(command)
        _set(command, f"codex.goal.{operation}")


def _add_session_commands(codex_sub: argparse._SubParsersAction) -> None:
    session = codex_sub.add_parser("session", help="Access Codex sessions")
    session_sub = session.add_subparsers(
        dest="session_command",
        required=True,
        parser_class=V1ArgumentParser,
    )

    list_command = session_sub.add_parser("list")
    _add_session_query_options(list_command)
    _set(list_command, "codex.session.list")

    find = session_sub.add_parser("find")
    find.add_argument("query")
    _add_session_query_options(find)
    _set(find, "codex.session.find")

    attach = session_sub.add_parser(
        "attach",
        help="Explicitly bind an existing Codex task for control",
    )
    attach.add_argument("--lane-id", help="Optional internal stable lane identifier")
    _add_alias_root(attach)
    attach.add_argument("--thread-id", required=True)
    attach.add_argument("--mode", choices=EXECUTION_MODES)
    attach.add_argument("--cwd")
    attach.add_argument("--title")
    _add_sandbox(attach)
    _set(attach, "codex.session.attach")

    name = session_sub.add_parser("name")
    name_sub = name.add_subparsers(
        dest="name_command",
        required=True,
        parser_class=V1ArgumentParser,
    )
    get = name_sub.add_parser("get")
    _add_thread_target(get)
    get.add_argument("--observe", choices=("stored", "live"), default="stored")
    _set(get, "codex.session.name.get")
    set_command = name_sub.add_parser("set")
    _add_lane_target(set_command)
    set_command.add_argument("--title", required=True)
    set_command.add_argument("--expected-title")
    _set(set_command, "codex.session.name.set")

    outline = session_sub.add_parser("outline")
    _add_thread_target(outline)
    outline.add_argument(
        "--observe", choices=("stored", "live"), default="stored"
    )
    _set(outline, "codex.session.outline")

    read = session_sub.add_parser("read")
    _add_thread_target(read)
    read.add_argument("--observe", choices=("stored", "live"), default="stored")
    read_scope = read.add_mutually_exclusive_group()
    read_scope.add_argument("--include-turns", action="store_true")
    read_scope.add_argument("--turn-id")
    read_scope.add_argument("--turn-index", type=int)
    _set(read, "codex.session.read")


def _add_session_query_options(parser: argparse.ArgumentParser) -> None:
    _add_alias_root(parser)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--scope", choices=("all", "lanes"), default="all")
    parser.add_argument("--threads", choices=("main", "all"), default="main")
    parser.add_argument("--observe", choices=("stored", "live"), default="stored")
    parser.add_argument("--detail", choices=("metadata", "summary"), default="summary")


def _add_lane_target(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
) -> None:
    target = parser.add_mutually_exclusive_group(required=required)
    target.add_argument(
        "--lane-id",
        help="Internal stable lane identifier (optional for normal use)",
    )
    target.add_argument("--thread-id", help="Exact Codex thread/session identifier")
    target.add_argument("--target-title", help="Exact known task title")
    target.add_argument(
        "--current",
        action="store_true",
        help="Resolve the only task bound to the current working directory",
    )
    _add_alias_root(parser)


def _add_thread_target(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--lane-id")
    target.add_argument("--thread-id")
    target.add_argument("--target-title")
    target.add_argument("--current", action="store_true")
    _add_alias_root(parser)


def _add_sandbox(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sandbox", choices=SANDBOX_MODES)


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model")
    parser.add_argument("--profile")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--effort")
    parser.add_argument(
        "--config",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
    )


def _add_worktree(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree",
        nargs="?",
        const="auto",
        choices=("auto",),
    )


def _add_observe_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)


def _add_commit_signing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--commit-signing",
        choices=COMMIT_SIGNING_MODES,
        help="Beta managed SSH commit signing; default: off",
    )


def _add_signing_replacement_authorization(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allow-signing-replacement", action="store_true")


def _add_alias_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alias-root", default=str(DEFAULT_ALIAS_ROOT))


def _add_prompt(parser: argparse.ArgumentParser) -> None:
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")


def _set(
    parser: argparse.ArgumentParser,
    handler: str,
    *,
    jsonl_output: bool = False,
) -> None:
    parser.set_defaults(
        handler=handler,
        command_name=handler,
        provider="codex",
        jsonl_output=jsonl_output,
    )
