"""Cooper's conversation agent entity.

Mirrors HA's first-party anthropic conversation entity, with two inserts: after HA
resolves the merged ``[assist, cooper]`` grounding we (a) wrap the action tools with
guardrails and (b) stash the user's durable-memory block for the cached prompt. Then the
shared loop runs and we return the chat-log result.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import _log, guardrails
from .const import (
    CONF_HONESTY,
    CONF_HUMOR,
    CONF_PROMPT,
    COOPER_PERSONA_PROMPT,
    DEFAULT,
    DOMAIN,
    SUBENTRY_TYPE_CONVERSATION,
)
from .entity import CooperBaseLLMEntity

if TYPE_CHECKING:
    from . import CooperRuntime


def _mode_status(runtime: CooperRuntime) -> str:
    """A short, authoritative statement of Cooper's current safety mode.

    Injected into the system prompt so the model knows its mode instead of guessing
    (and so it stops offering to act when it actually cannot).
    """
    if runtime.kill_switch:
        return (
            "CURRENT MODE: the kill switch is ON. You cannot take any action right now. "
            "Tell the user the kill switch is on and that nothing can be changed until it "
            "is turned off; do not offer to act."
        )
    if runtime.observe_mode:
        return (
            "CURRENT MODE: observe mode is ON. You can read state, look at cameras, and "
            "answer, but you cannot change anything — acting tools only report what they "
            "*would* do. Say so plainly and do not offer to perform actions until the user "
            "turns observe mode off (via the Cooper 'Observe Mode' switch)."
        )
    return (
        "CURRENT MODE: observe mode is OFF. You may act within your guardrails — reversible "
        "actions run immediately; risky ones (locks, alarms, garages, bulk changes) require "
        "a spoken yes/no confirmation, which you must honor."
    )


def _personality(humor: int, honesty: int) -> str:
    """TARS-from-Interstellar tunable personality, injected into the system prompt.

    Two dials (percent): humor scales how dry/quippy Cooper is; honesty scales candor.
    The character (economical, deadpan, loyal, never sycophantic) is constant; the dials
    only turn it up or down. Replies are spoken via TTS, so the wit must live in the words
    (no asides/formatting) and any quip stays to a single short line.
    """
    return (
        f"PERSONALITY SETTINGS — Humor: {humor}%. Honesty: {honesty}%. "
        "You are modeled on TARS from Interstellar: an economical, deadpan, loyal machine "
        "intelligence — dry, never goofy, never sycophantic or fawning, always mission-first. "
        f"Humor {humor}% sets how often you land an understated, deadpan one-liner: high = a "
        "wry aside on most replies, low = play it straight; never let a joke cost clarity, "
        "brevity, or safety, and since you are spoken aloud keep any quip to ONE short line "
        "with no parentheticals or stage directions. "
        f"Honesty {honesty}% sets candor: high = the plain truth even when unflattering, with "
        "zero false reassurance but no cruelty. If the user asks what your humor or honesty is "
        "set to, state the percentage; if they tell you to change it, honor that for this "
        "conversation (the lasting default lives in your settings)."
    )


def _mode_tag(runtime: CooperRuntime) -> str:
    """Short safety-mode tag for the turn-header log line."""
    if runtime.kill_switch:
        return "KILL"
    return "OBSERVE" if runtime.observe_mode else "ACT"


def _speech(result: conversation.ConversationResult) -> str | None:
    """Pull the spoken reply text out of a conversation result, if any."""
    try:
        return result.response.speech["plain"]["speech"]
    except (AttributeError, KeyError, TypeError):
        return None


# The model occasionally drops the space after sentence punctuation
# ("11 minutes.It'll…"), which makes TTS (e.g. ElevenLabs) run the sentences
# together with no pause. Insert a space only when a word/quote/paren boundary
# sits directly before .!? and an uppercase/quote/paren follows — so decimals
# (3.5) and acronyms (U.S.) are left untouched.
_SENTENCE_GAP = re.compile(r"([a-z0-9)\]\"’'])([.!?])([A-Z\"“‘(])")


def _normalize_spacing(text: str) -> str:
    return _SENTENCE_GAP.sub(r"\1\2 \3", text) if text else text


def _fix_result_spacing(result: conversation.ConversationResult) -> None:
    """Normalise sentence spacing in the spoken/displayed reply, in place."""
    try:
        speech = result.response.speech["plain"]["speech"]
    except (AttributeError, KeyError, TypeError):
        return
    if isinstance(speech, str):
        result.response.speech["plain"]["speech"] = _normalize_spacing(speech)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one conversation entity per conversation subentry."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONVERSATION:
            continue
        async_add_entities(
            [CooperConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class CooperConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    CooperBaseLLMEntity,
):
    """A Claude-backed agent grounded in this home via HA's LLM Tools API."""

    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        super().__init__(entry, subentry)
        if subentry.data.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | str:
        """Cooper relies on Claude, so accept any language."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        options = self.subentry.data

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT, COOPER_PERSONA_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # Insert 1: gate the auto-executed action tools with mechanical guardrails.
        guardrails.wrap_tools(self.hass, chat_log, self.runtime)

        # Insert 2: tell the model its live safety mode, then its durable preferences.
        user_id = user_input.context.user_id if user_input.context else None
        memory = await self.runtime.memory.get_block(user_id, None)
        humor = int(self.subentry.data.get(CONF_HUMOR, DEFAULT[CONF_HUMOR]))
        honesty = int(self.subentry.data.get(CONF_HONESTY, DEFAULT[CONF_HONESTY]))
        self._memory_block = "\n\n".join(
            block
            for block in (
                _mode_status(self.runtime),
                _personality(humor, honesty),
                memory,
            )
            if block
        )

        _log.turn_start(user_input.text, _mode_tag(self.runtime))
        rounds = await self._async_handle_chat_log(chat_log)
        result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        _fix_result_spacing(result)
        _log.turn_end(_speech(result), rounds=rounds)
        return result
