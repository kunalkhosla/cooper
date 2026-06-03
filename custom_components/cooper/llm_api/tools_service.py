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

from ..guardrails import CooperTool, audit
from ._guard import precheck_write

# Service domains this tool will never call (use proper flows / never via a generic caller).
_DENY_SERVICE_DOMAINS = frozenset(
    {"update", "backup", "hassio", "homeassistant", "recorder", "cloud", "system_log"}
)


def _is_risky(state: Any) -> bool:
    """Locks, alarms, and garage/gate/door covers must NOT be driven via this bypass."""
    domain = state.domain
    if domain in ("lock", "alarm_control_panel"):
        return True
    if domain in ("cover", "valve"):
        return state.attributes.get("device_class") in ("garage", "gate", "door")
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
        "'data' (e.g. {\"duration\": 15}). The tool resolves the names to real entity_ids — never "
        "guess or invent ids, just name the things. It refuses locks, alarms, and garage/gate/door "
        "covers (use the normal command for those). Changes the home, so it needs confirmation "
        "unless confirm=true."
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

        states = _resolve(hass, names, area, args.get("domain"), llm_context.assistant)
        if (names or area) and not states:
            return {
                "status": "error",
                "reason": (
                    f"Couldn't match {names or area} to any exposed entity. Re-check the exact "
                    "name, or look it up by area/domain — don't guess an id."
                ),
            }
        risky = [s.name for s in states if _is_risky(s)]
        if risky:
            return {
                "status": "refused",
                "reason": (
                    f"{', '.join(risky)} is a lock/alarm/garage-type entity — I won't drive that "
                    "through a generic service call; use the normal command so safety applies."
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

        call_data: dict[str, Any] = dict(data)
        if entity_ids:
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
