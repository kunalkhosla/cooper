"""Cooper's services: proactive_check, set_observe_mode, kill_switch.

``proactive_check`` is the single re-entry point for proactivity. Authored watch
automations and the optional add-on both call it; it re-enters the same agent loop with
a proactive seed (so the same wrapped tools and guardrails apply), throttled by a
per-reason cooldown so a chatty trigger cannot spam the user.
"""

from __future__ import annotations

import hashlib
import time

import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import (
    DOMAIN,
    LOGGER,
    PROACTIVE_SEED,
    SERVICE_KILL_SWITCH,
    SERVICE_PROACTIVE_CHECK,
    SERVICE_SET_OBSERVE_MODE,
)

ATTR_REASON = "reason"
ATTR_CONTEXT_ENTITIES = "context_entities"
ATTR_NOTIFY_TARGET = "notify_target"
ATTR_AGENT_ID = "agent_id"
ATTR_CONVERSATION_ID = "conversation_id"
ATTR_COOLDOWN_MINUTES = "cooldown_minutes"
ATTR_OBSERVE = "observe"
ATTR_ENABLED = "enabled"

DEFAULT_COOLDOWN_MINUTES = 15

PROACTIVE_CHECK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_REASON): cv.string,
        vol.Optional(ATTR_CONTEXT_ENTITIES): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_NOTIFY_TARGET): cv.string,
        vol.Optional(ATTR_AGENT_ID): cv.string,
        vol.Optional(ATTR_CONVERSATION_ID): cv.string,
        vol.Optional(ATTR_COOLDOWN_MINUTES, default=DEFAULT_COOLDOWN_MINUTES): vol.All(
            int, vol.Range(min=0, max=1440)
        ),
    }
)
SET_OBSERVE_MODE_SCHEMA = vol.Schema({vol.Required(ATTR_OBSERVE): cv.boolean})
KILL_SWITCH_SCHEMA = vol.Schema({vol.Required(ATTR_ENABLED): cv.boolean})


def _resolve_agent(hass: HomeAssistant, agent_id: str | None) -> str | None:
    """Return the target Cooper conversation entity_id (explicit, else the first)."""
    if agent_id:
        return agent_id
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.platform == DOMAIN and entry.domain == "conversation":
            return entry.entity_id
    return None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register Cooper's global services."""

    async def handle_proactive_check(call: ServiceCall) -> None:
        from . import get_runtime

        runtime = get_runtime(hass)
        reason = call.data[ATTR_REASON]
        cooldown = call.data[ATTR_COOLDOWN_MINUTES]

        key = hashlib.sha256(reason.encode()).hexdigest()[:16]
        now = time.monotonic()
        last = runtime.proactive_last_fired.get(key)
        if cooldown and last is not None and (now - last) < cooldown * 60:
            LOGGER.debug("proactive_check '%s' skipped (cooldown)", reason)
            return
        runtime.proactive_last_fired[key] = now

        agent_id = _resolve_agent(hass, call.data.get(ATTR_AGENT_ID))
        if agent_id is None:
            LOGGER.warning("proactive_check: no Cooper conversation agent found")
            return

        context_entities = call.data.get(ATTR_CONTEXT_ENTITIES)
        seed = PROACTIVE_SEED.format(reason=reason)
        if context_entities:
            seed += f"\nRelevant entities to look at: {', '.join(context_entities)}."

        result = await conversation.async_converse(
            hass,
            text=reason,
            conversation_id=call.data.get(ATTR_CONVERSATION_ID),
            context=call.context,
            language=hass.config.language,
            agent_id=agent_id,
            extra_system_prompt=seed,
        )

        target = call.data.get(ATTR_NOTIFY_TARGET)
        if target:
            speech = result.response.speech.get("plain", {}).get("speech", "").strip()
            if speech and "." in target:
                domain, service = target.split(".", 1)
                await hass.services.async_call(
                    domain, service, {"message": speech}, blocking=False
                )

    async def handle_set_observe_mode(call: ServiceCall) -> None:
        observe = call.data[ATTR_OBSERVE]
        for runtime in hass.data[DOMAIN]["runtimes"].values():
            runtime.observe_mode = observe
        LOGGER.info("Cooper observe mode set to %s", observe)

    async def handle_kill_switch(call: ServiceCall) -> None:
        enabled = call.data[ATTR_ENABLED]
        for runtime in hass.data[DOMAIN]["runtimes"].values():
            runtime.kill_switch = enabled
        LOGGER.warning("Cooper kill switch set to %s", enabled)

    hass.services.async_register(
        DOMAIN, SERVICE_PROACTIVE_CHECK, handle_proactive_check, PROACTIVE_CHECK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_OBSERVE_MODE, handle_set_observe_mode, SET_OBSERVE_MODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_KILL_SWITCH, handle_kill_switch, KILL_SWITCH_SCHEMA
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Cooper's global services."""
    for service in (
        SERVICE_PROACTIVE_CHECK,
        SERVICE_SET_OBSERVE_MODE,
        SERVICE_KILL_SWITCH,
    ):
        hass.services.async_remove(DOMAIN, service)
