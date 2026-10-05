"""Read-only adapter from Claude Code JSONL into canonical records.

The existing :class:`SessionAcc` remains the compatibility reference for the
calendar's analytics. This adapter projects the same input into canonical
objects; it does not rewrite the transcript or send data anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parser import (
    SessionAcc,
    classify_user,
    command_text,
    compact_info,
    content_text,
    parse_ts,
    tool_result_text,
)
from .models import (
    AnalysisPolicy,
    CanonicalProject,
    Event,
    ProjectAssignment,
    ProjectVisibility,
    Session,
    SessionCapabilities,
    SessionVisibility,
    Usage,
)
from .normalize import canonical_session_id
from .projects import ProjectAssigner


@dataclass
class CanonicalImport:
    session: Session
    events: list[Event]
    source: Any
    assignment: ProjectAssignment
    project: CanonicalProject | None


class ClaudeCodeAdapter:
    """Project Claude Code local records without changing their source of truth."""

    def __init__(
        self,
        *,
        source_instance: str | None = None,
        assigner: ProjectAssigner | None = None,
    ) -> None:
        self.source_instance = source_instance
        self.assigner = assigner or ProjectAssigner(
            {
                "__misc__": CanonicalProject(
                    id="__misc__",
                    name="Misc",
                    slug="misc",
                    visibility=ProjectVisibility.PRIVATE,
                    export_policy="local_only",
                    analysis_policy=AnalysisPolicy.LOCAL,
                    kind="misc",
                )
            }
        )

    def import_jsonl(self, path: Path, *, live: dict | None = None) -> CanonicalImport:
        session_id = path.stem
        source_name = self.source_instance or path.parent.name or "local"
        source = SessionAcc(
            session_id=session_id,
            path=str(path),
            project_dir=path.parent.name,
            source=source_name,
        )
        events: list[Event] = []
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_uuid = record.get("uuid")
                if record_uuid and record_uuid in source.seen_uuids:
                    continue
                source.feed(record)
                events.extend(self._events_for_record(record, session_id, len(events)))

        return self.from_acc(source, events, live=live)

    def from_acc(
        self, source: SessionAcc, events: list[Event] | None = None, *, live: dict | None = None
    ) -> CanonicalImport:
        source_instance = self.source_instance or f"claude-code-{source.source or 'local'}"
        session_id = canonical_session_id(
            source_instance=source_instance, native_id=source.session_id
        )
        status = source.state(live)[0]
        session = Session(
            id=session_id,
            native_id=source.session_id,
            provider="anthropic",
            product="claude-code",
            surface="local",
            source_instance=source_instance,
            title=source.title(),
            cwd=source.cwd,
            branch=source.git_branch,
            start=source.start,
            end=source.end,
            status=status,
            history_completeness="full",
            capabilities=SessionCapabilities(
                transcript=True,
                token_usage=True,
                cache_stats=True,
                tool_stats=True,
                git_commits=True,
                diff=False,
                resume=True,
                open_remote=False,
                live_status=True,
            ),
            visibility=SessionVisibility.PRIVATE,
        )
        assignment = self.assigner.assign(session)
        session.project_id = assignment.project_id
        canonical_events = events or []
        for index, event in enumerate(canonical_events):
            event.session_id = session.id
            event.id = f"{session.id}:event:{index}"
        return CanonicalImport(
            session,
            canonical_events,
            source,
            assignment,
            self.assigner.projects.get(assignment.project_id),
        )

    @staticmethod
    def _events_for_record(record: dict[str, Any], session_id: str, offset: int) -> list[Event]:
        ts = parse_ts(record.get("timestamp"))
        events: list[Event] = []

        def add(kind: str, **kwargs: Any) -> None:
            events.append(
                Event(
                    id=f"{session_id}:event:{offset + len(events)}",
                    session_id=session_id,
                    ts=ts,
                    kind=kind,
                    **kwargs,
                )
            )

        if record.get("sessionId") and record["sessionId"] != session_id:
            return events
        record_type = record.get("type")
        if record_type == "user":
            content = (record.get("message") or {}).get("content")
            kind = classify_user(record)
            if kind in ("prompt", "command"):
                text = content_text(content).strip()
                add(
                    "user_message",
                    role="user",
                    text=command_text(text) if kind == "command" else text,
                )
            elif kind == "interrupt":
                add("interrupt", role="user", text=content_text(content).strip())
            elif kind == "compact":
                add("compaction", metadata=compact_info(record))
            elif kind == "tool_result":
                blocks = content if isinstance(content, list) else []
                tool_blocks = [
                    b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if not tool_blocks:
                    add("tool_result", role="tool", text=tool_result_text(content))
                for block in tool_blocks:
                    add(
                        "tool_result",
                        role="tool",
                        text=tool_result_text(block.get("content")),
                        tool_call_id=block.get("tool_use_id"),
                        is_error=bool(block.get("is_error")),
                    )
        elif record_type == "assistant":
            message = record.get("message") or {}
            model = message.get("model")
            usage = _usage(message.get("usage"))
            if record.get("isApiErrorMessage") or model == "<synthetic>":
                add("api_error", metadata={"provider_type": "assistant"})
            else:
                blocks = message.get("content") or []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        add(
                            "assistant_message",
                            role="assistant",
                            text=block.get("text"),
                            model=model,
                            usage=usage,
                        )
                    elif block_type == "thinking":
                        add(
                            "thinking",
                            role="assistant",
                            text=block.get("thinking"),
                            model=model,
                            usage=usage,
                        )
                    elif block_type == "tool_use":
                        add(
                            "tool_call",
                            role="assistant",
                            model=model,
                            tool_name=block.get("name"),
                            tool_call_id=block.get("id"),
                            tool_input=block.get("input"),
                            usage=usage,
                        )
        elif record_type == "system" and record.get("subtype") == "compact_boundary":
            add("compaction", metadata=compact_info(record))
        elif record_type == "attachment":
            attachment = record.get("attachment")
            if isinstance(attachment, dict):
                metadata = {
                    key: attachment[key]
                    for key in ("id", "filename", "mime_type", "size_bytes")
                    if key in attachment
                }
                add("attachment", metadata=metadata)
        return events


def _usage(value: object) -> Usage | None:
    if not isinstance(value, dict):
        return None
    fields = {
        "input_tokens": value.get("input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "cache_read_tokens": value.get("cache_read_input_tokens"),
        "cache_write_tokens": value.get("cache_creation_input_tokens"),
    }
    if not any(isinstance(item, int) for item in fields.values()):
        return None
    return Usage(**{key: item if isinstance(item, int) else None for key, item in fields.items()})
