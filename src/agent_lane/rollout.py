"""Read durable closeout facts from a Codex rollout."""

from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any


def read_rollout_closeout(
    thread_id: str,
    *,
    session_path: str | None = None,
    sessions_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return the latest task, goal, and assistant closeout facts for a thread."""

    root = (sessions_root or (Path.home() / ".codex" / "sessions")).expanduser()
    for path in _rollout_candidates(thread_id, session_path=session_path, root=root):
        result = _read_rollout(path, thread_id)
        if result is not None:
            return result
    return None


def _rollout_candidates(
    thread_id: str,
    *,
    session_path: str | None,
    root: Path,
) -> list[Path]:
    candidates: list[Path] = []
    if session_path:
        candidate = _safe_rollout_path(Path(session_path), thread_id, root)
        if candidate is not None:
            candidates.append(candidate)

    pattern = str(
        Path(glob.escape(str(root)))
        / "*"
        / "*"
        / "*"
        / f"rollout-*-{glob.escape(thread_id)}.jsonl"
    )
    for raw_path in glob.glob(pattern):
        candidate = _safe_rollout_path(Path(raw_path), thread_id, root)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)

    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(candidates, key=modified_at, reverse=True)


def _safe_rollout_path(path: Path, thread_id: str, root: Path) -> Path | None:
    try:
        resolved_root = root.resolve()
        resolved = path.expanduser().resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    if not resolved.name.startswith("rollout-"):
        return None
    if not resolved.name.endswith(f"-{thread_id}.jsonl"):
        return None
    return resolved


def _read_rollout(path: Path, thread_id: str) -> dict[str, Any] | None:
    latest_goal: dict[str, Any] | None = None
    latest_task_type: str | None = None
    latest_task: dict[str, Any] | None = None
    latest_task_at: Any = None
    active_turn_id: Any = None
    active_turn_started_at: Any = None
    active_turn_user_message: str | None = None
    active_turn_user_message_at: Any = None
    active_turn_agent_message: str | None = None
    active_turn_agent_message_at: Any = None
    phase_aware = False
    explicit_final_seen = False
    explicit_final: str | None = None
    explicit_final_at: Any = None
    legacy_assistant: str | None = None
    legacy_assistant_at: Any = None

    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            first = _json_object(first_line)
            if not _session_matches(first, thread_id):
                return None

            for line in handle:
                if not _relevant_line(line):
                    continue
                record = _json_object(line)
                if record is None:
                    continue
                record_type = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record_type == "event_msg":
                    event_type = payload.get("type")
                    if event_type == "thread_goal_updated":
                        goal = payload.get("goal")
                        goal_thread_id = (
                            payload.get("threadId")
                            or (goal.get("threadId") if isinstance(goal, dict) else None)
                        )
                        if (
                            isinstance(goal, dict)
                            and str(goal_thread_id or thread_id) == thread_id
                        ):
                            latest_goal = dict(goal)
                    elif event_type == "task_started":
                        latest_task_type = "task_started"
                        latest_task = None
                        latest_task_at = None
                        active_turn_id = payload.get("turn_id") or payload.get("turnId")
                        active_turn_started_at = record.get("timestamp")
                        active_turn_user_message = None
                        active_turn_user_message_at = None
                        active_turn_agent_message = None
                        active_turn_agent_message_at = None
                        phase_aware = False
                        explicit_final_seen = False
                        explicit_final = None
                        explicit_final_at = None
                        legacy_assistant = None
                        legacy_assistant_at = None
                    elif event_type == "turn_aborted":
                        latest_task_type = "turn_aborted"
                        latest_task = None
                        latest_task_at = None
                        active_turn_id = None
                        active_turn_started_at = None
                        active_turn_user_message = None
                        active_turn_user_message_at = None
                        active_turn_agent_message = None
                        active_turn_agent_message_at = None
                        phase_aware = False
                        explicit_final_seen = False
                        explicit_final = None
                        explicit_final_at = None
                        legacy_assistant = None
                        legacy_assistant_at = None
                    elif event_type == "task_complete":
                        latest_task_type = "task_complete"
                        latest_task = dict(payload)
                        latest_task_at = record.get("timestamp")
                    elif event_type == "user_message":
                        message = str(payload.get("message") or "").strip()
                        if message:
                            active_turn_user_message = message
                            active_turn_user_message_at = record.get("timestamp")
                    elif event_type == "agent_message":
                        message = str(payload.get("message") or "").strip()
                        if message:
                            active_turn_agent_message = message
                            active_turn_agent_message_at = record.get("timestamp")
                        (
                            phase_aware,
                            explicit_final_seen,
                            explicit_final,
                            explicit_final_at,
                            legacy_assistant,
                            legacy_assistant_at,
                        ) = _track_assistant(
                            payload.get("phase"),
                            payload.get("message"),
                            record.get("timestamp"),
                            phase_aware=phase_aware,
                            explicit_final_seen=explicit_final_seen,
                            explicit_final=explicit_final,
                            explicit_final_at=explicit_final_at,
                            legacy_assistant=legacy_assistant,
                            legacy_assistant_at=legacy_assistant_at,
                        )
                elif record_type == "response_item" and payload.get("type") == "message":
                    message = _response_message_text(payload).strip()
                    if payload.get("role") == "user" and message:
                        active_turn_user_message = message
                        active_turn_user_message_at = record.get("timestamp")
                    if payload.get("role") != "assistant":
                        continue
                    if message:
                        active_turn_agent_message = message
                        active_turn_agent_message_at = record.get("timestamp")
                    (
                        phase_aware,
                        explicit_final_seen,
                        explicit_final,
                        explicit_final_at,
                        legacy_assistant,
                        legacy_assistant_at,
                    ) = _track_assistant(
                        payload.get("phase"),
                        message,
                        record.get("timestamp"),
                        phase_aware=phase_aware,
                        explicit_final_seen=explicit_final_seen,
                        explicit_final=explicit_final,
                        explicit_final_at=explicit_final_at,
                        legacy_assistant=legacy_assistant,
                        legacy_assistant_at=legacy_assistant_at,
                    )
    except (OSError, UnicodeError):
        return None

    task_complete = latest_task if latest_task_type == "task_complete" else None
    assistant_message = (
        explicit_final
        if explicit_final_seen
        else (None if phase_aware else legacy_assistant)
    )
    assistant_message_at = (
        explicit_final_at
        if explicit_final_seen
        else (None if phase_aware else legacy_assistant_at)
    )
    try:
        rollout_mtime = path.stat().st_mtime
    except OSError:
        rollout_mtime = None
    return {
        "status": "completed" if task_complete is not None else None,
        "turn_id": task_complete.get("turn_id") if task_complete else None,
        "task_complete_message": (
            task_complete.get("last_agent_message") if task_complete else None
        ),
        "task_complete_at": latest_task_at if task_complete else None,
        "assistant_message": assistant_message,
        "assistant_message_at": assistant_message_at,
        "active_turn_id": (
            active_turn_id if latest_task_type == "task_started" else None
        ),
        "active_turn_started_at": (
            active_turn_started_at if latest_task_type == "task_started" else None
        ),
        "active_turn_user_message": (
            active_turn_user_message if latest_task_type == "task_started" else None
        ),
        "active_turn_user_message_at": (
            active_turn_user_message_at if latest_task_type == "task_started" else None
        ),
        "active_turn_agent_message": (
            active_turn_agent_message if latest_task_type == "task_started" else None
        ),
        "active_turn_agent_message_at": (
            active_turn_agent_message_at if latest_task_type == "task_started" else None
        ),
        "rollout_mtime": rollout_mtime,
        "goal": latest_goal,
        "source": "rollout",
    }


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _session_matches(record: dict[str, Any] | None, thread_id: str) -> bool:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    payload = record.get("payload")
    return isinstance(payload, dict) and str(payload.get("id") or "") == thread_id


def _relevant_line(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "thread_goal_updated",
            "task_started",
            "task_complete",
            "turn_aborted",
            "user_message",
            "agent_message",
            '"assistant"',
            '"role":"user"',
            '"role": "user"',
        )
    )


def _track_assistant(
    phase: Any,
    message: Any,
    timestamp: Any,
    *,
    phase_aware: bool,
    explicit_final_seen: bool,
    explicit_final: str | None,
    explicit_final_at: Any,
    legacy_assistant: str | None,
    legacy_assistant_at: Any,
) -> tuple[bool, bool, str | None, Any, str | None, Any]:
    text = str(message or "").strip()
    if phase is not None:
        phase_aware = True
    if phase == "final_answer":
        explicit_final_seen = True
        explicit_final = text or None
        explicit_final_at = timestamp
    elif phase is None and text:
        legacy_assistant = text
        legacy_assistant_at = timestamp
    return (
        phase_aware,
        explicit_final_seen,
        explicit_final,
        explicit_final_at,
        legacy_assistant,
        legacy_assistant_at,
    )


def _response_message_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for content in payload.get("content") or []:
        if not isinstance(content, dict):
            continue
        text = content.get("text")
        if text:
            parts.append(str(text))
    if parts:
        return "\n".join(parts)
    return str(payload.get("text") or "")
