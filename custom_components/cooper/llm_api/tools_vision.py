"""Vision tool: grab a camera frame and have Claude describe it.

Read-only. The persona prompt already tells the model to narrate one short line before a
slow lookup, so the user hears "let me check the driveway camera…" while this runs.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..const import CONF_CHAT_MODEL, DEFAULT
from ..guardrails import CooperTool

_DEFAULT_QUESTION = "Describe what you see in this image concisely."


class VisionTool(CooperTool):
    """Capture a camera image and return a natural-language description."""

    name = "look_at_camera"
    description = (
        "Look at a camera and describe what is visible right now. Use to answer questions "
        "like 'what's on the driveway?' or 'is anyone at the front door?'. Pass the camera's "
        "entity_id and, optionally, a specific question about the scene."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Optional("question"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        entity_id = str(tool_input.tool_args["entity_id"])
        if not entity_id.startswith("camera."):
            return {
                "status": "error",
                "reason": f"'{entity_id}' is not a camera entity.",
            }
        if hass.states.get(entity_id) is None:
            return {"status": "error", "reason": f"Unknown camera '{entity_id}'."}

        try:
            from homeassistant.components.camera import async_get_image
        except ImportError:
            return {"status": "error", "reason": "The camera integration is unavailable."}

        try:
            image = await async_get_image(hass, entity_id, timeout=10)
        except HomeAssistantError as err:
            return {"status": "error", "reason": f"Could not get an image: {err}"}

        question = tool_input.tool_args.get("question") or _DEFAULT_QUESTION
        runtime = get_runtime(hass)
        description = await runtime.provider.describe_image(
            image.content,
            image.content_type,
            question,
            model=str(DEFAULT[CONF_CHAT_MODEL]),
        )
        return {"entity_id": entity_id, "description": description}
