"""Generic, guarded "call a service on named entities" tool.

The fix for Cooper guessing entity_ids: plain on/off goes through intents (which resolve
names internally and are tiered), but a service that takes PARAMETERS — e.g.
``rachio.start_multiple_zone_schedule`` with a duration — forces the model to write an
``entity_id`` it doesn't reliably know. This tool closes that gap generically: the model
names the entities, we resolve them the same way Assist does
(``intent.async_match_targets``), and call the service with the real ids. Works for any
integration, so it retires per-home wrapper scripts over time.

Because it can call arbitrary services it is the most powerful tool here, so it is fenced:
- the kill switch / observe mode / a confirmation gate apply (via ``precheck_write``),
- a denylist of infrastructure service domains is refused, and
- it refuses to act on locks, alarms, or garage/gate/door covers — those must go through
  the normal tiered commands, never this bypass.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from ..const import AUTHORED_PREFIX
from ..guardrails import CooperTool, audit
from ._guard import precheck_write

# Service domains this tool will never call (use proper flows / never via a generic caller).
_DENY_SERVICE_DOMAINS = frozenset(
    {"update", "backup", "hassio", "homeassistant", "recorder", "cloud", "system_log"}
)

# Services on automation/script that disable a standing item, as opposed to running one
# (turn_on/trigger) — see the domain note in _is_risky below.
_DISABLING_SERVICES = frozenset({"turn_off", "toggle"})

# 'data' is for service PARAMETERS only, never a target — see the check in async_call.
_TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")


def _is_risky(state: Any, svc: str) -> bool:
    """Locks, alarms, and garage/gate/door covers must NOT be driven via this bypass.

    Same for disabling (turn_off/toggle) an automation or script this tool didn't
    author itself: that's a standing change to the user's own automation/scheduling
    logic (e.g. a safety cutoff), not a one-shot device action — see the matching rule
    in guardrails.compute_tier, which this mirrors for the tools that path doesn't
    cover (CooperTool subclasses tier themselves and never reach compute_tier).
    """
    domain = state.domain
    if domain in ("lock", "alarm_control_panel"):
        return True
    if domain in ("cover", "valve"):
        return state.attributes.get("device_class") in ("garage", "gate", "door")
    if domain in ("automation", "script") and svc in _DISABLING_SERVICES:
        object_id = state.entity_id.split(".", 1)[-1]
        config_id = str(state.attributes.get("id") or "")
        authored = object_id.startswith(AUTHORED_PREFIX) or config_id.startswith(
            AUTHORED_PREFIX
        )
        return not authored
    return False


def _resolve(hass, names, area, domain, assistant) -> list[Any]:
    """Resolve friendly name(s)/area to entity states the way Assist does."""
    domains = [domain] if domain else None
    states: list[Any] = []
    seen: set[str] = set()
    targets = [{"name": n} for n in (names or [])] or ([{"area_name": area}] if area else [])
    for t in targets:
        constraints = intent.MatchTargetsConstraints(
            name=t.get("name"),
            area_name=t.get("area_name") or (area if t.get("name") else None),
            domains=domains,
            assistant=assistant,
        )
        try:
            result = intent.async_match_targets(hass, constraints)
        except Exception:  # noqa: BLE001 - matching is best-effort
            continue
        if result.is_match:
            for s in result.states:
                if s.entity_id not in seen:
                    seen.add(s.entity_id)
                    states.append(s)
    return states


class CallServiceTool(CooperTool):
    """Call any (non-risky) HA service on entities resolved by name — no id guessing."""

    name = "call_service"
    description = (
        "Call a Home Assistant service that takes parameters on entities you name — use this "
        "when a plain turn-on/off can't express the request, e.g. running sprinkler zones for a "
        "set number of minutes via rachio.start_multiple_zone_schedule (duration in minutes), or "
        "any integration service with options. Pass service as 'domain.service', the target "
        "entities by friendly name in 'names' (and/or an 'area'), and any extra parameters in "
        "'data' (e.g. {\"duration\": 15}) — 'data' is for parameters only, never a target; an "
        "entity_id/device_id/area_id there is refused, even if you already know the real id from "
        "list_automations or elsewhere — name it in 'names' instead so it's resolved and "
        "safety-checked. Refuses if nothing resolves (never calls a service with no target — that "
        "would hit the WHOLE domain), and refuses locks, alarms, garage/gate/door covers, and "
        "disabling an automation/script you didn't author (use the normal command for those). "
        "Changes the home, so it needs confirmation unless confirm=true."
    )
    parameters = vol.Schema(
        {
            vol.Required("service"): str,
            vol.Optional("names"): [str],
            vol.Optional("area"): str,
            vol.Optional("domain"): str,
            vol.Optional("data"): dict,
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
        args = tool_input.tool_args
        service = str(args["service"])
        if "." not in service:
            return {"status": "error", "reason": "service must be 'domain.service'."}
        domain, svc = service.split(".", 1)
        if domain in _DENY_SERVICE_DOMAINS:
            return {
                "status": "refused",
                "reason": f"The '{domain}' domain can't be called this way.",
            }

        names = list(args.get("names") or [])
        area = args.get("area")
        data = dict(args.get("data") or {})
        confirmed = bool(args.get("confirm", False))

        # The model naming entities via 'names'/'area' is what gets resolved and
        # risk-checked below; without this check a target key smuggled into 'data'
        # instead (the model already knows real ids from list_automations/
        # get_live_context) would reach hass.services.async_call untouched, bypassing
        # _is_risky entirely.
        smuggled = [k for k in _TARGET_KEYS if k in data]
        if smuggled:
            return {
                "status": "error",
                "reason": (
                    f"'data' can't carry {', '.join(smuggled)} — that's a target, not a "
                    "parameter. Pass the entity by friendly name in 'names' (or 'area') "
                    "so it's resolved and safety-checked properly."
                ),
            }

        states = _resolve(hass, names, area, args.get("domain"), llm_context.assistant)
        if not states:
            return {
                "status": "error",
                "reason": (
                    f"Couldn't match {names or area or 'anything'} to an exposed entity — "
                    "refusing to call a service with no resolved target (that would hit "
                    "every entity in the domain, not just what you meant). Re-check the "
                    "exact name, or look it up by area/domain — don't guess an id."
                ),
            }
        risky = [s.name for s in states if _is_risky(s, svc)]
        if risky:
            return {
                "status": "refused",
                "reason": (
                    f"{', '.join(risky)} can't be driven through a generic service call "
                    "(lock/alarm/garage-type entity, or disabling an automation/script you "
                    "didn't author) — use the normal command so safety/confirmation applies."
                ),
            }

        entity_ids = [s.entity_id for s in states]
        summary = f"call {service}" + (f" on {', '.join(entity_ids)}" if entity_ids else "")
        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=summary,
            args={"service": service, "entity_id": entity_ids, "data": data},
            confirmed=confirmed,
        )
        if blocked is not None:
            return blocked

        # Belt-and-suspenders: states/entity_ids are non-empty by this point (checked
        # above), but this call would silently hit every entity in the domain if that
        # invariant were ever violated by a future edit — never let that through.
        if not entity_ids:
            return {"status": "error", "reason": "Internal: no target entity resolved."}
        call_data: dict[str, Any] = dict(data)
        call_data["entity_id"] = entity_ids
        try:
            await hass.services.async_call(domain, svc, call_data, blocking=True)
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"Service call failed: {err}"}

        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"service": service, "entity_id": entity_ids},
                "tier": "CONFIRM",
                "decision": "executed",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
            },
        )
        return {"status": "done", "service": service, "entities": entity_ids}
