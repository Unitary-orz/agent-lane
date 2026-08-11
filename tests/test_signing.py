import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import main
from cli_result import decode_cli_output
from agent_lane.codex_rpc import thread_config_from_overrides
from agent_lane.signing import (
    CODEX_SIGNING_CONFIG_OVERRIDES,
    init_signing,
    signing_env,
    signing_paths,
    signing_smoke_test,
    signing_status,
    stop_agent,
    thread_signing_probe,
    thread_signing_probe_command,
)
from agent_lane.state import load_alias, save_alias


def test_resolve_commit_signing_defaults_to_off():
    assert cli._resolve_commit_signing(None, None) == "off"
    assert cli._resolve_commit_signing(None, {}) == "off"


def test_resolve_commit_signing_prefers_cli_then_alias():
    unsigned_alias = {"commit_signing": {"mode": "off"}}
    signed_alias = {"commit_signing": {"mode": "agent"}}

    assert cli._resolve_commit_signing("agent", unsigned_alias) == "agent"
    assert cli._resolve_commit_signing("off", signed_alias) == "off"
    assert cli._resolve_commit_signing(None, unsigned_alias) == "off"
    assert cli._resolve_commit_signing(None, signed_alias) == "agent"


def test_prepare_commit_signing_off_does_not_require_preflight():
    prepared = cli._prepare_commit_signing("off")

    assert prepared == {
        "metadata": {"mode": "off"},
        "extra_env": {},
        "config_overrides": [],
    }


def test_signing_env_uses_git_config_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(tmp_path))
    paths = signing_paths()

    env = signing_env(paths)

    assert env["SSH_AUTH_SOCK"] == str(paths.socket)
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_0"] == "commit.gpgsign"
    assert env["GIT_CONFIG_VALUE_0"] == "true"
    assert env["GIT_CONFIG_KEY_2"] == "gpg.ssh.program"
    assert env["GIT_CONFIG_VALUE_2"] == "/usr/bin/ssh-keygen"
    assert env["GIT_CONFIG_KEY_3"] == "user.signingkey"
    assert env["GIT_CONFIG_VALUE_3"] == str(paths.public_key)
    assert str(paths.private_key) not in env.values()


def test_codex_signing_does_not_narrow_native_shell_environment():
    assert CODEX_SIGNING_CONFIG_OVERRIDES == []

    config = thread_config_from_overrides(
        CODEX_SIGNING_CONFIG_OVERRIDES,
        extra_env={
            "SSH_AUTH_SOCK": "/tmp/agent-lane.sock",
            "GIT_CONFIG_COUNT": "4",
        },
    )

    assert config == {
        "shell_environment_policy": {
            "set": {
                "SSH_AUTH_SOCK": "/tmp/agent-lane.sock",
                "GIT_CONFIG_COUNT": "4",
            }
        }
    }


def test_thread_signing_probe_checks_effective_shell_without_private_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(tmp_path))
    paths = signing_paths()

    command, marker = thread_signing_probe_command(paths)

    assert str(paths.socket) in command
    assert str(paths.public_key) in command
    assert f"-f {paths.private_key} " not in command
    assert "git config --get gpg.ssh.program" in command
    assert "/usr/bin/ssh-keygen -Y sign" in command
    assert marker in command


def test_thread_signing_probe_writes_atomic_one_time_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(tmp_path))
    paths = signing_paths()

    probe = thread_signing_probe(paths)

    assert probe.receipt_path.parent == tmp_path / "probe-receipts"
    assert str(probe.receipt_path) in probe.command
    assert f"{probe.receipt_path}.tmp" in probe.command
    assert f"mv {probe.receipt_path}.tmp {probe.receipt_path}" in probe.command
    assert probe.marker in probe.command


def test_run_defaults_to_off_without_signing_preflight(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(tmp_path / "missing-signing"))
    alias_root = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "default-unsigned",
            "--alias-root",
            str(alias_root),
            "--cwd",
            str(tmp_path),
            "--prompt",
            "hello",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "default-unsigned", alias_root)
    assert rc == 0, output
    assert output["commit_signing"] == {"mode": "off"}
    assert alias["commit_signing"] == {"mode": "off"}
    assert fake.instances[0].extra_env == {}
    assert fake.instances[0].config_overrides == []


def test_run_commit_signing_off_saves_alias_without_preflight(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(tmp_path / "missing-signing"))
    alias_root = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "unsigned",
            "--alias-root",
            str(alias_root),
            "--cwd",
            str(tmp_path),
            "--commit-signing",
            "off",
            "--prompt",
            "hello",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "unsigned", alias_root)
    assert rc == 0, output
    assert output["commit_signing"] == {"mode": "off"}
    assert alias["commit_signing"] == {"mode": "off"}
    assert fake.instances[0].extra_env == {}
    assert fake.instances[0].config_overrides == []


