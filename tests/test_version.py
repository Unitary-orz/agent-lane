import tomllib
from pathlib import Path

from packaging.version import Version

from agent_lane import __version__
from agent_lane import codex_rpc


ROOT = Path(__file__).resolve().parents[1]


def test_project_and_app_server_client_versions_match():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project_version = tomllib.load(file)["project"]["version"]
    with (ROOT / "uv.lock").open("rb") as file:
        lock_version = tomllib.load(file)["package"][0]["version"]

    assert project_version == __version__ == "1.0.0-rc.1"
    assert Version(lock_version) == Version(__version__)
    assert codex_rpc.__version__ == __version__
