"""Lifecycle tools for Cooper-authored automations and scripts: list + delete.

Cooper can create automations/scripts (``author_automation``); these let it *clean up*
the ones it made that are no longer needed. The hard safety rule — **only Cooper-authored
items (``cooper_`` id/object-id prefix) can ever be removed** — is enforced in code here
(the tool refuses anything else) and again in ``autoconfig.async_remove_*`` (the storage
layer refuses too). ``automations.yaml``/``scripts.yaml`` also hold the user's own items,
so the prefix gate is what makes a hand-made automation structurally un-deletable by Cooper.

Identity note: the deletion handle is the durable config **id** — ``cooper_<ulid>`` for an
automation (the ``id:`` in automations.yaml), ``cooper_<slug>`` for a script (its key in
scripts.yaml). An automation's *entity_id* is slugified from its alias and differs from
that id, so we delete by id and only use the entity_id (resolved via the entity registry)
to show live state. Removal is confirm-tier: it goes through ``precheck_write`` (kill
switch / observe mode / confirmation) exactly like authoring.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, llm
from homeassistant.util.json import JsonObjectType

from ..const import AUTHORED_PREFIX
from ..guardrails import CooperTool, audit
from . import autoconfig
from ._guard import precheck_write


def _state_summary(hass: HomeAssistant, entity_id: str | None) -> dict[str, object]:
    """Live state + last-run, so Cooper can spot items that are off or never used."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return {"state": "unavailable", "last_triggered": None}
    return {
        "state": state.state,
        "last_triggered": str(state.attributes.get("last_triggered") or "never"),
    }


class ListAuthoredTool(CooperTool):
    """List the automations and scripts Cooper has authored (the only removable ones)."""

    name = "list_cooper_items"
    description = (
        "List the automations and scripts that Cooper authored — the only items Cooper is "
        "allowed to delete. Each entry gives its 'kind' (automation/script), 'id' (the "
        "handle to pass to delete_cooper_item), human 'name', whether it is on/off, and "
        "when it last ran — so you can identify ones no longer needed before removing them. "
        "Read-only."
    )
    parameters = vol.Schema({})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        # Map automation config-id -> real entity_id (alias-slugified) via the registry.
        registry = er.async_get(hass)
        automation_entity = {
            entry.unique_id: entry.entity_id
            for entry in registry.entities.values()
            if entry.domain == "automation" and entry.unique_id
        }

        items: list[dict[str, object]] = []
        for config in await autoconfig.async_list_authored_automations(hass):
            config_id = str(config["id"])
            entity_id = automation_entity.get(config_id)
            items.append(
                {
                    "kind": "automation",
                    "id": config_id,
                    "name": config.get("alias", config_id),
                    "entity_id": entity_id,
                    **_state_summary(hass, entity_id),
                }
            )
        for object_id, config in (
            await autoconfig.async_list_authored_scripts(hass)
        ).items():
            entity_id = f"script.{object_id}"
            items.append(
                {
                    "kind": "script",
                    "id": object_id,
                    "name": config.get("alias", object_id),
                    "entity_id": entity_id,
                    **_state_summary(hass, entity_id),
                }
            )
        return {"status": "ok", "count": len(items), "items": items}


class RemoveAuthoredTool(CooperTool):
    """Delete one Cooper-authored automation or script, with a hard ownership gate."""

    name = "delete_cooper_item"
    description = (
        "Permanently delete ONE Cooper-authored automation or script. Pass 'kind' "
        "('automation' or 'script') and the 'id' exactly as given by list_cooper_items "
        "(a 'cooper_...' id). HARD RULE: only ids carrying Cooper's 'cooper_' prefix can be "
        "removed — you cannot delete the user's own automations or scripts. Always call "
        "list_cooper_items first to get the id. This changes the home, so it needs "
        "confirm=true (ask one yes/no, then call again with confirm=true)."
    )
    parameters = vol.Schema(
        {
            vol.Required("kind"): vol.In(["automation", "script"]),
            vol.Required("id"): str,
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
        item_id = str(tool_input.tool_args["id"]).strip()
        confirmed = bool(tool_input.tool_args.get("confirm", False))

        # --- HARD ownership gate (enforced here, not trusted to the model) ---
        if not item_id.startswith(AUTHORED_PREFIX):
            return {
                "status": "refused",
                "reason": (
                    f"'{item_id}' is not a Cooper-authored id. Only {AUTHORED_PREFIX}* "
                    "automations/scripts can be deleted; the user's own items are off-limits."
                ),
            }

        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=f"delete {kind} '{item_id}'",
            args={"kind": kind, "id": item_id},
            confirmed=confirmed,
        )
        if blocked is not None:
            return blocked

        if kind == "automation":
            removed = await autoconfig.async_remove_automation(hass, item_id)
        else:
            removed = await autoconfig.async_remove_script(hass, item_id)

        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"kind": kind, "id": item_id},
                "tier": "CONFIRM",
                "decision": "executed" if removed else "not_found",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
                "result": item_id,
            },
        )
        if not removed:
            return {
                "status": "not_found",
                "id": item_id,
                "reason": (
                    f"No Cooper-authored {kind} with id '{item_id}' (already removed, or "
                    "wrong id — call list_cooper_items)."
                ),
            }
        return {"status": "removed", "kind": kind, "id": item_id}