def test_run_passes_runtime_options_without_persisting_raw_config(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)
    alias_root = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "runtime-options",
            "--alias-root",
            str(alias_root),
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-test",
            "--profile",
            "work",
            "--add-dir",
            str(tmp_path),
            "--effort",
            "high",
            "--config",
            "features.example=true",
            "--commit-signing",
            "off",
            "--prompt",
            "hello",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "runtime-options", alias_root)
    assert rc == 0
    assert output["model"] == "gpt-test"
    assert output["requested_model"] == "gpt-test"
    assert output["requested_model_source"] == "explicit"
    assert output["requested_effort"] == "high"
    assert output["requested_effort_source"] == "explicit"
    assert output["profile"] == "work"
    assert output["add_dirs"] == [str(tmp_path.resolve())]
    assert output["config_override_count"] == 1
    assert alias["model"] == "gpt-test"
    assert alias["requested_model"] == "gpt-test"
    assert alias["requested_model_source"] == "explicit"
    assert alias["requested_effort"] == "high"
    assert alias["requested_effort_source"] == "explicit"
    assert alias["profile"] == "work"
    assert "effort" not in alias
    assert "features.example" not in json.dumps(alias)
    assert fake.instances[0].profile == "work"
    assert fake.instances[0].config_overrides == [
        "features.example=true",
    ]
    assert fake.instances[0].turn_request["effort"] == "high"


def test_run_sets_active_goal_before_turn_and_persists_result(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)
    alias_root = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "goal-run",
            "--alias-root",
            str(alias_root),
            "--cwd",
            str(tmp_path),
            "--goal-objective",
            "Complete the workflow",
            "--commit-signing",
            "off",
            "--prompt",
            "start",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-run", alias_root)
    instance = fake.instances[0]
    assert rc == 0
    assert instance.goal_request == {
        "thread_id": "thread-1",
        "objective": "Complete the workflow",
        "status": "active",
        "token_budget": None,
    }
    assert instance.goal_was_set_when_turn_started is True
    assert output["goal"] == {
        "objective": "Complete the workflow",
        "status": "active",
    }
    assert alias["mode"] == "goal"
    assert alias["objective"] == "Complete the workflow"
    assert alias["goal_status"] == "active"


def test_run_timeout_returns_structured_recoverable_state(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(fake, "should_timeout", True)
    monkeypatch.setattr(cli, "CodexAppServer", fake)
    alias_root = tmp_path / "aliases"

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "timeout-run",
            "--alias-root",
            str(alias_root),
            "--cwd",
            str(tmp_path),
            "--commit-signing",
            "off",
            "--timeout",
            "1",
            "--prompt",
            "start",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "timeout-run", alias_root)
    assert rc == 1
    assert output["error_code"] == "TURN_TIMEOUT"
    assert output["status"] == "timed_out"
    assert output["requested_model"] is None
    assert output["requested_model_source"] == "default-or-unset"
    assert output["requested_effort"] is None
    assert output["requested_effort_source"] == "unset"
    assert output["effective_effort"] is None
    assert output["effective_effort_source"] == "unset"
    assert output["runner_alive"] is False
    assert output["needs_resume"] is True
    assert alias["last_status"] == "timed_out"
    assert alias["last_error_code"] == "TURN_TIMEOUT"
    assert alias["current_turn_id"] is None
    assert "runner_pid" not in alias
    assert "pending_turn_started_at" not in alias


def test_run_rejects_empty_goal_before_starting_app_server(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "empty-goal",
            "--alias-root",
            str(tmp_path / "aliases"),
            "--cwd",
            str(tmp_path),
            "--goal-objective",
            "   ",
            "--commit-signing",
            "off",
            "--prompt",
            "start",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    assert rc == 1
    assert output["ok"] is False
    assert "non-empty" in output["error"]
    assert fake.instances == []


def test_run_rejects_sensitive_config_key_before_starting_app_server(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "sensitive-config",
            "--alias-root",
            str(tmp_path / "aliases"),
            "--cwd",
            str(tmp_path),
            "--config",
            "service.token=placeholder",
            "--commit-signing",
            "off",
            "--prompt",
            "hello",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    assert rc == 1
    assert output["ok"] is False
    assert "potentially sensitive" in output["error"]
    assert fake.instances == []


def test_run_accepts_non_secret_token_limit_config(
    tmp_path, monkeypatch, capsys
):
    fake = FakeCodexAppServer
    fake.instances = []
    monkeypatch.setattr(cli, "CodexAppServer", fake)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "token-limit-config",
            "--alias-root",
            str(tmp_path / "aliases"),
            "--cwd",
            str(tmp_path),
            "--config",
            "model_auto_compact_token_limit=100000",
            "--commit-signing",
            "off",
            "--prompt",
            "hello",
        ]
    )
    output = decode_cli_output(capsys.readouterr().out)

    assert rc == 0, output
    assert fake.instances[0].config_overrides == [
        "model_auto_compact_token_limit=100000"
    ]


