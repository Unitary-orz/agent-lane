from types import SimpleNamespace

import pytest

import agent_lane.control_plane as control_plane
from agent_lane.state import save_alias
from agent_lane.workspace import WorkspaceError


def test_legacy_lane_defaults_to_independent_mode():
    mode, source = control_plane._resolve_execution_mode(
        None,
        {"codex_thread_id": "thread-1"},
    )

    assert mode == "independent"
    assert source == "default"
    assert control_plane._transport_for_mode(mode) == "stdio"


def test_lane_execution_mode_cannot_be_changed_in_place():
    with pytest.raises(WorkspaceError) as caught:
        control_plane._resolve_execution_mode(
            "app-sync",
            {"execution_mode": "independent"},
        )

    assert caught.value.error_code == "LANE_EXECUTION_MODE_CONFLICT"
    assert caught.value.details["stored_mode"] == "independent"
    assert caught.value.details["requested_mode"] == "app-sync"


@pytest.mark.parametrize(
    "alias",
    [
        {"execution_mode": "app-snyc"},
        {"binding": {"execution_mode": ""}},
        {
            "execution_mode": "independent",
            "binding": {"execution_mode": "app-sync"},
        },
    ],
)
def test_invalid_or_conflicting_stored_mode_fails_closed(alias):
    with pytest.raises(WorkspaceError) as caught:
        control_plane._resolve_execution_mode(None, alias)

    assert caught.value.error_code == "LANE_EXECUTION_MODE_INVALID"
    assert caught.value.details["retryable"] is False


@pytest.mark.parametrize(
    ("mode", "transport"),
    [("independent", "stdio"), ("app-sync", "daemon")],
)
def test_lane_mode_forces_transport(monkeypatch, mode, transport):
    captured = []

    class FakeCodex:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(control_plane, "CodexAppServer", FakeCodex)

    codex = control_plane._codex_for_alias({"execution_mode": mode})

    assert isinstance(codex, FakeCodex)
    assert captured == [{"transport": transport}]


def test_steer_rejects_independent_lane_before_connecting(
    tmp_path, monkeypatch
):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "execution_mode": "independent",
        },
        tmp_path,
    )

    class UnexpectedCodex:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("independent steer must not connect")

    monkeypatch.setattr(control_plane, "CodexAppServer", UnexpectedCodex)
    args = SimpleNamespace(
        lane_id="lane-1",
        alias_root=str(tmp_path),
        timeout=20.0,
        prompt="continue",
        prompt_file=None,
        turn_id=None,
    )

    with pytest.raises(WorkspaceError) as caught:
        control_plane.cmd_codex_steer(args)

    assert caught.value.error_code == "LANE_APP_SYNC_REQUIRED"


def test_alias_binding_persists_mode_contract(tmp_path):
    path = control_plane.save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "execution_mode": "app-sync",
            "execution_mode_source": "explicit",
        },
        tmp_path,
    )

    payload = path.read_text(encoding="utf-8")
    assert '"schema_version": 4' in payload
    assert '"execution_mode": "app-sync"' in payload
