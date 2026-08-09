"""Per-user agent-lane settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


USER_CONFIG_SCHEMA_VERSION = 1
DEFAULT_USER_CONFIG_PATH = Path.home() / ".agent-lane" / "config.json"
USER_CONFIG_PATH_ENV = "AGENT_LANE_CONFIG_PATH"


class UserConfigError(ValueError):
    """The user configuration cannot be read without guessing."""


def user_config_path() -> Path:
    override = str(os.environ.get(USER_CONFIG_PATH_ENV) or "").strip()
    return Path(override).expanduser() if override else DEFAULT_USER_CONFIG_PATH


def normalize_effort(value: Any, *, label: str = "effort") -> str:
    if not isinstance(value, str):
        raise UserConfigError(f"{label} must be a string")
    effort = value.strip()
    if not effort:
        raise UserConfigError(f"{label} requires a non-empty value")
    return "xhigh" if effort.casefold() == "xh" else effort


def read_default_effort(path: Path | None = None) -> dict[str, Any]:
    resolved = path or user_config_path()
    config = _read_config(resolved)
    defaults = config.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise UserConfigError("defaults must be an object")
    if isinstance(defaults, dict) and "effort" in defaults:
        current = normalize_effort(defaults["effort"], label="defaults.effort")
        if "effort" in config:
            legacy = normalize_effort(config["effort"], label="effort")
            if current != legacy:
                raise UserConfigError(
                    "defaults.effort and legacy effort fields disagree"
                )
        return {
            "value": current,
            "source": "user_config",
            "path": resolved,
            "config": config,
        }
    if "effort" in config:
        return {
            "value": normalize_effort(config["effort"], label="effort"),
            "source": "user_config_legacy",
            "path": resolved,
            "config": config,
        }
    return {
        "value": None,
        "source": "unset",
        "path": resolved,
        "config": config,
    }


def set_default_effort(value: str, path: Path | None = None) -> dict[str, Any]:
    resolved = path or user_config_path()
    normalized = normalize_effort(value)
    config = dict(_read_config(resolved))
    config.pop("effort", None)
    defaults = config.get("defaults")
    defaults = dict(defaults) if isinstance(defaults, dict) else {}
    defaults["effort"] = normalized
    config.update(
        {
            "schema_version": USER_CONFIG_SCHEMA_VERSION,
            "defaults": defaults,
        }
    )
    _write_config(resolved, config)
    return read_default_effort(resolved)


def clear_default_effort(path: Path | None = None) -> dict[str, Any]:
    resolved = path or user_config_path()
    config = dict(_read_config(resolved))
    config.pop("effort", None)
    defaults = config.get("defaults")
    defaults = dict(defaults) if isinstance(defaults, dict) else {}
    defaults.pop("effort", None)
    config.update(
        {
            "schema_version": USER_CONFIG_SCHEMA_VERSION,
            "defaults": defaults,
        }
    )
    _write_config(resolved, config)
    return read_default_effort(resolved)


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UserConfigError(f"could not read user config: {exc}") from exc
    if not isinstance(data, dict):
        raise UserConfigError("user config must be a JSON object")
    schema_version = data.get("schema_version")
    if schema_version not in {None, USER_CONFIG_SCHEMA_VERSION}:
        raise UserConfigError(
            f"unsupported user config schema_version {schema_version!r}"
        )
    return data


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
