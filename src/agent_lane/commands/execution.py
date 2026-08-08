"""Execution-layer command registration."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

from .. import control_plane


def execution_handlers() -> dict[str, Callable[[Namespace], dict[str, Any]]]:
    return {
        "codex.run": control_plane.cmd_codex_run,
        "codex.send": control_plane.cmd_codex_send,
        "codex.steer": control_plane.cmd_codex_steer,
        "codex.status": control_plane.cmd_status_v1,
        "codex.closeout": control_plane.cmd_codex_closeout,
        "codex.cleanup": control_plane.cmd_codex_cleanup,
        "codex.wait": control_plane.cmd_codex_wait,
        "codex.watch": control_plane.cmd_codex_watch,
        "codex.checkpoint": control_plane.cmd_codex_checkpoint,
        "codex.goal.set": control_plane.cmd_codex_goal_set,
        "codex.goal.run": control_plane.cmd_codex_goal_run,
        "codex.goal.get": control_plane.cmd_codex_goal_get,
        "codex.goal.complete": control_plane.cmd_codex_goal_complete,
        "codex.goal.clear": control_plane.cmd_codex_goal_clear,
    }
