"""Session-view command registration."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

from .. import control_plane


def session_handlers() -> dict[str, Callable[[Namespace], dict[str, Any]]]:
    return {
        "codex.session.list": control_plane.cmd_session_list_v1,
        "codex.session.find": control_plane.cmd_session_find_v1,
        "codex.session.attach": control_plane.cmd_codex_adopt,
        "codex.session.name.get": control_plane.cmd_session_name_get_v1,
        "codex.session.name.set": control_plane.cmd_codex_name_set,
        "codex.custom-title.get": control_plane.cmd_codex_custom_title_get,
        "codex.custom-title.set": control_plane.cmd_codex_custom_title_set,
        "codex.custom-title.clear": control_plane.cmd_codex_custom_title_clear,
        "codex.session.outline": control_plane.cmd_codex_outline,
        "codex.session.read": control_plane.cmd_codex_read,
    }
