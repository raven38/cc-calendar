# Collector schema compatibility

This document records field names, types, and structural differences observed
from the connected collector samples. It intentionally contains no real IDs,
titles, summaries, URLs, timestamps, or other history values; tests use
synthetic replacements.

## Shared summary snapshot

Each `*.jsonl` line is a conversation summary snapshot rather than a message
event. The normalized record accepts these shapes:

| Field | Type | Canonical use |
| --- | --- | --- |
| `schema_version` | integer | structural metadata |
| `source_instance` | string | source namespace and filter |
| `provider`, `product` | string | source identity metadata |
| `title`, `summary` | string | display title and summary event |
| `collector_conversation_id` | string or null | primary collector identity |
| `conversation_id` | string or null | provider-native ID, nullable/later-known |
| `collector_id_inputs` | array of strings | audit metadata only; never rehashed |
| `collector_id_method` | string | identity provenance |
| `created_at`, `first_message_at`, `last_message_at`, `last_activity_at` | string or null | explicit time fields only |
| `completed` | boolean or null | status input, never sufficient alone |
| `project` | object or null | independent project mapping |
| `collector` | object | `collected_at`, `history_completeness` |
| `decisions`, `open_items`, `waiting_for`, `attachments` | arrays | summary metadata |
| `message_count` | integer or null | summary metadata |
| `conversation_url` | string or null | local metadata only |

## ChatGPT collector

`collector_conversation_id` is authoritative. The identity sidecar is a JSON
document with `schema_version`, `source_instance`, `state_delta_type`,
`created_at`, `policy`, and `identity_entries`. Each entry contains
`collector_conversation_id`, `canonical_id_is_immutable`,
`identity_resolution`, `migration_method`, `match_aliases`, and optional
`observed_only`.

Sidecar aliases are used only for a unique match. Ambiguous aliases are
skipped. The sidecar migration method is authoritative for fixed legacy IDs:
eight legacy entries remain frozen and one existing deterministic entry remains
unchanged. A later native `conversation_id` enriches the same session and
cannot change its canonical ID. `last_activity_at` orders snapshots only; it
never participates in hashing.

The collector state file has `schema_version`, `source_instance`,
`last_collected_activity_at`, `last_successful_run_at`, and optional
`backfill_complete`. State is attached to sessions and does not make a session
complete by itself.

## Claude Chat collector

The summary shape is shared, but the observed stable identity is native
`conversation_id`; no identity sidecar is required. The adapter namespaces
that native ID by the explicit source instance and records
`collector_id_method=native_conversation_id`. Claude state may include
`backfill_complete`, `backfill_oldest_collected_activity_at`, and `notes` in
addition to the shared collection timestamps.

## Local ingestion boundary

`ChatActivityAdapter.import_directory()` reads only `*.jsonl`, matching
`*.identity.json`, and `_collector_state.json` under an explicit directory. Any
path containing `.corrected.tmp` is ignored. Repeated shards upsert by stable
identity; summary text is represented as a typed `summary` event, not as a
fabricated user or assistant message. Unknown time/model/effort/usage remain
unknown. No provider API, authentication, export, or remote analysis is
performed.
