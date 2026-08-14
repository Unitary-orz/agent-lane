import json
from types import SimpleNamespace

import pytest

import agent_lane.control_plane as cli
from agent_lane.cli import build_parser, main
from cli_result import decode_cli_output
from agent_lane.control_plane import (
    _collect_session_pages,
    _enrich_session_summaries_with_goals,
    _enrich_session_summaries_with_last_turns,
    cmd_codex_find,
    cmd_codex_recent,
    _find_fetch_limit,
    _is_subagent_thread,
    _last_turn_summary,
    _matches_session_summary,
    _merge_thread_items,
    _refresh_aliases_from_codex,
    _session_fetch_limit,
    _session_summaries,
    _thread_locations,
)
from agent_lane.codex_rpc import CodexRpcError
from agent_lane.output import CliUsageError
from agent_lane.state import save_alias


class FakeCodex:
    def __init__(self, threads):
        self.threads = threads

    def read_thread(self, thread_id, include_turns=False):
        return {"thread": self.threads[str(thread_id)]}

    def get_goal(self, _thread_id):
        return None


class FakeCodexContext(FakeCodex):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def test_session_list_and_find_detail_controls_summary_enrichment():
    parser = build_parser()

    recent = parser.parse_args(["codex", "session", "list"])
    recent_no = parser.parse_args(["codex", "session", "list", "--detail", "metadata"])
    find = parser.parse_args(["codex", "session", "find", "needle"])
    find_no = parser.parse_args(["codex", "session", "find", "needle", "--detail", "metadata"])

    assert recent.detail == "compact"
    assert recent.observe == "auto"
    assert recent.scope == "all"
    assert recent_no.detail == "metadata"
    assert find.detail == "compact"
    assert find.observe == "auto"
    assert find_no.detail == "metadata"


def test_include_last_turn_flag_is_not_part_of_lookup_contract():
    parser = build_parser()

    with pytest.raises(CliUsageError):
        parser.parse_args(["codex", "session", "list", "--include-last-turn"])
    with pytest.raises(CliUsageError):
        parser.parse_args(["codex", "session", "find", "needle", "--include-last-turn"])


def test_outline_and_read_parser_support_lane_or_thread_targets():
    parser = build_parser()

    outline = parser.parse_args(
        ["codex", "session", "outline", "--thread-id", "thread-1"]
    )
    selected = parser.parse_args(
        ["codex", "session", "read", "--lane-id", "lane-1", "--turn-index", "2"]
    )

    assert outline.thread_id == "thread-1"
    assert outline.lane_id is None
    assert outline.observe == "auto"
    assert selected.lane_id == "lane-1"
    assert selected.turn_index == 2

    with pytest.raises(CliUsageError):
        parser.parse_args(
            [
                "codex", "session", "outline",
                "--lane-id",
                "lane-1",
                "--thread-id",
                "thread-1",
            ]
        )
    with pytest.raises(CliUsageError):
        parser.parse_args(
            [
                "codex", "session", "read",
                "--thread-id",
                "thread-1",
                "--include-turns",
                "--turn-id",
                "turn-1",
            ]
        )


def test_aliases_only_lookup_reports_last_turn_disabled(tmp_path):
    save_alias(
        "codex",
        "lane-1",
        {"codex_thread_id": "thread-1", "custom_title": "Needle lane"},
        tmp_path,
    )

    recent = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=True,
            refresh=False,
            limit=10,
        )
    )
    found = cmd_codex_find(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=True,
            refresh=False,
            limit=10,
            query="Needle",
        )
    )

    assert recent["include_last_turn"] is False
    assert found["include_last_turn"] is False
    assert recent["view"] == "aliases"
    assert found["view"] == "aliases"


def test_enriched_adopted_session_reclassifies_workspace_from_live_cwd(tmp_path):
    old_cwd = tmp_path / "old-app-worktree"
    live_cwd = tmp_path / "local"
    old_cwd.mkdir()
    live_cwd.mkdir()
    codex = FakeCodex(
        {
            "thread-1": {
                "id": "thread-1",
                "cwd": str(live_cwd),
                "turns": [],
            }
        }
    )
    item = {
        "id": "thread-1",
        "cwd": str(old_cwd),
        "adopted_from": "codex-app",
        "workspace": {
            "kind": "app-worktree",
            "managed_by": "codex-app",
            "path": str(old_cwd),
            "cwd": str(old_cwd),
        },
    }

    enriched = _enrich_session_summaries_with_last_turns(codex, [item])[0]

    assert enriched["locations"]["cwd"] == str(live_cwd)
    assert enriched["workspace"]["kind"] == "local"
    assert enriched["workspace"]["path"] == str(live_cwd)
    assert enriched["workspace"]["app_native_handoff"] is None


def test_goal_enrichment_replaces_stale_alias_state_and_clears_removed_goal():
    class GoalCodex:
        def __init__(self):
            self.calls = []

        def get_goal(self, thread_id):
            self.calls.append(thread_id)
            if thread_id == "thread-active":
                return {"status": "active", "objective": "Current objective"}
            return None

    codex = GoalCodex()
    items = [
        {
            "id": "thread-active",
            "goal_status": "complete",
            "objective": "Stale objective",
        },
        {
            "id": "thread-cleared",
            "goal_status": "complete",
            "objective": "Finished objective",
        },
    ]

    enriched = _enrich_session_summaries_with_goals(codex, items)

    assert codex.calls == ["thread-active", "thread-cleared"]
    assert enriched[0]["goal_status"] == "active"
    assert enriched[0]["objective"] == "Current objective"
    assert enriched[0]["goal_status_source"] == "thread_goal_get"
    assert enriched[1]["goal_status"] is None
    assert enriched[1]["objective"] is None
    assert enriched[1]["goal_status_source"] == "thread_goal_get"


def test_goal_enrichment_clears_cached_state_when_refresh_fails():
    class BrokenGoalCodex:
        def get_goal(self, _thread_id):
            raise CodexRpcError("goal unavailable")

    enriched = _enrich_session_summaries_with_goals(
        BrokenGoalCodex(),
        [
            {
                "id": "thread-1",
                "goal_status": "active",
                "objective": "Cached objective",
            }
        ],
    )[0]

    assert enriched["goal_status"] is None
    assert enriched["objective"] is None
    assert enriched["goal_status_source"] == "unavailable"
    assert enriched["goal_refresh_error"] == "goal unavailable"


def test_adopted_summary_reclassifies_workspace_without_last_turn_enrichment(
    tmp_path,
):
    old_cwd = tmp_path / "old-app-worktree"
    live_cwd = tmp_path / "local"
    old_cwd.mkdir()
    live_cwd.mkdir()
    aliases = {
        "thread-1": {
            "lane_id": "lane-1",
            "cwd": str(old_cwd),
            "adopted_from": "codex-app",
            "workspace": {
                "kind": "app-worktree",
                "managed_by": "codex-app",
                "path": str(old_cwd),
                "cwd": str(old_cwd),
            },
        }
    }

    summary = _session_summaries(
        [{"id": "thread-1", "cwd": str(live_cwd)}],
        aliases,
        include_subagents=False,
        limit=10,
    )[0]

    assert summary["cwd"] == str(live_cwd)
    assert summary["workspace"]["kind"] == "local"
    assert summary["workspace"]["path"] == str(live_cwd)


def test_session_summary_reconciles_dead_cached_runner(
    monkeypatch,
):
    monkeypatch.setattr(cli, "process_running", lambda _pid: False)
    summaries = _session_summaries(
        [{"id": "thread-1", "status": {"type": "idle"}, "recencyAt": 10}],
        {
            "thread-1": {
                "lane_id": "lane-1",
                "last_status": "inProgress",
                "runner_pid": 999999,
            }
        },
        include_subagents=False,
        limit=1,
    )
    enriched = _enrich_session_summaries_with_goals(
        FakeCodex({}),
        summaries,
    )[0]

    assert enriched["last_status"] == "inProgress"
    assert enriched["local_runner_status"] == "stale"
    assert enriched["runner_status"] == "stale"
    assert enriched["runner_alive"] is False
    assert enriched["execution_active"] is False


