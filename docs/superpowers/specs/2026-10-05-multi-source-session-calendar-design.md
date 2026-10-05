# Multi-source Session Calendar Design

**Date:** 2026-10-05  
**Status:** Design specification  
**Tracking issue:** #1  
**Branch:** `design/multi-source-session-calendar`

## 1. Goal

Extend cc-calendar from a Claude Code-only local transcript viewer into a multi-source AI session calendar while preserving the existing strengths of cc-calendar: calendar visualization, transcript browsing, search, cost/token/cache analysis, tool statistics, event marks, continuation links, and session-state inspection.

The minimum supported sources are:

- Claude Code local
- Codex local
- Claude Chat
- ChatGPT
- Claude Code web/cloud sessions
- Codex Cloud sessions/tasks

The design must support multiple accounts for the same product, such as main/sub accounts.

The first implementation milestone is read-only history aggregation. Remote mutation, automatic project changes in providers, and cross-provider write-back are explicitly out of scope.

## 2. Design principles

1. **Provider-specific formats stop at the adapter boundary.**  
   Calendar, search, transcript, stats, and project views consume canonical data.

2. **Preserve existing Claude Code behavior during the refactor.**  
   The Phase 1 canonicalization should be behavior-preserving.

3. **Unknown is not zero.**  
   Missing token, cost, cache, effort, or tool information is represented as unavailable, not as zero.

4. **Remote/experimental sources are failure-isolated.**  
   A Claude Web or Codex Cloud schema/API failure must not break local Claude Code, local Codex, or chat history already stored locally.

5. **Privacy defaults are conservative.**  
   New canonical projects default to private. Export defaults to local-only. Project visibility never implies transcript publishability.

6. **The application remains local-first.**  
   Network-backed sources are optional and opt-in. Chat collector ingestion should prefer a locally synced directory over direct API access.

## 3. Current cc-calendar capabilities to preserve

The upstream project already implements most of the originally requested analytics features:

- cache hit rate and cache savings
- prompt / commit / compaction / API error marks
- Running / Waiting / Done / Interrupted state handling
- desktop notifications for waiting/interrupted sessions
- daily/weekly/monthly/year views
- active time and cost
- model / effort / cost / source color dimensions
- tool, MCP, and subagent statistics
- Markdown reports
- transcript viewer
- per-model token/cost statistics
- tool error rate
- resume command
- continuation links
- CSV / JSON export
- full-text search
- multiple Claude config directories

The fork should reuse these features through canonical models rather than reimplementing them per provider.

## 4. Target architecture

```text
Claude Code local ───────┐
Codex local ─────────────┤
Claude Code web ─────────┤
Codex Cloud ─────────────┤
Claude Chat collector ───┤
ChatGPT collector ───────┤
Claude export ───────────┤
ChatGPT export ──────────┘
            │
            ▼
       Source Adapters
            │
            ▼
      Canonical Event
            │
            ▼
     Canonical Session
            │
     ┌──────┼────────┐
     ▼      ▼        ▼
 Calendar Search  Transcript
            │
            ▼
          Stats
            │
            ▼
     Project / Work views
```

Provider-specific parsing must not leak into UI code.

## 5. Canonical models

### 5.1 Session

```python
@dataclass
class Session:
    id: str
    native_id: str

    provider: str
    # anthropic / openai

    product: str
    # claude-code / codex / claude-chat / chatgpt

    surface: str
    # local / web / cloud / collector / export

    source_instance: str
    # e.g. claude-main, claude-sub, codex-main

    account: str | None

    title: str | None
    project_id: str

    cwd: str | None
    repo: str | None
    branch: str | None

    start: int | None
    end: int | None

    status: str
    history_completeness: str
    capabilities: SessionCapabilities

    visibility: str
    external_project_ref: ExternalProjectRef | None
```

