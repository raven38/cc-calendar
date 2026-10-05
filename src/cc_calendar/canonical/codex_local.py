"""Defensive, read-only adapter for Codex CLI rollout JSONL files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..parser import parse_ts
from .claude_code import CanonicalImport
from .models import (
    AnalysisPolicy,
    CanonicalProject,
    Event,
    ProjectVisibility,
    Session,
    SessionCapabilities,
    SessionVisibility,
    Usage,
)
from .normalize import canonical_session_id
from .projects import ProjectAssigner


@dataclass
class CodexAccumulator:
    session_id: str
    path: str
    cwd: str | None = None
    branch: str | None = None
    title: str | None = None
    model: str | None = None
    effort: str | None = None
    start: int | None = None
    end: int | None = None
    status: str = "unknown"
    usage: Usage | None = None
    cache_seen: bool = False
    message_seen: bool = False
    tool_seen: bool = False
    seen_ids: set[str] = field(default_factory=set)
    message_keys: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)

    def feed(self, record: dict[str, Any], ordinal: int) -> None:
        record_id = record.get("id")
        if isinstance(record_id, str):
            if record_id in self.seen_ids:
                return
            self.seen_ids.add(record_id)

        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        timestamp = parse_ts(record.get("timestamp") or payload.get("timestamp"))
        if timestamp is not None:
            self.start = timestamp if self.start is None else min(self.start, timestamp)
            self.end = timestamp if self.end is None else max(self.end, timestamp)

        record_type = record.get("type")
        if record_type == "session_meta":
            self._feed_session_meta(payload)
        elif record_type == "turn_context":
            self._feed_context(payload)
        elif record_type == "response_item":
            self._feed_response_item(payload, timestamp, ordinal, record_id)
        elif record_type == "event_msg":
            self._feed_event_message(payload, timestamp, ordinal)

    def tokens(self) -> int | None:
        if self.usage is None:
            return None
        fields = (
            self.usage.input_tokens,
            self.usage.output_tokens,
            self.usage.cache_read_tokens,
            self.usage.cache_write_tokens,
        )
        return sum(value for value in fields if value is not None)

    def _feed_session_meta(self, payload: dict[str, Any]) -> None:
        self.cwd = _first_string(payload, "cwd", "working_directory") or self.cwd
        self.branch = _first_string(payload, "git_branch", "branch") or self.branch
        self.model = _first_string(payload, "model") or self.model
        self.title = _first_string(payload, "title", "session_title") or self.title

    def _feed_context(self, payload: dict[str, Any]) -> None:
        self.cwd = _first_string(payload, "cwd", "working_directory") or self.cwd
        self.branch = _first_string(payload, "git_branch", "branch") or self.branch
        self.model = _first_string(payload, "model") or self.model
        self.effort = _first_string(payload, "effort", "reasoning_effort") or self.effort

    def _feed_response_item(
        self,
        payload: dict[str, Any],
        ts: int | None,
        ordinal: int,
        record_id: str | None,
    ) -> None:
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role") if isinstance(payload.get("role"), str) else None
            text = _content_text(payload.get("content"))
            if role in ("user", "assistant") and text:
                message_key = _message_key(
                    role=role,
                    text=text,
                    timestamp=ts,
                    message_id=_first_string(payload, "message_id", "id") or record_id,
                )
                if message_key in self.message_keys:
                    return
                self.message_keys.add(message_key)
                self.message_seen = True
                if role == "user" and not self.title:
                    self.title = text.splitlines()[0][:120]
                self._add(
                    "user_message" if role == "user" else "assistant_message",
                    ts,
                    ordinal,
                    role=role,
                    text=text,
                    model=_first_string(payload, "model") or self.model,
                )
        elif kind == "function_call":
            self.tool_seen = True
            self._add(
                "tool_call",
                ts,
                ordinal,
                role="assistant",
                tool_name=_first_string(payload, "name") or "unknown",
                tool_call_id=_first_string(payload, "call_id", "id"),
                tool_input=_json_or_text(payload.get("arguments")),
                model=_first_string(payload, "model") or self.model,
            )
        elif kind == "function_call_output":
            self.tool_seen = True
            output = payload.get("output")
            self._add(
                "tool_result",
                ts,
                ordinal,
                role="tool",
                tool_call_id=_first_string(payload, "call_id", "id"),
                tool_output=output if isinstance(output, str) else _json_text(output),
                is_error=bool(payload.get("is_error")) if "is_error" in payload else None,
            )

    def _feed_event_message(self, payload: dict[str, Any], ts: int | None, ordinal: int) -> None:
        kind = payload.get("type")
        if kind in ("agent_message", "message"):
            role = _first_string(payload, "role") or "assistant"
            if role not in ("user", "assistant"):
                return
            text = _first_string(payload, "text", "message") or _content_text(
                payload.get("content")
            )
            if not text:
                return
            message_key = _message_key(
                role=role,
                text=text,
                timestamp=ts,
                message_id=_first_string(payload, "message_id", "response_item_id", "id"),
            )
            if message_key in self.message_keys:
                return
            self.message_keys.add(message_key)
            self.message_seen = True
            if role == "user" and not self.title:
                self.title = text.splitlines()[0][:120]
            self._add(
                "user_message" if role == "user" else "assistant_message",
                ts,
                ordinal,
                role=role,
                text=text,
                model=self.model,
            )
        elif kind == "token_count":
            usage = _usage(payload.get("info"))
            if usage is not None:
                self.usage = usage
                self.cache_seen |= usage.cache_read_tokens is not None
            self._add(
                "status",
                ts,
                ordinal,
                model=self.model,
                effort=self.effort,
                usage=usage,
                metadata={"provider_type": kind},
            )
        elif kind in ("task_started", "turn_started"):
            self.status = "running"
            self._add("status", ts, ordinal, metadata={"status": "running", "provider_type": kind})
        elif kind in ("task_complete", "turn_complete"):
            self.status = "done"
            self._add("status", ts, ordinal, metadata={"status": "done", "provider_type": kind})
        elif kind in ("turn_aborted", "error"):
            self.status = "interrupted" if kind == "turn_aborted" else "unknown"
            self._add(
                "api_error" if kind == "error" else "interrupt",
                ts,
                ordinal,
                metadata={"provider_type": kind},
            )

    def _add(self, kind: str, ts: int | None, ordinal: int, **kwargs: Any) -> None:
        self.events.append(
            Event(
                id=f"raw:{ordinal}:{len(self.events)}",
                session_id=self.session_id,
                ts=ts,
                kind=kind,
                **kwargs,
            )
        )


class CodexLocalAdapter:
    """Normalize one Codex rollout file; unknown records are ignored safely."""

    def __init__(
        self,
        *,
        source_instance: str = "codex-local",
        assigner: ProjectAssigner | None = None,
    ) -> None:
        self.source_instance = source_instance or "codex-local"
        self.assigner = assigner or ProjectAssigner(
            {
                "__misc__": CanonicalProject(
                    id="__misc__",
                    name="Misc",
                    slug="misc",
                    visibility=ProjectVisibility.PRIVATE,
                    kind="misc",
                    export_policy="local_only",
                    analysis_policy=AnalysisPolicy.LOCAL,
                )
            }
        )

    def import_jsonl(self, path: Path, *, live: dict | None = None) -> CanonicalImport | None:
        native_id = path.stem
        source = CodexAccumulator(native_id, str(path))
        valid_records = 0
        with path.open("rb") as stream:
            for ordinal, raw in enumerate(stream):
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict):
                    valid_records += 1
                    meta = record.get("payload")
                    if isinstance(meta, dict) and isinstance(meta.get("id"), str):
                        native_id = meta["id"]
                        source.session_id = native_id
                    source.feed(record, ordinal)

        # A trailing partial line can leave a file with no complete JSON object.
        # Do not expose a path-only phantom session until a complete record exists.
        if valid_records == 0:
            return None

        session_id = canonical_session_id(
            source_instance=self.source_instance, native_id=source.session_id
        )
        events = source.events
        for index, event in enumerate(events):
            event.session_id = session_id
            event.id = f"{session_id}:event:{index}"
        status = source.status
        session = Session(
            id=session_id,
            native_id=source.session_id,
            provider="openai",
            product="codex",
            surface="local",
            source_instance=self.source_instance,
            title=source.title or "(untitled session)",
            project_id="__misc__",
            cwd=source.cwd,
            branch=source.branch,
            start=source.start,
            end=source.end,
            status=status,
            history_completeness="full" if source.message_seen else "activity",
            capabilities=SessionCapabilities(
                transcript=source.message_seen,
                token_usage=source.usage is not None,
                cache_stats=source.cache_seen,
                tool_stats=source.tool_seen,
                git_commits=False,
                diff=False,
                resume=True,
                open_remote=False,
                live_status=False,
            ),
            visibility=SessionVisibility.PRIVATE,
        )
        assignment = self.assigner.assign(session)
        session.project_id = assignment.project_id
        return CanonicalImport(
            session=session,
            events=events,
            source=source,
            assignment=assignment,
            project=self.assigner.projects.get(assignment.project_id),
        )


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _message_key(*, role: str, text: str, timestamp: int | None, message_id: str | None) -> str:
    if message_id:
        return f"id:{message_id}"
    return f"text:{role}:{timestamp}:{text}"


def _json_or_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _usage(info: object) -> Usage | None:
    if not isinstance(info, dict):
        return None
    raw = info.get("total_token_usage")
    if not isinstance(raw, dict):
        return None
    values = {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "cache_read_tokens": raw.get("cached_input_tokens", raw.get("cache_read_input_tokens")),
        "cache_write_tokens": raw.get("cache_creation_input_tokens"),
    }
    if not any(isinstance(value, int) for value in values.values()):
        return None
    return Usage(
        **{key: value if isinstance(value, int) else None for key, value in values.items()}
    )
