from agent_lane.state import (
    DEFAULT_ALIAS_ROOT,
    alias_path,
    list_aliases,
    load_alias,
    safe_lane_id,
    save_alias,
)


def test_default_alias_root_uses_agent_lane_namespace():
    assert DEFAULT_ALIAS_ROOT.parts[-2:] == (".agent-lane", "lanes")


def test_safe_lane_id_keeps_readable_parts():
    assert safe_lane_id("assetmap:import fix") == "assetmap:import-fix"


def test_safe_lane_id_rejects_empty():
    try:
        safe_lane_id("   ")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_alias_roundtrip(tmp_path):
    path = save_alias("codex", "assetmap:import-fix", {"codex_thread_id": "t1"}, tmp_path)

    assert path == alias_path("codex", "assetmap:import-fix", tmp_path)

    alias = load_alias("codex", "assetmap:import-fix", tmp_path)
    assert alias["lane_id"] == "assetmap:import-fix"
    assert alias["provider"] == "codex"
    assert alias["codex_thread_id"] == "t1"

    aliases = list_aliases("codex", tmp_path)
    assert len(aliases) == 1
    assert aliases[0]["_path"] == str(path)
