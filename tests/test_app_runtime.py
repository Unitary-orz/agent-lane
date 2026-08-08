from types import SimpleNamespace

import pytest

import agent_lane.app_runtime as runtime
from agent_lane.app_runtime import AppRuntimeError


def test_unknown_app_build_is_diagnostic_not_a_detection_gate(tmp_path, monkeypatch):
    app = tmp_path / "ChatGPT.app"
    app.mkdir()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime,
        "_running_codex_processes",
        lambda: [runtime._RunningCodexProcess(pid=123, app_path=app)],
    )
    monkeypatch.setattr(runtime, "_read_app_version", lambda _path: ("99.0.0", "9999"))

    detected = runtime.detect_running_codex_app()

    assert detected.version == "99.0.0"
    assert detected.build == "9999"
    assert detected.pid == 123


@pytest.mark.parametrize(
    ("version", "build"),
    [("26.707.72221", "5307"), ("26.707.91948", "5440")],
)
def test_app_metadata_is_preserved_without_a_compatibility_allowlist(
    tmp_path, monkeypatch, version, build
):
    app = tmp_path / "ChatGPT.app"
    app.mkdir()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime,
        "_running_codex_processes",
        lambda: [runtime._RunningCodexProcess(pid=123, app_path=app)],
    )
    monkeypatch.setattr(runtime, "_read_app_version", lambda _path: (version, build))

    detected = runtime.detect_running_codex_app()

    assert detected.version == version
    assert detected.build == build


def test_unreadable_app_metadata_does_not_block_runtime_detection(
    tmp_path, monkeypatch
):
    app = tmp_path / "ChatGPT.app"
    app.mkdir()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime,
        "_running_codex_processes",
        lambda: [runtime._RunningCodexProcess(pid=123, app_path=app)],
    )

    detected = runtime.detect_running_codex_app()

    assert detected.version is None
    assert detected.build is None
    assert "failed to read Codex App metadata" in detected.metadata_error


@pytest.mark.parametrize(
    "failure",
    [OSError("ps unavailable"), SimpleNamespace(returncode=1, stdout="")],
)
def test_process_inspection_failure_is_not_reported_as_app_absent(
    monkeypatch, failure
):
    if isinstance(failure, BaseException):
        def fail_run(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(runtime.subprocess, "run", fail_run)
    else:
        monkeypatch.setattr(runtime.subprocess, "run", lambda *_args, **_kwargs: failure)

    with pytest.raises(AppRuntimeError) as caught:
        runtime._running_codex_processes()

    assert caught.value.error_code == "CODEX_APP_PROCESS_UNAVAILABLE"
    assert caught.value.retryable is True


def test_running_process_metadata_comes_from_that_exact_app_copy(
    tmp_path, monkeypatch
):
    running = tmp_path / "Running.app"
    running.mkdir()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime,
        "_running_codex_processes",
        lambda: [runtime._RunningCodexProcess(pid=456, app_path=running)],
    )
    monkeypatch.setattr(
        runtime,
        "_read_app_version",
        lambda path: ("99.0.0", "9999") if path == running else ("0", None),
    )

    detected = runtime.detect_running_codex_app()

    assert detected.path == running
    assert detected.version == "99.0.0"
    assert detected.build == "9999"
