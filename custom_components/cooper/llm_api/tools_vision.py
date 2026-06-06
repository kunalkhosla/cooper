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


def _image_candidates(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Ordered camera entities to try for a still, MAIN/clear stream first.

    Reolink (and similar) split each camera into ``<base>_clear`` (main/high-res) and
    ``<base>_fluent`` (sub/low-res). The SUB-stream snapshot frequently fails with a 500
    while the main stream returns fine, so when handed a fluent/clear entity we try the
    clear (main) sibling first, then the requested one, then the fluent — and use whichever
    actually yields an image. Non-paired cameras just use the given entity.
    """
    base = None
    for suffix in ("_clear", "_fluent"):
        if entity_id.endswith(suffix):
            base = entity_id[: -len(suffix)]
            break
    ordered = (
        [f"{base}_clear", entity_id, f"{base}_fluent"] if base is not None else [entity_id]
    )
    out: list[str] = []
    for cand in ordered:
        if cand not in out and hass.states.get(cand) is not None:
            out.append(cand)
    return out or [entity_id]


class VisionTool(CooperTool):
    """Capture a camera image and return a natural-language description."""

    name = "look_at_camera"
    description = (
        "Look at a camera and describe what is visible right now. Use to answer questions "
        "like 'what's on the driveway?' or 'is anyone at the front door?'. Pass the camera's "
        "entity_id and, optionally, a specific question about the scene. You can pass either the "
        "main or sub stream entity — it automatically uses the camera's best working stream. If it "
        "reports the live snapshot is down, use look_at_recorded_footage for that camera instead."
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

        # Try the main stream first, then siblings — sub-stream snapshots often 500.
        candidates = _image_candidates(hass, entity_id)
        image = None
        used = entity_id
        errors: list[str] = []
        for cand in candidates:
            try:
                image = await async_get_image(hass, cand, timeout=10)
                used = cand
                break
            except HomeAssistantError as err:
                errors.append(f"{cand}: {err}")
        if image is None:
            return {
                "status": "error",
                "reason": (
                    "Could not get a live image from "
                    + " / ".join(candidates)
                    + f" ({'; '.join(errors)}). The camera's snapshot may be down right now — "
                    "fall back to look_at_recorded_footage for this camera instead."
                ),
            }

        question = tool_input.tool_args.get("question") or _DEFAULT_QUESTION
        runtime = get_runtime(hass)
        description = await runtime.provider.describe_image(
            image.content,
            image.content_type,
            question,
            model=str(DEFAULT[CONF_CHAT_MODEL]),
        )
        return {"entity_id": used, "description": description}
