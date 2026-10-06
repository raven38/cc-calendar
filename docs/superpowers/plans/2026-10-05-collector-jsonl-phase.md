# Local collector JSONL phase

## Scope

This slice adds a read-only local importer for synthetic collector shards.
The command line accepts an explicit source instance and directory:

```text
--collector-dir [SOURCE=]PATH
  -> ChatActivityAdapter (JSONL)
  -> canonical Session + Event + ProjectAssignment
  -> combined session list, detail, canonical, and log APIs
```

The importer accepts session/conversation snapshots and activity records. It
upserts by the stable `collector_conversation_id` supplied by the collector,
then deduplicates events by explicit event ID or a deterministic event
fingerprint. A later native conversation ID is metadata on the same canonical
session and never changes that session's ID. Missing stable IDs are skipped;
path, title, and timestamp are not identity fallbacks.

## Privacy and policy boundary

Only caller-selected local directories are read. The importer does not call a
provider API, discover accounts, write collector state, or export data. The
default project is private `Misc`. Native project visibility, export policy,
and analysis policy are independent fields, so a public project does not make
its sessions public. Unknown or incomplete sync/backfill state remains
unknown, and full-history capability is downgraded to summary until backfill
is explicitly complete.

## Verification

Tests use only synthetic data and cover stable identity, source-instance
separation, event upsert, partial JSONL, missing metadata, project policy,
status/backfill boundaries, Store rescans, API projections, and the CLI parser.
The importer is intentionally a startup scan in this phase; incremental
offset/inode state, remote connectors, authentication, publishing, and export
remain separate follow-up work.
