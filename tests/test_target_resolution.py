import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from agent_lane.state import load_alias, save_alias
from cli_result import decode_cli_output


class TargetCodex:
    transport = "stdio"
    threads: dict[str, dict] = {}
    started = 0
    turn_calls = 0

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def start_thread(self, cwd, **_kwargs):
        type(self).started += 1
        thread_id = f"thread-new-{type(self).started}"
        type(self).threads[thread_id] = {
            "id": thread_id,
            "name": None,
            "cwd": cwd,
            "status": {"type": "idle"},
            "turns": [],
        }
        return thread_id

    def read_thread(self, thread_id, include_turns=False):
        source = type(self).threads.get(thread_id)
        if source is None:
            source = {
                "id": thread_id,
                "name": "Selected task",
                "cwd": None,
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-old",
                        "status": "completed",
                        "items": [],
                    }
                ],
            }
        thread = dict(source)
        if not include_turns:
            thread.pop("turns", None)
        return {"thread": thread}

    def set_thread_name(self, thread_id, title):
        type(self).threads.setdefault(thread_id, {"id": thread_id})["name"] = title

    def update_git_info(self, _thread_id, _git_info):
        return None

    def resume_thread(self, _thread_id, **_kwargs):
        return {}

    def run_turn(self, thread_id, _prompt, *, on_started=None, **_kwargs):
        type(self).turn_calls += 1
        if on_started:
            on_started("turn-new")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-new",
            status="completed",
            final_text="done",
            events=["turn/completed"],
        )

    def get_goal(self, _thread_id):
        return None

    def set_goal(self, _thread_id, **kwargs):
        return {"goal": dict(kwargs)}

    def clear_goal(self, _thread_id):
        return {"cleared": True}


def _reset_fake(monkeypatch):
    TargetCodex.threads = {}
    TargetCodex.started = 0
    TargetCodex.turn_calls = 0
    monkeypatch.setattr(cli, "CodexAppServer", TargetCodex)


def _save_target_alias(alias_root, lane_id, thread_id, cwd, title):
    save_alias(
        "codex",
        lane_id,
        {
            "codex_thread_id": thread_id,
            "cwd": str(cwd),
            "codex_title": title,
            "custom_title": title,
            "execution_mode": "independent",
            "execution_mode_source": "explicit",
            "commit_signing": {"mode": "off"},
            "last_status": "completed",
        },
        alias_root,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "run", "--cwd", "/path/to/project", "--prompt", "start"],
        ["codex", "send", "--thread-id", "thread-1", "--prompt", "continue"],
        ["codex", "status", "--target-title", "Exact title"],
        ["codex", "wait", "--current", "--timeout", "0"],
        ["codex", "checkpoint", "--thread-id", "thread-1", "--after", "0"],
        ["codex", "closeout", "--thread-id", "thread-1"],
        ["codex", "goal", "set", "--cwd", "/path/to/project", "--objective", "finish"],
        ["codex", "goal", "get", "--thread-id", "thread-1"],
        ["codex", "session", "attach", "--thread-id", "thread-1"],
        ["codex", "session", "read", "--target-title", "Exact title"],
        ["codex", "session", "outline", "--current"],
        ["codex", "session", "name", "get", "--thread-id", "thread-1"],
        [
            "codex",
            "session",
            "name",
            "set",
            "--thread-id",
            "thread-1",
            "--title",
            "New title",
        ],
    ],
)
def test_user_facing_target_forms_parse(argv):
    assert build_parser().parse_args(argv).handler


