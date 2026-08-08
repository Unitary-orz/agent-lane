"""Public command-line entrypoint for agent-lane."""

from .entry import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
