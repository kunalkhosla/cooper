"""Base LLM entity: builds model args (with split prompt cache) and runs the loop.

The loop mirrors HA's first-party anthropic integration but goes through our
``LLMProvider`` so other backends can slot in. Each iteration streams a turn, lets HA's
chat log accumulate + auto-execute tool calls, re-converts the new content into the
running message list, and stops when there are no unresponded tool results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT_CACHING,
    CONF_THINKING_BUDGET,
    CONF_WEB_SEARCH,
    CONF_WEB_SEARCH_MAX_USES,
    DEFAULT,
    DOMAIN,
    PromptCaching,
)
from .coordinator import CooperCoordinator
from .provider.anthropic_client import convert_content, format_tool

if TYPE_CHECKING:
    from . import CooperRuntime

MAX_TOOL_ITERATIONS = 10
# Anthropic's native server-side web search tool (no custom llm.Tool needed).
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"


class CooperBaseLLMEntity(CoordinatorEntity[CooperCoordinator]):
    """Shared model-args + agent-loop machinery for Cooper entities."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self.entry = entry
        self.subentry = subentry
        self.runtime: CooperRuntime = entry.runtime_data
        super().__init__(self.runtime.coordinator)
        self._attr_unique_id = subentry.subentry_id
        self._memory_block = ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Anthropic",
            model=str(subentry.data.get(CONF_CHAT_MODEL, DEFAULT[CONF_CHAT_MODEL])),
            entry_type=DeviceEntryType.SERVICE,
        )

    def _opt(self, key: str) -> Any:
        return self.subentry.data.get(key, DEFAULT.get(key))

    def _build_system(self, chat_log: conversation.ChatLog) -> list[dict[str, Any]] | str:
        """Persona + HA grounding as block #1, durable memory as block #2.

        Caching breakpoints sit on the first and last block so the big, stable prefix
        (tool defs + persona + grounding) caches independently of volatile memory.
        """
        grounding = chat_log.content[0].content or ""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": grounding}]
        if self._memory_block:
            blocks.append({"type": "text", "text": self._memory_block})
        if self._opt(CONF_PROMPT_CACHING) != PromptCaching.OFF.value:
            blocks[0]["cache_control"] = {"type": "ephemeral"}
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def _build_tools(self, chat_log: conversation.ChatLog) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if chat_log.llm_api is not None:
            tools = [
                format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            ]
        if self._opt(CONF_WEB_SEARCH):
            tools.append(
                {
                    "type": WEB_SEARCH_TOOL_TYPE,
                    "name": "web_search",
                    "max_uses": int(self._opt(CONF_WEB_SEARCH_MAX_USES)),
                }
            )
        return tools

    def _get_model_args(self, chat_log: conversation.ChatLog) -> dict[str, Any]:
        provider = self.runtime.provider
        return provider.build_stream_kwargs(
            system=self._build_system(chat_log),
            messages=convert_content(chat_log.content[1:]),
            tools=self._build_tools(chat_log),
            model=str(self._opt(CONF_CHAT_MODEL)),
            max_tokens=int(self._opt(CONF_MAX_TOKENS)),
            thinking_budget=int(self._opt(CONF_THINKING_BUDGET) or 0),
        )

    async def _async_handle_chat_log(self, chat_log: conversation.ChatLog) -> int:
        """Stream turns until the model stops calling tools (bounded).

        Returns the number of model rounds it took (for the turn-end log footer).
        """
        provider = self.runtime.provider
        model_args = self._get_model_args(chat_log)

        rounds = 0
        for _iteration in range(MAX_TOOL_ITERATIONS):
            rounds += 1
            stream = await provider.create_stream(model_args)
            new_content = [
                content
                async for content in chat_log.async_add_delta_content_stream(
                    self.entity_id, provider.map_stream(chat_log, stream)
                )
            ]
            model_args["messages"].extend(convert_content(new_content))
            if not chat_log.unresponded_tool_results:
                break
        return rounds
