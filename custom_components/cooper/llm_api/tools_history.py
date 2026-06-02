"""History tool: query the recorder for an entity's recent state changes.

Read-only. Answers "when did the front door last open?", "has the garage been opened
today?", etc. by summarising recorder history into a compact list the model can reason
over without being flooded with raw rows.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

MAX_CHANGES = 50


class HistoryTool(CooperTool):
    """Summarise an entity's recent state history from the recorder."""

    name = "get_history"
    description = (
        "Look up the recent state history of a single entity to answer questions about "
        "what happened and when (e.g. when a door last opened, whether a light has been on "
        "today). Returns the state changes over the requested window."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=720)),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        entity_id = str(tool_input.tool_args["entity_id"])
        hours = int(tool_input.tool_args.get("hours", 24))

        if hass.states.get(entity_id) is None:
            return {"status": "error", "reason": f"Unknown entity '{entity_id}'."}

        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return {"status": "error", "reason": "The recorder is not available."}

        end = dt_util.utcnow()
        start = end - timedelta(hours=hours)
        changes = await get_instance(hass).async_add_executor_job(
            partial(
                history.state_changes_during_period,
                hass,
                start,
                end,
                entity_id,
                no_attributes=True,
                include_start_time_state=True,
            )
        )
        states = changes.get(entity_id, [])
        items = [
            {
                "state": state.state,
                "when": state.last_changed.isoformat(),
            }
            for state in states
        ]
        if len(items) > MAX_CHANGES:
            items = items[-MAX_CHANGES:]
        return {
            "entity_id": entity_id,
            "window_hours": hours,
            "change_count": len(states),
            "changes": items,
        }
