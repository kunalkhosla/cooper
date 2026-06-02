"""Proactivity tools: let Cooper watch the home and wake itself up.

A "watch" is just a native HA automation whose only action calls the
``cooper.proactive_check`` service. When it fires, the service re-enters the same agent
loop (same wrapped tools, same guardrails) with a proactive seed. This delivers full
proactivity on every install type with no add-on. The per-watch ``min_interval_minutes``
becomes the cooldown the service enforces, so a noisy trigger cannot spam the user.

create_watch / remove_watch are confirm-tier; list_watches is read-only.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..const import DOMAIN, SERVICE_PROACTIVE_CHECK
from ..guardrails import CooperTool, audit
from ..validation import validate_config
from . import autoconfig
from ._guard import precheck_write

_PROACTIVE_SERVICE = f"{DOMAIN}.{SERVICE_PROACTIVE_CHECK}"


def _references_proactive_check(config: dict[str, Any]) -> bool:
    actions = config.get("action") or config.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    for action in actions:
        if isinstance(action, dict) and (
            action.get("service") == _PROACTIVE_SERVICE
            or action.get("action") == _PROACTIVE_SERVICE
        ):
            return True
    return False


class CreateWatchTool(CooperTool):
    """Author a watch automation that wakes Cooper when something happens."""

    name = "create_watch"
    description = (
        "Set up a proactive watch: a native automation that wakes you up when a condition "
        "occurs, so you can decide whether to tell the user something. Provide a Home "
        "Assistant trigger config and a short 'reason' describing why you should look. "
        "min_interval_minutes throttles how often the watch can wake you. This changes the "
        "home, so it needs confirmation unless confirm=true."
    )
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Required("trigger"): dict,
            vol.Required("reason"): str,
            vol.Optional("context_entities"): [str],
            vol.Optional("min_interval_minutes", default=15): vol.All(
                int, vol.Range(min=1, max=1440)
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
        args = tool_input.tool_args
        name = str(args["name"])
        reason = str(args["reason"])
        interval = int(args.get("min_interval_minutes", 15))
        confirmed = bool(args.get("confirm", False))

        data: dict[str, Any] = {"reason": reason, "cooldown_minutes": interval}
        if args.get("context_entities"):
            data["context_entities"] = list(args["context_entities"])

        config: dict[str, Any] = {
            "alias": f"Cooper watch: {name}",
            "trigger": args["trigger"],
            "action": [{"service": _PROACTIVE_SERVICE, "data": data}],
            "mode": "single",
        }

        ok, errors = validate_config(hass, "automation", config)
        if not ok:
            return {
                "status": "invalid",
                "errors": errors,
                "instructions": "Fix the trigger/config and call create_watch again.",
            }

        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=f"create proactive watch '{name}'",
            args={"name": name, "reason": reason},
            confirmed=confirmed,
        )
        if blocked is not None:
            return blocked

        entity_id = await autoconfig.async_save_automation(hass, config)
        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"name": name},
                "tier": "CONFIRM",
                "decision": "executed",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
                "result": entity_id,
            },
        )
        return {"status": "created", "entity_id": entity_id, "name": name}


class ListWatchesTool(CooperTool):
    """List the proactive watches Cooper has created."""

    name = "list_watches"
    description = "List the proactive watches you have set up, with what each one watches for."
    parameters = vol.Schema({})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        watches = []
        for config in await autoconfig.async_list_authored_automations(hass):
            if not _references_proactive_check(config):
                continue
            watches.append(
                {
                    "entity_id": f"automation.{config.get('id')}",
                    "name": config.get("alias", ""),
                    "trigger": config.get("trigger") or config.get("triggers"),
                }
            )
        return {"watches": watches}


class RemoveWatchTool(CooperTool):
    """Remove a proactive watch."""

    name = "remove_watch"
    description = (
        "Remove a proactive watch you previously created. Pass its automation entity_id "
        "(from list_watches). Needs confirmation unless confirm=true."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
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
        entity_id = str(tool_input.tool_args["entity_id"])
        confirmed = bool(tool_input.tool_args.get("confirm", False))
        config_id = entity_id.split(".", 1)[-1]

        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=f"remove watch '{entity_id}'",
            args={"entity_id": entity_id},
            confirmed=confirmed,
        )
        if blocked is not None:
            return blocked

        removed = await autoconfig.async_remove_automation(hass, config_id)
        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"entity_id": entity_id},
                "tier": "CONFIRM",
                "decision": "executed" if removed else "not_found",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
            },
        )
        return {"status": "removed" if removed else "not_found", "entity_id": entity_id}
