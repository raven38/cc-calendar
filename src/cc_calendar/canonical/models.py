"""Canonical data structures shared by all source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SessionVisibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"
    UNKNOWN = "unknown"


class ProjectVisibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ExportPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    METADATA_ONLY = "metadata_only"
    SUMMARY_ONLY = "summary_only"
    FULL = "full"


class AnalysisPolicy(StrEnum):
    DISABLED = "disabled"
    LOCAL = "local"
    REMOTE_ALLOWED = "remote_allowed"


@dataclass(frozen=True)
class SessionCapabilities:
    transcript: bool = False
    token_usage: bool = False
    cache_stats: bool = False
    tool_stats: bool = False
    git_commits: bool = False
    diff: bool = False
    resume: bool = False
    open_remote: bool = False
    live_status: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost: float | None = None


@dataclass
class Event:
    id: str
    session_id: str
    ts: int | None = None
    kind: str = "system"
    role: str | None = None
    text: str | None = None
    model: str | None = None
    effort: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_input: Any = None
    tool_output: str | None = None
    is_error: bool | None = None
    usage: Usage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    id: str
    native_id: str | None = None
    provider: str = ""
    product: str = ""
    surface: str = ""
    source_instance: str = ""
    account: str | None = None
    title: str | None = None
    project_id: str = "__misc__"
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None
    start: int | None = None
    end: int | None = None
    status: str = "unknown"
    history_completeness: str = "activity"
    capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)
    visibility: SessionVisibility = SessionVisibility.UNKNOWN
    external_project_ref: Any | None = None
    collector_conversation_id: str | None = None
    collector_id_method: str | None = None
    identity_metadata: dict[str, Any] = field(default_factory=dict)
    sync_state: str = "unknown"
    backfill_status: str = "unknown"
    backfill_complete: bool | None = None
    completed: bool | None = None


@dataclass
class CanonicalProject:
    id: str
    name: str
    slug: str
    visibility: ProjectVisibility = ProjectVisibility.PRIVATE
    kind: str = "canonical"
    export_policy: ExportPolicy = ExportPolicy.LOCAL_ONLY
    analysis_policy: AnalysisPolicy = AnalysisPolicy.LOCAL
    archived: bool = False
    description: str | None = None


@dataclass(frozen=True)
class ProjectAssignment:
    project_id: str
    provenance: str
    confidence: float | None = None