def test_refresh_aliases_updates_adopted_cwd_and_workspace(
    tmp_path, monkeypatch
):
    old_cwd = tmp_path / "old-app-worktree"
    live_cwd = tmp_path / "local"
    old_cwd.mkdir()
    live_cwd.mkdir()
    aliases = tmp_path / "aliases"
    alias = {
        "lane_id": "lane-1",
        "codex_thread_id": "thread-1",
        "cwd": str(old_cwd),
        "adopted_from": "codex-app",
        "workspace": {
            "kind": "app-worktree",
            "managed_by": "codex-app",
            "path": str(old_cwd),
        },
    }
    fake = FakeCodexContext(
        {"thread-1": {"id": "thread-1", "cwd": str(live_cwd)}}
    )
    monkeypatch.setattr(cli, "CodexAppServer", lambda: fake)

    refreshed = _refresh_aliases_from_codex([alias], aliases)[0]

    assert refreshed["cwd"] == str(live_cwd)
    assert refreshed["workspace"]["kind"] == "local"
    assert refreshed["workspace"]["path"] == str(live_cwd)


def test_refresh_aliases_reloads_inside_lane_lock_before_saving(
    tmp_path, monkeypatch
):
    aliases = tmp_path / "aliases"
    stale = {
        "lane_id": "lane-1",
        "codex_thread_id": "thread-1",
        "custom_title": "Old custom title",
    }
    save_alias("codex", "lane-1", stale, aliases)

    class ConcurrentTitleCodex(FakeCodexContext):
        def read_thread(self, thread_id, include_turns=False):
            save_alias(
                "codex",
                "lane-1",
                {
                    "lane_id": "lane-1",
                    "codex_thread_id": "thread-1",
                    "custom_title": "Concurrent custom title",
                },
                aliases,
            )
            return super().read_thread(thread_id, include_turns=include_turns)

    fake = ConcurrentTitleCodex(
        {"thread-1": {"id": "thread-1", "name": "Live Codex title"}}
    )
    monkeypatch.setattr(cli, "CodexAppServer", lambda: fake)

    refreshed = _refresh_aliases_from_codex([stale], aliases)[0]
    stored = cli.load_alias("codex", "lane-1", aliases)

    assert refreshed["custom_title"] == "Concurrent custom title"
    assert refreshed["codex_title"] == "Live Codex title"
    assert stored["custom_title"] == "Concurrent custom title"
    assert stored["codex_title"] == "Live Codex title"


def test_session_summaries_exclude_subagent_children_from_main_view():
    parent = {
        "id": "parent-1",
        "name": "Parent session",
        "recencyAt": 10,
        "source": "vscode",
    }
    child = {
        "id": "child-1",
        "name": "Child task",
        "parentThreadId": "parent-1",
        "recencyAt": 20,
        "source": {
            "subAgent": {
                "thread_spawn": {
                    "parent_thread_id": "parent-1",
                    "agent_nickname": "Russell",
                    "agent_role": "worker",
                }
            }
        },
        "agentNickname": "Russell",
        "agentRole": "worker",
    }

    items = _session_summaries(
        [child, parent],
        {},
        include_subagents=False,
        limit=10,
    )

    assert len(items) == 1
    assert items[0]["id"] == "parent-1"
    assert items[0]["name"] == "Parent session"
    assert items[0]["recency_at"] == 10
    assert "subagent_children" not in items[0]


def test_session_summaries_keep_raw_subagent_children_when_requested():
    parent = {"id": "parent-1", "name": "Parent session", "recencyAt": 10}
    child = {
        "id": "child-1",
        "name": "Child task",
        "parentThreadId": "parent-1",
        "recencyAt": 20,
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
    }

    items = _session_summaries(
        [child, parent],
        {},
        include_subagents=True,
        limit=10,
    )

    assert [item["id"] for item in items] == ["child-1", "parent-1"]
    assert items[0]["parent_thread_id"] == "parent-1"
    assert "subagent_child_count" not in items[0]


def test_session_summaries_exclude_parent_thread_id_without_source_marker():
    child = {
        "id": "child-1",
        "name": "Child task",
        "parentThreadId": "parent-1",
        "recencyAt": 20,
        "source": "vscode",
    }

    items = _session_summaries(
        [child],
        {},
        include_subagents=False,
        limit=10,
    )

    assert items == []


def test_session_summaries_do_not_synthesize_aliased_parent_from_child():
    child = {
        "id": "child-1",
        "name": "Child task",
        "parentThreadId": "parent-1",
        "recencyAt": 20,
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
    }
    aliases = {
        "parent-1": {"lane_id": "lane-parent", "custom_title": "Lane parent"}
    }

    items = _session_summaries(
        [child],
        aliases,
        include_subagents=False,
        limit=10,
    )

    assert items == []


def test_session_summaries_sort_main_threads_without_child_recency():
    older_parent = {"id": "parent-1", "name": "Older parent", "recencyAt": 10}
    active_parent = {"id": "parent-2", "name": "Active parent", "recencyAt": 1}
    child = {
        "id": "child-2",
        "name": "Recent child",
        "parentThreadId": "parent-2",
        "recencyAt": 30,
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "parent-2"}}},
    }

    items = _session_summaries(
        [older_parent, active_parent, child],
        {},
        include_subagents=False,
        limit=10,
    )

    assert [item["id"] for item in items] == ["parent-1", "parent-2"]


def test_session_fetch_limit_expands_only_default_view():
    assert _session_fetch_limit(10, include_subagents=False) == 50
    assert _session_fetch_limit(20, include_subagents=False) == 100
    assert _session_fetch_limit(10, include_subagents=True) == 10
    assert _session_fetch_limit(0, include_subagents=False) == 0


def test_find_fetch_limit_expands_for_lookup_recall():
    assert _find_fetch_limit(10) == 50
    assert _find_fetch_limit(20) == 100
    assert _find_fetch_limit(0) == 0


def test_merge_thread_items_deduplicates_by_thread_id():
    merged = _merge_thread_items(
        [{"id": "t1", "name": "old"}, {"id": "t2", "name": "two"}],
        [{"id": "t1", "name": "new"}],
    )

    assert [item["id"] for item in merged] == ["t1", "t2"]
    assert merged[0]["name"] == "new"


def test_collect_session_pages_uses_cursor_until_filtered_limit_is_satisfied():
    children = [
        {
            "id": f"child-{index}",
            "parentThreadId": "parent-1",
            "recencyAt": 100 - index,
        }
        for index in range(50)
    ]
    mains = [
        {"id": f"main-{index}", "name": f"Main {index}", "recencyAt": 40 - index}
        for index in range(5)
    ]

    class PagedCodex:
        def __init__(self):
            self.cursors = []

        def list_threads(self, *, cursor=None, **_kwargs):
            self.cursors.append(cursor)
            if cursor is None:
                return {"data": children, "nextCursor": "page-2"}
            assert cursor == "page-2"
            return {"data": mains, "nextCursor": None}

    codex = PagedCodex()
    items, pagination = _collect_session_pages(
        codex,
        page_limit=50,
        enough=lambda candidates: len(
            _session_summaries(
                candidates,
                {},
                include_subagents=False,
                limit=5,
            )
        )
        >= 5,
    )

    summaries = _session_summaries(
        items,
        {},
        include_subagents=False,
        limit=5,
    )
    assert codex.cursors == [None, "page-2"]
    assert [item["id"] for item in summaries] == [
        "main-0",
        "main-1",
        "main-2",
        "main-3",
        "main-4",
    ]
    assert pagination == {
        "pages": 2,
        "fetched": 55,
        "unique": 55,
        "limit_satisfied": True,
        "scan_exhausted": True,
        "page_cap_reached": False,
        "cursor_stalled": False,
    }