Global session IDs should not depend exclusively on provider-native IDs. For collector-backed chat sessions, use the stable `collector_conversation_id`; for sources with guaranteed native IDs, a namespaced native ID may be used.

```text
identity_key =
    collector_conversation_id
    OR namespaced_native_id

global_session_id = SHA256(source_instance + ":" + identity_key)
```

This allows a chat session to remain stable when the collector initially lacks a native conversation ID and learns it later.

### 5.2 History completeness

Supported values:

- `full` — messages + tool/usage data sufficient for full analysis
- `messages` — conversation message history
- `activity` — timestamps/title/project/activity metadata
- `summary` — activity metadata plus generated summary/decisions/open items
- `metadata_diff` — cloud task/status/diff metadata only

Examples:

| Source | Typical completeness |
|---|---|
| Claude Code local | full |
| Codex local | full |
| Claude Chat scheduled collector | activity / summary |
| ChatGPT scheduled collector | activity / summary |
| Claude export | messages |
| ChatGPT export | messages |
| Claude Code web | full / messages |
| Codex Cloud | metadata_diff |

### 5.3 Event

```python
@dataclass
class Event:
    id: str
    session_id: str
    ts: int | None

    kind: str
    role: str | None
    text: str | None

    model: str | None
    effort: str | None

    tool_name: str | None
    tool_call_id: str | None
    tool_input: object | None
    tool_output: str | None
    is_error: bool | None

    usage: Usage | None
    metadata: dict
```

Minimum event kinds:

- `user_message`
- `assistant_message`
- `tool_call`
- `tool_result`
- `thinking`
- `commit`
- `compaction`
- `api_error`
- `interrupt`
- `status`
- `attachment`
- `system`

Provider-specific raw type names remain available only in event metadata/debug output.

### 5.4 Capabilities

```python
@dataclass
class SessionCapabilities:
    transcript: bool
    token_usage: bool
    cache_stats: bool
    tool_stats: bool
    git_commits: bool
    diff: bool
    resume: bool
    open_remote: bool
    live_status: bool
```

UI logic should render based on capabilities rather than `if provider == ...` branches.

## 6. Source adapter interface

```python
class SessionSource(Protocol):
    def discover(self, cursor: SyncCursor | None) -> DiscoveryResult:
        ...

    def load_session(
        self,
        ref: SessionRef,
        cursor: EventCursor | None,
    ) -> SessionDelta:
        ...

    def stream_transcript(
        self,
        ref: SessionRef,
    ) -> Iterable[Event]:
        ...

    def capabilities(self) -> SourceCapabilities:
        ...
```

Proposed package structure:

```text
src/cc_calendar/
    canonical/
        event.py
        session.py
        usage.py
        project.py
        normalize.py

    sources/
        base.py
        claude_code_local.py
        codex_local.py
        claude_code_web.py
        codex_cloud.py
        chat_activity.py
        claude_export.py
        chatgpt_export.py

    collector_sinks/
        base.py
        drive.py
        github.py
        local.py

    sync/
        manager.py
        cursor.py
        cache.py
        dedupe.py
```

## 7. Claude Code local

The current Claude JSONL parser becomes the first adapter.

Input:

```text
~/.claude/projects/**/*.jsonl
```

Existing behavior to preserve includes:

- prompts
- assistant messages
- thinking
- tool calls/results
- token usage
- cache read/write
- model
- effort
- compaction
- API errors
- subagents
- background tasks
- commits
- continuation
- active time
- live process state

This adapter is the compatibility reference for the canonical layer.

## 8. Codex local

Primary input:

```text
$CODEX_HOME/sessions/
$CODEX_HOME/archived_sessions/
$CODEX_HOME/session_index.jsonl
```

Codex rollout JSONL should be parsed incrementally using byte offsets/inode/size state, as cc-calendar already does for Claude Code.

Large tool outputs and duplicate representations require defensive deduplication. The adapter should normalize tool calls/results, model/effort, usage, prompt/reply text, project/repo metadata, errors, and active time where available.

