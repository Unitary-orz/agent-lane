import plistlib
import subprocess
from types import SimpleNamespace

import pytest

import agent_lane.app_sync as app_sync
from agent_lane.workspace import WorkspaceError


def completed(args, *, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_enable_installs_login_agent_and_reports_ready(tmp_path, monkeypatch):
    agent_lane = tmp_path / "agent-lane"
    codex = tmp_path / "codex"
    agent_lane.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    agent_lane.chmod(0o755)
    codex.chmod(0o755)
    calls = []
    bootstrapped = False

    def run(args, **_kwargs):
        nonlocal bootstrapped
        calls.append(args)
        if args[:2] == ["/bin/launchctl", "print"]:
            return completed(args, returncode=0 if bootstrapped else 1)
        if args[:2] == ["/bin/launchctl", "bootstrap"]:
            bootstrapped = True
            return completed(args)
        if args[:2] == ["/bin/launchctl", "getenv"]:
            return completed(args, stdout="1\n")
        return completed(args)

    monkeypatch.setattr(app_sync, "_require_macos", lambda: None)
    monkeypatch.setattr(
        app_sync,
        "_probe_daemon_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(
            cli_version="1.2.3",
            app_server_version="1.2.3",
        ),
    )
    monkeypatch.setattr(
        app_sync,
        "detect_running_codex_app",
        lambda: (_ for _ in ()).throw(
            app_sync.AppRuntimeError("CODEX_APP_NOT_RUNNING", "not running")
        ),
    )

    result = app_sync.app_sync_enable(
        codex_bin=str(codex),
        executable=str(agent_lane),
        home=tmp_path,
        uid=501,
        run=run,
    )

    plist_path = (
        tmp_path
        / "Library"
        / "LaunchAgents"
        / f"{app_sync.APP_SYNC_LABEL}.plist"
    )
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"] == [
        str(agent_lane),
        "_app-sync-login",
        "--codex-bin",
        str(codex),
    ]
    assert ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)] in calls
    assert result["installed"] is True
    assert result["ready"] is True
    assert result["app_reopen_required"] is False


def test_login_accepts_version_mismatch_after_probe(monkeypatch):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return completed(args)

    monkeypatch.setattr(app_sync, "_require_macos", lambda: None)
    monkeypatch.setattr(
        app_sync,
        "_probe_daemon_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(
            cli_version="1.2.3",
            app_server_version="1.2.4",
        ),
    )

    result = app_sync.app_sync_login(codex_bin="/opt/codex", run=run)

    assert result["ready"] is True
    assert result["warnings"][0]["code"] == "APP_SYNC_VERSION_MISMATCH"
    assert ["/bin/launchctl", "setenv", app_sync.APP_SYNC_ENV, "1"] in calls
    assert not any(call[1:3] == ["unsetenv", app_sync.APP_SYNC_ENV] for call in calls)


def test_daemon_readiness_includes_websocket_initialize_probe(
    tmp_path, monkeypatch
):
    socket_path = tmp_path / "daemon.sock"
    opened = []

    class FakeCodex:
        def __init__(self, codex_bin, **kwargs):
            opened.append((codex_bin, kwargs))

        def __enter__(self):
            opened.append("entered")
            return self

        def __exit__(self, *_exc):
            opened.append("closed")

    info = SimpleNamespace(
        cli_version="1.2.3",
        app_server_version="1.2.4",
        socket_path=socket_path,
    )
    monkeypatch.setattr(
        app_sync,
        "probe_shared_daemon",
        lambda *_args, **_kwargs: info,
    )
    monkeypatch.setattr(app_sync, "CodexAppServer", FakeCodex)

    observed = app_sync._probe_daemon_readiness("/opt/codex", run=lambda: None)

    assert observed is info
    assert opened == [
        (
            "/opt/codex",
            {"transport": "daemon", "daemon_socket": socket_path},
        ),
        "entered",
        "closed",
    ]


def test_login_protocol_failure_unsets_environment_without_advertising(
    monkeypatch,
):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return completed(args)

    monkeypatch.setattr(app_sync, "_require_macos", lambda: None)
    monkeypatch.setattr(
        app_sync,
        "_probe_daemon_readiness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app_sync.CodexRpcError(
                "initialize failed",
                error_code="CODEX_DAEMON_UNAVAILABLE",
                retryable=True,
            )
        ),
    )

    with pytest.raises(app_sync.CodexRpcError) as caught:
        app_sync.app_sync_login(codex_bin="/opt/codex", run=run)

    assert caught.value.error_code == "CODEX_DAEMON_UNAVAILABLE"
    assert ["/bin/launchctl", "setenv", app_sync.APP_SYNC_ENV, "1"] not in calls
    assert ["/bin/launchctl", "unsetenv", app_sync.APP_SYNC_ENV] in calls


def test_disable_removes_login_activation_without_stopping_daemon(
    tmp_path, monkeypatch
):
    paths = app_sync._paths(tmp_path)
    paths["launch_agents"].mkdir(parents=True)
    paths["plist"].write_bytes(plistlib.dumps({"Label": app_sync.APP_SYNC_LABEL}))
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["/bin/launchctl", "print"]:
            return completed(args)
        return completed(args)

    monkeypatch.setattr(app_sync, "_require_macos", lambda: None)
    monkeypatch.setattr(app_sync, "_app_is_running", lambda: True)

    result = app_sync.app_sync_disable(home=tmp_path, uid=501, run=run)

    assert paths["plist"].exists() is False
    assert result["daemon_left_running"] is True
    assert result["app_reopen_required"] is True
    assert not any("daemon" in call for call in calls)


def test_login_configuration_is_macos_only(monkeypatch):
    monkeypatch.setattr(app_sync.sys, "platform", "linux")

    with pytest.raises(WorkspaceError) as caught:
        app_sync.app_sync_status()

    assert caught.value.error_code == "APP_SYNC_UNSUPPORTED_PLATFORM"