def test_recent_auto_transport_falls_back_to_marked_persisted_stdio(
    tmp_path,
    monkeypatch,
):
    calls = []

    class ReadOnlyCodex:
        transport = "stdio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {
                "data": [
                    {
                        "id": "thread-1",
                        "name": "Stored thread",
                        "recencyAt": 10,
                    }
                ]
            }

        def get_goal(self, _thread_id):
            return None

    def open_codex(*, transport=None):
        calls.append(transport)
        if transport is None:
            raise CodexRpcError(
                "daemon unavailable",
                error_code="CODEX_DAEMON_UNAVAILABLE",
                retryable=True,
            )
        assert transport == "stdio"
        return ReadOnlyCodex()

    monkeypatch.delenv("AGENT_LANE_CODEX_TRANSPORT", raising=False)
    monkeypatch.setattr(cli, "CodexAppServer", open_codex)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=1,
        )
    )

    assert calls == [None, "stdio"]
    assert result["app_server_transport"] == "stdio"
    assert result["transport_degraded"] is True
    assert result["transport_fallback_reason"] == "CODEX_DAEMON_UNAVAILABLE"
    assert result["observation_mode"] == "persisted_stdio"
    assert result["live_status_authoritative"] is False
    assert result["items"][0]["id"] == "thread-1"
    assert result["items"][0]["requested_model"] is None
    assert result["items"][0]["requested_model_source"] == "unknown"
    assert result["items"][0]["requested_effort"] is None
    assert result["items"][0]["requested_effort_source"] == "unknown"


def test_persisted_stdio_turn_is_evidence_not_an_authoritative_terminal_state(
    tmp_path,
    monkeypatch,
):
    class PersistedCodex:
        transport = "stdio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {
                "data": [
                    {
                        "id": "thread-1",
                        "name": "Persisted task",
                        "cwd": str(tmp_path),
                        "status": {"type": "notLoaded"},
                        "recencyAt": 10,
                    }
                ]
            }

        def read_thread(self, thread_id, include_turns=False):
            assert thread_id == "thread-1"
            assert include_turns is True
            return {
                "thread": {
                    "id": thread_id,
                    "cwd": str(tmp_path),
                    "status": {"type": "notLoaded"},
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "interrupted",
                            "items": [],
                        }
                    ],
                }
            }

        def get_goal(self, _thread_id):
            return None

    def open_codex(*, transport=None):
        if transport is None:
            raise CodexRpcError(
                "daemon unavailable",
                error_code="CODEX_DAEMON_UNAVAILABLE",
                retryable=True,
            )
        assert transport == "stdio"
        return PersistedCodex()

    monkeypatch.delenv("AGENT_LANE_CODEX_TRANSPORT", raising=False)
    monkeypatch.setattr(cli, "CodexAppServer", open_codex)
    alias_root = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_status": "interrupted",
        },
        alias_root,
    )

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(alias_root),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=True,
            refresh=False,
            limit=1,
            observe="auto",
            detail="summary",
        )
    )

    item = result["items"][0]
    assert item["last_turn"]["status"] == "interrupted"
    assert item["last_turn"]["source"] == "persisted_app_server"
    assert item["runner_status"] == "unknown"
    assert item["execution_active"] is None
    assert item["execution"] == {
        **item["execution"],
        "state": "unknown",
        "active": None,
        "effective_turn_status": "unknown",
        "authoritative": False,
        "stale": True,
        "observation_mode": "persisted_stdio",
    }
    assert isinstance(item["execution"]["observed_at"], float)
    assert item["execution"]["evidence"]["thread"]["authoritative"] is False
    assert result["warnings"][0]["code"] == (
        "CODEX_SESSION_STATE_NON_AUTHORITATIVE"
    )


def test_lane_scope_auto_fallback_preserves_warning_and_unknown_state(
    tmp_path,
    monkeypatch,
    capsys,
):
    alias_root = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "cwd": str(tmp_path),
            "last_status": "interrupted",
        },
        alias_root,
    )

    class PersistedCodex:
        transport = "stdio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read_thread(self, thread_id, include_turns=False):
            assert thread_id == "thread-1"
            assert include_turns is False
            return {
                "thread": {
                    "id": thread_id,
                    "cwd": str(tmp_path),
                    "status": {"type": "notLoaded"},
                }
            }

    def open_codex(*, transport=None):
        if transport is None:
            raise CodexRpcError(
                "daemon unavailable",
                error_code="CODEX_DAEMON_UNAVAILABLE",
                retryable=True,
            )
        assert transport == "stdio"
        return PersistedCodex()

    monkeypatch.delenv("AGENT_LANE_CODEX_TRANSPORT", raising=False)
    monkeypatch.setattr(cli, "CodexAppServer", open_codex)

    rc = main(
        [
            "codex",
            "session",
            "list",
            "--scope",
            "lanes",
            "--alias-root",
            str(alias_root),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    item = envelope["data"]["items"][0]

    assert rc == 0
    assert envelope["warnings"][0]["code"] == (
        "CODEX_SESSION_STATE_NON_AUTHORITATIVE"
    )
    assert envelope["data"]["live_status_authoritative"] is False
    assert item["execution"]["state"] == "unknown"
    assert item["execution"]["status"] == "unknown"
    assert item["execution"]["authoritative"] is False
    assert item["execution"]["stale"] is True


def test_lane_scope_stored_never_promotes_alias_terminal_status(
    tmp_path,
    monkeypatch,
    capsys,
):
    alias_root = tmp_path / "aliases"
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Needle lane",
            "cwd": str(tmp_path),
            "last_status": "interrupted",
        },
        alias_root,
    )
    monkeypatch.setattr(
        cli,
        "CodexAppServer",
        lambda *_args, **_kwargs: pytest.fail(
            "stored lane scope must not open app-server"
        ),
    )

    for command in (["list"], ["find", "Needle"]):
        rc = main(
            [
                "codex",
                "session",
                *command,
                "--scope",
                "lanes",
                "--observe",
                "stored",
                "--alias-root",
                str(alias_root),
            ]
        )
        envelope = json.loads(capsys.readouterr().out)
        item = envelope["data"]["items"][0]

        assert rc == 0
        assert envelope["warnings"][0]["code"] == (
            "CODEX_SESSION_STATE_NON_AUTHORITATIVE"
        )
        assert envelope["data"]["observation_mode"] == "stored_alias"
        assert envelope["data"]["live_status_authoritative"] is False
        assert item["execution"]["state"] == "unknown"
        assert item["execution"]["status"] == "unknown"
        assert item["execution"]["authoritative"] is False
        assert item["execution"]["stale"] is True


def test_session_list_default_compact_projection_is_small_and_actionable(
    tmp_path,
    monkeypatch,
    capsys,
):
    class CompactCodex:
        transport = "daemon"

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {
                "data": [
                    {
                        "id": "thread-1",
                        "name": None,
                        "cwd": str(tmp_path),
                        "status": {"type": "active"},
                        "recencyAt": 10,
                        "preview": "Compact task\n" + "x" * 2000,
                    }
                ]
            }

        def get_goal(self, _thread_id):
            return None

    monkeypatch.setattr(cli, "CodexAppServer", CompactCodex)

    rc = main(
        [
            "codex",
            "session",
            "list",
            "--observe",
            "live",
            "--limit",
            "1",
            "--alias-root",
            str(tmp_path / "aliases"),
        ]
    )
    raw = capsys.readouterr().out
    result = decode_cli_output(raw)

    assert rc == 0, result
    assert result["detail"] == "compact"
    assert "project_groups" not in result
    assert result["items"] == [
        {
            "thread_id": "thread-1",
            "title": "Compact task",
            "cwd": str(tmp_path),
            "updated_at": 10.0,
            "execution": {
                "state": "active",
                "status": "inProgress",
                "authoritative": True,
                "stale": False,
                "observed_at": result["items"][0]["execution"]["observed_at"],
            },
            "final_lead": None,
            "requires_attach": True,
        }
    ]
    assert isinstance(result["items"][0]["execution"]["observed_at"], float)
    assert len(raw.encode()) < 4096


