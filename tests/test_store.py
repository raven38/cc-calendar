import json
import os
import shutil

from conftest import PROJECT, LogBuilder

from cc_calendar import store as store_mod
from cc_calendar.store import ClaudeDir, CodexDir, CollectorDir, Store


def test_scan(claude_dir):
    store = Store(claude_dir)
    store.scan()
    assert set(store.sessions) == {"s-basic", "s-sub", "s-prev", "s-next", "s-empty"}

    sub = store.sessions["s-sub"]
    assert set(sub.subagents) == {"a1"}  # the dangling symlink is skipped
    a1 = sub.subagents["a1"]
    assert a1.agent_type == "Explore"
    assert a1.tool_use_id == "t-agent"
    assert a1.status == "completed"
    assert len(a1.usages) == 2
    assert sub.tokens() > sum(u.total for u in sub.usages.values())

    assert store.continued_from() == {"s-next": "s-prev"}
    nxt = store.sessions["s-next"]
    assert [p["text"] for p in nxt.prompts] == ["Carry on"]
    assert nxt.state(None)[0] == "done"
    assert store.sessions["s-prev"].state(None)[0] == "interrupted"


def test_canonical_import_projects_existing_session_without_replacing_analytics(claude_dir):
    store = Store(claude_dir)
    store.scan()

    imported = store.canonical_import("s-basic")

    assert imported is not None
    assert imported.session.product == "claude-code"
    assert imported.session.source_instance == "claude-code-local"
    assert imported.session.project_id == "__misc__"
    assert imported.source.tokens() == store.sessions["s-basic"].tokens()
    assert imported.events
    assert all(event.session_id == imported.session.id for event in imported.events)


def test_codex_source_scan_is_opt_in_and_exposes_source_catalog(claude_dir, tmp_path):
    codex_root = tmp_path / "codex"
    rollout = codex_root / "sessions" / "2026" / "10" / "05" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-synthetic",
                    "timestamp": "2026-10-05T01:00:00+00:00",
                    "cwd": "/work/demo",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-10-05T01:00:01+00:00",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "synthetic Codex"}],
                },
            }
        )
        + "\n"
    )
    store = Store(claude_dir, codex_dirs=[CodexDir("codex-main", codex_root)])
    store.scan()

    assert len(store.codex_sessions) == 1
    imported = next(iter(store.codex_sessions.values()))
    assert imported.session.source_instance == "codex-main"
    assert imported.session.project_id == "__misc__"
    assert {source["name"] for source in store.source_catalog()} == {"local", "codex-main"}


def test_codex_partial_jsonl_and_repeated_scan_are_safe(claude_dir, tmp_path):
    codex_root = tmp_path / "codex"
    rollout = codex_root / "sessions" / "partial.jsonl"
    rollout.parent.mkdir(parents=True)
    complete = json.dumps(
        {
            "type": "session_meta",
            "payload": {"id": "partial", "cwd": "/work/partial"},
        }
    )
    rollout.write_text(complete[:20])
    store = Store(claude_dir, codex_dirs=[CodexDir("codex-main", codex_root)])
    store.scan()
    assert store.codex_sessions == {}

    rollout.write_text(complete + "\n")
    store.scan()
    assert len(store.codex_sessions) == 1
    imported = next(iter(store.codex_sessions.values()))
    first_event_count = len(imported.events)
    store.scan()
    assert len(store.codex_sessions) == 1
    assert len(next(iter(store.codex_sessions.values())).events) == first_event_count


def test_collector_source_scan_is_opt_in_and_replaced_on_rescan(claude_dir, tmp_path):
    collector_root = tmp_path / "collector"
    shard = collector_root / "activity.jsonl"
    collector_root.mkdir()
    shard.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {
                    "record_type": "session",
                    "collector_conversation_id": "chatgpt-1",
                    "provider": "openai",
                    "product": "chatgpt",
                    "title": "Synthetic Chat",
                    "created_at": "2026-10-05T02:00:00Z",
                    "last_activity_at": "2026-10-05T02:01:00Z",
                },
                {
                    "record_type": "event",
                    "collector_conversation_id": "chatgpt-1",
                    "event_id": "m1",
                    "kind": "user_message",
                    "timestamp": "2026-10-05T02:00:01Z",
                    "text": "Synthetic prompt",
                },
            ]
        )
    )
    store = Store(
        claude_dir,
        collector_dirs=[CollectorDir("chatgpt-personal", collector_root)],
    )
    store.scan()

    assert len(store.collector_sessions) == 1
    imported = next(iter(store.collector_sessions.values()))
    assert imported.session.source_instance == "chatgpt-personal"
    assert imported.project.name == "Misc"
    assert {source["name"] for source in store.source_catalog()} == {
        "local",
        "chatgpt-personal",
    }
    first_event_count = len(imported.events)
    store.scan()
    assert len(store.collector_sessions) == 1
    assert len(next(iter(store.collector_sessions.values())).events) == first_event_count


def test_collector_directory_reads_state_and_ignores_corrected_tmp(claude_dir, tmp_path):
    collector_root = tmp_path / "collector"
    collector_root.mkdir()
    record = {
        "schema_version": 1,
        "source_instance": "claude-personal",
        "provider": "anthropic",
        "product": "claude-chat",
        "conversation_id": "claude-native-1",
        "title": "Synthetic Claude summary",
        "summary": "Synthetic summary",
        "last_activity_at": "2026-10-05T02:01:00Z",
        "collector": {"history_completeness": "summary"},
        "completed": True,
    }
    (collector_root / "01.jsonl").write_text(json.dumps(record) + "\n")
    (collector_root / "05.corrected.tmp.jsonl").write_text(
        json.dumps({**record, "conversation_id": "must-not-import"}) + "\n"
    )
    (collector_root / "_collector_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_instance": "claude-personal",
                "backfill_complete": False,
            }
        )
    )
    store = Store(
        claude_dir,
        collector_dirs=[CollectorDir("claude-personal", collector_root)],
    )
    store.scan()

    assert len(store.collector_sessions) == 1
    imported = next(
        value
        for value in store.collector_sessions.values()
        if value.session.native_id == "claude-native-1"
    )
    assert imported.session.backfill_complete is False
    assert imported.events[0].kind == "summary"
    assert all(
        value.session.native_id != "must-not-import" for value in store.collector_sessions.values()
    )