@pytest.mark.skipif(
    not all(
        shutil.which(name) for name in ("ssh-keygen", "ssh-agent", "ssh-add", "git")
    ),
    reason="requires ssh and git command line tools",
)
def test_signing_init_and_smoke_test_use_isolated_home(tmp_path, monkeypatch):
    short_home = Path(tempfile.mkdtemp(prefix="alsign.", dir="/tmp"))
    monkeypatch.setenv("AGENT_LANE_SIGNING_HOME", str(short_home))

    try:
        init_result = init_signing(generate=True)
        smoke = signing_smoke_test()
        status = signing_status()
        probe = thread_signing_probe()
        probe_result = subprocess.run(
            ["/bin/sh", "-c", probe.command],
            env={**os.environ, **signing_env()},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        probe_receipt = probe.receipt_path.read_text(encoding="utf-8").strip()
    finally:
        stop_agent()
        shutil.rmtree(short_home, ignore_errors=True)

    assert init_result["public_key_exists"] is True
    assert init_result["private_key_exists"] is True
    assert smoke["signed"] is True
    assert status["fingerprint"].startswith("SHA256:")
    assert probe_result.returncode == 0, probe_result.stderr
    assert probe_receipt == probe.marker


class FakeCodexAppServer:
    instances = []
    should_timeout = False

    def __init__(self, *args, **kwargs):
        self.profile = kwargs.get("profile")
        self.extra_env = kwargs.get("extra_env")
        self.config_overrides = kwargs.get("config_overrides")
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def start_thread(
        self,
        _cwd,
        sandbox=None,
        *,
        model=None,
        runtime_workspace_roots=None,
    ):
        self.sandbox = sandbox
        self.model = model
        self.runtime_workspace_roots = runtime_workspace_roots
        return "thread-1"

    def set_thread_name(self, _thread_id, _title):
        return None

    def update_git_info(self, _thread_id, _git_info):
        return None

    def set_goal(
        self,
        thread_id,
        *,
        objective=None,
        status=None,
        token_budget=None,
    ):
        self.goal_request = {
            "thread_id": thread_id,
            "objective": objective,
            "status": status,
            "token_budget": token_budget,
        }
        return {"goal": {"objective": objective, "status": status}}

    def get_goal(self, _thread_id):
        request = self.goal_request
        return {
            "objective": request["objective"],
            "status": request["status"],
        }

    def run_turn(
        self,
        thread_id,
        _prompt,
        *,
        sandbox=None,
        model=None,
        effort=None,
        workspace_cwd=None,
        runtime_workspace_roots=None,
        additional_context=None,
        timeout=600.0,
        on_started=None,
    ):
        self.goal_was_set_when_turn_started = hasattr(self, "goal_request")
        self.run_sandbox = sandbox
        self.run_model = model
        self.turn_request = {
            "effort": effort,
            "workspace_cwd": workspace_cwd,
            "additional_context": additional_context,
        }
        self.run_workspace_roots = runtime_workspace_roots
        self.timeout = timeout
        if on_started:
            on_started("turn-1")
        if self.should_timeout:
            raise TimeoutError("turn timed out after 1s")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-1",
            status="completed",
            final_text="done",
            events=[],
        )


