import json
import tempfile
import unittest
from pathlib import Path

from cc_calendar.canonical import AnalysisPolicy, ExportPolicy, SessionVisibility
from cc_calendar.canonical.codex_local import CodexLocalAdapter


class CodexLocalAdapterTest(unittest.TestCase):
    def test_imports_synthetic_rollout_and_preserves_unknown_usage(self):
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session-1",
                    "timestamp": "2026-10-05T01:00:00+00:00",
                    "cwd": "/work/demo",
                    "git_branch": "main",
                    "model": "gpt-5-codex",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-10-05T01:00:01+00:00",
                "payload": {"effort": "high"},
            },
            {
                "type": "response_item",
                "id": "r-user-1",
                "timestamp": "2026-10-05T01:00:02+00:00",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Inspect the project"}],
                },
            },
            {
                "type": "response_item",
                "id": "r-call-1",
                "timestamp": "2026-10-05T01:00:03+00:00",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "call-1",
                    "arguments": '{"command":"git status"}',
                },
            },
            {
                "type": "response_item",
                "id": "r-output-1",
                "timestamp": "2026-10-05T01:00:04+00:00",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "clean",
                },
            },
            {
                "type": "response_item",
                "id": "r-assistant-1",
                "timestamp": "2026-10-05T01:00:05+00:00",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "model": "gpt-5-codex",
                    "content": [{"type": "output_text", "text": "The tree is clean."}],
                },
            },
            {
                "type": "event_msg",
                "id": "r-usage-1",
                "timestamp": "2026-10-05T01:00:06+00:00",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 20,
                            "output_tokens": 7,
                            "cached_input_tokens": 4,
                        }
                    },
                },
            },
            {"type": "future_record", "payload": {"ignored": True}},
            {"type": "response_item", "id": "r-user-1", "payload": {"type": "message"}},
            b"not json\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            with path.open("wb") as stream:
                for record in records:
                    stream.write(
                        record if isinstance(record, bytes) else json.dumps(record).encode()
                    )
                    stream.write(b"\n")
            imported = CodexLocalAdapter(source_instance="codex-main").import_jsonl(path)

        self.assertEqual(imported.session.provider, "openai")
        self.assertEqual(imported.session.product, "codex")
        self.assertEqual(imported.session.surface, "local")
        self.assertEqual(imported.session.native_id, "codex-session-1")
        self.assertEqual(imported.session.visibility, SessionVisibility.PRIVATE)
        self.assertEqual(imported.session.project_id, "__misc__")
        self.assertTrue(imported.session.capabilities.token_usage)
        self.assertTrue(imported.session.capabilities.cache_stats)
        self.assertEqual(imported.source.tokens(), 31)
        self.assertEqual(imported.source.model, "gpt-5-codex")
        self.assertEqual(imported.source.effort, "high")
        self.assertEqual(
            [event.kind for event in imported.events],
            ["user_message", "tool_call", "tool_result", "assistant_message", "status"],
        )
        self.assertEqual(imported.events[-1].usage.cache_read_tokens, 4)
        self.assertTrue(all(event.session_id == imported.session.id for event in imported.events))
        self.assertEqual(imported.project.export_policy, ExportPolicy.LOCAL_ONLY)
        self.assertEqual(imported.project.analysis_policy, AnalysisPolicy.LOCAL)

    def test_missing_timestamps_and_usage_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-metrics.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-no-metrics", "cwd": "/work/unknown"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hello"}],
                        },
                    }
                )
                + "\n"
            )
            imported = CodexLocalAdapter(source_instance="codex-main").import_jsonl(path)

        self.assertIsNone(imported.session.start)
        self.assertIsNone(imported.session.end)
        self.assertFalse(imported.session.capabilities.token_usage)
        self.assertIsNone(imported.source.tokens())
        self.assertIsNone(imported.events[0].usage)

    def test_cumulative_usage_is_latest_snapshot_and_messages_are_deduplicated(self):
        records = [
            {
                "type": "response_item",
                "id": "message-1",
                "timestamp": "2026-10-05T01:00:00+00:00",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "same message"}],
                },
            },
            {
                "type": "event_msg",
                "id": "event-message-1",
                "timestamp": "2026-10-05T01:00:00+00:00",
                "payload": {
                    "type": "agent_message",
                    "message_id": "message-1",
                    "role": "assistant",
                    "text": "same message",
                },
            },
            {
                "type": "event_msg",
                "id": "event-message-2",
                "timestamp": "2026-10-05T01:00:01+00:00",
                "payload": {
                    "type": "agent_message",
                    "message_id": "message-2",
                    "role": "assistant",
                    "text": "event-only message",
                },
            },
            {
                "type": "event_msg",
                "id": "usage-1",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}},
                },
            },
            {
                "type": "event_msg",
                "id": "usage-2",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 5}},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cumulative.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            imported = CodexLocalAdapter().import_jsonl(path)

        messages = [event for event in imported.events if event.kind == "assistant_message"]
        self.assertEqual([event.text for event in messages], ["same message", "event-only message"])
        self.assertEqual(imported.source.tokens(), 35)

    def test_empty_metadata_is_safe_and_unknown_source_label_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "empty"}}) + "\n")
            imported = CodexLocalAdapter(source_instance="unknown-source").import_jsonl(path)

            defaulted = CodexLocalAdapter(source_instance="").import_jsonl(path)

        self.assertEqual(imported.session.source_instance, "unknown-source")
        self.assertEqual(imported.session.title, "(untitled session)")
        self.assertIsNone(imported.session.start)
        self.assertIsNone(imported.session.end)
        self.assertEqual(imported.session.status, "unknown")
        self.assertEqual(defaulted.session.source_instance, "codex-local")


if __name__ == "__main__":
    unittest.main()