def test_recent_auto_transport_falls_back_when_app_transport_is_unobserved(
    tmp_path,
    monkeypatch,
):
    calls = []

    class ReadOnlyCodex:
        transport = "stdio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {"data": []}

    def open_codex(*, transport=None):
        calls.append(transport)
        if transport is None:
            raise CodexRpcError(
                "transport unobserved",
                error_code="CODEX_APP_TRANSPORT_UNOBSERVED",
                retryable=True,
            )
        return ReadOnlyCodex()

    monkeypatch.delenv("AGENT_LANE_CODEX_TRANSPORT", raising=False)
    monkeypatch.setattr(cli, "CodexAppServer", open_codex)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=1,
        )
    )

    assert calls == [None, "stdio"]
    assert result["transport_degraded"] is True
    assert result["transport_fallback_reason"] == (
        "CODEX_APP_TRANSPORT_UNOBSERVED"
    )
    assert result["live_status_authoritative"] is False


def test_recent_refreshes_goal_without_last_turn_reads(tmp_path, monkeypatch):
    save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "goal_status": "complete",
            "objective": "Stale objective",
            "requested_model": "gpt-test",
            "requested_model_source": "alias",
            "requested_effort": None,
            "requested_effort_source": "default-or-unset",
        },
        tmp_path,
    )

    class GoalCodex:
        transport = "daemon"

        def __init__(self):
            self.goal_calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {"data": [{"id": "thread-1", "recencyAt": 10}]}

        def get_goal(self, thread_id):
            self.goal_calls.append(thread_id)
            return {"status": "active", "objective": "Current objective"}

    codex = GoalCodex()
    monkeypatch.setattr(cli, "CodexAppServer", lambda *args, **kwargs: codex)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=1,
        )
    )

    assert codex.goal_calls == ["thread-1"]
    assert result["items"][0]["goal_status"] == "active"
    assert result["items"][0]["objective"] == "Current objective"
    assert result["items"][0]["goal_status_source"] == "thread_goal_get"
    assert result["items"][0]["requested_model"] == "gpt-test"
    assert result["items"][0]["requested_model_source"] == "alias"
    assert result["items"][0]["requested_effort"] is None
    assert result["items"][0]["requested_effort_source"] == "default-or-unset"


def test_recent_sorts_completed_sessions_by_last_turn_completion_before_limit(
    tmp_path,
    monkeypatch,
):
    recent_threads = [
        {
            "id": f"thread-recent-{index}",
            "name": f"Recent {index}",
            "status": {"type": "idle"},
            "recencyAt": 200 - index,
        }
        for index in range(5)
    ]
    long_running = {
        "id": "thread-long",
        "name": "Long completed task",
        "status": {"type": "notLoaded"},
        "recencyAt": 100,
    }

    class CompletionAwareCodex:
        transport = "daemon"

        def __init__(self):
            self.thread_reads = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {"data": [*recent_threads, long_running]}

        def get_goal(self, _thread_id):
            return None

        def read_thread(self, thread_id, include_turns=False):
            self.thread_reads.append((thread_id, include_turns))
            raw = next(
                item
                for item in [*recent_threads, long_running]
                if item["id"] == thread_id
            )
            completed_at = 300 if thread_id == "thread-long" else raw["recencyAt"] + 1
            return {
                "thread": {
                    **raw,
                    "turns": [
                        {
                            "id": f"turn-{thread_id}",
                            "status": "completed",
                            "startedAt": raw["recencyAt"],
                            "completedAt": completed_at,
                            "items": [],
                        }
                    ],
                }
            }

    codex = CompletionAwareCodex()
    monkeypatch.setattr(cli, "CodexAppServer", lambda *args, **kwargs: codex)
    monkeypatch.setattr(cli, "read_rollout_closeout", lambda *_args, **_kwargs: None)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=True,
            refresh=False,
            limit=5,
        )
    )

    assert [item["id"] for item in result["items"]] == [
        "thread-long",
        "thread-recent-0",
        "thread-recent-1",
        "thread-recent-2",
        "thread-recent-3",
    ]
    assert result["items"][0]["thread_recency_at"] == 100
    assert result["items"][0]["recency_at"] == 300
    assert result["items"][0]["recency_source"] == "last_turn_completed_at"
    assert len(codex.thread_reads) == 6
    assert all(include_turns is True for _, include_turns in codex.thread_reads)
    assert result["sort"]["semantics"] == (
        "active_goals_first_then_completion_aware_activity_at"
    )


def test_recent_uses_rollout_completion_without_transcript_reads_and_live_mtime(
    tmp_path,
    monkeypatch,
):
    save_alias(
        "codex",
        "completed-lane",
        {
            "codex_thread_id": "thread-completed",
            "custom_title": "Completed task",
            "codex_recency_at": 20,
        },
        tmp_path,
    )
    threads = [
        {
            "id": "thread-live",
            "name": "Live task",
            "status": {"type": "active"},
            "recencyAt": 10,
        }
    ]

    class RolloutAwareCodex:
        transport = "daemon"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {"data": threads}

        def get_goal(self, _thread_id):
            return None

        def read_thread(self, *_args, **_kwargs):
            raise AssertionError("transcript reads must stay disabled")

    rollout_facts = {
        "thread-live": {
            "status": None,
            "task_complete_at": None,
            "assistant_message_at": None,
            "rollout_mtime": 400,
        },
        "thread-completed": {
            "status": "completed",
            "task_complete_at": "1970-01-01T00:05:00Z",
            "assistant_message_at": "1970-01-01T00:04:59Z",
            "rollout_mtime": 301,
        },
    }
    monkeypatch.setattr(
        cli,
        "CodexAppServer",
        lambda *args, **kwargs: RolloutAwareCodex(),
    )
    monkeypatch.setattr(
        cli,
        "read_rollout_closeout",
        lambda thread_id, **_kwargs: rollout_facts[thread_id],
    )

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=2,
        )
    )

    assert [item["id"] for item in result["items"]] == [
        "thread-live",
        "thread-completed",
    ]
    assert result["items"][0]["recency_at"] == 400
    assert result["items"][0]["recency_source"] == "live_rollout_mtime"
    assert result["items"][1]["recency_at"] == 300
    assert result["items"][1]["recency_source"] == "rollout_task_complete_at"
    assert result["items"][1]["lane_id"] == "completed-lane"


def test_recent_uses_rollout_active_turn_instead_of_previous_final(
    tmp_path,
    monkeypatch,
):
    save_alias(
        "codex",
        "security-ops-agent",
        {
            "codex_thread_id": "thread-active",
            "custom_title": "Security ops active",
            "last_status": "completed",
            "last_final_text": "Old V0 closeout final.",
            "goal_status": "active",
        },
        tmp_path,
    )

    class ActiveSessionCodex:
        transport = "daemon"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {
                "data": [
                    {
                        "id": "thread-active",
                        "name": "Security ops active",
                        "status": {"type": "idle"},
                        "recencyAt": 100,
                    }
                ]
            }

        def get_goal(self, _thread_id):
            return {"status": "active", "objective": "Cross-model acceptance"}

        def read_thread(self, _thread_id, include_turns=False):
            assert include_turns is True
            return {
                "thread": {
                    "id": "thread-active",
                    "status": {"type": "idle"},
                    "turns": [
                        {
                            "id": "previous-turn",
                            "status": "completed",
                            "startedAt": 10,
                            "completedAt": 20,
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [
                                        {"type": "text", "text": "Close V0."}
                                    ],
                                },
                                {
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": "Old V0 closeout final.",
                                },
                            ],
                        }
                    ],
                }
            }

    rollout = {
        "status": None,
        "task_complete_at": None,
        "assistant_message_at": None,
        "rollout_mtime": 200,
        "active_turn_id": "current-turn",
        "active_turn_started_at": 150,
        "active_turn_user_message": "Run cross-model acceptance.",
        "active_turn_agent_message": "MiniMax passed; DeepSeek retest is running.",
    }
    monkeypatch.setattr(
        cli,
        "CodexAppServer",
        lambda *args, **kwargs: ActiveSessionCodex(),
    )
    monkeypatch.setattr(
        cli,
        "read_rollout_closeout",
        lambda *_args, **_kwargs: rollout,
    )

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=True,
            refresh=False,
            limit=1,
        )
    )

    item = result["items"][0]
    assert item["last_turn"]["assistant_final_lead"] == "Old V0 closeout final."
    assert item["active_turn"] == {
        "turn_id": "current-turn",
        "status": "inProgress",
        "started_at": 150,
        "user_request": "Run cross-model acceptance.",
        "user_request_source": "rollout_user_message",
        "progress_lead": "MiniMax passed; DeepSeek retest is running.",
        "progress_excerpt": "MiniMax passed; DeepSeek retest is running.",
        "progress_source": "rollout_agent_message",
        "items_view": None,
        "items_complete": False,
        "source": "rollout",
    }


