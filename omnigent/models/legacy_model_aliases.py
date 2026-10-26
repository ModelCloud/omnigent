"""Retired model ids that may still sit in a user's on-disk Codex config.

Omnigent renamed its local model from ``QB/DSV4.1-Flash`` to
``DS v4.1 Flash 1017``. The old spelling is not a forward-compatible alias
on the provider side: sending it yields, depending on the route,

- ``The 'QB/DSV4.1-Flash' model is not supported when using Codex with a
  ChatGPT account`` (the id leaked to the official endpoint), or
- ``Model metadata for 'QB/DSV4.1-Flash' not found`` (fallback metadata).

Nothing in the live launch path emits the old name any more, but a
``~/.codex/config.toml`` or ``<profile>.config.toml`` written before the
rename keeps it as its top-level ``model``. Those files are *copied* into
each session's private ``CODEX_HOME``, so a stale line there governs new
sessions and resumes alike until the user edits the file by hand.

This module is the single place that knows the mapping, so the config-bridge
paths can normalize on copy instead of every caller re-spelling the check.
It is deliberately a one-way forward migration: the new spelling is
canonical and the old one is never re-emitted.
"""

from __future__ import annotations

#: Retired id -> canonical id. Keys are matched case-insensitively on the
#: stripped value; values are the exact spelling the live runtime uses.
LEGACY_MODEL_ALIASES: dict[str, str] = {
    "qb/dsv4.1-flash": "DS v4.1 Flash 1017",
    "qb/ds-v4.1-flash": "DS v4.1 Flash 1017",
    "qb/dsv4.1-flash-1017": "DS v4.1 Flash 1017",
}

#: Canonical spelling of the local model, re-exported for callers that need
#: it without importing the harness package (which pulls in httpx).
CANONICAL_LOCAL_MODEL = "DS v4.1 Flash 1017"

_ALIAS_LOOKUP: dict[str, str] = {
    key.strip().lower(): value for key, value in LEGACY_MODEL_ALIASES.items()
}


def canonical_model_id(model: str | None) -> str | None:
    """Map a retired model id to its canonical spelling.

    :param model: Model id read from config, argv, or a persisted session.
    :returns: The canonical id when *model* is a known retired spelling,
        otherwise *model* unchanged. ``None`` passes through so callers can
        use this on optional config values without a guard.
    """
    if model is None:
        return None
    return _ALIAS_LOOKUP.get(model.strip().lower(), model)


def is_legacy_model_id(model: str | None) -> bool:
    """Whether *model* is a retired spelling this module can rewrite."""
    if model is None:
        return False
    return model.strip().lower() in _ALIAS_LOOKUP
