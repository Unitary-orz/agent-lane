import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.state import load_alias, save_alias
from agent_lane.workspace import WorkspaceError


class FakeNameCodex:
    current_name = "App title"
    confirm_set = True
    set_calls = []
    init_kwargs = []

    def __init__(self, *_args, **kwargs):
        type(self).init_kwargs.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        del include_turns
        return {
            "thread": {
                "id": thread_id,
                "name": type(self).current_name,
                "status": {"type": "idle"},
            }
        }

    def set_thread_name(self, thread_id, name):
        type(self).set_calls.append((thread_id, name))
        if type(self).confirm_set:
            type(self).current_name = name

    def get_goal(self, _thread_id):
        return None


def _seed_alias(tmp_path, *, custom_title=None, codex_title=None):
    data = {
        "codex_thread_id": "thread-1",
        "title": "Removed legacy title",
        "title_source": "legacy",
        "lane_label": "Removed lane label",
        "lane_title": "Removed computed title",
        "lane_title_source": "title",
        "cwd": str(tmp_path),
    }
    if custom_title is not None:
        data["custom_title"] = custom_title
    if codex_title is not None:
        data["codex_title"] = codex_title
    save_alias(
        "codex",
        "lane-1",
        data,
        tmp_path,
    )


def _reset_fake(*, name="App title", confirm_set=True):
    FakeNameCodex.current_name = name
    FakeNameCodex.confirm_set = confirm_set
    FakeNameCodex.set_calls = []
    FakeNameCodex.init_kwargs = []


def test_name_get_uses_codex_title_and_removes_legacy_title_fields(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path, codex_title="App title")
    _reset_fake()
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex", "session", "name",
            "get",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert rc == 0
    assert FakeNameCodex.init_kwargs == [{}]
    assert result["lane_title"] == "App title"
    assert result["lane_title_source"] == "codex_title"
    assert result["custom_title"] is None
    assert result["codex_title_observation"] == "live"
    assert result["binding_generation"] == 1
    assert result["binding_origin"] == "legacy"
    assert result["lineage_complete"] is False
    assert alias["schema_version"] == 4
    assert alias["execution_mode"] == "independent"
    assert "lane_label" not in alias
    assert "title" not in alias
    assert "title_source" not in alias
    assert "lane_title" not in alias
    assert "lane_title_source" not in alias
    assert "custom_title" not in alias
    assert alias["codex_title"] == "App title"
    assert alias["binding"] == {
        "generation": 1,
        "thread_id": "thread-1",
        "bound_at": alias["binding"]["bound_at"],
        "origin": "legacy",
        "execution_mode": "independent",
        "execution_mode_source": "legacy-default",
    }
    assert alias["binding_history"] == []
    assert alias["lineage_complete"] is False


