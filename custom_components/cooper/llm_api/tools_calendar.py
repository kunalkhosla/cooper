"""Calendar tool: read events over an arbitrary date range.

A generic capability, not a use case: Home Assistant's built-in calendar intent
only looks one week ahead, so questions like "anything in June?" or "next month"
are invisible to it. This wraps the ``calendar.get_events`` service (which takes
an explicit start/end) so Cooper can query any window, across one calendar or all
of them. Read-only.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

MAX_EVENTS = 60


def _to_iso(value: str) -> str | None:
    """Accept a date or datetime string → local, tz-aware ISO for the service."""
    dt = dt_util.parse_datetime(value)
    if dt is None:
        date = dt_util.parse_date(value)
        if date is None:
            return None
        dt = dt_util.start_of_local_day(date)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt.isoformat()


class GetCalendarEventsTool(CooperTool):
    """Look up calendar events over any date range, across one or all calendars."""

    name = "get_calendar_events"
    description = (
        "Look up calendar events over ANY date range — the rest of this month, next "
        "month, a specific week or day. Use this rather than the built-in calendar "
        "lookup, which only sees one week ahead. Give 'start' and 'end' as ISO dates "
        "or datetimes (check today's date first if the range is relative). Omit "
        "'calendar' to search every calendar at once, or pass a calendar entity_id or "
        "name to limit the search."
    )
    parameters = vol.Schema(
        {
            vol.Required("start"): str,
            vol.Required("end"): str,
            vol.Optional("calendar"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        args = tool_input.tool_args
        start = _to_iso(str(args["start"]))
        end = _to_iso(str(args["end"]))
        if start is None or end is None:
            return {"status": "error", "reason": "Could not parse start/end dates."}

        # Resolve which calendars to query.
        all_cals = [s.entity_id for s in hass.states.async_all("calendar")]
        wanted = args.get("calendar")
        if wanted:
            wanted = str(wanted)
            if wanted in all_cals:
                entity_ids = [wanted]
            else:
                needle = wanted.lower()
                entity_ids = [
                    s.entity_id
                    for s in hass.states.async_all("calendar")
                    if needle in (s.attributes.get("friendly_name") or "").lower()
                    or needle in s.entity_id.lower()
                ]
            if not entity_ids:
                return {"status": "error", "reason": f"No calendar matched '{wanted}'."}
        else:
            entity_ids = all_cals
        if not entity_ids:
            return {"status": "error", "reason": "No calendars are available."}

        try:
            resp = await hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": entity_ids,
                    "start_date_time": start,
                    "end_date_time": end,
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - surface a clean error to the model
            return {"status": "error", "reason": f"Calendar lookup failed: {err}"}

        events: list[dict[str, Any]] = []
        for ent, data in (resp or {}).items():
            for ev in (data or {}).get("events", []):
                events.append(
                    {
                        "calendar": ent,
                        "summary": ev.get("summary"),
                        "start": ev.get("start"),
                        "end": ev.get("end"),
                        "location": ev.get("location"),
                    }
                )
        events.sort(key=lambda e: str(e.get("start") or ""))
        truncated = len(events) > MAX_EVENTS
        return {
            "status": "ok",
            "window": {"start": start, "end": end},
            "calendars_searched": entity_ids,
            "event_count": len(events),
            "events": events[:MAX_EVENTS],
            "truncated": truncated,
        }
