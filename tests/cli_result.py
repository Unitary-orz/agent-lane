"""Test helpers for the public V1 CLI envelope."""

from __future__ import annotations

import json
from typing import Any


def decode_cli_output(output: str) -> dict[str, Any]:
    """Return a command payload while asserting the public envelope shape."""

    assert output.strip(), "agent-lane produced no stdout"
    envelope = json.loads(output)
    assert envelope["schema_version"] == 1
    assert isinstance(envelope["command"], str)
    assert isinstance(envelope["warnings"], list)
    if envelope["ok"]:
        data = dict(envelope["data"] or {})
        return {"ok": True, **data}
    error = dict(envelope["error"] or {})
    details = dict(error.get("details") or {})
    return {
        "ok": False,
        "error_code": error.get("code"),
        "error": error.get("message"),
        "retryable": bool(error.get("retryable")),
        **details,
    }
