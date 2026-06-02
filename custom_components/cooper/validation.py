"""Deterministic validation for Cooper-authored automations and scripts.

Pure functions (no I/O beyond reading registries/state). Every referenced entity,
service, area and floor must exist before Cooper is allowed to save config. On failure
we return structured, human-readable errors that the model can read back and fix on its
next loop iteration.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, split_entity_id
from homeassistant.helpers import area_registry as ar, floor_registry as fr

# Action keys that are control flow / helpers, not service calls or entity targets.
_FLOW_KEYS = frozenset(
    {
        "delay",
        "wait_template",
        "wait_for_trigger",
        "repeat",
        "choose",
        "if",
        "then",
        "else",
        "parallel",
        "sequence",
        "variables",
        "stop",
        "event",
    }
)


def _walk(obj: Any):
    """Yield every dict found anywhere in a nested structure."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _collect_entity_ids(config: Any) -> set[str]:
    found: set[str] = set()
    for node in _walk(config):
        for key in ("entity_id", "entity_ids"):
            value = node.get(key)
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, list):
                found.update(v for v in value if isinstance(v, str))
        # Service-call targets can nest entity_id under "target".
        target = node.get("target")
        if isinstance(target, dict):
            tval = target.get("entity_id")
            if isinstance(tval, str):
                found.add(tval)
            elif isinstance(tval, list):
                found.update(v for v in tval if isinstance(v, str))
    # Drop templates and the "all"/"none" sentinels.
    return {e for e in found if "{" not in e and e not in ("all", "none")}


def _collect_services(config: Any) -> set[str]:
    found: set[str] = set()
    for node in _walk(config):
        for key in ("service", "action"):
            value = node.get(key)
            # "action" is only a service ref when it looks like "<domain>.<service>"
            # and the node is not a script-action wrapper using other flow keys.
            if (
                isinstance(value, str)
                and "." in value
                and "{" not in value
                and not (key == "action" and _FLOW_KEYS & node.keys())
            ):
                found.add(value)
    return found


def _collect_targets(config: Any, key: str) -> set[str]:
    found: set[str] = set()
    for node in _walk(config):
        for source in (node, node.get("target") if isinstance(node.get("target"), dict) else {}):
            value = source.get(key)
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, list):
                found.update(v for v in value if isinstance(v, str))
    return {v for v in found if "{" not in v}


def validate_config(
    hass: HomeAssistant, kind: str, config: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Validate an automation or script dict. Returns (ok, errors)."""
    errors: list[str] = []

    if kind not in ("automation", "script"):
        return False, [f"Unknown config kind '{kind}'."]

    # Structural sanity.
    if kind == "automation":
        if not (config.get("trigger") or config.get("triggers")):
            errors.append("Automation has no trigger.")
        if not (config.get("action") or config.get("actions")):
            errors.append("Automation has no action.")
    else:  # script
        if not (config.get("sequence") or config.get("actions")):
            errors.append("Script has no sequence of actions.")

    # Entities must exist in the state machine.
    for entity_id in sorted(_collect_entity_ids(config)):
        if "." not in entity_id:
            errors.append(f"'{entity_id}' is not a valid entity id.")
        elif hass.states.get(entity_id) is None:
            errors.append(f"Entity '{entity_id}' does not exist.")

    # Services must be registered.
    for service in sorted(_collect_services(config)):
        try:
            domain, name = split_entity_id(service)
        except ValueError:
            errors.append(f"'{service}' is not a valid service name.")
            continue
        if not hass.services.has_service(domain, name):
            errors.append(f"Service '{service}' is not available.")

    # Areas / floors must exist.
    area_reg = ar.async_get(hass)
    known_areas = {a.id for a in area_reg.areas.values()} | {
        a.name for a in area_reg.areas.values()
    }
    for area in sorted(_collect_targets(config, "area_id")):
        if area not in known_areas:
            errors.append(f"Area '{area}' does not exist.")

    floor_reg = fr.async_get(hass)
    known_floors = {f.floor_id for f in floor_reg.floors.values()} | {
        f.name for f in floor_reg.floors.values()
    }
    for floor in sorted(_collect_targets(config, "floor_id")):
        if floor not in known_floors:
            errors.append(f"Floor '{floor}' does not exist.")

    return (not errors), errors
