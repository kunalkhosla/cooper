"""Location tool: force a fresh GPS fix for the family and report back.

Forcing a fresh fix is asynchronous — the watch/phone takes up to a couple of minutes to
report back. This tool supports two shapes:

* DEFAULT (``wait=false``): trigger the refresh, spawn an in-integration **background task**
  (``hass.async_create_background_task``) that watches the person's location sensor for a fresh
  update for up to ``LOCATE_TIMEOUT`` seconds and then notifies, and return instantly.
* ``wait=true``: block the turn briefly (≤ ``LOCATE_WAIT_TIMEOUT``) and return the fresh place
  INLINE if it lands in that window — so the user who says "I'll wait, no notification" gets the
  answer in the conversation. Anything still not in by then falls back to the background notify.

Follow-up delivery: the notification goes to the **device the user is speaking from** (resolved
from the conversation context's ``device_id`` → that device's ``notify.mobile_app_*`` service),
never a hard-coded list. An explicit ``notify`` argument overrides that; a configured option is a
last resort. If none can be determined, the tool asks the model to ask the user.

This keeps everything inside the integration: it replaces the four HA-side ``script.locate_*``
scripts and needs no automation or ``input_boolean`` (issue #1). Background tasks survive the end
of the conversation turn, so a follow-up still fires once the fix arrives.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify
from homeassistant.util.json import JsonObjectType

from ..const import (
    LOCATE_DEFAULT_NOTIFY,
    LOCATE_TARGETS,
    LOCATE_TIMEOUT,
    LOCATE_WAIT_TIMEOUT,
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


# --- follow-up target resolution: prefer the device the user is speaking from ---------------

def _mobile_notifies(hass: HomeAssistant) -> list[str]:
    """All registered companion-app notify services, as 'notify.mobile_app_*' strings."""
    services = hass.services.async_services().get("notify", {})
    return [f"notify.{name}" for name in services if name.startswith("mobile_app_")]


def _origin_notify(hass: HomeAssistant, llm_context: llm.LLMContext) -> str | None:
    """The companion-app notify service for the device this conversation came from.

    Maps the context's ``device_id`` to that device's ``notify.mobile_app_<slug>`` (the
    mobile_app integration names the service from the device name). Returns None for a voice
    satellite or anything without a companion app — the caller then asks the user.
    """
    device_id = getattr(llm_context, "device_id", None)
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for name in (device.name_by_user, device.name):
        if not name:
            continue
        service = f"mobile_app_{slugify(name)}"
        if hass.services.has_service("notify", service):
            return f"notify.{service}"
    return None


def _resolve_notify_arg(hass: HomeAssistant, value: str) -> str | None:
    """Resolve a model-supplied notify target (service or device name) to a real service."""
    value = value.strip()
    if not value:
        return None
    candidates = [value]
    if not value.startswith("notify."):
        candidates += [f"notify.{value}", f"notify.mobile_app_{slugify(value)}"]
    for cand in candidates:
        if cand.startswith("notify.") and hass.services.has_service(
            "notify", cand.split(".", 1)[1]
        ):
            return cand
    return None


async def _notify_all(hass: HomeAssistant, targets: list[str], message: str) -> None:
    """Deliver the follow-up to every notify.* / persistent_notification target."""
    for target in targets:
        domain, service = target.split(".", 1)
        data: dict[str, Any] = {"message": message}
        if domain == "persistent_notification":
            data["title"] = "Cooper — location"
        try:
            await hass.services.async_call(domain, service, data, blocking=False)
        except Exception as err:  # noqa: BLE001 - one bad target must not lose the rest
            LOGGER.warning("cooper: location notify to %s failed: %s", target, err)


def _place_of(hass: HomeAssistant, place_entity: str) -> str | None:
    """The readable place for a person, or None if the sensor has no usable state."""
    place = hass.states.get(place_entity)
    if place and place.state not in (None, "unknown", "unavailable", ""):
        return place.state
    return None


async def _wait_for_fix(
    hass: HomeAssistant, watch_entity: str, baseline_updated: Any, timeout: float
) -> bool:
    """Block ≤ ``timeout`` for a state update on ``watch_entity`` newer than the baseline."""
    loop = hass.loop
    fut: asyncio.Future = loop.create_future()

    @callback
    def _listener(event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if baseline_updated is None or new_state.last_updated > baseline_updated:
            if not fut.done():
                fut.set_result(new_state)

    unsub = async_track_state_change_event(hass, [watch_entity], _listener)
    try:
        await asyncio.wait_for(fut, timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    except Exception as err:  # noqa: BLE001 - never crash the turn/background task
        LOGGER.warning("cooper: location watch for %s failed: %s", watch_entity, err)
        return False
    finally:
        unsub()


async def _watch_and_notify(
    hass: HomeAssistant,
    person: str,
    watch_entity: str,
    place_entity: str,
    baseline_updated: Any,
    notify_targets: list[str],
    timeout: float = LOCATE_TIMEOUT,
) -> None:
    """Wait for a fresh fix to land, then notify the caller (or report a timeout)."""
    if not await _wait_for_fix(hass, watch_entity, baseline_updated, timeout):
        await _notify_all(
            hass,
            notify_targets,
            f"Still no fresh location fix for {person.title()} after a couple of minutes.",
        )
        return
    where = _place_of(hass, place_entity)
    message = (
        f"{person.title()} is now at: {where}."
        if where
        else f"Got a fresh location fix for {person.title()}."
    )
    await _notify_all(hass, notify_targets, message)


class RefreshLocationTool(CooperTool):
    """Force a fresh GPS fix for one or more family members and report back."""

    name = "refresh_location"
    description = (
        "Force a fresh, current GPS fix for one or more family members (kunal, sara, shuchi, "
        "vir). Use it ONLY when the user wants someone's CURRENT location, not the last-known "
        "one — and never unprompted. You MUST confirm first: ask one yes/no question ('Want me "
        "to request a fresh location fix?') and only then call this with confirm=true. Do not "
        "set confirm=true on a plain 'where is X' — that is not permission to refresh. "
        "WAITING: by default it returns immediately and the fresh fix is delivered later as a "
        "notification. If the user says they'll wait, wants the answer right now in the "
        "conversation, or doesn't want a notification, pass wait=true — it then blocks briefly "
        "(~45s) and returns status 'located' with the place inline if it lands in time; tell them "
        "that location directly, and only fall back to 'I'll follow up' for anyone whose fix "
        "didn't arrive in that window. FOLLOW-UP TARGET: notifications go to the device the user "
        "is speaking from automatically — you do NOT need to choose. If the tool can't tell which "
        "device that is, it returns status 'needs_notify_target' with the available devices; ask "
        "the user which one and call again with notify=<that device or service>."
    )
    parameters = vol.Schema(
        {
            vol.Required("people"): vol.All(
                [vol.In(list(LOCATE_TARGETS))], vol.Length(min=1)
            ),
            vol.Optional("wait", default=False): bool,
            vol.Optional("notify"): str,
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
        wait = bool(tool_input.tool_args.get("wait", False))
        notify_arg = str(tool_input.tool_args.get("notify") or "").strip()

        def _audit(decision: str, result: Any = None) -> None:
            audit(
                hass,
                runtime,
                {
                    "tool": self.name,
                    "base": self.name,
                    "args": {"people": people, "wait": wait},
                    "tier": "CONFIRM",
                    "decision": decision,
                    "observe": runtime.observe_mode,
                    "kill": runtime.kill_switch,
                    **({"result": result} if result is not None else {}),
                },
            )

        if runtime.kill_switch:
            _audit("refused_kill_switch")
            return {
                "status": "refused",
                "reason": "Cooper's kill switch is on; no location refresh was triggered.",
            }
        if not confirmed:
            _audit("needs_confirmation")
            return {
                "status": "needs_confirmation",
                "summary": f"request a fresh location fix for {', '.join(people)}",
                "instructions": (
                    "Ask the user a single yes/no question to confirm the refresh. If they "
                    "agree, call refresh_location again with the same people and confirm=true."
                ),
            }

        # Resolve where a follow-up notification would go: an explicit arg wins, then the
        # device this conversation came from, then a configured option as a last resort.
        target: str | None = None
        if notify_arg:
            target = _resolve_notify_arg(hass, notify_arg)
        if target is None:
            target = _origin_notify(hass, llm_context)
        configured = list(runtime.location_notify)
        notify_targets = [target] if target else (configured or [])

        # If we'll need to notify (i.e. not waiting, or waiting may not catch it) and we have
        # no idea where to send it, ask the user first — don't fire a refresh into the void.
        if not notify_targets and not wait:
            _audit("needs_notify_target")
            return {
                "status": "needs_notify_target",
                "reason": (
                    "I couldn't tell which device you're on, so I don't know where to send the "
                    "location follow-up."
                ),
                "available_targets": _mobile_notifies(hass),
                "instructions": (
                    "Ask the user which device should get the result (or whether they'd rather "
                    "wait), then call again with notify=<device or service> — or with wait=true."
                ),
            }

        triggered: list[tuple[str, dict[str, str], Any]] = []
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
            triggered.append((person, cfg, baseline.last_updated if baseline else None))

        located: list[dict[str, Any]] = []
        pending: list[str] = []

        if wait and triggered:
            results = await asyncio.gather(
                *[
                    _wait_for_fix(hass, cfg["watch"], base, LOCATE_WAIT_TIMEOUT)
                    for (_, cfg, base) in triggered
                ]
            )
            remaining = max(0.0, LOCATE_TIMEOUT - LOCATE_WAIT_TIMEOUT)
            for (person, cfg, base), got in zip(triggered, results):
                if got:
                    located.append(
                        {"person": person, "where": _place_of(hass, cfg["place"])}
                    )
                    continue
                pending.append(person)
                if notify_targets and remaining:
                    hass.async_create_background_task(
                        _watch_and_notify(
                            hass, person, cfg["watch"], cfg["place"], base,
                            notify_targets, remaining,
                        ),
                        name=f"cooper_locate_{person}",
                    )
        else:
            for person, cfg, base in triggered:
                pending.append(person)
                hass.async_create_background_task(
                    _watch_and_notify(
                        hass, person, cfg["watch"], cfg["place"], base, notify_targets
                    ),
                    name=f"cooper_locate_{person}",
                )

        started = [d["person"] for d in located] + pending
        status = "located" if located else ("refreshing" if pending else "failed")
        will_notify = bool(notify_targets) and bool(pending)
        if located and pending:
            note = (
                "State the located place(s) now. The rest are still coming"
                + (" and will arrive as a notification." if will_notify else ".")
            )
        elif located:
            note = "Fresh fix landed — tell the user the located place(s) now."
        elif pending:
            note = (
                "Still pending; the fresh location will arrive as a notification shortly."
                if will_notify
                else "Refresh triggered but I have nowhere to send the follow-up — ask the user "
                "where to send it, or have them wait."
            )
        else:
            note = "No refresh could be started; see problems."

        _audit(
            "located" if located else ("confirmed" if pending else "no_targets"),
            {"located": located, "pending": pending, "follow_up": notify_targets},
        )
        return {
            "status": status,
            "located": located,
            "still_pending": pending,
            "started": started,
            "problems": problems,
            "follow_up": notify_targets if will_notify else [],
            "note": note,
        }
