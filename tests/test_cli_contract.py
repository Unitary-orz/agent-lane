import json

import pytest

from agent_lane import __version__
import agent_lane.entry as entry
from agent_lane.cli import build_parser, main


@pytest.mark.parametrize(
    "argv",
    [
        ["signing", "init", "--generate"],
        ["config", "app-sync", "enable"],
        ["config", "app-sync", "status"],
        ["config", "effort", "set", "xh"],
        ["config", "effort", "status"],
        ["config", "effort", "clear"],
        ["doctor", "--mode", "app-sync", "--probe"],
        [
            "codex",
            "run",
            "--lane-id",
            "api-refactor",
            "--cwd",
            "/path/to/project",
            "--prompt",
            "Refactor the API client.",
        ],
        [
            "codex",
            "send",
            "--lane-id",
            "api-refactor",
            "--prompt",
            "Continue.",
        ],
        ["codex", "status", "--lane-id", "api-refactor"],
        ["codex", "wait", "--lane-id", "api-refactor", "--timeout", "600"],
        ["codex", "closeout", "--lane-id", "api-refactor"],
        [
            "codex",
            "steer",
            "--lane-id",
            "collaborative-review",
            "--prompt",
            "Check compatibility.",
        ],
        ["codex", "session", "list", "--scope", "all", "--threads", "main"],
        ["codex", "session", "find", "refactor", "--observe", "live"],
        [
            "codex",
            "session",
            "outline",
            "--thread-id",
            "task-id",
            "--observe",
            "live",
        ],
        [
            "codex",
            "session",
            "read",
            "--thread-id",
            "task-id",
            "--include-turns",
        ],
        [
            "codex",
            "session",
            "attach",
            "--lane-id",
            "imported-task",
            "--thread-id",
            "task-id",
        ],
        ["codex", "session", "name", "get", "--lane-id", "imported-task"],
        [
            "codex",
            "session",
            "name",
            "set",
            "--lane-id",
            "imported-task",
            "--title",
            "API cleanup",
        ],
        [
            "codex",
            "goal",
            "set",
            "--lane-id",
            "migration",
            "--cwd",
            "/path/to/project",
            "--objective",
            "Complete the migration.",
        ],
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "migration",
            "--max-turns",
            "20",
            "--max-runtime",
            "7200",
        ],
        [
            "codex",
            "cleanup",
            "--lane-id",
            "isolated-fix",
            "--confirm-thread-inactive",
        ],
    ],
)
def test_documented_v1_examples_parse(argv):
    args = build_parser().parse_args(argv)

    assert args.handler


def test_root_help_contains_only_public_v1_surfaces(capsys):
    try:
        build_parser().parse_args(["--help"])
    except SystemExit as exc:
        assert exc.code == 0

    output = capsys.readouterr().out
    assert "{doctor,config,signing,codex}" in output
    assert "_app-sync-login" not in output
    assert "refresh" not in output


def test_root_version_reports_package_version(capsys):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out == f"agent-lane {__version__}\n"


def test_commit_signing_help_marks_beta_and_defaults_off(capsys):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(
            ["codex", "run", "--help"]
        )

    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "--commit-signing {off,agent}" in output
    assert "Beta managed SSH commit signing; default: off" in output


def test_usage_failure_is_one_json_envelope(capsys):
    rc = main(["codex", "run"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload == {
        "schema_version": 1,
        "ok": False,
        "command": "codex.run",
        "data": None,
        "error": {
            "code": "CLI_USAGE_ERROR",
            "message": "one of the arguments --prompt --prompt-file is required",
            "retryable": False,
            "details": {},
        },
        "warnings": [],
    }


def test_removed_navigation_returns_migration_error(capsys):
    rc = main(["codex", "refresh", "--lane-id", "lane-1"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "CLI_REMOVED"
    assert payload["error"]["details"]["replacement"] is None
    assert "App page navigation" in payload["error"]["message"]


def test_handler_key_error_is_not_misreported_as_missing_handler(
    monkeypatch, capsys
):
    def broken(_args):
        raise KeyError("domain-key")

    monkeypatch.setattr(entry, "command_handlers", lambda: {"doctor": broken})

    rc = main(["doctor"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error"]["code"] == "AGENT_LANE_UNEXPECTED_ERROR"
    assert payload["error"]["details"]["exception_type"] == "KeyError"