def test_incremental_reads(claude_dir):
    store = Store(claude_dir)
    store.scan()
    path = claude_dir / "projects" / PROJECT / "s-basic.jsonl"
    b = LogBuilder("s-basic")
    b._n = 1000  # fresh uuids
    b.prompt(30, "Another request")
    line = json.dumps(b.records[0]) + "\n"

    # A partially written line is not consumed until it is complete.
    with open(path, "a") as f:
        f.write(line[:20])
    store.update_file(path)
    assert len(store.sessions["s-basic"].prompts) == 1
    with open(path, "a") as f:
        f.write(line[20:])
    assert store.update_file(path) == "s-basic"
    assert len(store.sessions["s-basic"].prompts) == 2


def test_rewritten_file_is_reparsed(claude_dir):
    store = Store(claude_dir)
    store.scan()
    path = claude_dir / "projects" / PROJECT / "s-sub.jsonl"
    b = LogBuilder("s-sub")
    b.prompt(0, "only")
    b.write(path)  # shorter than before
    store.update_file(path)
    s = store.sessions["s-sub"]
    assert [p["text"] for p in s.prompts] == ["only"]
    assert "a1" in s.subagents  # subagent data survives the rebuild


def test_live_sessions(claude_dir):
    sessions = claude_dir / "sessions"
    (sessions / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "s-basic", "status": "busy"})
    )
    (sessions / "2.json").write_text(json.dumps({"pid": 2**22 + 12345, "sessionId": "s-sub"}))
    (sessions / "3.json").write_text("not json")
    live = Store(claude_dir).live_sessions()
    assert set(live) == {"s-basic"}


def test_missing_claude_dir(tmp_path):
    store = Store(tmp_path / "nope")
    store.scan()
    assert store.sessions == {}
    assert store.live_sessions() == {}


def two_dirs(claude_dir, laptop_dir):
    return [ClaudeDir("local", claude_dir), ClaudeDir("laptop", laptop_dir)]


def test_multiple_dirs(claude_dir, laptop_dir):
    store = Store(two_dirs(claude_dir, laptop_dir))
    store.scan()
    assert "s-laptop" in store.sessions and "s-sub" in store.sessions
    assert store.sessions["s-laptop"].source == "laptop"
    assert store.sessions["s-sub"].source == "local"
    assert store.sessions["s-laptop"].path.startswith(str(laptop_dir))


def test_duplicate_session_keeps_newest_copy(claude_dir, laptop_dir):
    store = Store(two_dirs(claude_dir, laptop_dir))
    store.scan()
    # The laptop copy of s-basic stops before the commit, so the local one is newer.
    assert store.sessions["s-basic"].source == "local"
    assert store.also_in("s-basic") == ["laptop"]
    assert store.also_in("s-laptop") == []

    # Once the laptop copy has later activity, it is shown instead.
    b = LogBuilder("s-basic")
    b._n = 1000
    b.prompt(30, "More work on the laptop")
    path = laptop_dir / "projects" / PROJECT / "s-basic.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(b.records[0]) + "\n")
    assert store.update_file(path) == "s-basic"
    assert store.sessions["s-basic"].source == "laptop"
    assert store.also_in("s-basic") == ["local"]


def test_duplicate_session_tie_goes_to_first_dir(claude_dir, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(claude_dir, copy, symlinks=True)
    store = Store([ClaudeDir("copy", copy), ClaudeDir("local", claude_dir)])
    store.scan()
    assert store.sessions["s-basic"].source == "copy"
    assert store.also_in("s-basic") == ["local"]


def test_live_sessions_across_dirs(claude_dir, laptop_dir):
    (laptop_dir / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "s-laptop"})
    )
    live = Store(two_dirs(claude_dir, laptop_dir)).live_sessions()
    assert set(live) == {"s-laptop"}


def test_live_sessions_check_start_time(claude_dir, monkeypatch):
    monkeypatch.setattr(store_mod, "_proc_start", lambda pid: "Sun Oct  4 05:53:32 2026")
    sessions = claude_dir / "sessions"
    pid = os.getpid()
    for sid, start in [
        ("s-basic", "Sun Oct  4 05:53:32 2026"),  # same process
        ("s-sub", "Mon Sep 28 09:00:00 2026"),  # another process that had this PID
        ("s-prev", None),  # older Claude Code: PID check only
    ]:
        rec = {"pid": pid, "sessionId": sid, **({"procStart": start} if start else {})}
        (sessions / f"{sid}.json").write_text(json.dumps(rec))
    assert set(Store(claude_dir).live_sessions()) == {"s-basic", "s-prev"}


def test_live_sessions_without_ps(claude_dir, monkeypatch):
    monkeypatch.setattr(store_mod, "_proc_start", lambda pid: None)
    (claude_dir / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "s-basic", "procStart": "whenever"})
    )
    assert set(Store(claude_dir).live_sessions()) == {"s-basic"}


def test_proc_start_of_this_process():
    start = store_mod._proc_start(os.getpid())
    assert start is None or len(start.split()) == 5  # e.g. "Sun Oct  4 05:53:32 2026"