class FakeLoadedSigningCodexAppServer:
    instances = []
    workspace = None
    loaded_ids = {"thread-app"}
    probe_succeeds = True
    idle_succeeds = True
    goal_set_fails = False
    goal_get_fails = False
    replacement_goal_get_fails = False
    origin_goal = None

    def __init__(self, *args, **kwargs):
        self.transport = "daemon"
        self.extra_env = kwargs.get("extra_env")
        self.config_overrides = kwargs.get("config_overrides")
        self.start_thread_calls = []
        self.resume_thread_calls = []
        self.shell_commands = []
        self.wait_thread_idle_calls = []
        self.archive_thread_calls = []
        self.events = []
        self.goal = None
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_thread(self, thread_id, include_turns=False):
        return {
            "thread": {
                "id": thread_id,
                "cwd": str(self.workspace),
                "status": {"type": "idle"},
            }
        }

    def list_loaded_thread_ids(self):
        return set(self.loaded_ids)

    def start_thread(
        self,
        cwd,
        sandbox=None,
        *,
        model=None,
        runtime_workspace_roots=None,
    ):
        self.start_thread_calls.append(
            {
                "cwd": cwd,
                "sandbox": sandbox,
                "model": model,
                "runtime_workspace_roots": runtime_workspace_roots,
            }
        )
        return "thread-managed"

    def resume_thread(self, thread_id, **kwargs):
        self.resume_thread_calls.append({"thread_id": thread_id, **kwargs})
        return {}

    def set_thread_name(self, thread_id, title):
        self.thread_name = {"thread_id": thread_id, "title": title}

    def update_git_info(self, thread_id, git_info):
        self.git_info = {"thread_id": thread_id, "git_info": git_info}

    def run_thread_shell_command(
        self,
        thread_id,
        command,
        *,
        timeout=30.0,
        success_receipt=None,
    ):
        self.events.append(("shell", thread_id))
        self.shell_commands.append(
            {
                "thread_id": thread_id,
                "command": command,
                "timeout": timeout,
                "success_receipt": success_receipt,
            }
        )
        match = re.search(r"AGENT_LANE_SIGNING_OK:[a-f0-9]+", command)
        marker = match.group(0) if match else ""
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="probe-turn",
            item_id="probe-item",
            status="completed" if self.probe_succeeds else "failed",
            exit_code=0 if self.probe_succeeds else 1,
            output=f"{marker}\n" if self.probe_succeeds else "",
        )

    def wait_thread_idle(self, thread_id, *, timeout=5.0):
        self.events.append(("idle", thread_id))
        self.wait_thread_idle_calls.append(
            {"thread_id": thread_id, "timeout": timeout}
        )
        if not self.idle_succeeds:
            raise TimeoutError("thread stayed active")
        return {
            "thread": {
                "id": thread_id,
                "status": {"type": "idle"},
            }
        }

    def archive_thread(self, thread_id):
        self.archive_thread_calls.append(thread_id)

    def run_turn(
        self,
        thread_id,
        prompt,
        *,
        sandbox=None,
        model=None,
        effort=None,
        workspace_cwd=None,
        runtime_workspace_roots=None,
        additional_context=None,
        timeout=600.0,
        on_started=None,
    ):
        self.events.append(("turn", thread_id))
        self.turn_request = {
            "thread_id": thread_id,
            "prompt": prompt,
            "workspace_cwd": workspace_cwd,
            "additional_context": additional_context,
        }
        if self.goal is not None:
            self.goal = {**self.goal, "status": "complete"}
        if on_started:
            on_started("turn-managed")
        return SimpleNamespace(
            thread_id=thread_id,
            turn_id="turn-managed",
            status="completed",
            final_text="continued",
            events=[],
        )

    def set_goal(
        self,
        thread_id,
        *,
        objective=None,
        status=None,
        token_budget=None,
    ):
        self.events.append(("goal", thread_id))
        if self.goal_set_fails:
            raise cli.CodexRpcError("goal migration failed")
        self.goal = {
            "objective": objective,
            "status": status,
            "tokenBudget": token_budget,
        }
        return {"goal": dict(self.goal)}

    def get_goal(self, thread_id):
        if thread_id == "thread-managed":
            if self.replacement_goal_get_fails:
                raise cli.CodexRpcError("replacement goal read failed")
            return dict(self.goal) if self.goal is not None else None
        if self.goal_get_fails:
            raise cli.CodexRpcError("goal read failed")
        return (
            dict(self.origin_goal)
            if isinstance(self.origin_goal, dict)
            else None
        )


def _fake_prepared_signing(mode):
    if mode == "off":
        return {
            "metadata": {"mode": "off"},
            "extra_env": {},
            "config_overrides": [],
        }
    assert mode == "agent"
    return {
        "metadata": {
            "mode": "agent",
            "backend": "ssh-agent",
            "public_key_path": "/tmp/agent-lane.pub",
            "fingerprint": "SHA256:test-agent-lane",
            "git_program": "/usr/bin/ssh-keygen",
        },
        "extra_env": {"SSH_AUTH_SOCK": "/tmp/agent-lane.sock"},
        "config_overrides": [],
    }