def test_recent_groups_visible_sessions_by_cwd_without_resorting(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "agent-lane"
    other_project = tmp_path / "other-project"
    project.mkdir()
    other_project.mkdir()
    save_alias(
        "codex",
        "new-fix",
        {
            "codex_thread_id": "thread-new",
            "cwd": str(project),
            "last_status": "completed",
        },
        tmp_path / "aliases",
    )
    save_alias(
        "codex",
        "old-fix",
        {
            "codex_thread_id": "thread-old",
            "cwd": str(project),
            "last_status": "completed",
        },
        tmp_path / "aliases",
    )

    class ProjectGroupedCodex:
        transport = "daemon"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {
                "data": [
                    {
                        "id": "thread-new",
                        "name": "Newest fix",
                        "cwd": str(project),
                        "recencyAt": 30,
                    },
                    {
                        "id": "thread-other",
                        "name": "Other project",
                        "cwd": str(other_project),
                        "recencyAt": 20,
                    },
                    {
                        "id": "thread-old",
                        "name": "Older fix",
                        "cwd": str(project / ".." / project.name),
                        "recencyAt": 10,
                    },
                ]
            }

        def get_goal(self, _thread_id):
            return None

    monkeypatch.setattr(
        cli,
        "CodexAppServer",
        lambda *args, **kwargs: ProjectGroupedCodex(),
    )
    monkeypatch.setattr(cli, "read_rollout_closeout", lambda *_args, **_kwargs: None)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path / "aliases"),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=3,
        )
    )

    assert [item["id"] for item in result["items"]] == [
        "thread-new",
        "thread-other",
        "thread-old",
    ]
    assert result["project_groups"][0] == {
        "key": str(project),
        "name": "agent-lane",
        "cwd": str(project),
        "visible_session_count": 2,
        "visible_lane_count": 2,
        "visible_lane_ids": ["new-fix", "old-fix"],
        "visible_thread_ids": ["thread-new", "thread-old"],
    }
    newest_group = result["items"][0]["project_group"]
    oldest_group = result["items"][2]["project_group"]
    assert newest_group["position"] == 1
    assert newest_group["related_lane_ids"] == ["old-fix"]
    assert newest_group["related_thread_ids"] == ["thread-old"]
    assert oldest_group["position"] == 2
    assert oldest_group["related_lane_ids"] == ["new-fix"]
    assert oldest_group["related_thread_ids"] == ["thread-new"]



def test_completed_session_recency_falls_back_to_assistant_then_rollout_mtime():
    item = {
        "recency_at": 10,
        "thread_recency_at": 10,
        "last_status": "completed",
        "execution_active": False,
    }

    assistant = cli._effective_session_recency(
        item,
        {
            "status": None,
            "task_complete_at": None,
            "assistant_message_at": 30,
            "rollout_mtime": 40,
        },
    )
    mtime = cli._effective_session_recency(
        item,
        {
            "status": None,
            "task_complete_at": None,
            "assistant_message_at": None,
            "rollout_mtime": 40,
        },
    )

    assert assistant == (30, "rollout_assistant_message_at")
    assert mtime == (40, "rollout_mtime")


def test_recent_pins_hidden_aliased_active_goal_ahead_of_recency_limit(
    tmp_path, monkeypatch
):
    save_alias(
        "codex",
        "active-lane",
        {
            "codex_thread_id": "thread-active",
            "custom_title": "Hidden active lane",
            "goal_status": None,
            "last_status": "idle",
        },
        tmp_path,
    )

    recent_threads = [
        {
            "id": f"thread-recent-{index}",
            "name": f"Recent {index}",
            "status": {"type": "idle"},
            "recencyAt": 100 - index,
        }
        for index in range(5)
    ]

    class HiddenActiveGoalCodex:
        transport = "daemon"

        def __init__(self):
            self.goal_calls = []
            self.thread_reads = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, **_kwargs):
            return {"data": recent_threads}

        def get_goal(self, thread_id):
            self.goal_calls.append(thread_id)
            if thread_id == "thread-active":
                return {
                    "status": "active",
                    "objective": "Keep working",
                    "updatedAt": 200,
                }
            return None

        def read_thread(self, thread_id, include_turns=False):
            self.thread_reads.append((thread_id, include_turns))
            assert thread_id == "thread-active"
            assert include_turns is False
            return {
                "thread": {
                    "id": thread_id,
                    "name": "Old but active",
                    "status": {"type": "active"},
                    "recencyAt": 1,
                }
            }

    codex = HiddenActiveGoalCodex()
    monkeypatch.setattr(cli, "CodexAppServer", lambda *args, **kwargs: codex)

    result = cmd_codex_recent(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_unaliased=True,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=5,
        )
    )

    assert [item["id"] for item in result["items"]] == [
        "thread-active",
        "thread-recent-0",
        "thread-recent-1",
        "thread-recent-2",
        "thread-recent-3",
    ]
    active = result["items"][0]
    assert active["lane_id"] == "active-lane"
    assert active["goal_status"] == "active"
    assert active["hidden_active_goal"] is True
    assert active["runner_status"] == "inProgress"
    assert active["runner_alive"] is False
    assert active["thread_active"] is True
    assert active["execution_active"] is True
    assert active["execution_source"] == "thread"
    assert active["needs_resume"] is False
    assert codex.goal_calls.count("thread-active") == 1
    assert codex.thread_reads == [("thread-active", False)]
    assert result["sort"]["key"] == "active_goal_then_recency_at"
    assert result["active_goal_count"] == 1
    assert result["hidden_active_goal_count"] == 1
    assert result["visible_active_goal_count"] == 1
    assert result["active_goal_scan"] == {
        "scope": "aliased_threads",
        "scanned": 1,
        "errors": 0,
        "thread_read_errors": 0,
    }


def test_recent_explicit_daemon_does_not_fall_back(tmp_path, monkeypatch):
    calls = []

    def open_codex(*, transport=None):
        calls.append(transport)
        raise CodexRpcError(
            "daemon unavailable",
            error_code="CODEX_DAEMON_UNAVAILABLE",
            retryable=True,
        )

    monkeypatch.setenv("AGENT_LANE_CODEX_TRANSPORT", "daemon")
    monkeypatch.setattr(cli, "CodexAppServer", open_codex)

    with pytest.raises(CodexRpcError):
        cmd_codex_recent(
            SimpleNamespace(
                alias_root=str(tmp_path),
                aliases_only=False,
                include_unaliased=True,
                include_subagents=False,
                include_last_turn=False,
                refresh=False,
                limit=1,
            )
        )

    assert calls == [None]


def test_find_paginates_past_helper_only_pages(tmp_path, monkeypatch):
    children = [
        {
            "id": f"child-{index}",
            "parentThreadId": "parent-1",
            "name": "Helper thread",
            "recencyAt": 100 - index,
        }
        for index in range(50)
    ]
    main = {
        "id": "main-needle",
        "name": "Ordinary main",
        "recencyAt": 40,
    }

    class PagedFindCodex:
        transport = "daemon"
        calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, *, search_term=None, cursor=None, **_kwargs):
            self.calls.append((search_term, cursor))
            if cursor is None:
                return {"data": children, "nextCursor": "page-2"}
            assert cursor == "page-2"
            return {"data": [main], "nextCursor": None}

    codex = PagedFindCodex()
    codex.goal_calls = []

    def get_goal(thread_id):
        codex.goal_calls.append(thread_id)
        return {"status": "active", "objective": "Needle objective"}

    codex.get_goal = get_goal
    monkeypatch.setattr(cli, "CodexAppServer", lambda *args, **kwargs: codex)

    result = cmd_codex_find(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=1,
            query="Needle objective",
        )
    )

    assert codex.calls == [
        ("Needle objective", None),
        ("Needle objective", "page-2"),
        (None, None),
        (None, "page-2"),
    ]
    assert [item["id"] for item in result["items"]] == ["main-needle"]
    assert codex.goal_calls == ["main-needle"]
    assert result["items"][0]["goal_status"] == "active"
    assert result["items"][0]["goal_status_source"] == "thread_goal_get"
    assert result["pagination"]["search"]["pages"] == 2
    assert result["pagination"]["recent"]["pages"] == 2


