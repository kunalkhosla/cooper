"""The Cooper integration.

One config entry holds the Anthropic key; each conversation subentry is one agent entity.
At setup we build the provider + coordinator, store a per-entry ``CooperRuntime`` (which the
tools reach via ``get_runtime``), register Cooper's LLM Tools API once so it shows in the
config picker, register services, and forward the conversation platform.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .const import (
    CONF_CONFIRM_BULK_THRESHOLD,
    CONF_OBSERVE_MODE,
    DEFAULT,
    DOMAIN,
    SUBENTRY_TYPE_CONVERSATION,
)
from .coordinator import CooperCoordinator
from .guardrails import PendingConfirmations
from .memory import MemoryStore
from .provider.anthropic_client import AnthropicProvider
from .services import async_setup_services, async_unload_services

PLATFORMS = [Platform.CONVERSATION, Platform.SWITCH]
AUDIT_LOG_MAX = 200


@dataclass
class CooperRuntime:
    """Per-entry shared state: the brain, its guardrail flags, memory, and audit."""

    provider: AnthropicProvider
    coordinator: CooperCoordinator
    memory: MemoryStore
    observe_mode: bool
    kill_switch: bool = False
    confirm_bulk_threshold: int = 5
    pending_confirmations: PendingConfirmations = field(
        default_factory=PendingConfirmations
    )
    audit_log: deque = field(default_factory=lambda: deque(maxlen=AUDIT_LOG_MAX))
    proactive_last_fired: dict[str, float] = field(default_factory=dict)
    # UI toggle entities (registered by the switch platform) so services can sync them.
    observe_switch: Any = None
    kill_switch_entity: Any = None


CooperConfigEntry = ConfigEntry[CooperRuntime]


def get_runtime(hass: HomeAssistant) -> CooperRuntime:
    """Return the active Cooper runtime (tools call this; one key per home is typical)."""
    runtimes: dict[str, CooperRuntime] = hass.data[DOMAIN]["runtimes"]
    return next(iter(runtimes.values()))


def _seed_observe_mode(entry: ConfigEntry) -> bool:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_CONVERSATION:
            return bool(subentry.data.get(CONF_OBSERVE_MODE, DEFAULT[CONF_OBSERVE_MODE]))
    return bool(DEFAULT[CONF_OBSERVE_MODE])


async def async_setup_entry(hass: HomeAssistant, entry: CooperConfigEntry) -> bool:
    """Set up Cooper from a config entry."""
    domain_data = hass.data.setdefault(
        DOMAIN, {"runtimes": {}, "api_registered": False, "services_registered": False}
    )

    api_key = entry.data[CONF_API_KEY]
    provider = AnthropicProvider(hass, api_key)
    coordinator = CooperCoordinator(hass, entry, provider)
    await coordinator.async_config_entry_first_refresh()

    confirm_threshold = int(
        _seed_subentry_value(entry, CONF_CONFIRM_BULK_THRESHOLD)
        or DEFAULT[CONF_CONFIRM_BULK_THRESHOLD]
    )
    runtime = CooperRuntime(
        provider=provider,
        coordinator=coordinator,
        memory=MemoryStore(hass),
        observe_mode=_seed_observe_mode(entry),
        confirm_bulk_threshold=confirm_threshold,
    )
    entry.runtime_data = runtime
    domain_data["runtimes"][entry.entry_id] = runtime

    # Register Cooper's LLM API once so it appears in the subentry API picker.
    if not domain_data["api_registered"]:
        from .llm_api.custom_api import CooperAPI

        llm.async_register_api(hass, CooperAPI(hass))
        domain_data["api_registered"] = True

    if not domain_data["services_registered"]:
        async_setup_services(hass)
        domain_data["services_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _seed_subentry_value(entry: ConfigEntry, key: str):
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_CONVERSATION:
            return subentry.data.get(key)
    return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options/subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CooperConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    domain_data.get("runtimes", {}).pop(entry.entry_id, None)

    # When the last entry goes away, tear down the global services.
    if not domain_data.get("runtimes") and domain_data.get("services_registered"):
        async_unload_services(hass)
        domain_data["services_registered"] = False
        # The LLM API has no public unregister; it is harmless to leave registered,
        # and re-registering is guarded, so leave api_registered as-is.
    return True