def test_name_get_live_observation_forces_daemon_for_lane_target(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    _reset_fake()
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex",
            "session",
            "name",
            "get",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--observe",
            "live",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert FakeNameCodex.init_kwargs == [{"transport": "daemon"}]
    assert result["observation_mode"] == "live"
    assert result["live_status_authoritative"] is True


def test_name_get_live_observation_forces_daemon_for_thread_target(
    tmp_path, monkeypatch, capsys
):
    _reset_fake()
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex",
            "session",
            "name",
            "get",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
            "--observe",
            "live",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert FakeNameCodex.init_kwargs == [{"transport": "daemon"}]
    assert result["observation_mode"] == "live"


def test_name_get_falls_back_to_lane_id_when_codex_name_is_empty(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    _reset_fake(name=None)
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex", "session", "name",
            "get",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert result["codex_title"] is None
    assert result["codex_title_observation"] == "live"
    assert result["lane_title"] == "lane-1"
    assert result["lane_title_source"] == "lane_id"


def test_name_set_checks_optional_precondition_before_remote_write(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    _reset_fake(name="Human rename")
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex", "session", "name",
            "set",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--title",
            "Agent rename",
            "--expected-title",
            "Old title",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert rc == 1
    assert result["error_code"] == "CODEX_NAME_CONFLICT"
    assert result["observed_title"] == "Human rename"
    assert FakeNameCodex.set_calls == []
    assert alias["codex_title"] == "Human rename"


def test_name_set_writes_reads_back_and_reports_codex_title(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    _reset_fake()
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex", "session", "name",
            "set",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--title",
            "Renamed in Codex",
            "--expected-title",
            "App title",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert rc == 0
    assert FakeNameCodex.set_calls == [("thread-1", "Renamed in Codex")]
    assert result["renamed"] is True
    assert result["previous_codex_title"] == "App title"
    assert result["lane_title"] == "Renamed in Codex"
    assert result["lane_title_source"] == "codex_title"
    assert alias["codex_title"] == "Renamed in Codex"
    assert "custom_title" not in alias


def test_name_set_fails_when_readback_does_not_match(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    _reset_fake(confirm_set=False)
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex", "session", "name",
            "set",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--title",
            "Unconfirmed title",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 1
    assert result["error_code"] == "CODEX_NAME_READBACK_MISMATCH"
    assert result["requested_title"] == "Unconfirmed title"
    assert result["observed_title"] == "App title"
    assert result["retryable"] is True


def test_status_uses_live_codex_title_without_mutating_alias(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path)
    alias_path = tmp_path / "codex" / "lane-1.json"
    before = alias_path.read_text(encoding="utf-8")
    _reset_fake(name="Renamed in App")
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--detail",
            "full",
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert result["lane_title"] == "Renamed in App"
    assert result["lane_title_source"] == "codex_title"
    assert result["codex_title_observation"] == "live"
    assert not {
        "title",
        "title_source",
        "lane_label",
        "lane_title",
        "lane_title_source",
    }.intersection(result["alias"])
    assert alias_path.read_text(encoding="utf-8") == before


def test_name_get_shares_lane_lock_with_custom_title_writes(tmp_path):
    _seed_alias(tmp_path)
    name_get = build_parser().parse_args(
        [
            "codex",
            "session",
            "name",
            "get",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    custom_title_set = build_parser().parse_args(
        [
            "codex",
            "custom-title",
            "set",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--title",
            "Pinned lane title",
        ]
    )

    with cli._command_locks(name_get):
        with pytest.raises(WorkspaceError) as caught:
            with cli._command_locks(custom_title_set):
                pass

    assert caught.value.error_code == "LANE_OPERATION_BUSY"


def test_status_keeps_explicit_custom_title_over_live_codex_rename(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path, custom_title="Pinned lane title")
    alias_path = tmp_path / "codex" / "lane-1.json"
    before = alias_path.read_text(encoding="utf-8")
    _reset_fake(name="Renamed in App")
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    rc = main(
        [
            "codex",
            "status",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    result = decode_cli_output(capsys.readouterr().out)

    assert rc == 0
    assert result["lane_title"] == "Pinned lane title"
    assert result["lane_title_source"] == "custom_title"
    assert result["custom_title"] == "Pinned lane title"
    assert result["codex_title"] == "Renamed in App"
    assert result["codex_title_observation"] == "live"
    assert alias_path.read_text(encoding="utf-8") == before


def test_custom_title_set_get_and_clear_override_codex_title(
    tmp_path, monkeypatch, capsys
):
    _seed_alias(tmp_path, codex_title="App title")
    _reset_fake()
    monkeypatch.setattr(cli, "CodexAppServer", FakeNameCodex)

    set_rc = main(
        [
            "codex",
            "custom-title",
            "set",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
            "--title",
            "Pinned lane title",
        ]
    )
    set_result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert set_rc == 0
    assert set_result["lane_title"] == "Pinned lane title"
    assert set_result["lane_title_source"] == "custom_title"
    assert set_result["custom_title"] == "Pinned lane title"
    assert set_result["codex_title"] == "App title"
    assert alias["custom_title"] == "Pinned lane title"
    assert "title" not in alias
    assert "lane_label" not in alias
    assert FakeNameCodex.set_calls == []

    get_rc = main(
        [
            "codex",
            "custom-title",
            "get",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    get_result = decode_cli_output(capsys.readouterr().out)

    assert get_rc == 0
    assert get_result["lane_title"] == "Pinned lane title"
    assert get_result["lane_title_source"] == "custom_title"

    clear_rc = main(
        [
            "codex",
            "custom-title",
            "clear",
            "--lane-id",
            "lane-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    clear_result = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "lane-1", tmp_path)

    assert clear_rc == 0
    assert clear_result["cleared"] is True
    assert clear_result["custom_title"] is None
    assert clear_result["lane_title"] == "App title"
    assert clear_result["lane_title_source"] == "codex_title"
    assert "custom_title" not in alias


def test_codex_alias_save_rejects_mismatched_binding(tmp_path):
    with pytest.raises(WorkspaceError) as exc_info:
        cli.save_alias(
            "codex",
            "lane-1",
            {
                "codex_thread_id": "thread-2",
                "binding": {
                    "generation": 1,
                    "thread_id": "thread-1",
                    "bound_at": 1,
                    "origin": "created",
                },
            },
            tmp_path,
        )

    assert exc_info.value.error_code == "LANE_BINDING_INTEGRITY_ERROR"