def test_find_refreshes_stale_objective_before_stopping_pagination(
    tmp_path,
    monkeypatch,
):
    save_alias(
        "codex",
        "lane-stale",
        {
            "codex_thread_id": "thread-stale",
            "objective": "Needle objective",
        },
        tmp_path,
    )
    stale = {"id": "thread-stale", "name": "Old lane", "recencyAt": 20}
    current = {"id": "thread-current", "name": "Current lane", "recencyAt": 10}

    class PagedGoalCodex:
        transport = "daemon"

        def __init__(self):
            self.list_calls = []
            self.goal_calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def list_threads(self, *, search_term=None, cursor=None, **_kwargs):
            self.list_calls.append((search_term, cursor))
            if cursor is None:
                return {"data": [stale], "nextCursor": "page-2"}
            assert cursor == "page-2"
            return {"data": [current], "nextCursor": None}

        def get_goal(self, thread_id):
            self.goal_calls.append(thread_id)
            if thread_id == "thread-stale":
                return {"status": "active", "objective": "Different objective"}
            return {"status": "active", "objective": "Needle objective"}

    codex = PagedGoalCodex()
    monkeypatch.setattr(cli, "CodexAppServer", lambda *args, **kwargs: codex)

    result = cmd_codex_find(
        SimpleNamespace(
            alias_root=str(tmp_path),
            aliases_only=False,
            include_subagents=False,
            include_last_turn=False,
            refresh=False,
            limit=1,
            query="Needle objective",
        )
    )

    assert codex.list_calls == [
        ("Needle objective", None),
        ("Needle objective", "page-2"),
        (None, None),
        (None, "page-2"),
    ]
    assert codex.goal_calls == ["thread-stale", "thread-current"]
    assert [item["id"] for item in result["items"]] == ["thread-current"]
    assert result["items"][0]["objective"] == "Needle objective"


def test_matches_session_summary_uses_raw_subagent_fields():
    item = {
        "id": "child-1",
        "name": "Migrate CLI entrypoint",
        "parent_thread_id": "parent-1",
        "agent_nickname": "Russell",
    }

    assert _matches_session_summary(item, "migrate cli")
    assert _matches_session_summary(item, "parent-1")
    assert _matches_session_summary(item, "Russell")
    assert not _matches_session_summary(item, "unrelated")


@pytest.mark.parametrize(
    "marker",
    [
        {"source": {"subAgent": "review"}},
        {"threadSource": "subAgentReview"},
        {"sourceKind": "subAgentCompact"},
        {"thread_source": "subAgentThreadSpawn"},
        {"source": "guardian"},
    ],
)
def test_session_summaries_exclude_guardian_and_subagent_source_variants(marker):
    item = {"id": "child-1", "name": "Background helper", **marker}

    assert _is_subagent_thread(item) is True
    assert (
        _session_summaries(
            [item],
            {},
            include_subagents=False,
            limit=10,
        )
        == []
    )
    raw = _session_summaries(
        [item],
        {},
        include_subagents=True,
        limit=10,
    )
    assert [summary["id"] for summary in raw] == ["child-1"]


def test_thread_summary_uses_nested_subagent_metadata_in_raw_mode():
    child = {
        "id": "child-1",
        "name": "Child task",
        "parentThreadId": "parent-1",
        "recencyAt": 20,
        "source": {
            "subAgent": {
                "thread_spawn": {
                    "parent_thread_id": "parent-1",
                    "agent_nickname": "Russell",
                    "agent_role": "worker",
                }
            }
        },
    }

    items = _session_summaries(
        [child],
        {},
        include_subagents=True,
        limit=10,
    )

    assert items[0]["agent_nickname"] == "Russell"
    assert items[0]["agent_role"] == "worker"


def test_last_turn_summary_extracts_completed_turn_and_cleans_directives():
    thread = {
        "turns": [
            {
                "id": "turn-1",
                "status": "completed",
                "startedAt": 10,
                "completedAt": 20,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "first user"}],
                    },
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "last user"}],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": (
                            "已完成。\n\n"
                            "::git-stage{cwd=\"/tmp\"}\n"
                            "<oai-mem-citation>\n"
                            "MEMORY.md:1-2|note=[x]\n"
                            "</oai-mem-citation>\n"
                            "保留这一行。"
                        ),
                    },
                ],
            }
        ]
    }

    summary = _last_turn_summary(thread)

    assert summary["turn_id"] == "turn-1"
    assert summary["user_request"] == "last user"
    assert summary["assistant_final_lead"] == "已完成。"
    assert "::git-stage" not in summary["assistant_final_excerpt"]
    assert "MEMORY.md" not in summary["assistant_final_excerpt"]
    assert "保留这一行。" in summary["assistant_final_excerpt"]


def test_last_turn_summary_uses_latest_interrupted_turn_without_old_final_answer():
    thread = {
        "turns": [
            {
                "id": "old",
                "status": "completed",
                "startedAt": 10,
                "completedAt": 20,
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "old final",
                    }
                ],
            },
            {
                "id": "new",
                "status": "interrupted",
                "startedAt": 30,
                "completedAt": None,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "new user"}],
                    }
                ],
            },
        ]
    }

    summary = _last_turn_summary(thread)

    assert summary["turn_id"] == "new"
    assert summary["status"] == "interrupted"
    assert summary["user_request"] == "new user"
    assert summary["assistant_final_lead"] is None
    assert summary["assistant_final_excerpt"] is None


def test_active_turn_summary_uses_current_request_and_latest_agent_progress():
    thread = {
        "status": {"type": "active"},
        "turns": [
            {
                "id": "completed-turn",
                "status": "completed",
                "startedAt": 10,
                "completedAt": 20,
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Old completed final.",
                    }
                ],
            },
            {
                "id": "active-turn",
                "status": "inProgress",
                "startedAt": 30,
                "itemsView": "full",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "Current active request."}
                        ],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "First progress.",
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "Latest active progress.",
                    },
                ],
            },
        ],
    }

    summary = cli._active_turn_summary(thread)

    assert summary == {
        "turn_id": "active-turn",
        "status": "inProgress",
        "started_at": 30,
        "user_request": "Current active request.",
        "user_request_source": "app_server",
        "progress_lead": "Latest active progress.",
        "progress_excerpt": "Latest active progress.",
        "progress_source": "app_server_agent_message",
        "items_view": "full",
        "items_complete": True,
        "source": "app_server",
    }


def test_active_turn_uses_prior_human_request_for_goal_continuation():
    internal_prompt = """<codex_internal_context source="goal">
Continue working toward the active thread goal.

<objective>
Finish the workflow.
</objective>
</codex_internal_context>"""
    thread = {
        "status": {"type": "active"},
        "turns": [
            {
                "id": "human-turn",
                "status": "completed",
                "startedAt": 10,
                "completedAt": 20,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "Test the real workflow."}
                        ],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Old completed final.",
                    },
                ],
            },
            {
                "id": "active-turn",
                "status": "inProgress",
                "startedAt": 30,
                "itemsView": "full",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": internal_prompt}],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "Current workflow progress.",
                    },
                ],
            },
        ],
    }

    active_turn = cli._active_turn_summary(thread)
    assert active_turn["user_request"] == "Test the real workflow."
    assert active_turn["user_request_source"] == "app_server_prior_user_message"
    assert active_turn["progress_lead"] == "Current workflow progress."


