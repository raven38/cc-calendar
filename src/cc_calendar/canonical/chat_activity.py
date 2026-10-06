"""Read-only importer for local Claude Chat and ChatGPT collector JSONL shards.

Collector files are treated as immutable input.  The collector conversation ID is
the only stable identity accepted from this format; titles and timestamps are
never used to create or migrate an identity.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..parser import parse_ts
from .claude_code import CanonicalImport
from .models import (
    AnalysisPolicy,
    CanonicalProject,
    Event,
    ExportPolicy,
    ProjectAssignment,
    ProjectVisibility,
    Session,
    SessionCapabilities,
    SessionVisibility,
    Usage,
)
from .normalize import collector_session_id
from .projects import ProjectAssigner


@dataclass
class CollectorSource:
    source_instance: str
    provider: str = ""
    product: str = ""
    account: str | None = None
    model: str | None = None
    effort: str | None = None
    usage: Usage | None = None
    event_usages: dict[str, Usage] = field(default_factory=dict)

    def tokens(self) -> int | None:
        usage = self.usage
        if usage is not None:
            return _usage_total(usage)
        if not self.event_usages:
            return None
        return sum(_usage_total(item) for item in self.event_usages.values())


@dataclass
class _SessionState:
    session: Session
    source: CollectorSource
    events: dict[str, Event] = field(default_factory=dict)
    event_order: dict[str, int] = field(default_factory=dict)
    assignment: ProjectAssignment = field(
        default_factory=lambda: ProjectAssignment("__misc__", "misc", 1.0)
    )
    project: CanonicalProject | None = None
    snapshot_ts: int | None = None
    snapshot_order: int = -1


class ChatActivityAdapter:
    """Normalize local collector shards for one explicit source instance."""

    def __init__(
        self,
        *,
        source_instance: str,
        assigner: ProjectAssigner | None = None,
    ) -> None:
        if not source_instance:
            raise ValueError("source_instance is required")
        self.source_instance = source_instance
        self.assigner = assigner or ProjectAssigner({"__misc__": _misc_project()})

    def import_jsonl(self, path: Path, *, live: dict | None = None) -> dict[str, CanonicalImport]:
        del live
        return self.import_paths([path])

    def import_directory(
        self, root: Path, *, live: dict | None = None
    ) -> dict[str, CanonicalImport]:
        """Read summary shards plus sidecars from one explicit collector directory."""
        if not root.is_dir():
            return {}
        paths = sorted(path for path in root.rglob("*.jsonl") if not _is_corrected_temporary(path))
        identity_paths = sorted(root.rglob("*.identity.json"))
        state_paths = sorted(
            path
            for path in root.rglob("_collector_state.json")
            if not _is_corrected_temporary(path)
        )
        return self.import_paths(
            paths,
            identity_paths=identity_paths,
            state_paths=state_paths,
            live=live,
        )

    def import_paths(
        self,
        paths: Iterable[Path],
        *,
        identity_paths: Iterable[Path] = (),
        state_paths: Iterable[Path] = (),
        live: dict | None = None,
    ) -> dict[str, CanonicalImport]:
        del live
        states: dict[str, _SessionState] = {}
        identity_entries, alias_index, sidecar_policies = self._read_identity_paths(identity_paths)
        self._seed_identity_states(states, identity_entries, sidecar_policies)
        for path in paths:
            self._read_path(path, states, alias_index)
        self._apply_state_paths(states, state_paths)
        return {session_id: self._finalize(state) for session_id, state in states.items()}

    def _read_path(
        self,
        path: Path,
        states: dict[str, _SessionState],
        alias_index: dict[str, set[str]],
    ) -> None:
        if _is_corrected_temporary(path):
            return
        with path.open("rb") as stream:
            for ordinal, raw in enumerate(stream):
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict) or not self._matches_source(record):
                    continue
                record_type = _record_type(record)
                if record_type in {"session", "conversation", "session_snapshot"}:
                    self._feed_session(record, states, ordinal, alias_index)
                elif record_type in {"event", "activity", "message", "session_event"}:
                    self._feed_event(record, states, ordinal, alias_index)

    def _read_identity_paths(
        self, paths: Iterable[Path]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, dict[str, Any]]]:
        entries: dict[str, dict[str, Any]] = {}
        aliases: dict[str, set[str]] = defaultdict(set)
        policies: dict[str, dict[str, Any]] = {}
        for path in paths:
            if _is_corrected_temporary(path):
                continue
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict) or not self._matches_source(document):
                continue
            policy = document.get("policy")
            raw_entries = document.get("identity_entries")
            if not isinstance(raw_entries, list):
                continue
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    continue
                collector_id = _first_string(raw_entry, "collector_conversation_id")
                if not collector_id:
                    continue
                entries[collector_id] = dict(raw_entry)
                if isinstance(policy, dict):
                    policies[collector_id] = dict(policy)
                match_aliases = raw_entry.get("match_aliases")
                if isinstance(match_aliases, dict):
                    for value in match_aliases.values():
                        if isinstance(value, str) and value.strip():
                            aliases[_normalize_alias(value)].add(collector_id)
        return entries, dict(aliases), policies

    def _seed_identity_states(
        self,
        states: dict[str, _SessionState],
        entries: dict[str, dict[str, Any]],
        policies: dict[str, dict[str, Any]],
    ) -> None:
        for collector_id, entry in entries.items():
            state = self._state_for(states, ("collector", collector_id), {})
            state.session.identity_metadata["identity_sidecar"] = entry
            policy = policies.get(collector_id)
            if policy is not None:
                state.session.identity_metadata["identity_policy"] = policy
            migration_method = _first_string(entry, "migration_method")
            if migration_method:
                state.session.collector_id_method = migration_method
            self._refresh(state)

    def _apply_state_paths(self, states: dict[str, _SessionState], paths: Iterable[Path]) -> None:
        documents: list[dict[str, Any]] = []
        for path in paths:
            if _is_corrected_temporary(path):
                continue
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and self._matches_source(document):
                documents.append(document)
        if not documents:
            return
        document = documents[-1]
        for state in states.values():
            state.session.identity_metadata["collector_state"] = document
            complete = document.get("backfill_complete")
            if isinstance(complete, bool):
                state.session.backfill_complete = complete
            self._refresh(state)

    def _matches_source(self, record: dict[str, Any]) -> bool:
        source = record.get("source_instance")
        nested = record.get("session")
        if source is None and isinstance(nested, dict):
            source = nested.get("source_instance")
        return not source or source == self.source_instance

    def _feed_session(
        self,
        record: dict[str, Any],
        states: dict[str, _SessionState],
        ordinal: int,
        alias_index: dict[str, set[str]],
    ) -> None:
        payload = _payload(record, "session")
        identity = _identity_key(record, payload, alias_index)
        if identity is None:
            return
        state = self._state_for(states, identity, payload)
        activity = _snapshot_timestamp(payload)
        replace = _snapshot_is_newer(state, activity, ordinal)
        self._feed_common_fields(state, payload, replace=replace)
        self._feed_session_fields(state, payload, activity, ordinal, replace)
        self._feed_summary_event(state, payload, activity, ordinal, replace)
        self._refresh(state)

    def _feed_event(
        self,
        record: dict[str, Any],
        states: dict[str, _SessionState],
        ordinal: int,
        alias_index: dict[str, set[str]],
    ) -> None:
        payload = _payload(record, "event")
        identity = _identity_key(record, payload, alias_index)
        if identity is None:
            return
        state = self._state_for(states, identity, payload)
        self._feed_common_fields(state, payload)
        event_kind = _first_string(payload, "kind", "event_kind")
        if not event_kind:
            return
        event_id = _first_string(payload, "event_id", "id")
        if not event_id:
            event_id = _fingerprint(payload)
        canonical_event_id = f"{state.session.id}:event:{event_id}"
        usage = _parse_usage(payload.get("usage"))
        event = state.events.get(canonical_event_id)
        if event is None:
            event = Event(
                id=canonical_event_id,
                session_id=state.session.id,
                ts=parse_ts(payload.get("timestamp")),
                kind=event_kind,
                role=_first_string(payload, "role"),
                text=_event_text(payload),
                model=_first_string(payload, "model"),
                effort=_first_string(payload, "effort", "reasoning_effort"),
                tool_name=_first_string(payload, "tool_name", "name"),
                tool_call_id=_first_string(payload, "tool_call_id", "call_id"),
                tool_input=payload.get("tool_input", payload.get("arguments")),
                tool_output=_first_string(payload, "tool_output", "output"),
                is_error=(
                    payload.get("is_error") if isinstance(payload.get("is_error"), bool) else None
                ),
                usage=usage,
                metadata=_event_metadata(payload, event_id, ordinal),
            )
            state.events[canonical_event_id] = event
            state.event_order[canonical_event_id] = ordinal
        else:
            _merge_event(event, payload, usage)
        if usage is not None:
            state.source.event_usages[canonical_event_id] = usage
        self._refresh(state)

    def _state_for(
        self,
        states: dict[str, _SessionState],
        identity: tuple[str, str],
        payload: dict[str, Any],
    ) -> _SessionState:
        identity_kind, identity_value = identity
        session_id = collector_session_id(
            source_instance=self.source_instance,
            collector_conversation_id=identity_value if identity_kind == "collector" else None,
            native_id=identity_value if identity_kind == "native" else None,
        )
        state = states.get(session_id)
        if state is not None:
            return state
        source = CollectorSource(source_instance=self.source_instance)
        native_id = _nonempty_string(payload.get("conversation_id"))
        session = Session(
            id=session_id,
            native_id=native_id or (identity_value if identity_kind == "native" else None),
            provider=_first_string(payload, "provider") or "",
            product=_first_string(payload, "product") or "",
            surface="collector",
            source_instance=self.source_instance,
            account=_first_string(payload, "account"),
            title="(untitled session)",
            project_id="__misc__",
            status="unknown",
            history_completeness="activity",
            capabilities=SessionCapabilities(resume=False),
            visibility=SessionVisibility.PRIVATE,
            collector_conversation_id=identity_value if identity_kind == "collector" else None,
            collector_id_method=(
                _first_string(payload, "collector_id_method")
                if identity_kind == "collector"
                else "native_conversation_id"
            ),
        )
        state = _SessionState(session=session, source=source)
        states[session_id] = state
        self._refresh(state)
        return state

    def _feed_common_fields(
        self, state: _SessionState, payload: dict[str, Any], *, replace: bool = True
    ) -> None:
        session = state.session
        provider = _first_string(payload, "provider")
        product = _first_string(payload, "product")
        account = _first_string(payload, "account")
        if provider and (replace or not session.provider):
            session.provider = provider
            state.source.provider = provider
        if product and (replace or not session.product):
            session.product = product
            state.source.product = product
        if account and (replace or not session.account):
            session.account = account
            state.source.account = account
        native_id = _nonempty_string(payload.get("conversation_id"))
        if native_id and (replace or not session.native_id):
            session.native_id = native_id

    def _feed_summary_event(
        self,
        state: _SessionState,
        payload: dict[str, Any],
        activity: int | None,
        ordinal: int,
        replace: bool,
    ) -> None:
        summary = _first_string(payload, "summary")
        if not summary:
            return
        event_id = f"{state.session.id}:summary"
        event = state.events.get(event_id)
        if event is None:
            state.events[event_id] = Event(
                id=event_id,
                session_id=state.session.id,
                ts=activity,
                kind="summary",
                text=summary,
                metadata={"source_record": "collector_summary"},
            )
            state.event_order[event_id] = ordinal
        elif replace:
            event.ts = activity
            event.text = summary

    def _feed_session_fields(
        self,
        state: _SessionState,
        payload: dict[str, Any],
        activity: int | None,
        ordinal: int,
        replace: bool,
    ) -> None:
        session = state.session
        source = state.source
        title = _first_string(payload, "title", "session_title")
        if replace and title:
            session.title = title
        created = parse_ts(payload.get("created_at")) if "created_at" in payload else None
        if created is not None:
            session.start = created if session.start is None else min(session.start, created)
        if activity is not None:
            session.end = activity if session.end is None else max(session.end, activity)
        model = _first_string(payload, "model")
        effort = _first_string(payload, "effort", "reasoning_effort")
        if replace and model:
            source.model = model
        if replace and effort:
            source.effort = effort
        usage = _parse_usage(payload.get("usage"))
        if replace and usage is not None:
            source.usage = usage
        collector = payload.get("collector")
        history = _first_string(payload, "history_completeness")
        if history is None and isinstance(collector, dict):
            history = _first_string(collector, "history_completeness")
        if replace and history:
            session.history_completeness = history
        sync_state = _first_string(payload, "sync_state", "state")
        if replace and sync_state:
            session.sync_state = sync_state
        backfill = _first_string(payload, "backfill_status")
        if replace and backfill:
            session.backfill_status = backfill
        completed = payload.get("completed")
        if replace and isinstance(completed, bool):
            session.completed = completed
        if replace and isinstance(payload.get("backfill_complete"), bool):
            session.backfill_complete = payload["backfill_complete"]
        method = _first_string(payload, "collector_id_method")
        if (
            method
            and session.collector_conversation_id is not None
            and (replace or not session.collector_id_method)
        ):
            sidecar_entry = session.identity_metadata.get("identity_sidecar")
            session.collector_id_method = (
                _first_string(sidecar_entry, "migration_method")
                if isinstance(sidecar_entry, dict)
                else None
            ) or method
        sidecar = payload.get("identity_sidecar", payload.get("identity_migration"))
        if isinstance(sidecar, dict):
            session.identity_metadata.update(sidecar)
        record_metadata = {
            key: payload[key]
            for key in (
                "schema_version",
                "collector_id_inputs",
                "collector",
                "conversation_url",
                "decisions",
                "open_items",
                "waiting_for",
                "attachments",
                "message_count",
                "first_message_at",
                "last_message_at",
            )
            if key in payload
        }
        if record_metadata:
            session.identity_metadata["summary_record"] = record_metadata
        visibility = _enum_value(SessionVisibility, payload.get("visibility"))
        if replace and visibility is not None:
            session.visibility = visibility
        project = payload.get("project")
        if replace and isinstance(project, dict):
            self._register_project(state, project)
        if replace:
            state.snapshot_ts = activity
            state.snapshot_order = ordinal

    def _register_project(self, state: _SessionState, project: dict[str, Any]) -> None:
        native_id = _first_string(project, "id", "native_project_id")
        if not native_id:
            return
        digest = hashlib.sha256(native_id.encode()).hexdigest()[:24]
        project_id = f"{self.source_instance}:project:{digest}"
        canonical = CanonicalProject(
            id=project_id,
            name=_first_string(project, "name") or native_id,
            slug=_slug(_first_string(project, "name") or native_id),
            visibility=_enum_value(ProjectVisibility, project.get("visibility"))
            or ProjectVisibility.UNKNOWN,
            kind="native",
            export_policy=_enum_value(ExportPolicy, project.get("export_policy"))
            or ExportPolicy.LOCAL_ONLY,
            analysis_policy=_enum_value(AnalysisPolicy, project.get("analysis_policy"))
            or AnalysisPolicy.LOCAL,
        )
        self.assigner.projects[project_id] = canonical
        key = (state.session.provider, state.session.product, self.source_instance, native_id)
        self.assigner.native_mappings[key] = project_id
        state.session.external_project_ref = {
            "native_project_id": native_id,
            "native_project_name": canonical.name,
        }

    def _refresh(self, state: _SessionState) -> None:
        session = state.session
        session.status = _status(session)
        if session.backfill_complete is None:
            if session.backfill_status in {"complete", "completed", "not_required"}:
                session.backfill_complete = True
            elif session.backfill_status not in {"unknown", ""}:
                session.backfill_complete = False
        if session.history_completeness == "full" and session.backfill_complete is not True:
            session.history_completeness = "summary"
        session.capabilities = SessionCapabilities(
            transcript=session.history_completeness == "full",
            token_usage=state.source.tokens() is not None,
            cache_stats=False,
            tool_stats=any(
                event.kind in {"tool_call", "tool_result"} for event in state.events.values()
            ),
            resume=False,
            live_status=False,
        )
        state.assignment = self.assigner.assign(session)
        session.project_id = state.assignment.project_id
        state.project = self.assigner.projects.get(state.assignment.project_id)

    def _finalize(self, state: _SessionState) -> CanonicalImport:
        events = [state.events[key] for key in sorted(state.events, key=state.event_order.get)]
        return CanonicalImport(
            session=state.session,
            events=events,
            source=state.source,
            assignment=state.assignment,
            project=state.project,
        )


def _record_type(record: dict[str, Any]) -> str:
    value = record.get("record_type", record.get("type"))
    if isinstance(value, str):
        return value
    if record.get("event_id") or record.get("event_kind"):
        return "event"
    if any(
        key in record
        for key in ("collector_conversation_id", "conversation_id", "last_activity_at")
    ) and ("title" in record or "summary" in record or "collector" in record):
        return "session"
    return ""


def _payload(record: dict[str, Any], key: str) -> dict[str, Any]:
    nested = record.get(key)
    return nested if isinstance(nested, dict) else record


def _identity_key(
    record: dict[str, Any],
    payload: dict[str, Any],
    alias_index: dict[str, set[str]],
) -> tuple[str, str] | None:
    direct = _first_string(payload, "collector_conversation_id") or _first_string(
        record, "collector_conversation_id"
    )
    if direct:
        return "collector", direct
    identity = payload.get("identity", record.get("identity"))
    if isinstance(identity, dict):
        sidecar_id = _first_string(identity, "collector_conversation_id")
        if sidecar_id:
            return "collector", sidecar_id
    title = _first_string(payload, "title", "session_title")
    if title:
        matches = alias_index.get(_normalize_alias(title), set())
        if len(matches) == 1:
            return "collector", next(iter(matches))
        if len(matches) > 1:
            return None
    native = _nonempty_string(payload.get("conversation_id"))
    if native:
        return "native", native
    return None


def _is_corrected_temporary(path: Path) -> bool:
    return ".corrected.tmp" in path.name


def _normalize_alias(value: str) -> str:
    return " ".join(value.casefold().split())


def _event_text(payload: dict[str, Any]) -> str | None:
    text = _first_string(payload, "text", "summary")
    if text:
        return text
    content = payload.get("content")
    return content if isinstance(content, str) and content else None


def _snapshot_timestamp(payload: dict[str, Any]) -> int | None:
    if "last_activity_at" in payload:
        return parse_ts(payload.get("last_activity_at"))
    if "created_at" in payload:
        return parse_ts(payload.get("created_at"))
    return None


def _snapshot_is_newer(state: _SessionState, timestamp: int | None, ordinal: int) -> bool:
    if state.snapshot_order < 0:
        return True
    if timestamp is not None and state.snapshot_ts is None:
        return True
    if timestamp is None and state.snapshot_ts is not None:
        return False
    if timestamp is not None and state.snapshot_ts is not None:
        return timestamp >= state.snapshot_ts
    return ordinal >= state.snapshot_order


def _event_metadata(payload: dict[str, Any], event_id: str, ordinal: int) -> dict[str, Any]:
    metadata = payload.get("metadata")
    result = dict(metadata) if isinstance(metadata, dict) else {}
    result.update({"collector_event_id": event_id, "ordinal": ordinal})
    return result


def _merge_event(event: Event, payload: dict[str, Any], usage: Usage | None) -> None:
    timestamp = parse_ts(payload.get("timestamp"))
    if timestamp is not None:
        event.ts = timestamp
    for field_name, value in (
        ("role", _first_string(payload, "role")),
        ("text", _event_text(payload)),
        ("model", _first_string(payload, "model")),
        ("effort", _first_string(payload, "effort", "reasoning_effort")),
        ("tool_name", _first_string(payload, "tool_name", "name")),
        ("tool_call_id", _first_string(payload, "tool_call_id", "call_id")),
        ("tool_output", _first_string(payload, "tool_output", "output")),
    ):
        if value is not None:
            setattr(event, field_name, value)
    if "tool_input" in payload or "arguments" in payload:
        event.tool_input = payload.get("tool_input", payload.get("arguments"))
    if isinstance(payload.get("is_error"), bool):
        event.is_error = payload["is_error"]
    if usage is not None:
        event.usage = usage
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        event.metadata.update(metadata)


def _parse_usage(value: object) -> Usage | None:
    if not isinstance(value, dict):
        return None
    values = {
        "input_tokens": value.get("input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "cache_read_tokens": value.get("cache_read_tokens", value.get("cached_input_tokens")),
        "cache_write_tokens": value.get(
            "cache_write_tokens", value.get("cache_creation_input_tokens")
        ),
        "cost": value.get("cost"),
    }
    if not any(isinstance(item, int) for item in values.values()):
        return None
    return Usage(
        input_tokens=item if isinstance(item := values["input_tokens"], int) else None,
        output_tokens=item if isinstance(item := values["output_tokens"], int) else None,
        cache_read_tokens=item if isinstance(item := values["cache_read_tokens"], int) else None,
        cache_write_tokens=item if isinstance(item := values["cache_write_tokens"], int) else None,
        cost=values["cost"] if isinstance(values["cost"], (int, float)) else None,
    )


def _usage_total(usage: Usage) -> int:
    return sum(
        value
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        if value is not None
    )


def _status(session: Session) -> str:
    if session.sync_state == "running":
        return "running"
    if session.sync_state == "waiting":
        return "waiting"
    if session.sync_state in {"interrupted", "aborted"}:
        return "interrupted"
    if (
        session.completed is True
        and session.sync_state in {"complete", "completed"}
        and session.backfill_status in {"complete", "completed", "not_required"}
    ):
        return "done"
    return "unknown"


def _misc_project() -> CanonicalProject:
    return CanonicalProject(
        id="__misc__",
        name="Misc",
        slug="misc",
        visibility=ProjectVisibility.PRIVATE,
        kind="misc",
        export_policy=ExportPolicy.LOCAL_ONLY,
        analysis_policy=AnalysisPolicy.LOCAL,
    )


def _enum_value(enum_type, value):
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _fingerprint(value: dict[str, Any]) -> str:
    payload = {
        key: value.get(key)
        for key in (
            "kind",
            "event_kind",
            "timestamp",
            "role",
            "text",
            "content",
            "model",
            "effort",
            "tool_name",
            "tool_call_id",
            "tool_input",
            "tool_output",
            "usage",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"fingerprint-{hashlib.sha256(encoded.encode()).hexdigest()}"


def _slug(value: str) -> str:
    return "-".join(value.lower().split()) or "project"