A complete reparse of all Codex rollout files on each scan is prohibited.

## 9. Chat history: Claude Chat and ChatGPT

### 9.1 Primary path: scheduled collectors

Claude Chat and ChatGPT are treated symmetrically.

Both products can use their own scheduled/background chat capabilities to inspect conversation history and emit machine-readable activity records.

The scheduled collectors are the primary incremental activity source. Official exports are fallback/backfill sources.

### 9.2 Primary sink: one shared central Google Drive folder

The first-choice deployment is a **single shared central sink** owned by the personal Google account.

1. Create `AI Session History` in the personal Google Drive.
2. Share that folder with the work Google account with **Editor** permission.
3. Personal and work Claude Chat / ChatGPT collectors write into the same logical sink.
4. cc-calendar ingests the shared folder **read-only** and never changes collector files or Drive sharing.

The sink remains private by default. The work account is an explicitly granted collaborator; the folder must not be made public or link-accessible merely because a canonical project is public.

Recommended logical layout:

```text
AI Session History/
├── claude-personal/
│   └── 2026/10/05.jsonl
├── claude-work/
│   └── 2026/10/05.jsonl
├── chatgpt-personal/
│   └── 2026/10/05.jsonl
└── chatgpt-work/
    └── 2026/10/05.jsonl
```

A physical subfolder per source is preferred for operational clarity, but the canonical identity is `source_instance`. At minimum the following source instances must remain distinguishable:

- `claude-personal`
- `claude-work`
- `chatgpt-personal`
- `chatgpt-work`

Collectors should write daily or monthly shards rather than one unbounded append-only file.

### 9.3 Collector sink abstraction

Collectors must not depend directly on Google Drive semantics. They write through a sink interface so the storage backend can be replaced later without changing collector logic.

```python
class CollectorSink(Protocol):
    def put_shard(
        self,
        source_instance: str,
        shard_key: str,
        records: Iterable[dict],
    ) -> None:
        ...

    def list_shards(
        self,
        source_instance: str,
        cursor: str | None = None,
    ) -> Iterable[SinkObject]:
        ...
```

Planned sink implementations:

- `DriveSink` — first-choice central sink
- `LocalSink` — local filesystem / sync-folder testing and offline use
- `GitHubSink` — future optional backend

The sink abstraction covers collector persistence only. Canonical Session/Event normalization remains downstream in cc-calendar.

### 9.4 cc-calendar ingestion path

Preferred path:

```text
claude-personal ─┐
claude-work ─────┤
chatgpt-personal ├─→ CollectorSink (Drive)
chatgpt-work ────┘          │
                            ▼
              AI Session History
              (personal Drive owner,
               work account = editor)
                            │
                            ▼
              read-only cc-calendar ingest
```

For ordinary desktop use, cc-calendar should prefer a locally synced representation of the shared Drive folder so the application stays local-first and does not require Drive API access on every read.

A direct read-only Google Drive source may be added later as an optional ingestion path.

### 9.5 Workspace sharing fallback

The single central sink is the preferred topology.

Use separate personal/work Drive sinks **only when the work Google Workspace policy prevents external sharing with the personal Google account**.

Fallback:

```text
Personal collectors ─→ Personal Drive / AI Session History
Work collectors ─────→ Work Drive / AI Session History
                              │
                              ▼
                    cc-calendar merges both
                    as separate source instances
```

The fallback must not change canonical session IDs, project logic, or source-instance semantics.

### 9.6 Attachment policy

Collectors do **not** persist attachment bodies by default.

Images, ZIP archives, documents, and other attachment payloads are represented only by metadata when available:

```json
{
  "attachment_id": "att_...",
  "filename": "example.zip",
  "mime_type": "application/zip",
  "size_bytes": 123456
}
```

The exact metadata may vary by provider, but attachment binary content is excluded from the central sink unless a future explicit opt-in policy is designed.

