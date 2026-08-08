import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_lane.codex_rpc as codex_rpc


ROOT = Path(__file__).resolve().parents[1]


def test_thread_config_fallback_parses_supported_override_subset(monkeypatch):
    monkeypatch.setattr(codex_rpc, "tomllib", None)

    config = codex_rpc.thread_config_from_overrides(
        [
            "features.example=true",
            'shell_environment_policy.inherit="core"',
            'shell_environment_policy.include_only=["PATH","SSH_AUTH_SOCK"]',
        ]
    )

    assert config == {
        "features": {"example": True},
        "shell_environment_policy": {
            "inherit": "core",
            "include_only": ["PATH", "SSH_AUTH_SOCK"],
        },
    }


def test_cli_module_import_survives_missing_tomllib():
    script = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "tomllib":
        raise ModuleNotFoundError("simulated missing tomllib")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from agent_lane.cli import build_parser
assert build_parser().prog == "agent-lane"
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="system Python is unavailable",
)
def test_source_entrypoint_starts_with_system_python():
    result = subprocess.run(
        ["/usr/bin/python3", "bin/agent-lane", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run and inspect durable Codex tasks" in result.stdout
