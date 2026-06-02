"""Cooper's conversation agent entity.

Mirrors HA's first-party anthropic conversation entity, with two inserts: after HA
resolves the merged ``[assist, cooper]`` grounding we (a) wrap the action tools with
guardrails and (b) stash the user's durable-memory block for the cached prompt. Then the
shared loop runs and we return the chat-log result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import guardrails
from .const import (
    CONF_PROMPT,
    COOPER_PERSONA_PROMPT,
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
        self._memory_block = "\n\n".join(
            block for block in (_mode_status(self.runtime), memory) if block
        )

        await self._async_handle_chat_log(chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