def test_loaded_app_thread_requires_explicit_replacement_authorization(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "loaded-app",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "loaded-app",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "loaded-app", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert (
        output["error_code"]
        == "CODEX_SIGNING_REPLACEMENT_AUTHORIZATION_REQUIRED"
    )
    assert output["required_option"] == "--allow-signing-replacement"
    assert output["authorization_scope"] == "single_command"
    assert output["replacement_title"] == "Existing App task [agent-lane]"
    assert output["original_task_preserved"] is True
    assert output["side_effects"] == {
        "creates_app_visible_task": True,
        "keeps_original_task_visible": True,
        "rebinds_lane_after_verification": True,
        "copies_live_active_goal_to_replacement": True,
        "keeps_origin_goal_unchanged": True,
        "uses_bounded_context_handoff": True,
        "adds_signing_shell_turn": True,
    }
    assert output["retryable"] is False
    assert execution.start_thread_calls == []
    assert execution.shell_commands == []
    assert execution.archive_thread_calls == []
    assert not hasattr(execution, "turn_request")
    assert alias["codex_thread_id"] == "thread-app"
    assert alias["adopted_from"] == "codex-app"


def test_loaded_app_thread_defaults_to_off_without_probe_or_replacement(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "loaded-app-default-off",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "execution_mode": "app-sync",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "loaded-app-default-off",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "loaded-app-default-off", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["resumed"] is True
    assert output["codex_thread_id"] == "thread-app"
    assert output["commit_signing"] == {"mode": "off"}
    assert "thread_replaced" not in output
    assert alias["commit_signing"] == {"mode": "off"}
    assert execution.start_thread_calls == []
    assert execution.shell_commands == []


def test_live_goal_read_failure_stops_before_replacement_creation(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "goal-read-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_get_fails",
        True,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "goal-read-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-read-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert output["error_code"] == "CODEX_RPC_ERROR"
    assert execution.start_thread_calls == []
    assert execution.shell_commands == []
    assert execution.archive_thread_calls == []
    assert alias["codex_thread_id"] == "thread-app"
    assert not hasattr(execution, "turn_request")


def test_loaded_adopted_app_thread_moves_to_verified_managed_thread(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "loaded-app",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task [agent-lane]",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "last_completed_final_text": (
                "Seven intended files are staged; signing failed before commit."
            ),
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "loaded-app",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "继续",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "loaded-app", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["resumed"] is False
    assert output["thread_replaced"] is True
    assert output["origin_thread_id"] == "thread-app"
    assert output["codex_thread_id"] == "thread-managed"
    assert output["lane_title"] == "Existing App task [agent-lane]"
    assert output["lane_title_source"] == "codex_title"
    assert output["commit_signing"]["effective"] is True
    assert output["commit_signing"]["effective_thread_id"] == "thread-managed"
    assert output["commit_signing"]["verification"] == "thread_shell_probe_idle"
    assert alias["codex_thread_id"] == "thread-managed"
    assert alias["origin_codex_thread_id"] == "thread-app"
    assert "title" not in alias
    assert alias["codex_title"] == "Existing App task [agent-lane]"
    assert alias["binding"]["generation"] == 2
    assert alias["binding"]["thread_id"] == "thread-managed"
    assert alias["binding_history"][-1]["thread_id"] == "thread-app"
    assert alias["binding_history"][-1]["unbound_reason"] == (
        "loaded_thread_resume_config_not_effective"
    )
    assert "adopted_from" not in alias
    assert execution.resume_thread_calls == []
    assert execution.start_thread_calls == [
        {
            "cwd": str(workspace),
            "sandbox": "danger-full-access",
            "model": None,
            "runtime_workspace_roots": None,
        }
    ]
    assert execution.shell_commands[0]["thread_id"] == "thread-managed"
    assert execution.thread_name == {
        "thread_id": "thread-managed",
        "title": "Existing App task [agent-lane]",
    }
    assert execution.events[:3] == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("turn", "thread-managed"),
    ]
    assert execution.turn_request["thread_id"] == "thread-managed"
    assert execution.turn_request["prompt"] == "继续"
    handoff = execution.turn_request["additional_context"][
        "agent_lane_signing_handoff"
    ]
    assert handoff["kind"] == "application"
    assert "Seven intended files are staged" in handoff["value"]
    cli._update_thread_alias(
        alias,
        {"name": "Existing App task [agent-lane]"},
    )
    assert alias["codex_title"] == "Existing App task [agent-lane]"
    assert "title" not in alias
    assert cli._signing_replacement_titles(
        "Existing App task [agent-lane] [agent-lane]"
    ) == (
        "Existing App task",
        "Existing App task [agent-lane]",
    )


def test_send_goal_refresh_failure_uses_copied_live_goal(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    goal = {
        "objective": "Finish the signed change",
        "status": "active",
        "tokenBudget": 4321,
        "tokensUsed": 321,
    }
    save_alias(
        "codex",
        "goal-send",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "mode": "goal",
            "objective": "Stale completed goal",
            "goal": {
                "objective": "Stale completed goal",
                "status": "complete",
                "tokenBudget": 9999,
            },
            "goal_status": "complete",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_set_fails",
        False,
    )
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        goal,
    )
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "replacement_goal_get_fails",
        True,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "goal-send",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-send", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["thread_replaced"] is True
    assert output["codex_thread_id"] == "thread-managed"
    assert output["goal"]["status"] == "active"
    assert output["goal"]["tokenBudget"] == 4000
    assert alias["codex_thread_id"] == "thread-managed"
    assert alias["goal_status"] == "active"
    assert alias["objective"] == goal["objective"]
    assert "replacement goal read failed" in alias["goal_refresh_error"]
    assert execution.goal == {
        "objective": goal["objective"],
        "status": "complete",
        "tokenBudget": 4000,
    }
    handoff = execution.turn_request["additional_context"][
        "agent_lane_signing_handoff"
    ]["value"]
    assert goal["objective"] in handoff
    assert "Stale completed goal" not in handoff
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("goal", "thread-managed"),
        ("turn", "thread-managed"),
    ]


def test_run_replacement_refreshes_copied_live_goal_without_override(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    goal = {
        "objective": "Finish the existing live goal",
        "status": "active",
        "tokenBudget": 5000,
        "tokensUsed": 1000,
    }
    save_alias(
        "codex",
        "goal-run",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        goal,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "goal-run",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-run", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["thread_replaced"] is True
    assert output["goal"]["status"] == "complete"
    assert output["goal"]["tokenBudget"] == 4000
    assert alias["goal_status"] == "complete"
    assert alias["objective"] == goal["objective"]
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("goal", "thread-managed"),
        ("turn", "thread-managed"),
    ]


def test_send_replacement_does_not_revive_stale_alias_goal(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "stale-goal-send",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "mode": "goal",
            "objective": "Stale active goal",
            "goal": {
                "objective": "Stale active goal",
                "status": "active",
                "tokenBudget": 9999,
            },
            "goal_status": "active",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_set_fails",
        False,
    )
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        None,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "stale-goal-send",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "stale-goal-send", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["goal"] is None
    assert "goal" not in alias
    assert "objective" not in alias
    assert "goal_status" not in alias
    assert ("goal", "thread-managed") not in execution.events
    handoff = execution.turn_request["additional_context"][
        "agent_lane_signing_handoff"
    ]["value"]
    assert "Stale active goal" not in handoff


def test_unloaded_adopted_app_thread_cold_resumes_with_verified_signing(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "cold-app",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = set()
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "cold-app",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "cold-app", aliases)
    assert rc == 0, output
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert output["resumed"] is True
    assert "thread_replaced" not in output
    assert output["codex_thread_id"] == "thread-app"
    assert output["commit_signing"]["effective"] is True
    assert alias["adopted_from"] == "codex-app"
    assert execution.start_thread_calls == []
    assert execution.resume_thread_calls[0]["thread_id"] == "thread-app"
    assert execution.shell_commands[0]["thread_id"] == "thread-app"


def test_cold_probe_failure_does_not_create_unauthorized_replacement(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "cold-probe-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = set()
    FakeLoadedSigningCodexAppServer.probe_succeeds = False
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "cold-probe-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "cold-probe-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert (
        output["error_code"]
        == "CODEX_SIGNING_REPLACEMENT_AUTHORIZATION_REQUIRED"
    )
    assert execution.resume_thread_calls[0]["thread_id"] == "thread-app"
    assert execution.shell_commands[0]["thread_id"] == "thread-app"
    assert execution.wait_thread_idle_calls[0]["thread_id"] == "thread-app"
    assert execution.start_thread_calls == []
    assert execution.archive_thread_calls == []
    assert not hasattr(execution, "turn_request")
    assert alias["codex_thread_id"] == "thread-app"


def test_loaded_managed_thread_reprobes_persisted_signing_verification(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "verified-managed",
        {
            "codex_thread_id": "thread-managed",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "commit_signing": {
                "mode": "agent",
                "backend": "ssh-agent",
                "public_key_path": "/tmp/agent-lane.pub",
                "fingerprint": "SHA256:test-agent-lane",
                "git_program": "/usr/bin/ssh-keygen",
                "effective": True,
                "effective_thread_id": "thread-managed",
                "verification": "thread_shell_probe_receipt_idle",
                "verified_at": 123.0,
                "verification_turn_id": "probe-turn",
            },
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-managed"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "verified-managed",
            "--alias-root",
            str(aliases),
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["resumed"] is True
    assert "thread_replaced" not in output
    assert output["codex_thread_id"] == "thread-managed"
    assert output["commit_signing"]["verification"] == "thread_shell_probe_idle"
    assert output["commit_signing"]["verified_at"] != 123.0
    assert execution.start_thread_calls == []
    assert execution.shell_commands[0]["thread_id"] == "thread-managed"
    assert execution.wait_thread_idle_calls[0]["thread_id"] == "thread-managed"
    assert execution.resume_thread_calls == [
        {"thread_id": "thread-managed", "apply_config": False}
    ]
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("turn", "thread-managed"),
    ]


def test_replacement_probe_failure_keeps_original_alias_and_starts_no_user_turn(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "probe-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = False
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "probe-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "probe-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert output["error_code"] == "CODEX_AGENT_SIGNING_ENV_UNAVAILABLE"
    assert output["codex_thread_id"] == "thread-managed"
    assert alias["codex_thread_id"] == "thread-app"
    assert alias["adopted_from"] == "codex-app"
    assert execution.archive_thread_calls == ["thread-managed"]
    assert output["replacement_thread_archived"] is True
    assert not hasattr(execution, "turn_request")


def test_replacement_probe_waits_for_idle_before_user_turn(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "probe-stays-active",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = False
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "probe-stays-active",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "probe-stays-active", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert output["error_code"] == "CODEX_AGENT_SIGNING_PROBE_NOT_QUIESCENT"
    assert output["replacement_thread_id"] == "thread-managed"
    assert output["replacement_thread_archived"] is True
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
    ]
    assert execution.archive_thread_calls == ["thread-managed"]
    assert not hasattr(execution, "turn_request")
    assert alias["codex_thread_id"] == "thread-app"


def test_replacement_alias_save_failure_archives_unbound_task(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "alias-save-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)
    real_save_alias = cli.save_alias

    def fail_replacement_save(provider, lane_id, data, root):
        if data.get("codex_thread_id") == "thread-managed":
            raise OSError("alias root is read-only")
        return real_save_alias(provider, lane_id, data, root)

    monkeypatch.setattr(cli, "save_alias", fail_replacement_save)

    rc = main(
        [
            "codex",
            "send",
            "--lane-id",
            "alias-save-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "alias-save-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert (
        output["error_code"]
        == "CODEX_SIGNING_REPLACEMENT_ALIAS_SAVE_FAILED"
    )
    assert output["replacement_thread_id"] == "thread-managed"
    assert output["replacement_thread_archived"] is True
    assert alias["codex_thread_id"] == "thread-app"
    assert alias["adopted_from"] == "codex-app"
    assert execution.archive_thread_calls == ["thread-managed"]
    assert not hasattr(execution, "turn_request")


def test_goal_migration_failure_archives_unbound_replacement(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    goal = {
        "objective": "Finish the signed change",
        "status": "active",
        "tokenBudget": 5000,
    }
    save_alias(
        "codex",
        "goal-migration-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing goal task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "mode": "goal",
            "objective": goal["objective"],
            "goal": goal,
            "goal_status": "active",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_set_fails",
        True,
    )
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        goal,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "goal-migration-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--turn-timeout",
            "10",
            "--max-runtime",
            "30",
            "--max-turns",
            "1",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-migration-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert output["error_code"] == "CODEX_RPC_ERROR"
    assert output["replacement_thread_id"] == "thread-managed"
    assert output["replacement_thread_archived"] is True
    assert alias["codex_thread_id"] == "thread-app"
    assert alias["goal_status"] == "active"
    assert execution.archive_thread_calls == ["thread-managed"]
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("goal", "thread-managed"),
    ]
    assert not hasattr(execution, "turn_request")


@pytest.mark.parametrize(
    ("case", "live_goal", "expected_rc", "stop_condition", "error_code"),
    [
        ("missing", None, 1, "goal_missing", "GOAL_MISSING"),
        (
            "complete",
            {
                "objective": "Already finished",
                "status": "complete",
                "tokenBudget": 5000,
                "tokensUsed": 1000,
            },
            0,
            "goal_complete",
            None,
        ),
    ],
)
def test_goal_run_stops_before_replacement_when_live_goal_is_not_active(
    case,
    live_goal,
    expected_rc,
    stop_condition,
    error_code,
    tmp_path,
    monkeypatch,
    capsys,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    lane_id = f"goal-{case}"
    save_alias(
        "codex",
        lane_id,
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing goal task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "mode": "goal",
            "objective": "Stale active goal",
            "goal": {
                "objective": "Stale active goal",
                "status": "active",
            },
            "goal_status": "active",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        live_goal,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            lane_id,
            "--alias-root",
            str(aliases),
            "--turn-timeout",
            "10",
            "--max-runtime",
            "30",
            "--max-turns",
            "1",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", lane_id, aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == expected_rc, output
    assert output["stop_condition"] == stop_condition
    if error_code is None:
        assert "error_code" not in output
        assert alias["goal_status"] == "complete"
    else:
        assert output["error_code"] == error_code
        assert "goal_status" not in alias
    assert "thread_replaced" not in output
    assert execution.start_thread_calls == []
    assert execution.shell_commands == []
    assert execution.archive_thread_calls == []
    assert not hasattr(execution, "turn_request")


def test_run_goal_override_failure_archives_unbound_replacement(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    save_alias(
        "codex",
        "goal-override-fails",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing App task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_set_fails",
        True,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "run",
            "--lane-id",
            "goal-override-fails",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--goal-objective",
            "Start a new active goal",
            "--prompt",
            "continue",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-override-fails", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 1
    assert output["error_code"] == "CODEX_RPC_ERROR"
    assert output["replacement_thread_id"] == "thread-managed"
    assert output["replacement_thread_archived"] is True
    assert alias["codex_thread_id"] == "thread-app"
    assert execution.archive_thread_calls == ["thread-managed"]
    assert execution.events == [
        ("shell", "thread-managed"),
        ("idle", "thread-managed"),
        ("goal", "thread-managed"),
    ]
    assert not hasattr(execution, "turn_request")


def test_goal_runner_moves_loaded_app_goal_to_managed_signing_thread(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    aliases = tmp_path / "aliases"
    goal = {
        "objective": "Finish the change and create the signed commit",
        "status": "active",
        "tokenBudget": 5000,
    }
    save_alias(
        "codex",
        "goal-app",
        {
            "codex_thread_id": "thread-app",
            "cwd": str(workspace),
            "codex_title": "Existing goal task",
            "sandbox": "danger-full-access",
            "adopted_from": "codex-app",
            "mode": "goal",
            "objective": goal["objective"],
            "goal": goal,
            "goal_status": "active",
            "last_completed_final_text": "Implementation is staged; commit remains.",
        },
        aliases,
    )
    FakeLoadedSigningCodexAppServer.instances = []
    FakeLoadedSigningCodexAppServer.workspace = workspace
    FakeLoadedSigningCodexAppServer.loaded_ids = {"thread-app"}
    FakeLoadedSigningCodexAppServer.probe_succeeds = True
    FakeLoadedSigningCodexAppServer.idle_succeeds = True
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "goal_set_fails",
        False,
    )
    monkeypatch.setattr(
        FakeLoadedSigningCodexAppServer,
        "origin_goal",
        goal,
    )
    monkeypatch.setattr(cli, "CodexAppServer", FakeLoadedSigningCodexAppServer)
    monkeypatch.setattr(cli, "_prepare_commit_signing", _fake_prepared_signing)

    rc = main(
        [
            "codex",
            "goal",
            "run",
            "--lane-id",
            "goal-app",
            "--alias-root",
            str(aliases),
            "--commit-signing",
            "agent",
            "--allow-signing-replacement",
            "--turn-timeout",
            "10",
            "--max-runtime",
            "30",
            "--max-turns",
            "1",
        ]
    )

    output = decode_cli_output(capsys.readouterr().out)
    alias = load_alias("codex", "goal-app", aliases)
    execution = FakeLoadedSigningCodexAppServer.instances[-1]
    assert rc == 0, output
    assert output["thread_replaced"] is True
    assert output["origin_thread_id"] == "thread-app"
    assert output["codex_thread_id"] == "thread-managed"
    assert output["completed"] is True
    assert output["commit_signing"]["effective"] is True
    assert alias["codex_thread_id"] == "thread-managed"
    assert alias["goal_status"] == "complete"
    assert execution.goal["objective"] == goal["objective"]
    assert execution.turn_request["prompt"] == cli.GOAL_CONTINUATION_PROMPT
    assert execution.turn_request["additional_context"] is not None
