"""Anthropic provider: streaming Claude tool-use turns mapped into HA chat-log deltas.

The delta-stream / content-conversion / tool-formatting logic is adapted from Home
Assistant's first-party ``anthropic`` integration (homeassistant/components/anthropic/
entity.py), trimmed to what Cooper uses: text, extended thinking, client-side tool calls,
and Anthropic's native server-side ``web_search`` tool. We vendor it (rather than import the
private classes) so we own the prompt-cache breakpoints and model arguments and are not
coupled to a private API surface.
"""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any, Literal, cast

import anthropic
from anthropic import AsyncStream
from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageStreamEvent,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RedactedThinkingBlock,
    ServerToolUseBlock,
    SignatureDelta,
    TextBlock,
    TextBlockParam,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolUseBlock,
)
from voluptuous_openapi import convert

from homeassistant.components import conversation
from homeassistant.helpers import llm
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.json import json_dumps

from ..const import LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

UNSUPPORTED_SCHEMA_KEYS = frozenset({"oneOf", "anyOf", "allOf"})
WEB_SEARCH_TOOL_NAME = "web_search"


def format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Format an llm.Tool into an Anthropic tool spec.

    voluptuous_openapi cannot express anyOf/oneOf/allOf, so they are stripped; author
    custom-tool parameter schemas without them.
    """
    schema = convert(tool.parameters, custom_serializer=custom_serializer)
    schema = {k: v for k, v in schema.items() if k not in UNSUPPORTED_SCHEMA_KEYS}
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": schema,
    }


@dataclass(slots=True)
class ContentDetails:
    """Native data carried on an AssistantContent so thinking can round-trip."""

    thinking_signature: str | None = None
    redacted_thinking: str | None = None

    def __bool__(self) -> bool:
        return self.thinking_signature is not None or self.redacted_thinking is not None


def convert_content(
    chat_content: Iterable[conversation.Content],
) -> list[dict[str, Any]]:
    """Transform HA chat-log content into Anthropic message params."""
    messages: list[dict[str, Any]] = []

    for content in chat_content:
        if isinstance(content, conversation.ToolResultContent):
            external = content.tool_name == WEB_SEARCH_TOOL_NAME
            if external:
                block: dict[str, Any] = {
                    "type": "web_search_tool_result",
                    "tool_use_id": content.tool_call_id,
                    "content": content.tool_result.get("content", content.tool_result),
                }
            else:
                block = {
                    "type": "tool_result",
                    "tool_use_id": content.tool_call_id,
                    "content": json_dumps(content.tool_result),
                }
            role = "assistant" if external else "user"
            if not messages or messages[-1]["role"] != role:
                messages.append({"role": role, "content": [block]})
            elif isinstance(messages[-1]["content"], str):
                messages[-1]["content"] = [
                    {"type": "text", "text": messages[-1]["content"]},
                    block,
                ]
            else:
                messages[-1]["content"].append(block)

        elif isinstance(content, conversation.UserContent):
            if not messages or messages[-1]["role"] != "user":
                messages.append({"role": "user", "content": content.content})
            elif isinstance(messages[-1]["content"], str):
                messages[-1]["content"] = [
                    {"type": "text", "text": messages[-1]["content"]},
                    {"type": "text", "text": content.content},
                ]
            else:
                messages[-1]["content"].append(
                    {"type": "text", "text": content.content}
                )

        elif isinstance(content, conversation.AssistantContent):
            if not messages or messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": []})
            elif isinstance(messages[-1]["content"], str):
                messages[-1]["content"] = [
                    {"type": "text", "text": messages[-1]["content"]}
                ]

            blocks: list[dict[str, Any]] = messages[-1]["content"]  # type: ignore[assignment]

            if (
                isinstance(content.native, ContentDetails)
                and content.native.thinking_signature
                and content.thinking_content
            ):
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": content.thinking_content,
                        "signature": content.native.thinking_signature,
                    }
                )
            if isinstance(content.native, ContentDetails) and content.native.redacted_thinking:
                blocks.append(
                    {"type": "redacted_thinking", "data": content.native.redacted_thinking}
                )

            if content.content:
                blocks.append({"type": "text", "text": content.content})

            for tool_call in content.tool_calls or ():
                if tool_call.external and tool_call.tool_name == WEB_SEARCH_TOOL_NAME:
                    blocks.append(
                        {
                            "type": "server_tool_use",
                            "id": tool_call.id,
                            "name": WEB_SEARCH_TOOL_NAME,
                            "input": tool_call.tool_args,
                        }
                    )
                else:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.tool_name,
                            "input": tool_call.tool_args,
                        }
                    )

            # Collapse a lone text block back to a plain string.
            if len(blocks) == 1 and blocks[0].get("type") == "text":
                messages[-1]["content"] = blocks[0]["text"]

    return messages


class CooperDeltaStream:
    """Map an Anthropic streaming response into HA AssistantContentDeltaDict items."""

    def __init__(self, stream: AsyncStream[MessageStreamEvent]) -> None:
        self._stream = stream
        self._iter: AsyncIterator[MessageStreamEvent] | None = None
        self._buffer: deque[
            conversation.AssistantContentDeltaDict
            | conversation.ToolResultContentDeltaDict
        ] = deque()
        self._tool_block: dict[str, Any] | None = None
        self._tool_args = ""
        self._details = ContentDetails()
        self._first = True

    def __aiter__(
        self,
    ) -> AsyncIterator[
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ]:
        self._iter = self._stream.__aiter__()
        return self

    async def __anext__(
        self,
    ) -> (
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ):
        while True:
            if self._buffer:
                return self._buffer.popleft()
            assert self._iter is not None
            event = await self._iter.__anext__()
            self._handle(event)

    def _new_assistant_message(self) -> None:
        if self._details:
            self._buffer.append({"native": self._details})
        self._details = ContentDetails()
        self._buffer.append({"role": "assistant"})
        self._first = False

    def _handle(self, event: MessageStreamEvent) -> None:  # noqa: C901
        if isinstance(event, RawMessageStartEvent):
            self._first = True
            return
        if isinstance(event, RawContentBlockStartEvent):
            block = event.content_block
            if isinstance(block, ToolUseBlock):
                self._tool_block = {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input) if block.input else {},
                }
                self._tool_args = ""
            elif isinstance(block, ServerToolUseBlock):
                self._tool_block = {
                    "type": "server_tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input) if block.input else {},
                }
                self._tool_args = ""
            elif isinstance(block, TextBlock):
                if self._first:
                    self._new_assistant_message()
                if block.text:
                    self._buffer.append({"content": block.text})
            elif isinstance(block, ThinkingBlock):
                if self._first:
                    self._new_assistant_message()
                if block.thinking:
                    self._buffer.append({"thinking_content": block.thinking})
            elif isinstance(block, RedactedThinkingBlock):
                if self._first:
                    self._new_assistant_message()
                self._details.redacted_thinking = block.data
            elif block.type.endswith("tool_result"):
                # Native server-tool result (web_search). Surface as a tool_result.
                self._buffer.append(
                    {
                        "role": "tool_result",
                        "tool_call_id": block.tool_use_id,
                        "tool_name": WEB_SEARCH_TOOL_NAME,
                        "tool_result": {"content": _to_jsonable(block.content)},
                    }
                )
                self._first = True
            return
        if isinstance(event, RawContentBlockDeltaEvent):
            delta = event.delta
            if isinstance(delta, TextDelta):
                if self._first:
                    self._new_assistant_message()
                if delta.text:
                    self._buffer.append({"content": delta.text})
            elif isinstance(delta, ThinkingDelta):
                if self._first:
                    self._new_assistant_message()
                if delta.thinking:
                    self._buffer.append({"thinking_content": delta.thinking})
            elif isinstance(delta, SignatureDelta):
                self._details.thinking_signature = delta.signature
            elif isinstance(delta, InputJSONDelta):
                self._tool_args += delta.partial_json
            return
        if isinstance(event, RawContentBlockStopEvent):
            if self._tool_block is not None:
                if self._tool_args:
                    try:
                        self._tool_block["input"] |= json.loads(self._tool_args)
                    except json.JSONDecodeError:
                        LOGGER.debug("Could not parse tool args: %s", self._tool_args)
                self._buffer.append(
                    {
                        "tool_calls": [
                            llm.ToolInput(
                                id=self._tool_block["id"],
                                tool_name=self._tool_block["name"],
                                tool_args=self._tool_block["input"],
                                external=self._tool_block["type"] == "server_tool_use",
                            )
                        ]
                    }
                )
                self._tool_block = None
            return
        if isinstance(event, RawMessageDeltaEvent):
            if event.delta.stop_reason == "refusal":
                raise anthropic.AnthropicError("The model refused to respond.")
            return
        if isinstance(event, RawMessageStopEvent):
            if self._details:
                self._buffer.append({"native": self._details})
            self._details = ContentDetails()


def _to_jsonable(content: Any) -> Any:
    """Best-effort convert SDK objects to JSON-serialisable structures."""
    if isinstance(content, list):
        return [_to_jsonable(item) for item in content]
    if hasattr(content, "to_dict"):
        return content.to_dict()
    return content


class AnthropicProvider:
    """LLMProvider backed by the Anthropic Python SDK."""

    def __init__(self, hass: HomeAssistant, api_key: str) -> None:
        self._hass = hass
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, http_client=get_async_client(hass)
        )

    async def validate_key(self) -> None:
        """Validate credentials by listing models."""
        await self.client.models.list(timeout=10.0)

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
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "system": system,
            "stream": True,
        }
        if thinking_budget and thinking_budget > 0:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
        if tools:
            kwargs["tools"] = tools
        return kwargs

    async def create_stream(
        self, stream_kwargs: dict[str, Any]
    ) -> AsyncStream[MessageStreamEvent]:
        return await self.client.messages.create(**stream_kwargs)

    def map_stream(
        self,
        chat_log: conversation.ChatLog,
        stream: AsyncStream[MessageStreamEvent],
    ) -> CooperDeltaStream:
        return CooperDeltaStream(stream)

    async def describe_image(
        self, image: bytes, mime_type: str, question: str, *, model: str
    ) -> str:
        """One-shot vision call returning a textual description."""
        if mime_type == "image/jpg":
            mime_type = "image/jpeg"
        data = base64.b64encode(image).decode("utf-8")
        response: Message = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": cast(Any, mime_type),
                                "data": data,
                            },
                        },
                        {"type": "text", "text": question},
                    ],
                }
            ],
        )
        return "".join(
            block.text
            for block in response.content
            if isinstance(block, TextBlock)
        ).strip()
