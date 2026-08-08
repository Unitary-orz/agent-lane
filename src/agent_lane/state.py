"""Alias files for agent lanes."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


DEFAULT_ALIAS_ROOT = Path.home() / ".agent-lane" / "lanes"


def safe_lane_id(lane_id: str) -> str:
    """Return a filesystem-safe lane id while preserving readability."""
    cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", lane_id.strip())
    cleaned = cleaned.strip(".-")
    if not cleaned:
        raise ValueError("lane-id cannot be empty")
    if len(cleaned) > 160:
        cleaned = cleaned[:160].rstrip(".-")
    return cleaned


def alias_dir(provider: str, root: Path | None = None) -> Path:
    return (root or DEFAULT_ALIAS_ROOT) / provider


def alias_path(provider: str, lane_id: str, root: Path | None = None) -> Path:
    return alias_dir(provider, root) / f"{safe_lane_id(lane_id)}.json"


def load_alias(provider: str, lane_id: str, root: Path | None = None) -> dict[str, Any] | None:
    path = alias_path(provider, lane_id, root)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"lane alias file is not an object: {path}")
    return data


def save_alias(
    provider: str,
    lane_id: str,
    data: dict[str, Any],
    root: Path | None = None,
) -> Path:
    path = alias_path(provider, lane_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["lane_id"] = lane_id
    data["provider"] = provider
    data["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    return path


def list_aliases(provider: str, root: Path | None = None) -> list[dict[str, Any]]:
    directory = alias_dir(provider, root)
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data["_path"] = str(path)
                items.append(data)
        except Exception as exc:
            items.append({"_path": str(path), "error": str(exc)})
    return items
