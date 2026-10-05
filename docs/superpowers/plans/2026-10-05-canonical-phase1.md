# Canonical Phase 1 continuation

## Current slice

The first vertical slice keeps Claude Code's existing `SessionAcc` as the
analytics compatibility reference and adds a read-only canonical projection:

```text
Claude Code JSONL
  -> SessionAcc (existing incremental parser)
  -> ClaudeCodeAdapter
  -> canonical Session + Event + ProjectAssignment
  -> /api/sessions/{id}/canonical
```

An opt-in Codex local path now follows the same boundary:

```text
--codex-dir [NAME=]PATH
  -> CodexLocalAdapter (rollout JSONL)
  -> canonical Session + Event + ProjectAssignment
  -> combined /api/sessions list and source selector
```

The projection is local-only. It does not copy logs, write provider data, make
network requests, or infer public visibility. Sessions without a mapping use
the private `__misc__` project. Project visibility, export policy, and analysis
policy remain independent fields.

Collector records must carry a stable `collector_conversation_id`. When a
provider-native ID is unavailable, the collector supplies a persistent fallback
UUID/key; a title/timestamp fingerprint alone is never used as the canonical
identity. Later native IDs do not replace the canonical identity.

## Verification procedure

When the normal development environment is available:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

In the restricted checkout used for this slice, the dependency-backed commands
could not resolve `hatchling` because PyPI DNS was unavailable. The dependency-
free checks are:

```bash
PYTHONPATH=src python3 -m unittest tests.test_canonical -v
PYTHONPATH=src python3 -m compileall -q src tests
```

Fixtures must remain synthetic. Do not add personal transcripts, Drive shards,
credentials, or collector state to this repository.

## Next slices

1. Add a canonical event projection for subagent transcripts without changing
   existing search/stats behavior.
2. Move search/log/stats consumers behind canonical event interfaces while
   preserving current API response shapes.
3. Add byte offset/inode incremental state to the Codex local adapter; the
   current slice performs safe read-only startup scans.
4. Add local collector-shard ingestion for Claude Chat and ChatGPT activity
   records; keep remote/network-backed sources opt-in and failure-isolated.
5. Persist project mappings and policy state locally, with explicit manual
   assignment provenance and no automatic publication.
