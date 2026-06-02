"""Location tool: force a fresh GPS fix for the family and follow up when it lands.

Forcing a fresh fix is asynchronous — the watch/phone takes up to a couple of minutes to
report back — so this tool does NOT block the voice turn. It triggers the refresh (presses
COSMO's ``button.vir_request_location`` for Vir, or sends the companion app a
``request_location_update`` for a phone) and spawns an in-integration **background task**
(``hass.async_create_background_task``) that watches the person's location sensor for a fresh
update for up to ``LOCATE_TIMEOUT`` seconds, then notifies the caller — and returns instantly.

This keeps everything inside the integration: it replaces the four HA-side ``script.locate_*``
scripts and needs no automation or ``input_boolean`` (issue #1). The background task survives
the end of the conversation turn, so the follow-up still fires once the fix arrives.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.json import JsonObjectType

from ..const import (
    LOCATE_DEFAULT_NOTIFY,
    LOCATE_TARGETS,
    LOCATE_TIMEOUT,
    LOGGER,
)
from ..guardrails import CooperTool

# Payload the Home Assistant companion app interprets as "send me a fresh GPS fix now".
_COMPANION_REFRESH_MESSAGE = "request_location_update"


async def _trigger_refresh(hass: HomeAssistant, refresh: str) -> None:
    """Kick off a forced fix via either a button press or a companion-app notify."""
    domain, service = refresh.split(".", 1)
    if domain == "button":
        await hass.services.async_call(
            "button", "press", {"entity_id": refresh}, blocking=True
        )
    elif domain == "notify":
        await hass.services.async_call(
            "notify", service, {"message": _COMPANION_REFRESH_MESSAGE}, blocking=True
        )
    else:  # pragma: no cover - config is ours, but stay defensive
        raise ValueError(f"Don't know how to refresh via '{refresh}'")


async def _notify(hass: HomeAssistant, target: str, message: str) -> None:
    """Deliver the follow-up to a notify.* service or persistent_notification.create."""
    domain, service = target.split(".", 1)
    data: dict[str, Any] = {"message": message}
    if domain == "persistent_notification":
        data["title"] = "Cooper — location"
    await hass.services.async_call(domain, service, data, blocking=False)


async def _watch_and_notify(
    hass: HomeAssistant,
    person: str,
    watch_entity: str,
    place_entity: str,
    baseline_updated: Any,
    notify_target: str,
) -> None:
    """Wait (≤ LOCATE_TIMEOUT) for a fresh fix to land, then notify the caller."""
    loop = hass.loop
    fut: asyncio.Future = loop.create_future()

    @callback
    def _listener(event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        # Any update strictly newer than our baseline means a fresh fix arrived.
        if baseline_updated is None or new_state.last_updated > baseline_updated:
            if not fut.done():
                fut.set_result(new_state)

    unsub = async_track_state_change_event(hass, [watch_entity], _listener)
    try:
        await asyncio.wait_for(fut, timeout=LOCATE_TIMEOUT)
    except asyncio.TimeoutError:
        await _notify(
            hass,
            notify_target,
            f"Still no fresh location fix for {person.title()} after "
            f"{LOCATE_TIMEOUT // 60} minutes.",
        )
        return
    except Exception as err:  # noqa: BLE001 - background task must never crash silently
        LOGGER.warning("cooper: location watch for %s failed: %s", person, err)
        return
    finally:
        unsub()

    place = hass.states.get(place_entity)
    where = place.state if place and place.state not in (None, "unknown", "") else None
    message = (
        f"{person.title()} is now at: {where}."
        if where
        else f"Got a fresh location fix for {person.title()}."
    )
    await _notify(hass, notify_target, message)


class RefreshLocationTool(CooperTool):
    """Force a fresh GPS fix for one or more family members, then follow up by notification."""

    name = "refresh_location"
    description = (
        "Force a fresh, current GPS fix for one or more family members (kunal, sara, shuchi, "
        "vir). This is ASYNCHRONOUS: it triggers the refresh and returns immediately, then "
        "sends a notification a few seconds to ~2 minutes later when the fresh location lands "
        "(or times out). Use it when the user wants someone's CURRENT location, not the "
        "last-known one. Pass notify_target as the notify.* service for the person asking "
        "(e.g. notify.mobile_app_pixel_10_pro) so the follow-up reaches them; if omitted it "
        "goes to the Home Assistant notification bell. Tell the user you've started it and "
        "will follow up — do not claim to have the new location yet. If the user says not to "
        "refresh, don't call this."
    )
    parameters = vol.Schema(
        {
            vol.Required("people"): vol.All(
                [vol.In(list(LOCATE_TARGETS))], vol.Length(min=1)
            ),
            vol.Optional("notify_target"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        people = list(tool_input.tool_args["people"])
        notify_target = str(
            tool_input.tool_args.get("notify_target") or LOCATE_DEFAULT_NOTIFY
        )

        started: list[str] = []
        problems: list[dict[str, str]] = []
        for person in people:
            cfg = LOCATE_TARGETS[person]
            watch_entity = cfg["watch"]
            if hass.states.get(watch_entity) is None:
                problems.append(
                    {"person": person, "reason": f"'{watch_entity}' is unavailable"}
                )
                continue
            try:
                await _trigger_refresh(hass, cfg["refresh"])
            except Exception as err:  # noqa: BLE001 - report, keep going for others
                problems.append({"person": person, "reason": str(err)})
                continue

            baseline = hass.states.get(watch_entity)
            baseline_updated = baseline.last_updated if baseline else None
            hass.async_create_background_task(
                _watch_and_notify(
                    hass,
                    person,
                    watch_entity,
                    cfg["place"],
                    baseline_updated,
                    notify_target,
                ),
                name=f"cooper_locate_{person}",
            )
            started.append(person)

        return {
            "status": "refreshing" if started else "failed",
            "started": started,
            "problems": problems,
            "follow_up": notify_target,
            "note": (
                "Refresh started; a fresh location will be sent to the notify target within "
                f"~{LOCATE_TIMEOUT // 60} minutes. Do not state the new location yet."
            ),
        }
