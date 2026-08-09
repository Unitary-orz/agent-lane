"""Self-configuration and diagnostic command registration."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

from .. import control_plane


def system_handlers() -> dict[str, Callable[[Namespace], dict[str, Any]]]:
    return {
        "doctor": control_plane.cmd_doctor_v1,
        "config.app-sync.enable": control_plane.cmd_app_sync_enable,
        "config.app-sync.status": control_plane.cmd_app_sync_status,
        "config.app-sync.disable": control_plane.cmd_app_sync_disable,
        "config.effort.set": control_plane.cmd_effort_set,
        "config.effort.status": control_plane.cmd_effort_status,
        "config.effort.clear": control_plane.cmd_effort_clear,
        "internal.app-sync-login": control_plane.cmd_app_sync_login,
        "signing.init": control_plane.cmd_codex_signing_init,
        "signing.status": control_plane.cmd_codex_signing_status,
        "signing.test": control_plane.cmd_codex_signing_test,
        "signing.stop": control_plane.cmd_codex_signing_stop,
    }
