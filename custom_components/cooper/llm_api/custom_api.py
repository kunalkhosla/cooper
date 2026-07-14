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
from .tools_coach import (
    GetCoachContextTool,
    GetFitnessSummaryTool,
    GetTodayProgressTool,
    LogAlcoholTool,
    LogMealTool,
    LogTrainingTool,
    LogWeightTool,
)
from .tools_footage import LookAtFootageTool
from .tools_history import HistoryTool
from .tools_lifecycle import ListAuthoredTool, ListAutomationsTool, RemoveAuthoredTool
from .tools_location import RefreshLocationTool
from .tools_media import SearchTvTool, WatchTvTool
from .tools_memory import ForgetTool, RecallTool, RememberTool
from .tools_service import CallServiceTool
from .tools_proactivity import CreateWatchTool, ListWatchesTool, RemoveWatchTool
from .tools_swim import GetSwimInfoTool
from .tools_vision import VisionTool

COOPER_API_PROMPT = (
    "You also have Cooper's own tools, beyond controlling exposed devices: look at a "
    "camera live and describe it, look at a camera's RECORDED footage at a past time "
    "(look_at_recorded_footage), query an entity's recent history, look up calendar events over any date range (get_calendar_events — use it "
    "instead of the built-in one-week calendar lookup), force a fresh current "
    "location fix for a family member (refresh_location) — it answers inline if the user waits, "
    "otherwise follows up on the device they're speaking from — remember and recall the "
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
    "the unneeded ones with confirmation. "
    "For anything about SWIMMING — a swimmer's meets and events, live meet results "
    "(heat/lane/place), best times, how far from their Silver/Gold cut, recent results, or "
    "the team PRACTICE schedule (today/this week/where) — use get_swim_info; if the asker is "
    "a swimmer, 'my/I' resolves to them. "
    "For watching TV: a decisive 'put on / play <X> [on the <room> TV]' → watch_tv (it plays "
    "immediately; ASK which TV if none is named and resolve it to a media_player first); a "
    "browse 'show me / what do you have' → search_tv (lists matches, doesn't play). Use plain "
    "media controls (pause/volume/stop) for a TV that's already playing. "
    "For nutrition/fat-loss coaching — meals, calories, macros, ingredient swaps, or "
    "progress questions — call get_coach_context first and reason over the plan it returns "
    "(report cooked/final weight by default, raw only for Sunday meal-prep); log_weight, "
    "log_training, and log_alcohol record what the user tells you happened; whenever the "
    "user tells you what they ate, estimate its macros yourself (same reasoning as a swap "
    "question) and call log_meal so it's tracked — don't just acknowledge it and move on; "
    "get_today_progress answers 'how many calories/protein do I have left today' from what's "
    "been logged via log_meal; get_fitness_summary answers 'how am I doing' with the rolling "
    "weekly weight trend, recent training, and this month's drink count."
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
            GetSwimInfoTool(),
            SearchTvTool(),
            WatchTvTool(),
            GetCoachContextTool(),
            LogWeightTool(),
            LogTrainingTool(),
            LogAlcoholTool(),
            LogMealTool(),
            GetTodayProgressTool(),
            GetFitnessSummaryTool(),
        ]
        return llm.APIInstance(
            api=self,
            api_prompt=COOPER_API_PROMPT,
            llm_context=llm_context,
            tools=tools,
        )
