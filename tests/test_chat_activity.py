import json
import tempfile
import unittest
from pathlib import Path

from cc_calendar.canonical import SessionVisibility, collector_session_id
from cc_calendar.canonical.chat_activity import ChatActivityAdapter


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value) + "\n")
    return path


def summary_record(
    collector_id: str | None,
    *,
    native_id: str | None = None,
    title: str = "Synthetic summary",
    last_activity: str = "2026-10-05T01:01:00+00:00",
    created_at: str | None = "2026-10-05T01:00:00+00:00",
) -> dict:
    return {
        "schema_version": 1,
        "source_instance": "chatgpt-personal",
        "provider": "openai",
        "product": "chatgpt",
        "collector_conversation_id": collector_id,
        "collector_id_inputs": ["initial_title", "first_message_at"],
        "collector_id_method": "persistent_initial_fingerprint_uuid",
        "conversation_id": native_id,
        "title": title,
        "summary": "Synthetic summary text",
        "created_at": created_at,
        "first_message_at": created_at,
        "last_message_at": last_activity,
        "last_activity_at": last_activity,
        "completed": True,
        "project": None,
        "collector": {
            "collected_at": "2026-10-05T01:02:00+00:00",
            "history_completeness": "summary",
        },
        "decisions": ["Synthetic decision"],
        "open_items": ["Synthetic open item"],
        "waiting_for": [],
        "attachments": [],
        "message_count": None,
        "conversation_url": None,
    }


def identity_entry(collector_id: str, title: str, *, method: str) -> dict:
    return {
        "collector_conversation_id": collector_id,
        "canonical_id_is_immutable": True,
        "identity_resolution": "unique_title_alias_at_migration",
        "migration_method": method,
        "match_aliases": {"initial_title": title, "normalized_title": title.lower()},
        "observed_only": {"legacy_last_activity_at": "2026-10-05T01:01:00+00:00"},
    }


