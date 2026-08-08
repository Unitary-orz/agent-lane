import json

from agent_lane.rollout import read_rollout_closeout


def _write_rollout(root, thread_id, records):
    directory = root / "2026" / "07" / "20"
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-07-20T12-00-00-{thread_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in [
            {
                "timestamp": "2026-07-20T04:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id},
            },
            *records,
        ]:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")
    return path


def _event(event_type, **payload):
    return {
        "timestamp": "2026-07-20T04:00:01Z",
        "type": "event_msg",
        "payload": {"type": event_type, **payload},
    }


def _assistant(text, phase="final_answer"):
    return {
        "timestamp": "2026-07-20T04:00:02Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def test_rollout_closeout_extracts_task_goal_usage_and_final_message(tmp_path):
    thread_id = "019f8000-0000-7000-8000-000000000001"
    path = _write_rollout(
        tmp_path,
        thread_id,
        [
            _event("task_started", turn_id="active-turn"),
            _assistant("Assistant fallback."),
            _event(
                "thread_goal_updated",
                threadId=thread_id,
                goal={
                    "threadId": thread_id,
                    "objective": "Finish",
                    "status": "complete",
                    "tokensUsed": 1234,
                    "timeUsedSeconds": 56,
                },
            ),
            _event(
                "task_complete",
                turn_id="turn-1",
                last_agent_message="Task complete lead.\n\nDetails.",
            ),
        ],
    )

    result = read_rollout_closeout(
        thread_id,
        session_path=str(path),
        sessions_root=tmp_path,
    )

    assert result == {
        "status": "completed",
        "turn_id": "turn-1",
        "task_complete_message": "Task complete lead.\n\nDetails.",
        "task_complete_at": "2026-07-20T04:00:01Z",
        "assistant_message": "Assistant fallback.",
        "assistant_message_at": "2026-07-20T04:00:02Z",
        "active_turn_id": None,
        "active_turn_started_at": None,
        "active_turn_user_message": None,
        "active_turn_user_message_at": None,
        "active_turn_agent_message": None,
        "active_turn_agent_message_at": None,
        "rollout_mtime": path.stat().st_mtime,
        "goal": {
            "threadId": thread_id,
            "objective": "Finish",
            "status": "complete",
            "tokensUsed": 1234,
            "timeUsedSeconds": 56,
        },
        "source": "rollout",
    }


def test_rollout_closeout_uses_last_assistant_when_task_message_is_missing(
    tmp_path,
):
    thread_id = "019f8000-0000-7000-8000-000000000002"
    _write_rollout(
        tmp_path,
        thread_id,
        [
            _event("task_started"),
            _assistant("Final assistant fallback.\n\nDetails."),
            _event("task_complete", turn_id="turn-2", last_agent_message=""),
        ],
    )

    result = read_rollout_closeout(thread_id, sessions_root=tmp_path)

    assert result["status"] == "completed"
    assert result["task_complete_message"] == ""
    assert result["assistant_message"] == "Final assistant fallback.\n\nDetails."


def test_rollout_closeout_does_not_reuse_completion_before_new_active_turn(
    tmp_path,
):
    thread_id = "019f8000-0000-7000-8000-000000000003"
    _write_rollout(
        tmp_path,
        thread_id,
        [
            _event("task_started"),
            _assistant("Old final."),
            _event(
                "thread_goal_updated",
                threadId=thread_id,
                goal={"threadId": thread_id, "status": "complete"},
            ),
            _event(
                "task_complete",
                turn_id="old-turn",
                last_agent_message="Old final.",
            ),
            _event("task_started", turn_id="active-turn"),
            _event(
                "thread_goal_updated",
                threadId=thread_id,
                goal={"threadId": thread_id, "status": "active"},
            ),
            _event("user_message", message="Current active request."),
            _assistant("Progress only.", phase="commentary"),
        ],
    )

    result = read_rollout_closeout(thread_id, sessions_root=tmp_path)

    assert result["status"] is None
    assert result["turn_id"] is None
    assert result["task_complete_message"] is None
    assert result["assistant_message"] is None
    assert result["active_turn_id"] == "active-turn"
    assert result["active_turn_started_at"] == "2026-07-20T04:00:01Z"
    assert result["active_turn_user_message"] == "Current active request."
    assert result["active_turn_user_message_at"] == "2026-07-20T04:00:01Z"
    assert result["active_turn_agent_message"] == "Progress only."
    assert result["active_turn_agent_message_at"] == "2026-07-20T04:00:02Z"
    assert result["goal"]["status"] == "active"