def test_run_without_lane_id_generates_internal_identity(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--cwd",
            str(workspace),
            "--alias-root",
            str(aliases),
            "--prompt",
            "start",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"].startswith("task-")
    assert result["target_resolution"]["source"] == "generated_internal"
    assert result["target_resolution"]["user_supplied_lane_id"] is False
    assert result["target_resolution"]["resolved"] == {
        "lane_id": result["lane_id"],
        "thread_id": result["codex_thread_id"],
    }
    alias = load_alias("codex", result["lane_id"], aliases)
    assert alias is not None
    assert "custom_title" not in alias
    assert alias["codex_title"] == "workspace"
    assert result["lane_title"] == "workspace"
    assert result["lane_title_source"] == "codex_title"


def test_session_attach_without_lane_id_creates_stable_internal_binding(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    TargetCodex.threads["thread-app"] = {
        "id": "thread-app",
        "name": "App task",
        "cwd": str(workspace),
        "status": {"type": "idle"},
        "turns": [],
    }
    aliases = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "session",
            "attach",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"].startswith("session-thread-app-")
    assert result["target_resolution"]["source"] == "generated_for_attach"
    assert result["control"]["send_target_argv"] == [
        "codex",
        "send",
        "--thread-id",
        "thread-app",
        "--alias-root",
        str(aliases),
    ]
    assert TargetCodex.turn_calls == 0

    second_rc = main(
        [
            "codex",
            "session",
            "attach",
            "--thread-id",
            "thread-app",
            "--alias-root",
            str(aliases),
        ]
    )
    second = decode_cli_output(capsys.readouterr().out)

    assert second_rc == 0, second
    assert second["lane_id"] == result["lane_id"]
    assert second["adopted"] is False
    assert second["target_resolution"]["source"] == "thread_binding"

    send_rc = main(
        [*result["control"]["send_target_argv"], "--prompt", "continue"]
    )
    sent = decode_cli_output(capsys.readouterr().out)

    assert send_rc == 0, sent
    assert sent["lane_id"] == result["lane_id"]
    assert sent["target_resolution"]["source"] == "thread_binding"


def test_send_resolves_an_attached_thread_without_lane_id(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    _save_target_alias(aliases, "internal-lane", "thread-1", workspace, "Task")

    rc = main(
        [
            "codex",
            "send",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"] == "internal-lane"
    assert result["target_resolution"]["source"] == "thread_binding"
    assert result["target_resolution"]["requested"] == {
        "kind": "thread_id",
        "value": "thread-1",
    }


def test_run_by_attached_thread_preserves_create_or_resume_semantics(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    _save_target_alias(aliases, "internal-lane", "thread-1", workspace, "Task")

    rc = main(
        [
            "codex",
            "run",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["resumed"] is True
    assert result["lane_id"] == "internal-lane"
    assert result["target_resolution"]["source"] == "thread_binding"
    assert TargetCodex.started == 0


def test_goal_set_without_lane_id_generates_internal_identity(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "goal",
            "set",
            "--cwd",
            str(workspace),
            "--title",
            "Goal task",
            "--objective",
            "finish",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"].startswith("task-")
    assert result["target_resolution"]["source"] == "generated_internal"
    assert result["target_resolution"]["resolved"] == {
        "lane_id": result["lane_id"],
        "thread_id": result["codex_thread_id"],
    }
    assert result["goal"]["objective"] == "finish"
    assert result["custom_title"] == "Goal task"
    assert result["lane_title"] == "Goal task"
    assert result["lane_title_source"] == "custom_title"


def test_session_name_set_resolves_attached_thread_without_lane_id(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    _save_target_alias(aliases, "internal-lane", "thread-1", workspace, "Task")
    TargetCodex.threads["thread-1"] = {
        "id": "thread-1",
        "name": "Task",
        "cwd": str(workspace),
        "status": {"type": "idle"},
        "turns": [],
    }

    rc = main(
        [
            "codex",
            "session",
            "name",
            "set",
            "--thread-id",
            "thread-1",
            "--title",
            "Renamed task",
            "--alias-root",
            str(aliases),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["codex_title"] == "Renamed task"
    assert result["target_resolution"]["source"] == "thread_binding"


def test_unbound_send_fails_closed_with_lane_free_attach_route(
    tmp_path, monkeypatch, capsys
):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("unbound target must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    rc = main(
        [
            "codex",
            "send",
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_ATTACH_REQUIRED"
    assert result["control_created"] is False
    assert result["attach_argv"] == [
        "codex",
        "session",
        "attach",
        "--thread-id",
        "thread-unbound",
        "--alias-root",
        str(tmp_path),
    ]
    assert result["after_attach_argv"] == [
        "codex",
        "send",
        "--thread-id",
        "thread-unbound",
        "--alias-root",
        str(tmp_path),
        "--prompt",
        "continue",
    ]
    assert build_parser().parse_args(result["after_attach_argv"]).handler == (
        "codex.send"
    )


def test_custom_alias_root_attach_route_executes_end_to_end(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    TargetCodex.threads["thread-unbound"] = {
        "id": "thread-unbound",
        "name": "App task",
        "cwd": str(workspace),
        "status": {"type": "idle"},
        "turns": [],
    }

    first_rc = main(
        [
            "codex",
            "send",
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )
    first = decode_cli_output(capsys.readouterr().out)

    assert first_rc == 1
    attach_rc = main(first["attach_argv"])
    attached = decode_cli_output(capsys.readouterr().out)
    assert attach_rc == 0, attached
    assert load_alias("codex", attached["lane_id"], aliases) is not None

    send_rc = main(first["after_attach_argv"])
    sent = decode_cli_output(capsys.readouterr().out)
    assert send_rc == 0, sent
    assert sent["lane_id"] == attached["lane_id"]
    assert sent["codex_thread_id"] == "thread-unbound"


@pytest.mark.parametrize(
    "command,extra",
    [
        (["codex", "run"], ["--prompt", "continue"]),
        (["codex", "steer"], ["--prompt", "continue"]),
        (["codex", "cleanup"], []),
        (["codex", "goal", "run"], []),
        (["codex", "goal", "complete"], []),
        (["codex", "goal", "clear"], []),
        (
            ["codex", "goal", "set"],
            ["--objective", "finish"],
        ),
        (
            ["codex", "session", "name", "set"],
            ["--title", "Renamed"],
        ),
    ],
)
def test_unbound_control_commands_require_separate_explicit_attach(
    command, extra, tmp_path, monkeypatch, capsys
):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("unbound control must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    rc = main(
        [
            *command,
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
            *extra,
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_ATTACH_REQUIRED"
    assert result["control_created"] is False
    expected_attach_argv = [
        "codex",
        "session",
        "attach",
        "--thread-id",
        "thread-unbound",
    ]
    if command == ["codex", "steer"]:
        expected_attach_argv.extend(["--mode", "app-sync"])
    expected_attach_argv.extend(["--alias-root", str(tmp_path)])
    assert result["attach_argv"] == expected_attach_argv
    assert result["after_attach_argv"] == [
        *command,
        "--thread-id",
        "thread-unbound",
        "--alias-root",
        str(tmp_path),
        *extra,
    ]


def test_exact_title_ambiguity_fails_closed_with_thread_choices(
    tmp_path, monkeypatch, capsys
):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("ambiguous target must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_target_alias(tmp_path, "lane-a", "thread-a", workspace, "Same title")
    _save_target_alias(tmp_path, "lane-b", "thread-b", workspace, "Same title")

    rc = main(
        [
            "codex",
            "status",
            "--target-title",
            "Same title",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_AMBIGUOUS"
    assert [choice["thread_id"] for choice in result["choices"]] == [
        "thread-a",
        "thread-b",
    ]
    assert all(
        choice["target_argv"][-2:] == ["--alias-root", str(tmp_path)]
        for choice in result["choices"]
    )


def test_current_context_ambiguity_fails_closed(tmp_path, monkeypatch, capsys):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("ambiguous target must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    monkeypatch.chdir(tmp_path)
    _save_target_alias(tmp_path, "lane-a", "thread-a", tmp_path, "Task A")
    _save_target_alias(tmp_path, "lane-b", "thread-b", tmp_path, "Task B")

    rc = main(
        [
            "codex",
            "status",
            "--current",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_AMBIGUOUS"
    assert result["requested_target"] == {"kind": "current", "value": str(tmp_path)}
    assert len(result["choices"]) == 2


def test_unique_current_context_resolves_without_lane_id(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _save_target_alias(tmp_path, "internal-lane", "thread-1", tmp_path, "Task")

    rc = main(
        ["codex", "status", "--current", "--alias-root", str(tmp_path)]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"] == "internal-lane"
    assert result["target_resolution"]["source"] == "current_cwd"


def test_removed_legacy_title_is_not_a_target_selector(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias_dir = tmp_path / "codex"
    alias_dir.mkdir()
    (alias_dir / "legacy-task.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "codex_thread_id": "thread-legacy",
                "cwd": str(workspace),
                "title": "Legacy title",
                "execution_mode": "independent",
                "commit_signing": {"mode": "off"},
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "codex",
            "status",
            "--target-title",
            "legacy title",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_NOT_FOUND"


def test_invalid_alias_registry_fails_closed_before_target_selection(
    tmp_path, monkeypatch, capsys
):
    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid registry must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_target_alias(tmp_path, "valid", "thread-1", workspace, "Task")
    (tmp_path / "codex" / "broken.json").write_text("{", encoding="utf-8")

    rc = main(
        [
            "codex",
            "status",
            "--target-title",
            "Task",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_REGISTRY_INVALID"
    assert result["invalid_entries"][0]["path"].endswith("broken.json")


def test_exact_thread_read_ignores_unrelated_invalid_alias(
    tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "broken.json").write_text("{", encoding="utf-8")

    rc = main(
        [
            "codex",
            "status",
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"] is None
    assert result["codex_thread_id"] == "thread-unbound"
    assert result["target_resolution"]["source"] == "unbound_thread_read_only"


def test_explicit_unreadable_lane_target_returns_typed_registry_error(
    tmp_path, capsys
):
    alias_dir = tmp_path / "codex"
    alias_dir.mkdir()
    (alias_dir / "broken.json").write_text("{", encoding="utf-8")

    rc = main(
        [
            "codex",
            "session",
            "read",
            "--lane-id",
            "broken",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_REGISTRY_INVALID"
    assert result["invalid_entries"][0]["path"].endswith("broken.json")


@pytest.mark.parametrize(
    "target_args",
    [["--thread-id", "thread-1"], ["--lane-id", "internal-lane"]],
)
def test_binding_change_after_resolution_fails_closed(
    target_args, tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _save_target_alias(tmp_path, "internal-lane", "thread-1", workspace, "Task")

    @contextmanager
    def changing_lock(_root, _key, **_kwargs):
        save_alias(
            "codex",
            "internal-lane",
            {
                "codex_thread_id": "thread-2",
                "cwd": str(workspace),
                "execution_mode": "independent",
                "commit_signing": {"mode": "off"},
            },
            tmp_path,
        )
        yield

    monkeypatch.setattr(cli, "operation_lock", changing_lock)

    rc = main(
        [
            "codex",
            "send",
            *target_args,
            "--alias-root",
            str(tmp_path),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_TARGET_CHANGED"
    assert result["expected_thread_id"] == "thread-1"
    assert result["observed_thread_id"] == "thread-2"


@pytest.mark.parametrize(
    "command,extra",
    [
        (["codex", "status"], []),
        (["codex", "wait"], ["--timeout", "0"]),
        (["codex", "watch"], ["--timeout", "0"]),
        (["codex", "checkpoint"], ["--after", "0"]),
        (["codex", "closeout"], []),
    ],
)
def test_read_only_execution_commands_accept_unbound_thread_without_attach(
    command, extra, tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)

    rc = main(
        [
            *command,
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
            *extra,
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"] is None
    assert result["codex_thread_id"] == "thread-unbound"
    assert result["control"]["requires_explicit_attach"] is True
    assert list((tmp_path / "codex").glob("*.json")) == []


@pytest.mark.parametrize(
    "command",
    [
        ["codex", "session", "name", "get"],
        ["codex", "session", "outline"],
        ["codex", "session", "read"],
    ],
)
def test_session_inspection_accepts_unbound_exact_thread_without_attach(
    command, tmp_path, monkeypatch, capsys
):
    _reset_fake(monkeypatch)

    rc = main(
        [
            *command,
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["target_resolution"]["resolved"]["thread_id"] == (
        "thread-unbound"
    )
    assert list((tmp_path / "codex").glob("*.json")) == []


def test_goal_get_accepts_unbound_thread_read_only(tmp_path, monkeypatch, capsys):
    _reset_fake(monkeypatch)

    rc = main(
        [
            "codex",
            "goal",
            "get",
            "--thread-id",
            "thread-unbound",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["lane_id"] is None
    assert result["codex_thread_id"] == "thread-unbound"
    assert result["control"]["requires_explicit_attach"] is True