@pytest.mark.parametrize(
    ("objective", "preview", "expected", "source"),
    [
        ("Finish the durable goal.", "Readable preview.", "Finish the durable goal.", "goal_objective"),
        (None, "Readable preview.", "Readable preview.", "thread_preview"),
    ],
)
def test_internal_goal_continuation_falls_back_to_objective_or_preview(
    objective,
    preview,
    expected,
    source,
):
    item = {
        "id": "thread-1",
        "goal_status": "active",
        "objective": objective,
        "preview": preview,
        "active_turn": {
            "turn_id": "active-turn",
            "status": "inProgress",
            "started_at": 30,
            "user_request": cli.GOAL_CONTINUATION_PROMPT,
            "user_request_source": "rollout_user_message",
            "progress_lead": "Current progress.",
            "progress_excerpt": "Current progress.",
            "progress_source": "rollout_agent_message",
            "items_view": None,
            "items_complete": False,
            "source": "rollout",
        },
    }

    enriched = cli._enrich_session_summaries_with_active_turns(
        [item],
        rollout_facts={},
    )[0]

    assert enriched["active_turn"]["user_request"] == expected
    assert enriched["active_turn"]["user_request_source"] == source
    assert enriched["active_turn"]["progress_lead"] == "Current progress."


def test_incomplete_app_active_turn_uses_newer_matching_rollout_messages():
    item = {
        "id": "thread-1",
        "goal_status": "active",
        "active_turn": {
            "turn_id": "active-turn",
            "status": "inProgress",
            "started_at": 30,
            "user_request": "Initial request.",
            "user_request_source": "app_server",
            "progress_lead": "Early progress.",
            "progress_excerpt": "Early progress.",
            "progress_source": "app_server_agent_message",
            "items_view": "summary",
            "items_complete": False,
            "source": "app_server",
        },
    }
    rollout = {
        "thread-1": {
            "active_turn_id": "active-turn",
            "active_turn_user_message": "Latest steering request.",
            "active_turn_agent_message": "Latest rollout progress.",
        }
    }

    enriched = cli._enrich_session_summaries_with_active_turns(
        [item],
        rollout_facts=rollout,
    )[0]

    assert enriched["active_turn"]["user_request"] == "Latest steering request."
    assert enriched["active_turn"]["user_request_source"] == "rollout_user_message"
    assert enriched["active_turn"]["progress_lead"] == "Latest rollout progress."
    assert enriched["active_turn"]["progress_source"] == "rollout_agent_message"


@pytest.mark.parametrize("rollout_turn_id", [None, "different-turn"])
def test_active_turn_rollout_fallback_requires_matching_turn_id(rollout_turn_id):
    item = {
        "id": "thread-1",
        "goal_status": "active",
        "active_turn": {
            "turn_id": "app-turn",
            "status": "inProgress",
            "started_at": 30,
            "user_request": "App request.",
            "user_request_source": "app_server",
            "progress_lead": "App progress.",
            "progress_excerpt": "App progress.",
            "progress_source": "app_server_agent_message",
            "items_view": "summary",
            "items_complete": False,
            "source": "app_server",
        },
    }
    rollout = {
        "thread-1": {
            "active_turn_id": rollout_turn_id,
            "active_turn_agent_message": "Progress from another turn.",
        }
    }

    enriched = cli._enrich_session_summaries_with_active_turns(
        [item],
        rollout_facts=rollout,
    )[0]

    assert enriched["active_turn"]["progress_lead"] == "App progress."
    assert enriched["active_turn"]["progress_source"] == "app_server_agent_message"


def test_steering_message_stays_in_same_turn_and_becomes_last_user_prompt():
    thread = {
        "turns": [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Implement the fix."}],
                    },
                    {
                        "id": "user-steer",
                        "type": "userMessage",
                        "clientId": "agent-lane-steer-client",
                        "content": [
                            {"type": "text", "text": "Focus on failing tests first."}
                        ],
                    },
                    {
                        "id": "agent-final",
                        "type": "agentMessage",
                        "text": "Done.",
                    },
                ],
            }
        ]
    }

    summary = cli._last_turn_summary(thread)
    outline = cli._thread_outline(thread, None, fallback_thread_id="thread-1")

    assert len(thread["turns"]) == 1
    assert summary["turn_id"] == "turn-1"
    assert summary["user_request"] == "Focus on failing tests first."
    assert [prompt["heading"] for prompt in outline["outline"][0]["prompts"]] == [
        "Implement the fix.",
        "Focus on failing tests first.",
    ]


def test_last_turn_summary_uses_phase_less_final_only_for_completed_turns():
    completed = {
        "turns": [
            {
                "id": "completed",
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "text": "legacy final"},
                ],
            }
        ]
    }
    in_progress = {
        "turns": [
            {
                "id": "active",
                "status": "inProgress",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "not terminal",
                    }
                ],
            }
        ]
    }

    assert _last_turn_summary(completed)["assistant_final_lead"] == "legacy final"
    assert _last_turn_summary(in_progress)["assistant_final_lead"] is None


def test_phase_aware_turns_never_fall_back_to_phase_less_agent_messages():
    cleaned_empty_final = {
        "turns": [
            {
                "id": "empty-final",
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "text": "phase-less draft"},
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": '::git-stage{cwd="/private/repo"}',
                    },
                ],
            }
        ]
    }
    commentary_only = {
        "turns": [
            {
                "id": "commentary-only",
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "text": "phase-less draft"},
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "progress update",
                    },
                ],
            }
        ]
    }

    assert _last_turn_summary(cleaned_empty_final)["assistant_final_lead"] is None
    assert _last_turn_summary(commentary_only)["assistant_final_lead"] is None


def test_thread_outline_preserves_prompts_and_reports_incomplete_history():
    thread = {
        "id": "thread-1",
        "name": "App title",
        "cwd": "/repo",
        "turns": [
            {
                "id": "turn-1",
                "status": "completed",
                "startedAt": 10,
                "completedAt": 20,
                "durationMs": 10000,
                "items": [
                    {
                        "id": "message-1",
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "First heading\nMore detail"},
                            {"type": "image", "url": "https://private.example/a"},
                        ],
                    },
                    {
                        "id": "message-2",
                        "type": "userMessage",
                        "content": [
                            {
                                "type": "skill",
                                "name": "/Users/private/skills/reviewer",
                                "path": "/Users/private/skills/reviewer/SKILL.md",
                            },
                            {
                                "type": "mention",
                                "name": "docs/spec.md",
                                "path": "/Users/private/repo/docs/spec.md",
                            },
                        ],
                    },
                    {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": (
                            "Done.\n"
                            "::git-stage{cwd=\"/private/repo\"}\n"
                            "<oai-mem-citation>\nsecret\n</oai-mem-citation>"
                        ),
                    },
                ],
            },
            {
                "id": "turn-2",
                "status": "inProgress",
                "itemsView": "summary",
                "items": [
                    {
                        "id": "answer-2",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Must not be final",
                    }
                ],
            },
            {
                "id": "turn-3",
                "status": "failed",
                "itemsView": "notLoaded",
                "items": [],
                "error": {"message": "failed"},
            },
        ],
    }

    result = cli._thread_outline(
        thread,
        {"lane_id": "lane-1", "custom_title": "Lane title"},
        fallback_thread_id="fallback",
    )

    assert result["lane_id"] == "lane-1"
    assert result["lane_title"] == "Lane title"
    assert result["lane_title_source"] == "custom_title"
    assert result["codex_title"] == "App title"
    assert result["turn_count"] == 3
    assert result["prompt_count"] == 2
    assert result["history_complete"] is False
    assert result["incomplete_turn_ids"] == ["turn-2", "turn-3"]
    assert result["outline"][0]["items_view"] == "full"
    prompts = result["outline"][0]["prompts"]
    assert prompts[0] == {
        "prompt_index": 1,
        "item_id": "message-1",
        "heading": "First heading",
        "text_excerpt": "First heading\nMore detail",
        "input_types": ["text", "image"],
    }
    assert prompts[1]["heading"] == "Skill: reviewer, Mention: spec.md"
    assert result["outline"][0]["assistant_final_lead"] == "Done."
    assert result["outline"][1]["assistant_final_lead"] is None
    assert result["outline"][2]["error"] == {"message": "failed"}
    serialized = json.dumps(result, ensure_ascii=False)
    assert "https://private.example" not in serialized
    assert "/Users/private" not in serialized


