import pytest


@pytest.fixture(autouse=True)
def isolate_agent_lane_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENT_LANE_CONFIG_PATH",
        str(tmp_path / "agent-lane-config.json"),
    )
