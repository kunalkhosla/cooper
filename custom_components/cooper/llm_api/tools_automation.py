"""Automation/script authoring tool.

Writes native HA automations and scripts so behaviour survives restarts and is editable
by the user. Scripts let Cooper express sequenced/timed jobs (e.g. run sprinkler zone 1
for 10 min, then zone 2). Confirm-tier: gated by ``precheck_write`` and validated
deterministically by ``validation.validate_config`` before anything is persisted.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool, audit
from ..validation import validate_config
from . import autoconfig
from ._guard import precheck_write


class AuthorAutomationTool(CooperTool):
    """Create a native HA automation or script from a config dict."""

    name = "author_automation"
    description = (
        "Create a native Home Assistant automation or script. Use 'automation' for things "
        "that should run on a trigger (time, state change, event). Use 'script' for a named, "
        "ordered sequence of actions to run on demand, including TIMED steps with delays — "
        "this is the right tool for sequenced jobs like running sprinkler zone 5 for 1 minute "
        "then zone 4 for 2 minutes (turn a switch on, add a `delay`, turn it off, then the "
        "next). For such a request, author ONE script with the full delay sequence (always "
        "include the explicit turn-off after each delay) and set run_now=true to start it "
        "immediately — it then runs reliably to completion even after this conversation ends. "
        "Do not try to run timed sequences by flipping switches yourself. The config is "
        "validated before saving; if rejected, read the errors and call again with a fix. "
        "This changes the home, so it needs confirmation unless confirm=true."
    )
    parameters = vol.Schema(
        {
            vol.Required("kind"): vol.In(["automation", "script"]),
            vol.Required("alias"): str,
            vol.Required("config"): dict,
            vol.Optional("run_now", default=False): bool,
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
        kind = str(tool_input.tool_args["kind"])
        alias = str(tool_input.tool_args["alias"])
        config = dict(tool_input.tool_args["config"])
        confirmed = bool(tool_input.tool_args.get("confirm", False))
        run_now = bool(tool_input.tool_args.get("run_now", False))

        config.setdefault("alias", alias)

        ok, errors = validate_config(hass, kind, config)
        if not ok:
            return {
                "status": "invalid",
                "errors": errors,
                "instructions": "Fix these problems and call author_automation again.",
            }

        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=f"create {kind} '{alias}'",
            args={"kind": kind, "alias": alias},
            confirmed=confirmed,
        )
        if blocked is not None:
            return blocked

        if kind == "automation":
            entity_id = await autoconfig.async_save_automation(hass, config)
        else:
            entity_id = await autoconfig.async_save_script(hass, alias, config)

        # We are past precheck_write (kill off, observe off, confirmed), so it is safe to
        # start the thing we just created. A script runs its full delayed sequence to
        # completion independently of this conversation; an automation gets triggered once.
        started = False
        if run_now:
            run_service = "turn_on" if kind == "script" else "trigger"
            run_domain = "script" if kind == "script" else "automation"
            await hass.services.async_call(
                run_domain, run_service, {"entity_id": entity_id}, blocking=False
            )
            started = True

        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"kind": kind, "alias": alias, "run_now": run_now},
                "tier": "CONFIRM",
                "decision": "executed",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
                "result": entity_id,
            },
        )
        return {
            "status": "created",
            "kind": kind,
            "entity_id": entity_id,
            "started": started,
        }
