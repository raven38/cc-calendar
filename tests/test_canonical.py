import json
import tempfile
import unittest
from pathlib import Path

from cc_calendar.canonical import (
    AnalysisPolicy,
    CanonicalProject,
    Event,
    ExportPolicy,
    ProjectAssigner,
    ProjectAssignment,
    ProjectVisibility,
    Session,
    SessionCapabilities,
    SessionVisibility,
    canonical_session_id,
    collector_session_id,
    derived_collector_id,
)
from cc_calendar.canonical.claude_code import ClaudeCodeAdapter


class CanonicalModelsTest(unittest.TestCase):
    def test_session_and_event_keep_unknown_metrics_unavailable(self):
        session = Session(
            id="session-1",
            native_id=None,
            provider="openai",
            product="chatgpt",
            surface="collector",
            source_instance="chatgpt-personal",
            account="main",
            title="Synthetic research",
            project_id="__misc__",
            capabilities=SessionCapabilities(
                transcript=False,
                token_usage=False,
                cache_stats=False,
                tool_stats=False,
                git_commits=False,
                diff=False,
                resume=False,
                open_remote=False,
                live_status=False,
            ),
        )
        event = Event(
            id="event-1",
            session_id=session.id,
            ts=None,
            kind="status",
            metadata={"source": "synthetic"},
        )

        self.assertIsNone(event.usage)
        self.assertIsNone(event.model)
        self.assertEqual(session.visibility, SessionVisibility.UNKNOWN)
        self.assertEqual(session.history_completeness, "activity")

    def test_collector_id_is_stable_without_native_conversation_id(self):
        args = {
            "provider": "openai",
            "source_instance": "chatgpt-personal",
            "created_at": "2026-10-05T11:02:00+09:00",
            "first_message_at": "2026-10-05T11:02:03+09:00",
            "title": "Synthetic research",
        }
        with self.assertRaises(ValueError):
            derived_collector_id(**args)
        stable = derived_collector_id(**args, fallback_key="collector-uuid-1")
        self.assertEqual(
            stable,
            derived_collector_id(
                **{
                    **args,
                    "title": "Changed title",
                    "first_message_at": "2026-10-05T11:12:04+09:00",
                    "fallback_key": "collector-uuid-1",
                }
            ),
        )
        self.assertTrue(stable.startswith("chatgpt-personal:collector:"))

    def test_collector_identity_survives_later_native_id_discovery(self):
        stable = collector_session_id(
            source_instance="chatgpt-personal", collector_conversation_id="collector-uuid-1"
        )
        self.assertEqual(
            stable,
            collector_session_id(
                source_instance="chatgpt-personal",
                collector_conversation_id="collector-uuid-1",
                native_id="provider-native-id-discovered-later",
            ),
        )

    def test_fallback_id_accepts_missing_times_without_using_title_alone(self):
        args = {
            "provider": "openai",
            "source_instance": "chatgpt-personal",
            "created_at": None,
            "first_message_at": None,
            "title": "Repeated title",
        }
        with self.assertRaises(ValueError):
            derived_collector_id(**args)
        one = derived_collector_id(**args, fallback_key="collector-1")
        other = derived_collector_id(**{**args, "fallback_key": "collector-2"})
        self.assertNotEqual(one, other)

    def test_session_id_namespaces_native_identity_by_source_instance(self):
        self.assertEqual(
            canonical_session_id(source_instance="codex-main", native_id="abc"),
            canonical_session_id(source_instance="codex-main", native_id="abc"),
        )
        self.assertNotEqual(
            canonical_session_id(source_instance="codex-main", native_id="abc"),
            canonical_session_id(source_instance="codex-work", native_id="abc"),
        )

    def test_project_assigner_falls_back_to_private_misc(self):
        projects = {
            "__misc__": CanonicalProject(
                id="__misc__",
                name="Misc",
                slug="misc",
            )
        }
        result = ProjectAssigner(projects).assign(Session(id="s-1", project_id=""))

        self.assertEqual(result.project_id, "__misc__")
        self.assertEqual(result.provenance, "misc")
        self.assertEqual(projects["__misc__"].visibility, "private")
        self.assertEqual(projects["__misc__"].export_policy, ExportPolicy.LOCAL_ONLY)
        self.assertEqual(projects["__misc__"].analysis_policy, AnalysisPolicy.LOCAL)

    def test_unknown_mapping_target_falls_back_to_misc(self):
        projects = {"__misc__": CanonicalProject(id="__misc__", name="Misc", slug="misc")}
        assigner = ProjectAssigner(
            projects,
            native_mappings={("openai", "chatgpt", "chatgpt-personal", "missing"): "gone"},
        )
        session = Session(
            id="s-1",
            provider="openai",
            product="chatgpt",
            source_instance="chatgpt-personal",
            external_project_ref={"native_project_id": "missing"},
        )
        self.assertEqual(assigner.assign(session), ProjectAssignment("__misc__", "misc", 1.0))

    def test_project_assignment_precedence_is_native_then_repo_then_path(self):
        projects = {
            "__misc__": CanonicalProject(id="__misc__", name="Misc", slug="misc"),
            "native": CanonicalProject(id="native", name="Native", slug="native"),
            "repo": CanonicalProject(id="repo", name="Repo", slug="repo"),
            "path": CanonicalProject(id="path", name="Path", slug="path"),
        }
        assigner = ProjectAssigner(
            projects,
            native_mappings={("openai", "chatgpt", "chatgpt-personal", "p-1"): "native"},
            repo_mappings={"github.com/example/repo": "repo"},
            path_mappings={"/work/project": "path"},
        )
        session = Session(
            id="s-1",
            provider="openai",
            product="chatgpt",
            source_instance="chatgpt-personal",
            external_project_ref={"native_project_id": "p-1"},
            repo="github.com/example/repo",
            cwd="/work/project/src",
        )

        result = assigner.assign(session)
        self.assertEqual((result.project_id, result.provenance), ("native", "native"))

    def test_project_assignment_uses_provider_name_then_classifier_before_misc(self):
        projects = {
            "__misc__": CanonicalProject(id="__misc__", name="Misc", slug="misc"),
            "provider": CanonicalProject(id="provider", name="Provider", slug="provider"),
            "classified": CanonicalProject(id="classified", name="Classified", slug="classified"),
        }
        assigner = ProjectAssigner(
            projects,
            provider_name_mappings={
                ("openai", "chatgpt", "chatgpt-personal", "Research"): "provider"
            },
            classifier=lambda session: ("classified", 0.7),
        )
        session = Session(
            id="s-1",
            provider="openai",
            product="chatgpt",
            source_instance="chatgpt-personal",
            external_project_ref={"native_project_name": "Research"},
        )
        self.assertEqual(
            (assigner.assign(session).project_id, assigner.assign(session).provenance),
            ("provider", "provider_name"),
        )

        session.external_project_ref = None
        self.assertEqual(
            (assigner.assign(session).project_id, assigner.assign(session).provenance),
            ("classified", "classifier"),
        )

    def test_public_project_does_not_make_session_public_or_exportable(self):
        project = CanonicalProject(
            id="public", name="Public", slug="public", visibility=ProjectVisibility.PUBLIC
        )
        session = Session(id="s-1", project_id=project.id)
        self.assertEqual(project.visibility, "public")
        self.assertEqual(session.visibility, SessionVisibility.UNKNOWN)
        self.assertEqual(project.export_policy, ExportPolicy.LOCAL_ONLY)
        self.assertEqual(project.analysis_policy, AnalysisPolicy.LOCAL)

    def test_claude_jsonl_import_reaches_canonical_and_existing_analysis(self):
        records = [
            {
                "type": "user",
                "sessionId": "synthetic-session",
                "uuid": "u1",
                "timestamp": "2026-10-05T00:00:00+00:00",
                "cwd": "/work/demo",
                "gitBranch": "main",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": "Write the README"},
            },
            {
                "type": "assistant",
                "sessionId": "synthetic-session",
                "uuid": "a1",
                "timestamp": "2026-10-05T00:05:00+00:00",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-synthetic",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [
                        {"type": "text", "text": "I will update it."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Write",
                            "input": {"file_path": "/work/demo/README.md"},
                        },
                    ],
                    "stop_reason": "tool_use",
                },
            },
            {
                "type": "user",
                "sessionId": "synthetic-session",
                "uuid": "u2",
                "timestamp": "2026-10-05T00:06:00+00:00",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
                },
            },
        ]
        records.append(records[0].copy())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-session.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            imported = ClaudeCodeAdapter(source_instance="claude-code-local").import_jsonl(path)

        self.assertEqual(imported.session.provider, "anthropic")
        self.assertEqual(imported.session.product, "claude-code")
        self.assertEqual(imported.session.surface, "local")
        self.assertEqual(imported.session.project_id, "__misc__")
        self.assertEqual(imported.assignment.provenance, "misc")
        self.assertEqual(imported.session.history_completeness, "full")
        self.assertEqual(imported.source.tokens(), 15)
        self.assertEqual(len(imported.source.segments(15 * 60_000)), 1)
        self.assertEqual(
            [event.kind for event in imported.events],
            ["user_message", "assistant_message", "tool_call", "tool_result"],
        )

    def test_source_instance_and_policy_are_preserved_at_canonical_boundary(self):
        records = [
            {
                "type": "user",
                "sessionId": "s",
                "uuid": "u",
                "origin": {"kind": "human"},
                "message": {"content": "same"},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.jsonl"
            path.write_text(json.dumps(records[0]) + "\n")
            personal = ClaudeCodeAdapter(source_instance="claude-code-personal").import_jsonl(path)
            work = ClaudeCodeAdapter(source_instance="claude-code-work").import_jsonl(path)

        self.assertNotEqual(personal.session.id, work.session.id)
        self.assertEqual(personal.session.visibility, SessionVisibility.PRIVATE)
        self.assertEqual(personal.assignment.project_id, "__misc__")
        self.assertIsNotNone(personal.project)
        self.assertEqual(personal.project.export_policy, ExportPolicy.LOCAL_ONLY)
        self.assertEqual(personal.project.analysis_policy, AnalysisPolicy.LOCAL)


if __name__ == "__main__":
    unittest.main()
