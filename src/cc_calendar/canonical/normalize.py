"""Stable identifiers for collector-backed conversations."""

from __future__ import annotations

import hashlib


def canonical_session_id(*, source_instance: str, native_id: str) -> str:
    """Namespace a provider-native ID so accounts cannot collide."""
    value = f"{source_instance}:{native_id}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"session:sha256:{digest}"


def collector_session_id(
    *,
    source_instance: str,
    collector_conversation_id: str | None = None,
    native_id: str | None = None,
) -> str:
    """Return the stable identity for a collector-backed conversation.

    A collector ID wins over a provider-native ID so learning the native ID
    later cannot rename an already-ingested conversation.
    """
    if collector_conversation_id:
        kind, identity = "collector", collector_conversation_id
    elif native_id:
        kind, identity = "native", native_id
    else:
        raise ValueError("a stable collector_conversation_id or native_id is required")
    value = "\n".join((source_instance, kind, identity))
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{source_instance}:{kind}:sha256:{digest}"


def derived_collector_id(
    *,
    provider: str,
    source_instance: str,
    created_at: str | None,
    first_message_at: str | None,
    title: str,
    fallback_key: str | None = None,
) -> str:
    """Return a stable fallback identity backed by a persistent collector key.

    The other arguments are retained as fingerprint context for callers and
    documentation, but never form the identity: titles and activity timestamps
    can change during a conversation.
    """
    del provider, created_at, first_message_at, title
    if not fallback_key:
        raise ValueError("a persistent collector fallback key is required")
    return collector_session_id(
        source_instance=source_instance,
        collector_conversation_id=fallback_key,
    )