def test_outline_safe_fallbacks_cover_empty_image_and_interrupted_turns():
    thread = {
        "id": "thread-1",
        "turns": [
            {
                "id": "turn-1",
                "status": "interrupted",
                "items": [
                    {
                        "id": "image-only",
                        "type": "userMessage",
                        "content": [
                            {
                                "type": "localImage",
                                "path": "/Users/private/screenshots/secret.png",
                            }
                        ],
                    },
                    {
                        "id": "empty",
                        "type": "userMessage",
                        "content": [],
                    },
                    {
                        "id": "unfinished-answer",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Do not expose this as a final answer",
                    },
                ],
            }
        ],
    }

    result = cli._thread_outline(thread, None, fallback_thread_id="thread-1")

    assert result["history_complete"] is True
    assert result["prompt_count"] == 2
    turn = result["outline"][0]
    assert turn["items_view"] == "full"
    assert turn["assistant_final_lead"] is None
    assert turn["prompts"][0]["heading"] == "Image"
    assert turn["prompts"][0]["input_types"] == ["localImage"]
    assert turn["prompts"][1]["heading"] == "(No content)"
    assert turn["prompts"][1]["text_excerpt"] is None
    assert "/Users/private" not in json.dumps(result)


def test_outline_and_selected_read_are_read_only_for_lane_or_thread(
    tmp_path,
    monkeypatch,
):
    thread = {
        "id": "thread-1",
        "name": "Thread title",
        "turns": [
            {"id": "turn-1", "status": "completed", "items": []},
            {"id": "turn-2", "status": "completed", "items": []},
        ],
    }
    fake = FakeCodexContext({"thread-1": thread, "thread-2": {"id": "thread-2", "turns": []}})
    monkeypatch.setattr(cli, "CodexAppServer", lambda: fake)
    alias_path = save_alias(
        "codex",
        "lane-1",
        {
            "codex_thread_id": "thread-1",
            "custom_title": "Lane title",
            "title": "Removed title",
            "title_source": "legacy",
            "lane_label": "Removed label",
            "lane_title": "Removed computed title",
            "lane_title_source": "title",
        },
        tmp_path,
    )
    before = alias_path.read_text(encoding="utf-8")

    lane_outline = cli.cmd_codex_outline(
        SimpleNamespace(
            alias_root=str(tmp_path),
            lane_id="lane-1",
            thread_id=None,
        )
    )
    raw_outline = cli.cmd_codex_outline(
        SimpleNamespace(
            alias_root=str(tmp_path),
            lane_id=None,
            thread_id="thread-2",
        )
    )
    selected = cli.cmd_codex_read(
        SimpleNamespace(
            alias_root=str(tmp_path),
            lane_id=None,
            thread_id="thread-1",
            include_turns=False,
            turn_id=None,
            turn_index=2,
        )
    )
    legacy = cli.cmd_codex_read(
        SimpleNamespace(
            alias_root=str(tmp_path),
            lane_id="lane-1",
            thread_id=None,
            include_turns=False,
            turn_id=None,
            turn_index=None,
        )
    )
    included = cli.cmd_codex_read(
        SimpleNamespace(
            alias_root=str(tmp_path),
            lane_id="lane-1",
            thread_id=None,
            include_turns=True,
            turn_id=None,
            turn_index=None,
        )
    )

    assert lane_outline["lane_id"] == "lane-1"
    assert raw_outline["lane_id"] is None
    assert selected["selection"] == {"turn_id": "turn-2", "turn_index": 2}
    assert selected["turn"]["id"] == "turn-2"
    assert "turns" not in selected["thread"]
    assert legacy["thread"] == {"thread": thread}
    assert included["thread"] == {"thread": thread}
    removed_title_fields = {
        "title",
        "title_source",
        "lane_label",
        "lane_title",
        "lane_title_source",
    }
    assert not removed_title_fields.intersection(selected["alias"])
    assert not removed_title_fields.intersection(legacy["alias"])
    assert not removed_title_fields.intersection(included["alias"])
    assert alias_path.read_text(encoding="utf-8") == before

    with pytest.raises(ValueError, match="no alias found"):
        cli.cmd_codex_outline(
            SimpleNamespace(
                alias_root=str(tmp_path),
                lane_id="missing",
                thread_id=None,
            )
        )


def test_outline_json_dispatch_and_read_selection_error_envelope(
    tmp_path,
    monkeypatch,
    capsys,
):
    fake = FakeCodexContext(
        {
            "thread-1": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {
                                "id": "message-1",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "hello"}],
                            }
                        ],
                    }
                ],
            }
        }
    )
    monkeypatch.setattr(cli, "CodexAppServer", lambda **_kwargs: fake)

    outline_rc = main(
        [
            "codex", "session", "outline",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
        ]
    )
    outline = decode_cli_output(capsys.readouterr().out)
    error_rc = main(
        [
            "codex", "session", "read",
            "--thread-id",
            "thread-1",
            "--alias-root",
            str(tmp_path),
            "--turn-index",
            "2",
        ]
    )
    error = decode_cli_output(capsys.readouterr().out)

    assert outline_rc == 0
    assert outline["outline"][0]["prompts"][0]["text_excerpt"] == "hello"
    assert error_rc == 1
    assert error["ok"] is False
    assert "out of range" in error["error"]


def test_selected_read_rejects_invalid_turn_selectors():
    turns = [{"id": "turn-1"}]

    selected, index = cli._select_turn(
        turns,
        turn_id="turn-1",
        turn_index=None,
    )
    assert selected == {"id": "turn-1"}
    assert index == 1

    with pytest.raises(ValueError, match="at least 1"):
        cli._select_turn(turns, turn_id=None, turn_index=0)
    with pytest.raises(ValueError, match="out of range"):
        cli._select_turn(turns, turn_id=None, turn_index=2)
    with pytest.raises(ValueError, match="was not found"):
        cli._select_turn(turns, turn_id="missing", turn_index=None)


def test_last_turn_summary_empty_turns_and_missing_path_locations_are_stable():
    assert _last_turn_summary({"turns": []}) == {
        "turn_id": None,
        "status": None,
        "started_at": None,
        "completed_at": None,
        "user_request": None,
        "assistant_final_lead": None,
        "assistant_final_excerpt": None,
    }

    assert _thread_locations({"id": "t1"}, {"cwd": "/tmp/project"}) == {
        "thread_id": "t1",
        "codex_url": "codex://threads/t1",
        "session_path": None,
        "cwd": "/tmp/project",
    }


def test_enrich_session_summaries_keeps_parent_last_turn_separate_from_subagent():
    codex = FakeCodex(
        {
            "parent-1": {
                "id": "parent-1",
                "path": "/sessions/parent.jsonl",
                "cwd": "/repo",
                "name": "Parent",
                "recencyAt": 10,
                "turns": [
                    {
                        "id": "parent-turn",
                        "status": "completed",
                        "startedAt": 1,
                        "completedAt": 2,
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "parent user"}],
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "parent final",
                            },
                        ],
                    }
                ],
            },
            "child-1": {
                "id": "child-1",
                "path": "/sessions/child.jsonl",
                "cwd": "/repo",
                "name": "Child",
                "recencyAt": 20,
                "agentNickname": "Ada",
                "agentRole": "explorer",
            },
        }
    )
    item = {
        "id": "parent-1",
        "cwd": "/repo",
        "latest_subagent_thread_id": "child-1",
        "latest_subagent_name": "Child",
        "latest_subagent_recency_at": 20,
    }

    enriched = _enrich_session_summaries_with_last_turns(codex, [item])[0]

    assert enriched["locations"]["session_path"] == "/sessions/parent.jsonl"
    assert enriched["last_turn"]["user_request"] == "parent user"
    assert enriched["last_turn"]["assistant_final_lead"] == "parent final"
    assert enriched["latest_activity"]["kind"] == "subagent"
    assert enriched["latest_activity"]["thread_id"] == "child-1"
    assert enriched["latest_activity"]["session_path"] == "/sessions/child.jsonl"
    assert enriched["latest_activity"]["agent_nickname"] == "Ada"
