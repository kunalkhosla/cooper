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
from ..guardrails import CooperTool, audit

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


async def _notify_all(
    hass: HomeAssistant, targets: list[str], message: str
) -> None:
    """Deliver the follow-up to every configured notify.* / persistent_notification target."""
    for target in targets:
        domain, service = target.split(".", 1)
        data: dict[str, Any] = {"message": message}
        if domain == "persistent_notification":
            data["title"] = "Cooper — location"
        try:
            await hass.services.async_call(domain, service, data, blocking=False)
        except Exception as err:  # noqa: BLE001 - one bad target must not lose the rest
            LOGGER.warning("cooper: location notify to %s failed: %s", target, err)


async def _watch_and_notify(
    hass: HomeAssistant,
    person: str,
    watch_entity: str,
    place_entity: str,
    baseline_updated: Any,
    notify_targets: list[str],
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
        await _notify_all(
            hass,
            notify_targets,
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
    await _notify_all(hass, notify_targets, message)


class RefreshLocationTool(CooperTool):
    """Force a fresh GPS fix for one or more family members, then follow up by notification."""

    name = "refresh_location"
    description = (
        "Force a fresh, current GPS fix for one or more family members (kunal, sara, shuchi, "
        "vir). Use it ONLY when the user wants someone's CURRENT location, not the last-known "
        "one — and never unprompted. You MUST confirm first: ask one yes/no question ('Want me "
        "to request a fresh location fix?') and only then call this with confirm=true. Do not "
        "set confirm=true on a plain 'where is X' — that is not permission to refresh. The tool "
        "refuses an unconfirmed call. It is ASYNCHRONOUS: once confirmed it triggers the refresh "
        "and returns immediately, then sends a notification a few seconds to ~2 minutes later "
        "when the fresh location lands (or times out). So tell the user you've started it and "
        "will follow up — do NOT claim to have the new location yet. The follow-up goes to the "
        "devices configured in Cooper's options; you don't choose where."
    )
    parameters = vol.Schema(
        {
            vol.Required("people"): vol.All(
                [vol.In(list(LOCATE_TARGETS))], vol.Length(min=1)
            ),
            vol.Optional("confirm", default=False): bool,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        people = list(tool_input.tool_args["people"])
        confirmed = bool(tool_input.tool_args.get("confirm", False))

        # Kill switch hard-stops everything; otherwise this is a benign read-style refresh
        # that we gate only on an explicit confirmation (no observe-mode block).
        if runtime.kill_switch:
            audit(
                hass,
                runtime,
                {
                    "tool": self.name,
                    "base": self.name,
                    "args": {"people": people},
                    "tier": "CONFIRM",
                    "decision": "refused_kill_switch",
                    "observe": runtime.observe_mode,
                    "kill": runtime.kill_switch,
                },
            )
            return {
                "status": "refused",
                "reason": "Cooper's kill switch is on; no location refresh was triggered.",
            }
        if not confirmed:
            audit(
                hass,
                runtime,
                {
                    "tool": self.name,
                    "base": self.name,
                    "args": {"people": people},
                    "tier": "CONFIRM",
                    "decision": "needs_confirmation",
                    "observe": runtime.observe_mode,
                    "kill": runtime.kill_switch,
                },
            )
            return {
                "status": "needs_confirmation",
                "summary": f"request a fresh location fix for {', '.join(people)}",
                "instructions": (
                    "Ask the user a single yes/no question to confirm the refresh. If they "
                    "agree, call refresh_location again with the same people and confirm=true."
                ),
            }

        notify_targets = list(runtime.location_notify) or [LOCATE_DEFAULT_NOTIFY]

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
                    notify_targets,
                ),
                name=f"cooper_locate_{person}",
            )
            started.append(person)

        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"people": people},
                "tier": "CONFIRM",
                "decision": "confirmed" if started else "no_targets",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
                "result": {"started": started, "follow_up": notify_targets},
            },
        )
        return {
            "status": "refreshing" if started else "failed",
            "started": started,
            "problems": problems,
            "follow_up": notify_targets,
            "note": (
                "Refresh started; a fresh location will be sent to the configured notify "
                f"device(s) within ~{LOCATE_TIMEOUT // 60} minutes. Do not state the new "
                "location yet."
            ),
        }
