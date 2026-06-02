"""Long-term preference memory, backed by helpers.storage.Store.

Short-term memory is free (HA's per-conversation ChatLog). This module holds durable,
per-user preferences. A compact block is injected into the (cached) system prompt; it is
capped so it never grows without bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

STORAGE_VERSION = 1
STORAGE_KEY = "cooper.memory"
MAX_ENTRIES_PER_SCOPE = 40


def _scope(user_id: str | None, subentry_id: str | None) -> str:
    return f"{user_id or 'global'}::{subentry_id or 'default'}"


@dataclass(slots=True)
class MemoryEntry:
    """A single remembered preference."""

    text: str
    created: str


class MemoryStore:
    """Namespaced preference store (per user + per agent subentry)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._data: dict[str, list[dict[str, Any]]] | None = None

    async def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._data is None:
            self._data = await self._store.async_load() or {}
        return self._data

    async def remember(
        self, user_id: str | None, subentry_id: str | None, text: str
    ) -> None:
        """Store a durable preference, de-duplicating and capping the scope."""
        data = await self._load()
        scope = _scope(user_id, subentry_id)
        entries = data.setdefault(scope, [])
        text = text.strip()
        entries[:] = [e for e in entries if e.get("text") != text]
        entries.append({"text": text, "created": dt_util.utcnow().isoformat()})
        if len(entries) > MAX_ENTRIES_PER_SCOPE:
            del entries[: len(entries) - MAX_ENTRIES_PER_SCOPE]
        await self._store.async_save(data)

    async def recall(
        self, user_id: str | None, subentry_id: str | None, query: str | None = None
    ) -> list[str]:
        """Return remembered preferences, optionally filtered by a substring query."""
        data = await self._load()
        entries = [e["text"] for e in data.get(_scope(user_id, subentry_id), [])]
        if query:
            q = query.lower()
            entries = [e for e in entries if q in e.lower()]
        return entries

    async def forget(
        self, user_id: str | None, subentry_id: str | None, text: str
    ) -> bool:
        """Remove a preference by exact or substring match. Returns True if removed."""
        data = await self._load()
        scope = _scope(user_id, subentry_id)
        entries = data.get(scope, [])
        q = text.strip().lower()
        new = [e for e in entries if q not in e.get("text", "").lower()]
        removed = len(new) != len(entries)
        if removed:
            data[scope] = new
            await self._store.async_save(data)
        return removed

    async def get_block(self, user_id: str | None, subentry_id: str | None) -> str:
        """Return a compact prompt block of known preferences (empty string if none)."""
        entries = await self.recall(user_id, subentry_id)
        if not entries:
            return ""
        lines = "\n".join(f"- {e}" for e in entries)
        return f"What you know about this user's preferences:\n{lines}"
