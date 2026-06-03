"""CooperAPI — Cooper's own LLM Tools API, merged alongside HA's built-in Assist API.

Registered once at setup so it appears in the config flow's API picker. When a turn
resolves ``[assist, cooper]``, HA's ``MergedAPI`` namespaces these tools as
``cooper__<name>`` and concatenates this API's prompt after Assist's. Assist already
grounds the model in the home; this prompt only points at the extra capabilities.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..const import DOMAIN
from .tools_automation import AuthorAutomationTool
from .tools_calendar import GetCalendarEventsTool
from .tools_footage import LookAtFootageTool
from .tools_history import HistoryTool
from .tools_lifecycle import ListAuthoredTool, ListAutomationsTool, RemoveAuthoredTool
from .tools_location import RefreshLocationTool
from .tools_memory import ForgetTool, RecallTool, RememberTool
from .tools_service import CallServiceTool
from .tools_proactivity import CreateWatchTool, ListWatchesTool, RemoveWatchTool
from .tools_vision import VisionTool

COOPER_API_PROMPT = (
    "You also have Cooper's own tools, beyond controlling exposed devices: look at a "
    "camera live and describe it, look at a camera's RECORDED footage at a past time "
    "(look_at_recorded_footage), query an entity's recent history, look up calendar events over any date range (get_calendar_events — use it "
    "instead of the built-in one-week calendar lookup), force a fresh current "
    "location fix for a family member and follow up by notification when it lands "
    "(refresh_location, asynchronous), remember and recall the "
    "user's lasting preferences, author native automations and scripts (including timed, "
    "sequenced ones), and set up proactive watches that wake you when something happens. "
    "Call a parameterized service on entities you NAME via call_service (e.g. a sprinkler "
    "zone for a set number of minutes) — it resolves names to entity_ids, so never guess an "
    "id; prefer it over authoring a one-off script for a timed/parameterized action. "
    "list_automations shows ALL the home's automations (read-only) with their state and "
    "last-run, and their config when you filter by name, so you can explain what they do. "
    "Prefer authoring a durable automation/script over promising to do something later. "
    "To tidy up, list_cooper_items shows the automations and scripts you authored and "
    "delete_cooper_item removes one — you can only ever delete your own (cooper_) items, "
    "never the user's, so when asked to clean up old automations, list first then remove "
    "the unneeded ones with confirmation."
)


class CooperAPI(llm.API):
    """The Cooper tool set."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Cooper")

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        tools: list[llm.Tool] = [
            HistoryTool(),
            VisionTool(),
            LookAtFootageTool(),
            GetCalendarEventsTool(),
            RefreshLocationTool(),
            RememberTool(),
            RecallTool(),
            ForgetTool(),
            CallServiceTool(),
            AuthorAutomationTool(),
            ListAutomationsTool(),
            ListAuthoredTool(),
            RemoveAuthoredTool(),
            CreateWatchTool(),
            ListWatchesTool(),
            RemoveWatchTool(),
        ]
        return llm.APIInstance(
            api=self,
            api_prompt=COOPER_API_PROMPT,
            llm_context=llm_context,
            tools=tools,
        )
