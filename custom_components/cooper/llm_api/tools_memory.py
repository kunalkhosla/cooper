"""Durable preference memory tools (remember / recall / forget).

Memory is scoped per user (``subentry_id`` is left ``None`` so a person's preferences
follow them across agents). These are internal state, not home control, so they run
regardless of observe mode — but the kill switch still silences writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

if TYPE_CHECKING:
    pass


def _user_id(llm_context: llm.LLMContext) -> str | None:
    context = llm_context.context
    return context.user_id if context is not None else None


class RememberTool(CooperTool):
    """Store a durable fact or preference about the user."""

    name = "remember"
    description = (
        "Save a lasting preference or fact about this user so you can use it in future "
        "conversations (e.g. 'prefers the living room warm in the evening'). Use only "
        "for durable preferences, not one-off requests."
    )
    parameters = vol.Schema({vol.Required("text"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        if runtime.kill_switch:
            return {"status": "refused", "reason": "Cooper's kill switch is on."}
        text = str(tool_input.tool_args["text"]).strip()
        await runtime.memory.remember(_user_id(llm_context), None, text)
        return {"status": "remembered", "text": text}


class RecallTool(CooperTool):
    """Recall stored preferences, optionally filtered."""

    name = "recall"
    description = (
        "Look up what you already know about this user's preferences. Optionally pass a "
        "query to filter. The most relevant known preferences are already in your context, "
        "so use this only when you need to search for something specific."
    )
    parameters = vol.Schema({vol.Optional("query"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        query = tool_input.tool_args.get("query")
        entries = await runtime.memory.recall(_user_id(llm_context), None, query)
        return {"preferences": entries}


class ForgetTool(CooperTool):
    """Forget a previously stored preference."""

    name = "forget"
    description = (
        "Remove something you previously remembered about this user. Pass enough of the "
        "text to identify it. Use when the user asks you to forget a preference."
    )
    parameters = vol.Schema({vol.Required("text"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        if runtime.kill_switch:
            return {"status": "refused", "reason": "Cooper's kill switch is on."}
        text = str(tool_input.tool_args["text"])
        removed = await runtime.memory.forget(_user_id(llm_context), None, text)
        return {"status": "forgotten" if removed else "not_found", "query": text}