### 9.7 Collector conversation identity

Provider-native conversation IDs are useful but are not required. Some chat-history surfaces may expose timestamps, titles, or stable references without exposing the native conversation ID.

Therefore:

- `conversation_id` is nullable and means only the provider-native ID.
- `collector_conversation_id` is required and is the stable identity used by collector ingestion and deduplication.
- Once assigned, `collector_conversation_id` must not change merely because a better native identifier becomes available later.
- Native identifiers discovered later are attached as additional identity evidence.

Identity resolution priority:

1. provider-native `conversation_id`
2. stable ID embedded in the provider conversation URL
3. stable conversation/thread reference exposed by the history system
4. deterministic hash of `created_at + first_message_at + normalized_title`, namespaced by provider and `source_instance`
5. persistent collector UUID mapped from the best available fingerprint

For deterministic derived IDs, use a namespaced canonical input such as:

```text
sha256(
  provider + "\n" +
  source_instance + "\n" +
  created_at + "\n" +
  first_message_at + "\n" +
  normalized_title
)
```

Title alone must never be used as a conversation identity because repeated titles are common.

If the collector must use fallback 5, it must persist a `fingerprint -> UUID` mapping in collector state. It must not generate a new UUID on every run.

Example:

```json
{
  "conversation_id": null,
  "collector_conversation_id": "chatgpt-personal:sha256:abc123...",
  "collector_id_method": "created_at+first_message_at+title",
  "collector_id_inputs": [
    "created_at",
    "first_message_at",
    "title"
  ]
}
```

A missing native ID must never stop collection.

### 9.8 Collector record schema

```json
{
  "provider": "openai",
  "product": "chatgpt",
  "account": "main",

  "conversation_id": null,
  "collector_conversation_id": "chatgpt-personal:sha256:abc123...",
  "collector_id_method": "created_at+first_message_at+title",
  "title": "Quad dominant mesh research",

  "project": {
    "id": "native-project-id",
    "name": "3D Research",
    "visibility": "private"
  },

  "created_at": "2026-10-05T11:02:00+09:00",
  "last_activity_at": "2026-10-05T11:38:12+09:00",
  "message_count": 14,

  "summary": "...",
  "decisions": [],
  "open_items": [],
  "waiting_for": [],
  "completed": false,

  "source": "scheduled-collector"
}
```

If native project information is unavailable, `project` is `null`. The collector must not invent native project IDs.

### 9.9 Export backfill

Claude and ChatGPT official exports are used for:

- initial historical backfill
- periods missed by scheduled collectors
- full message history when collector output only contains activity/summary data

Repeated imports must be idempotent.

## 10. Claude Code web

Claude Code web support is isolated behind an experimental adapter.

If full session/event history can be obtained, normalize it into canonical events. If the API/schema changes, only this source becomes degraded.

Fallbacks such as teleporting sessions back into local Claude Code should be treated as local sessions once they appear in the local filesystem.

## 11. Codex Cloud

MVP support uses the public Codex CLI surface where available, including task listing/status/diff.

Codex Cloud sessions/tasks should use:

```text
provider = openai
product = codex
surface = cloud
history_completeness = metadata_diff
```

Do not pretend that metadata+diff is a full transcript.

If a stable transcript API becomes available later, only the adapter should need to change.

## 12. Canonical projects

Project is a first-class entity, not a string field.

```python
@dataclass
class Project:
    id: str
    name: str
    slug: str

    visibility: str
    # private / shared / public / mixed / unknown

    kind: str
    # canonical / misc / external-only

    archived: bool = False
    description: str | None = None
```

### 12.1 External projects

Provider-native project data is represented separately:

```python
@dataclass
class ExternalProjectRef:
    provider: str
    product: str
    source_instance: str

    native_project_id: str | None
    native_project_name: str | None
    native_visibility: str | None
```

External projects map to canonical projects.

