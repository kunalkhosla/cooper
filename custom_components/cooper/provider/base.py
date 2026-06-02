"""Thin provider interface so non-Anthropic backends can be added later.

Everything Cooper needs from an LLM backend goes through this Protocol. The agent
loop in ``entity.py`` only ever talks to a ``LLMProvider``; it never imports the
Anthropic SDK directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from homeassistant.components import conversation


class LLMProvider(Protocol):
    """Backend that can stream a Claude-style tool-use turn into HA deltas."""

    async def validate_key(self) -> None:
        """Raise if the configured credentials are invalid."""

    def build_stream_kwargs(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        thinking_budget: int,
    ) -> dict[str, Any]:
        """Return the kwargs for a single streaming completion call."""

    async def map_stream(
        self,
        chat_log: conversation.ChatLog,
        stream: Any,
    ) -> AsyncIterator[
        "conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict"
    ]:
        """Map a backend stream into HA chat-log deltas."""

    async def create_stream(self, stream_kwargs: dict[str, Any]) -> Any:
        """Open the backend streaming connection."""

    async def describe_image(
        self, image: bytes, mime_type: str, question: str, *, model: str
    ) -> str:
        """One-shot vision call used by the camera tool."""
