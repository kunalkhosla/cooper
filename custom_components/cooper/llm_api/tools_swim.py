"""Swim tool: answer questions about a swimmer's meets, events, times, cuts, and practice.

Read-only. Calls a small local "swim API" (served by the swim-watcher service) which
returns one rich snapshot: the next meet + the swimmer's events (dates/venue/seed times),
live meet results (heat/lane/place while a meet is on), best times, gap-to-Silver/Gold
cut, recent results, and the weekly team PRACTICE schedule (today / this week / where).

Config lives OUTSIDE this (public) repo in ``/config/cooper_swim.json`` so the API token
and family HA user-ids aren't committed::

    {
      "url": "http://SWIM_API_HOST:8081",
      "token": "<SWIM_API_TOKEN>",
      "default_swimmer": "Jane Doe",
      "users": {"<ha_user_id>": "Jane Doe"}
    }

'swimmer' resolution: explicit arg > the asking user's mapping > default. So a swimmer
asking "when do I swim next?" resolves to them; another user falls back to the default.
"""

from __future__ import annotations

import json
import os

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

CONFIG_PATH = os.environ.get("COOPER_SWIM_CONFIG", "/config/cooper_swim.json")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


class GetSwimInfoTool(CooperTool):
    """Answer any question about a swimmer's swimming (schedule, results, times, practice)."""

    name = "get_swim_info"
    description = (
        "Answer questions about a swimmer's SWIMMING. Returns a data snapshot covering: the "
        "next meet and the events they're entered in (dates, venue, seed times), LIVE meet "
        "results (heat/lane/place while a meet is happening), best times per event, how their "
        "times are trending and how far they are from their Silver/Gold time-standard cut, "
        "recent results and any DQs, and the weekly team PRACTICE schedule (today's practice, "
        "this week, and where). Optionally pass 'swimmer' as a full name; if omitted it uses "
        "the person asking, falling back to the household's default swimmer. Use this for any "
        "swim-meet, swim-time, cut, or swim-practice question."
    )
    parameters = vol.Schema({vol.Optional("swimmer"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        cfg = await hass.async_add_executor_job(_load_config)  # avoid blocking the event loop
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            return {"status": "error", "reason": f"swim API not configured ({CONFIG_PATH} missing 'url')"}

        swimmer = tool_input.tool_args.get("swimmer")
        if not swimmer:
            user_id = llm_context.context.user_id if llm_context.context else None
            swimmer = (cfg.get("users") or {}).get(user_id) or cfg.get("default_swimmer")
        if not swimmer:
            return {"status": "error", "reason": "no swimmer given and no default configured"}

        headers = {}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        session = async_get_clientsession(hass)
        try:
            async with session.get(
                f"{url}/swim/context",
                params={"swimmer": swimmer},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return {"status": "error", "reason": f"swim API returned {resp.status}"}
                data = await resp.json()
        except Exception as err:
            return {"status": "error", "reason": f"swim API call failed: {err}"}

        if not isinstance(data, dict):
            return {"status": "error", "reason": "swim API returned non-object JSON"}
        data.setdefault("status", "ok")
        return data
