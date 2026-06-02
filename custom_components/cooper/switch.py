"""User-facing toggle entities for Cooper's runtime safety flags.

Observe Mode and the Kill Switch are per-entry runtime flags the guardrails read. These
switches are the canonical UI control for them and keep the runtime in sync. They use
RestoreEntity so a user's choice survives restarts/reloads (overriding the config-seeded
default), which fixes the "there's no way to actually turn observe mode off" gap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, ENTRY_TITLE

if TYPE_CHECKING:
    from . import CooperConfigEntry, CooperRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CooperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Observe Mode and Kill Switch controls for this entry."""
    runtime = entry.runtime_data
    async_add_entities(
        [CooperObserveModeSwitch(entry, runtime), CooperKillSwitch(entry, runtime)]
    )


class _CooperControlSwitch(SwitchEntity, RestoreEntity):
    """Base for a switch that mirrors a CooperRuntime boolean flag."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _key: str

    def __init__(self, entry: CooperConfigEntry, runtime: CooperRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_controls")},
            name=f"{ENTRY_TITLE} Controls",
            manufacturer="Cooper",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _get(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def _set(self, value: bool) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _register(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def is_on(self) -> bool:
        return self._get()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore the user's last choice, overriding the config-seeded default.
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._set(last.state == "on")
        self._register()


class CooperObserveModeSwitch(_CooperControlSwitch):
    """When on, acting tools only report what they *would* do (no changes)."""

    _key = "observe_mode"
    _attr_name = "Observe Mode"
    _attr_icon = "mdi:eye-check-outline"

    def _get(self) -> bool:
        return self._runtime.observe_mode

    def _set(self, value: bool) -> None:
        self._runtime.observe_mode = value

    def _register(self) -> None:
        self._runtime.observe_switch = self


class CooperKillSwitch(_CooperControlSwitch):
    """When on, Cooper refuses every action."""

    _key = "kill_switch"
    _attr_name = "Kill Switch"
    _attr_icon = "mdi:stop-circle-outline"

    def _get(self) -> bool:
        return self._runtime.kill_switch

    def _set(self, value: bool) -> None:
        self._runtime.kill_switch = value

    def _register(self) -> None:
        self._runtime.kill_switch_entity = self
