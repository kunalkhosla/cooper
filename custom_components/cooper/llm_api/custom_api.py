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
from .tools_footage import LookAtFootageTool
from .tools_history import HistoryTool
from .tools_lifecycle import ListAuthoredTool, RemoveAuthoredTool
from .tools_memory import ForgetTool, RecallTool, RememberTool
from .tools_proactivity import CreateWatchTool, ListWatchesTool, RemoveWatchTool
from .tools_vision import VisionTool

COOPER_API_PROMPT = (
    "You also have Cooper's own tools, beyond controlling exposed devices: look at a "
    "camera live and describe it, look at a camera's RECORDED footage at a past time "
    "(look_at_recorded_footage), query an entity's recent history, remember and recall the "
    "user's lasting preferences, author native automations and scripts (including timed, "
    "sequenced ones), and set up proactive watches that wake you when something happens. "
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
            RememberTool(),
            RecallTool(),
            ForgetTool(),
            AuthorAutomationTool(),
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
