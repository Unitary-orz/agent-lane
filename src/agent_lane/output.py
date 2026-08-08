"""Stable machine-readable output contracts for agent-lane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = 1


@dataclass
class CliUsageError(RuntimeError):
    """Argument parsing failed before a command handler ran."""

    message: str
    command: str = "unknown"
    details: Mapping[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def success_envelope(
    command: str,
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Wrap one successful command result in the V1 envelope."""

    data = dict(result or {})
    data.pop("ok", None)
    raw_warnings = data.pop("warnings", [])
    warnings = _warning_list(raw_warnings)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "data": data,
        "error": None,
        "warnings": warnings,
    }


def failure_envelope(
    command: str,
    error: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize legacy structured failures into the V1 envelope."""

    payload = dict(error)
    payload.pop("ok", None)
    code = str(payload.pop("error_code", "AGENT_LANE_ERROR"))
    message = str(payload.pop("error", "agent-lane command failed"))
    retryable = bool(payload.pop("retryable", False))
    raw_warnings = payload.pop("warnings", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": payload,
        },
        "warnings": _warning_list(raw_warnings),
    }


def usage_failure(
    command: str,
    message: str,
    *,
    code: str = "CLI_USAGE_ERROR",
    **details: Any,
) -> dict[str, Any]:
    return failure_envelope(
        command,
        {
            "ok": False,
            "error_code": code,
            "error": message,
            "retryable": False,
            **details,
        },
    )


def _warning_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        value = [value]
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            warning = dict(item)
            warning.setdefault("code", "AGENT_LANE_WARNING")
            warning.setdefault("message", str(warning["code"]))
            normalized.append(warning)
        elif item is not None:
            normalized.append(
                {
                    "code": "AGENT_LANE_WARNING",
                    "message": str(item),
                }
            )
    return normalized