This supports many-to-many relationships such as:

```text
Canonical: avatar-reconstruction
├── ChatGPT Project: 4-view reconstruction
├── Claude Project: Character Reconstruction
├── GitHub repo: foo/avatar-reconstruction
└── cwd: ~/work/avatar-reconstruction
```

## 13. Project assignment

Assignment priority:

1. explicit provider-native project ID mapping
2. Git repository mapping
3. cwd/path mapping
4. provider/account/project-name mapping when IDs are unavailable
5. optional classifier suggestion
6. Misc

Assignment provenance must be stored:

- `native`
- `repo`
- `path`
- `manual`
- `rule`
- `classifier`
- `misc`

Classifier assignments also store confidence.

Provider-native project names alone must not trigger cross-provider merges.

## 14. Misc

Every session belongs to a canonical project.

Unclassified sessions go to:

```python
Project(
    id="__misc__",
    name="Misc",
    kind="misc",
    visibility="private",
)
```

`Misc` means "currently unclassified", not "unimportant".

Optional high-level categories such as Personal, Research, Work, Finance, and Shopping should be session categories, not project hierarchy.

Moving a session from Misc to a real project changes only the canonical assignment, not provider-native data.

## 15. Visibility and data policy

Project visibility and session visibility are separate.

Canonical project visibility values:

- `private`
- `shared`
- `public`
- `mixed`
- `unknown`

A public project may contain private sessions.

Example:

```text
Canonical project: public
GitHub repo: public
ChatGPT research conversation: private
Claude Code session: private
```

This is valid and expected.

### 15.1 Visibility inheritance

Session canonical visibility resolves in this order:

1. native session visibility
2. native project visibility
3. canonical project default
4. unknown

Never promote a private/unknown conversation to public because its canonical project is public.

### 15.2 Export policy

Each canonical project has an independent export policy:

- `local_only`
- `metadata_only`
- `summary_only`
- `full`

Default: `local_only`.

### 15.3 Analysis policy

Each project also has an independent analysis policy:

- `disabled`
- `local`
- `remote_allowed`

Default: `local`.

Visibility, export policy, and analysis policy are three separate axes.

### 15.4 Google Drive collector privacy

Collector files stored in Google Drive are private by default.

In the preferred central-sink topology, the personal Google account owns `AI Session History` and the work Google account receives explicit Editor access. This collaborator permission is an ingestion topology choice, not a project-visibility signal.

A provider-native or canonical project being public must not automatically change Google Drive sharing permissions. Drive sharing remains independent from:

- canonical project visibility
- session visibility
- export policy
- analysis policy

Provider visibility is recorded as metadata only. cc-calendar has read-only semantics for the collector sink and must not change Drive ACLs.

## 16. Project persistence

State storage should include:

```text
projects
project_aliases
external_projects
project_mappings
project_assignments
```

Suggested fields:

```text
projects
--------
id
name
visibility
export_policy
analysis_policy
archived

external_projects
-----------------
source_instance
native_project_id
name
native_visibility

project_mappings
----------------
external_project
canonical_project

project_assignments
-------------------
session_id
project_id
source
confidence
updated_at
```

## 17. Search, transcript, and stats refactor

Current search/log/stats code understands Claude raw records directly. That dependency must be removed.

New flow:

```text
raw provider record
      │
      ▼
   adapter
      │
      ▼
Canonical Event
      │
      ├── SearchDocument
      ├── Transcript entry
      └── Stats aggregation
```

This is the most important structural refactor.

Provider-specific formats must not be parsed independently in:

- `search.py`
- `logview.py`
- `stats.py`
- frontend rendering code

## 18. Sync and source health

Local sources use filesystem notifications and incremental offsets.

Remote sources use polling with persistent cursors.

Proposed health states:

- `healthy`
- `degraded`
- `auth_required`
- `schema_changed`
- `disabled`

Adapter failures are contained to the source.