class ChatActivityAdapterTest(unittest.TestCase):
    def test_fixed_collector_identity_survives_native_id_and_sidecar_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_jsonl(
                root / "first.jsonl",
                [
                    {
                        "type": "session",
                        "source_instance": "chatgpt-personal",
                        "provider": "openai",
                        "product": "chatgpt",
                        "account": "personal",
                        "conversation_id": None,
                        "collector_conversation_id": "fixed-chat-1",
                        "collector_id_method": "sidecar",
                        "identity_sidecar": {"legacy_title": "Old title", "migration": "kept"},
                        "title": "Initial title",
                        "created_at": "2026-10-05T01:00:00+00:00",
                        "last_activity_at": "2026-10-05T01:01:00+00:00",
                        "sync_state": "partial",
                        "backfill_status": "pending",
                        "history_completeness": "summary",
                    },
                    {
                        "type": "event",
                        "collector_conversation_id": "fixed-chat-1",
                        "event_id": "message-1",
                        "timestamp": "2026-10-05T01:00:05+00:00",
                        "kind": "user_message",
                        "role": "user",
                        "text": "Synthetic prompt",
                    },
                ],
            )
            update = write_jsonl(
                root / "update.jsonl",
                [
                    {
                        "type": "session",
                        "source_instance": "chatgpt-personal",
                        "provider": "openai",
                        "product": "chatgpt",
                        "conversation_id": "native-later",
                        "collector_conversation_id": "fixed-chat-1",
                        "collector_id_method": "sidecar",
                        "title": "Updated title",
                        "last_activity_at": "2026-10-05T01:03:00+00:00",
                        "sync_state": "complete",
                        "backfill_status": "complete",
                        "completed": True,
                        "history_completeness": "full",
                    },
                    {
                        "type": "event",
                        "collector_conversation_id": "fixed-chat-1",
                        "event_id": "message-2",
                        "timestamp": "2026-10-05T01:02:00+00:00",
                        "kind": "assistant_message",
                        "role": "assistant",
                        "text": "Synthetic reply",
                    },
                ],
            )
            adapter = ChatActivityAdapter(source_instance="chatgpt-personal")
            first_import = adapter.import_jsonl(first)
            first_session = next(iter(first_import.values())).session
            merged = adapter.import_paths([first, update, first])

        imported = next(iter(merged.values()))
        self.assertEqual(imported.session.id, first_session.id)
        self.assertEqual(imported.session.native_id, "native-later")
        self.assertEqual(imported.session.title, "Updated title")
        self.assertEqual(imported.session.status, "done")
        self.assertEqual(imported.session.identity_metadata["migration"], "kept")
        self.assertEqual(
            [event.id.rsplit(":", 1)[-1] for event in imported.events],
            ["message-1", "message-2"],
        )

    def test_source_instances_are_separate_and_native_id_is_nullable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "session",
                        "provider": "anthropic",
                        "product": "claude-chat",
                        "collector_conversation_id": "fixed-same",
                        "conversation_id": None,
                        "title": "Same synthetic title",
                    }
                ],
            )
            personal = ChatActivityAdapter(source_instance="claude-personal").import_jsonl(path)
            work = ChatActivityAdapter(source_instance="claude-work").import_jsonl(path)

        personal_session = next(iter(personal.values())).session
        work_session = next(iter(work.values())).session
        self.assertIsNone(personal_session.native_id)
        self.assertNotEqual(personal_session.id, work_session.id)
        self.assertEqual(personal_session.source_instance, "claude-personal")
        self.assertEqual(work_session.source_instance, "claude-work")

    def test_missing_collector_id_and_partial_jsonl_are_not_path_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = write_jsonl(
                root / "title-last-activity.jsonl",
                [{"type": "session", "title": "Not an identity"}],
            )
            partial = root / "partial.jsonl"
            partial.write_text('{"type":"session","collector_conversation_id":"fixed')
            adapter = ChatActivityAdapter(source_instance="chatgpt-personal")
            self.assertEqual(adapter.import_jsonl(missing), {})
            self.assertEqual(adapter.import_jsonl(partial), {})

    def test_missing_metrics_are_unknown_and_status_never_infers_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_jsonl(
                Path(directory) / "unknown.jsonl",
                [
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "unknown-1",
                        "completed": True,
                        "history_completeness": "full",
                    },
                    {
                        "type": "event",
                        "collector_conversation_id": "unknown-1",
                        "event_id": "unknown-event",
                        "kind": "assistant_message",
                        "text": "No timestamp or model",
                    },
                ],
            )
            imported = next(
                iter(
                    ChatActivityAdapter(source_instance="chatgpt-personal")
                    .import_jsonl(path)
                    .values()
                )
            )

        self.assertIsNone(imported.session.start)
        self.assertIsNone(imported.session.end)
        self.assertEqual(imported.session.status, "unknown")
        self.assertFalse(imported.session.capabilities.token_usage)
        self.assertIsNone(imported.source.tokens())
        self.assertIsNone(imported.events[0].model)
        self.assertIsNone(imported.events[0].usage)

    def test_project_and_misc_policies_stay_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_jsonl(
                Path(directory) / "projects.jsonl",
                [
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "public-project",
                        "conversation_id": "native-project",
                        "project": {
                            "id": "native-project",
                            "name": "Synthetic public project",
                            "visibility": "public",
                            "export_policy": "local_only",
                            "analysis_policy": "local",
                        },
                        "visibility": "private",
                    },
                    {
                        "type": "session",
                        "provider": "anthropic",
                        "product": "claude-chat",
                        "collector_conversation_id": "misc-session",
                    },
                ],
            )
            imports = ChatActivityAdapter(source_instance="chatgpt-personal").import_jsonl(path)

        projects = {item.session.collector_conversation_id: item for item in imports.values()}
        public = projects["public-project"]
        misc = projects["misc-session"]
        self.assertEqual(public.session.visibility, SessionVisibility.PRIVATE)
        self.assertEqual(public.session.project_id, public.project.id)
        self.assertEqual(public.project.visibility.value, "public")
        self.assertEqual(public.project.export_policy.value, "local_only")
        self.assertEqual(public.project.analysis_policy.value, "local")
        self.assertEqual(misc.session.project_id, "__misc__")
        self.assertEqual(misc.project.visibility.value, "private")

    def test_state_requires_explicit_complete_before_done(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_jsonl(
                Path(directory) / "states.jsonl",
                [
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "missing-state",
                        "completed": True,
                    },
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "partial-state",
                        "sync_state": "partial",
                        "completed": True,
                    },
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "backfill-pending",
                        "sync_state": "complete",
                        "backfill_status": "pending",
                        "completed": True,
                    },
                    {
                        "type": "session",
                        "provider": "openai",
                        "product": "chatgpt",
                        "collector_conversation_id": "complete",
                        "sync_state": "complete",
                        "backfill_status": "complete",
                        "completed": True,
                    },
                ],
            )
            imports = ChatActivityAdapter(source_instance="chatgpt-personal").import_jsonl(path)

        statuses = {
            item.session.collector_conversation_id: item.session.status for item in imports.values()
        }
        self.assertEqual(statuses["missing-state"], "unknown")
        self.assertEqual(statuses["partial-state"], "unknown")
        self.assertEqual(statuses["backfill-pending"], "unknown")
        self.assertEqual(statuses["complete"], "done")

    def test_real_summary_snapshot_shape_imports_sidecar_and_summary_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_dir = root / "_collector_state.d"
            identity_dir.mkdir()
            write_jsonl(root / "05.jsonl", [summary_record("legacy-1")])
            write_json(
                identity_dir / "2026.identity.json",
                {
                    "schema_version": 1,
                    "source_instance": "chatgpt-personal",
                    "state_delta_type": "identity_migration",
                    "created_at": "2026-10-05T01:03:00+00:00",
                    "policy": {
                        "ambiguous_alias": "skip",
                        "legacy_activity_derived_ids": "freeze",
                        "new_unidentified_conversations": "persistent_uuid_only",
                        "title_usage": "alias_only",
                    },
                    "identity_entries": [
                        identity_entry(
                            "legacy-1", "Synthetic summary", method="freeze_legacy_assigned_id"
                        )
                    ],
                },
            )
            imported = ChatActivityAdapter(source_instance="chatgpt-personal").import_directory(
                root
            )

        item = next(iter(imported.values()))
        self.assertEqual(item.session.collector_conversation_id, "legacy-1")
        self.assertEqual(item.session.title, "Synthetic summary")
        self.assertEqual(item.session.history_completeness, "summary")
        self.assertEqual(len(item.events), 1)
        self.assertEqual(item.events[0].kind, "summary")
        self.assertEqual(item.events[0].metadata["source_record"], "collector_summary")
        self.assertEqual(
            item.session.identity_metadata["identity_sidecar"]["migration_method"],
            "freeze_legacy_assigned_id",
        )

    def test_nine_fixed_sidecar_entries_keep_legacy_and_deterministic_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_dir = root / "_collector_state.d"
            identity_dir.mkdir()
            records = [
                summary_record(f"fixed-{index}", title=f"Synthetic {index}") for index in range(9)
            ]
            write_jsonl(root / "05.jsonl", records)
            write_json(
                identity_dir / "2026.identity.json",
                {
                    "source_instance": "chatgpt-personal",
                    "identity_entries": [
                        identity_entry(
                            f"fixed-{index}",
                            f"Synthetic {index}",
                            method=(
                                "freeze_legacy_assigned_id"
                                if index < 8
                                else "retain_existing_deterministic_id"
                            ),
                        )
                        for index in range(9)
                    ],
                },
            )
            adapter = ChatActivityAdapter(source_instance="chatgpt-personal")
            first = adapter.import_directory(root)
            first_ids = {
                item.session.collector_conversation_id: item.session.id for item in first.values()
            }
            records[-1]["last_activity_at"] = "2026-10-05T09:09:00+00:00"
            write_jsonl(root / "05.jsonl", records)
            second = adapter.import_directory(root)

        self.assertEqual(len(second), 9)
        methods = [item.session.collector_id_method for item in second.values()]
        self.assertEqual(methods.count("freeze_legacy_assigned_id"), 8)
        self.assertEqual(methods.count("retain_existing_deterministic_id"), 1)
        self.assertEqual(
            {item.session.collector_conversation_id: item.session.id for item in second.values()},
            first_ids,
        )

    def test_older_summary_snapshot_cannot_overwrite_newer_upsert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            newer = summary_record(
                "fixed-1",
                title="Newer title",
                last_activity="2026-10-05T02:00:00+00:00",
            )
            older = summary_record(
                "fixed-1",
                title="Older title",
                last_activity="2026-10-05T01:00:00+00:00",
            )
            older["summary"] = "Older summary"
            write_jsonl(root / "01.jsonl", [newer])
            write_jsonl(root / "02.jsonl", [older])
            imported = ChatActivityAdapter(source_instance="chatgpt-personal").import_directory(
                root
            )

        item = next(iter(imported.values()))
        self.assertEqual(item.session.title, "Newer title")
        self.assertEqual(item.events[0].text, "Synthetic summary text")
        self.assertEqual(item.events[0].ts, item.session.end)

    def test_native_conversation_id_is_a_stable_fallback_without_collector_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_jsonl(
                Path(directory) / "claude.jsonl",
                [
                    {
                        **summary_record(None, native_id="claude-native-1"),
                        "source_instance": "claude-personal",
                        "provider": "anthropic",
                        "product": "claude-chat",
                    }
                ],
            )
            imported = ChatActivityAdapter(source_instance="claude-personal").import_jsonl(path)

        item = next(iter(imported.values()))
        self.assertIsNone(item.session.collector_conversation_id)
        self.assertEqual(item.session.native_id, "claude-native-1")
        self.assertEqual(item.session.collector_id_method, "native_conversation_id")

    def test_sidecar_alias_keeps_identity_when_native_id_appears_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_dir = root / "_collector_state.d"
            identity_dir.mkdir()
            write_json(
                identity_dir / "2026.identity.json",
                {
                    "source_instance": "chatgpt-personal",
                    "identity_entries": [
                        identity_entry("fixed-1", "Stable alias", method="freeze")
                    ],
                },
            )
            write_jsonl(
                root / "05.jsonl",
                [summary_record(None, native_id="native-later", title="Stable alias")],
            )
            imported = ChatActivityAdapter(source_instance="chatgpt-personal").import_directory(
                root
            )

        item = next(iter(imported.values()))
        self.assertEqual(item.session.collector_conversation_id, "fixed-1")
        self.assertEqual(item.session.native_id, "native-later")
        self.assertEqual(
            item.session.id,
            collector_session_id(
                source_instance="chatgpt-personal", collector_conversation_id="fixed-1"
            ),
        )

    def test_ambiguous_alias_is_skipped_and_corrected_tmp_shard_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_dir = root / "_collector_state.d"
            identity_dir.mkdir()
            write_jsonl(root / "01.jsonl", [summary_record(None, title="Ambiguous title")])
            write_jsonl(
                root / "05.corrected.tmp.jsonl",
                [summary_record("must-not-import", title="Corrected temporary")],
            )
            write_json(
                identity_dir / "2026.identity.json",
                {
                    "source_instance": "chatgpt-personal",
                    "identity_entries": [
                        identity_entry("legacy-1", "Ambiguous title", method="freeze"),
                        identity_entry("legacy-2", "Ambiguous title", method="freeze"),
                    ],
                },
            )
            imported = ChatActivityAdapter(source_instance="chatgpt-personal").import_directory(
                root
            )

        self.assertEqual(
            {item.session.collector_conversation_id for item in imported.values()},
            {"legacy-1", "legacy-2"},
        )
        self.assertNotIn(
            "must-not-import",
            {item.session.collector_conversation_id for item in imported.values()},
        )

    def test_state_and_sidecar_updates_are_reimported_without_identity_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_dir = root / "_collector_state.d"
            identity_dir.mkdir()
            write_jsonl(root / "01.jsonl", [summary_record("fixed-1")])
            sidecar = identity_dir / "2026.identity.json"
            write_json(
                sidecar,
                {
                    "source_instance": "chatgpt-personal",
                    "identity_entries": [
                        identity_entry("fixed-1", "Synthetic summary", method="freeze")
                    ],
                },
            )
            write_json(
                root / "_collector_state.json",
                {
                    "schema_version": 1,
                    "source_instance": "chatgpt-personal",
                    "last_collected_activity_at": "2026-10-05T01:04:00+00:00",
                    "last_successful_run_at": "2026-10-05T01:04:01+00:00",
                    "backfill_complete": False,
                },
            )
            adapter = ChatActivityAdapter(source_instance="chatgpt-personal")
            first = next(iter(adapter.import_directory(root).values()))
            first_id = first.session.id
            write_json(
                sidecar,
                {
                    "source_instance": "chatgpt-personal",
                    "identity_entries": [
                        identity_entry("fixed-1", "Renamed alias", method="sidecar_update")
                    ],
                },
            )
            second = next(iter(adapter.import_directory(root).values()))

        self.assertEqual(second.session.id, first_id)
        self.assertFalse(second.session.backfill_complete)
        self.assertEqual(
            second.session.identity_metadata["identity_sidecar"]["migration_method"],
            "sidecar_update",
        )


if __name__ == "__main__":
    unittest.main()
