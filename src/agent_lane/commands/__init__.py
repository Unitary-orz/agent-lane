"""Command registries grouped by the V1 product layers."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

from .execution import execution_handlers
from .session import session_handlers
from .system import system_handlers


CommandHandler = Callable[[Namespace], dict[str, Any]]


def command_handlers() -> dict[str, CommandHandler]:
    """Return the complete, collision-free V1 command registry."""

    registry: dict[str, CommandHandler] = {}
    for group in (system_handlers(), execution_handlers(), session_handlers()):
        overlap = registry.keys() & group.keys()
        if overlap:
            names = ", ".join(sorted(overlap))
            raise RuntimeError(f"duplicate command handler registration: {names}")
        registry.update(group)
    return registry