Schema drift should be detectable by storing lightweight payload fingerprints/top-level key information for unstable remote sources.

## 19. Storage

Local Claude/Codex transcript files remain the source of truth and are not copied wholesale.

Proposed additional storage:

```text
~/.local/share/cc-calendar/state.db
~/.cache/cc-calendar/search.db
~/.cache/cc-calendar/remote/
```

`state.db` stores:

- source configuration state
- sync cursors
- canonical projects
- external project mappings
- assignment provenance
- remote schema fingerprints
- export hashes
- source health metadata

The search database remains an incremental derived index.

## 20. Credential handling

Credentials must not be stored in cc-calendar configuration.

Codex Cloud should prefer Codex CLI authentication.

Claude Code web uses a credential-provider abstraction if remote access is enabled.

Raw access tokens must not be returned to the frontend or written to logs.

## 21. Network policy

Upstream cc-calendar currently promises no network requests.

The fork changes this only for explicitly enabled sources.

Default behavior:

```text
network-backed sources = disabled
```

When enabled, the UI should make network-source status visible.

Local Claude Code, local Codex, and locally synced chat collector files must continue working with all remote adapters disabled.

## 22. Quota monitoring

Quota/remaining-capacity display is separate from session ingestion.

```python
class QuotaProvider(Protocol):
    def usage_limits(self) -> list[Quota]:
        ...
```

Do not mix account quota logic into transcript adapters.

## 23. Work-management analysis

Summary, decisions, open issues, completion judgement, and waiting state are downstream analysis, not provider parsing.

```text
Canonical Session
      │
      ▼
Analysis Pipeline
      │
      ├── summary
      ├── decisions
      ├── open items
      ├── waiting_for
      └── completed
```

Chat collectors may provide these fields opportunistically, but the canonical core must not require them.

## 24. UI additions

Add filters for:

- Provider
- Product
- Surface
- Account
- Source
- Canonical Project
- History completeness
- Visibility

Add project/source badges to session cards.

Project pages aggregate activity across providers.

Misc gets a dedicated view for unclassified sessions and project suggestions.

Cloud/session cards must visibly distinguish full transcript data from partial metadata/diff sources.

## 25. Implementation sequence

### Phase 1 — Canonical compatibility layer

- introduce canonical Session/Event/Usage/Capabilities
- move Claude Code parsing behind `ClaudeCodeLocalAdapter`
- preserve current behavior exactly
- keep existing tests green

### Phase 2 — Canonical consumers

Refactor:

- transcript viewer
- full-text search
- stats
- calendar summaries

to consume canonical events/session data.

### Phase 3 — Codex local

Add incremental Codex local adapter.

### Phase 4 — Chat activity ingestion

Add:

- shared `chat_activity.py` adapter for Claude Chat and ChatGPT collector JSONL
- `CollectorSink` abstraction
- `DriveSink` as the first-choice sink
- `LocalSink` for local/offline development and testing
- source instances for `claude-personal`, `claude-work`, `chatgpt-personal`, and `chatgpt-work`
- read-only ingestion of the shared central sink
- separate personal/work Drive fallback only when Workspace external-sharing policy requires it

### Phase 5 — Export backfill

Add Claude and ChatGPT official export importers.

### Phase 6 — Project/Misc/visibility

Add canonical projects, mappings, assignment provenance, Misc, visibility, export policy, and analysis policy.

### Phase 7 — Claude Code web

Add isolated experimental web/cloud adapter.

### Phase 8 — Codex Cloud

Add task/status/diff adapter.

## 26. PR decomposition

Recommended sequence:

1. Canonical models + Claude Code compatibility adapter
2. Refactor search/stats/transcript onto canonical events
3. Codex local source
4. Chat activity source
5. Chat export backfills
6. Project mapping / Misc / visibility
7. Claude Code web source
8. Codex Cloud source

All implementation PRs should reference #1.

## 27. Testing

Never commit real transcripts.

