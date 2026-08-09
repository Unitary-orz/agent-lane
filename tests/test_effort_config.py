import json
from types import SimpleNamespace

import agent_lane.control_plane as cli
from agent_lane.cli import main
from agent_lane.state import load_alias, save_alias
from cli_result import decode_cli_output


class EffortCodex:
    efforts = []

    def __init__(self, *_args, **_kwargs):
        self.transport = "stdio"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        return {
            "thread": {
                "id": thread_id,
                "name": "Effort lane",
                "cwd": None,
                "status": {"type": "idle"},
                "turns": [] if include_turns else None,
            }
        }

    def resume_thread(self, _thread_id, **_kwargs):
        return {}

    def run_turn(
        self,
        thread_id,
        _prompt,
        *,
        effort=None,
        on_started=None,
        **_kwargs,
    ):
        type(self).efforts.append(effort)
        if on_started:
            on_started("turn-1")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-1",
            status="completed",
            final_text="done",
            events=["turn/completed"],
        )

    def get_goal(self, _thread_id):
        return None


def _configure_effort(path, value, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(path))
    rc = main(["config", "effort", "set", value])
    result = decode_cli_output(capsys.readouterr().out)
    assert rc == 0, result
    return result


def _send(tmp_path, monkeypatch, capsys, *extra, alias_extra=None):
    aliases = tmp_path / "aliases"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    save_alias(
        "codex",
        "effort-lane",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(workspace),
            "execution_mode": "independent",
            "execution_mode_source": "explicit",
            "commit_signing": {"mode": "off"},
            **(alias_extra or {}),
        },
        aliases,
    )
    EffortCodex.efforts = []
    monkeypatch.setattr(cli, "CodexAppServer", EffortCodex)
    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "effort-lane",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
            *extra,
        ]
    )
    return rc, decode_cli_output(capsys.readouterr().out), aliases


def test_user_default_effort_accepts_xh_and_is_reported(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.json"

    configured = _configure_effort(config_path, "xh", monkeypatch, capsys)

    assert configured["effective_effort"] == "xhigh"
    assert configured["effective_effort_source"] == "user_config"
    assert configured["config_path"] == str(config_path)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "defaults": {"effort": "xhigh"},
        "schema_version": 1,
    }

    rc, result, aliases = _send(tmp_path, monkeypatch, capsys)

    assert rc == 0, result
    assert EffortCodex.efforts == ["xhigh"]
    assert result["effort"] == "xhigh"
    assert result["effective_effort"] == "xhigh"
    assert result["effective_effort_source"] == "user_config"
    alias = load_alias("codex", "effort-lane", aliases)
    assert alias["effective_effort"] == "xhigh"
    assert alias["effective_effort_source"] == "user_config"


def test_explicit_effort_overrides_user_default_and_xh_alias_normalizes(
    tmp_path, monkeypatch, capsys
):
    _configure_effort(tmp_path / "config.json", "medium", monkeypatch, capsys)

    rc, result, _aliases = _send(
        tmp_path,
        monkeypatch,
        capsys,
        "--effort",
        "xh",
    )

    assert rc == 0, result
    assert EffortCodex.efforts == ["xhigh"]
    assert result["effective_effort"] == "xhigh"
    assert result["effective_effort_source"] == "explicit"


def test_legacy_user_config_and_old_alias_are_read_without_becoming_precedence(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"effort": "xh"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc, result, aliases = _send(
        tmp_path,
        monkeypatch,
        capsys,
        alias_extra={
            "requested_effort": "low",
            "requested_effort_source": "explicit",
        },
    )

    assert rc == 0, result
    assert EffortCodex.efforts == ["xhigh"]
    assert result["effective_effort"] == "xhigh"
    assert result["effective_effort_source"] == "user_config_legacy"
    alias = load_alias("codex", "effort-lane", aliases)
    assert alias["effective_effort_source"] == "user_config_legacy"


def test_conflicting_legacy_and_current_effort_config_fails_closed(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {"effort": "high"},
                "effort": "low",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc = main(["config", "effort", "status"])
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "USER_CONFIG_INVALID"
    assert "disagree" in result["error"]


def test_effort_set_repairs_conflicting_effort_fields(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {"effort": "high", "other": True},
                "effort": "low",
                "unrelated": "preserved",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc = main(["config", "effort", "set", "xh"])
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["effective_effort"] == "xhigh"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "defaults": {"effort": "xhigh", "other": True},
        "unrelated": "preserved",
    }


def test_effort_clear_repairs_invalid_effort_value(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "defaults": {"effort": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc = main(["config", "effort", "clear"])
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, result
    assert result["effective_effort"] is None
    assert result["effective_effort_source"] == "unset"


def test_invalid_user_effort_config_fails_closed_before_codex(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"defaults":{"effort":[]}}', encoding="utf-8")
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid config must fail before app-server")

    monkeypatch.setattr(cli, "CodexAppServer", UnexpectedCodex)
    aliases = tmp_path / "aliases"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_alias(
        "codex",
        "effort-lane",
        {"codex_thread_id": "thread-1", "cwd": str(workspace)},
        aliases,
    )

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "effort-lane",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "USER_CONFIG_INVALID"
    assert result["config_path"] == str(config_path)


def test_explicit_effort_wins_without_reading_invalid_user_default(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"defaults":{"effort":[]}}', encoding="utf-8")
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc, result, _aliases = _send(
        tmp_path,
        monkeypatch,
        capsys,
        "--effort",
        "high",
    )

    assert rc == 0, result
    assert EffortCodex.efforts == ["high"]
    assert result["effective_effort"] == "high"
    assert result["effective_effort_source"] == "explicit"


def test_effort_set_reports_unwritable_config_path(tmp_path, monkeypatch, capsys):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    config_path = parent_file / "config.json"
    monkeypatch.setenv("AGENT_LANE_CONFIG_PATH", str(config_path))

    rc = main(["config", "effort", "set", "high"])
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "USER_CONFIG_WRITE_FAILED"
    assert result["retryable"] is False
    assert result["config_path"] == str(config_path)


def test_effort_clear_restores_unset_source(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.json"
    _configure_effort(config_path, "high", monkeypatch, capsys)

    rc = main(["config", "effort", "clear"])
    cleared = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, cleared
    assert cleared["effective_effort"] is None
    assert cleared["effective_effort_source"] == "unset"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "defaults": {},
        "schema_version": 1,
    }