Synthetic fixtures:

```text
tests/fixtures/
    claude_code/
    codex/
    chat_activity/
    claude_export/
    chatgpt_export/
    claude_web/
    codex_cloud/
```

Required cases include:

- shared central sink with four source instances
- Workspace external-sharing fallback with separate personal/work sinks
- read-only sink ingestion
- attachment metadata without attachment bodies
- sink backend substitution without changing canonical events
- malformed/truncated JSONL
- file append
- file truncate/rewrite
- inode replacement
- unknown fields
- missing fields
- duplicate events
- huge tool outputs
- repeated export
- missing native conversation ID
- URL/thread-ref identity fallback
- deterministic derived conversation ID
- duplicate titles with different timestamps
- persistent fingerprint-to-UUID fallback
- later discovery of native ID without changing canonical identity
- remote pagination
- network/auth failures
- remote schema change
- unavailable metrics
- project reassignment
- mixed visibility projects

## 28. Performance requirements

The architecture should assume:

- 10,000+ sessions
- 1,000 recent/active sessions
- tens of GB of transcript data

Startup must not reparse all transcripts.

Required mechanisms:

- incremental byte offsets
- persistent cursors
- lazy transcript parsing
- incremental FTS
- remote cache
- event deduplication

## 29. MVP acceptance criteria

- [ ] Existing Claude Code behavior remains equivalent to upstream after canonicalization
- [ ] Codex local sessions appear in the same calendar
- [ ] Claude Chat scheduled activity appears in calendar/search
- [ ] ChatGPT scheduled activity appears in calendar/search
- [ ] personal/work Claude and ChatGPT collectors can write to one personal-Drive-owned shared central sink
- [ ] `claude-personal`, `claude-work`, `chatgpt-personal`, and `chatgpt-work` remain distinguishable
- [ ] chat collection continues when provider-native conversation IDs are unavailable
- [ ] every collector record has a stable `collector_conversation_id`
- [ ] title-only identity generation is prohibited
- [ ] fallback UUID identities persist fingerprint-to-UUID mappings across runs
- [ ] discovering a native conversation ID later does not change the canonical session identity
- [ ] cc-calendar treats collector sink data as read-only
- [ ] attachment bodies are omitted by default while attachment metadata is retained
- [ ] separate personal/work Drive sinks work as a fallback when Workspace external sharing is prohibited
- [ ] collector storage can be swapped through the sink abstraction without changing canonical session/event consumers
- [ ] Claude and ChatGPT export backfills are idempotent
- [ ] Claude Code web sessions can be represented without breaking local sources
- [ ] Codex Cloud tasks/status/diff appear with partial-history labeling
- [ ] Provider/Product/Account/Project filters work
- [ ] main/sub accounts can be shown simultaneously
- [ ] unclassified sessions land in Misc
- [ ] public project visibility never auto-publishes private sessions
- [ ] unknown metrics are not displayed as zero
- [ ] source failures do not crash the application
- [ ] full-text search spans all supported sources
- [ ] credentials are not persisted in plaintext
- [ ] collector Google Drive data remains non-public by default; only explicitly configured collaborators such as the work account receive access
- [ ] project/session visibility never mutates Drive sharing permissions

## 30. Non-goals for the first implementation

- automatic provider-side project movement/renaming
- private web API scraping for ChatGPT
- fuzzy cross-provider deduplication
- automatic browser login
- remote write operations
- mandatory LLM summaries
- mandatory quota integration
- publishing transcripts from public projects
- changing Google Drive sharing permissions

## 31. Key architectural invariant

The central invariant is:

> Every source normalizes into Canonical Session/Event before calendar, search, transcript, stats, or project logic sees it.

Breaking this invariant by teaching each consumer about Claude/Codex/ChatGPT/Claude Chat formats would recreate provider-specific coupling and make the fork difficult to maintain.

This specification tracks GitHub Issue #1.
