"""Coordinator: holds the Anthropic provider and caches the available model list."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import anthropic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER
from .provider.anthropic_client import AnthropicProvider

if TYPE_CHECKING:
    from anthropic.types import ModelInfo

UPDATE_INTERVAL = timedelta(hours=12)


class CooperCoordinator(DataUpdateCoordinator[list["ModelInfo"]]):
    """Fetches and caches the list of available Claude models for the config UI."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, provider: AnthropicProvider
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.provider = provider

    async def _async_update_data(self) -> list[ModelInfo]:
        try:
            result = await self.provider.client.models.list(timeout=10.0)
        except anthropic.AuthenticationError as err:
            raise ConfigEntryAuthFailed("Invalid Anthropic API key") from err
        except anthropic.AnthropicError as err:
            raise UpdateFailed(f"Error listing models: {err}") from err
        return list(result.data)

    def get_model_info(self, model_id: str) -> tuple["ModelInfo | None", bool]:
        """Return (model_info, found) for a model id or alias.

        Matches exact id first, then a prefix (Anthropic ids may carry a date suffix).
        """
        models: list[Any] = self.data or []
        for model in models:
            if model.id == model_id:
                return model, True
        for model in models:
            if model.id.startswith(model_id) or model_id.startswith(model.id):
                return model, True
        return None, False

    def model_display_name(self, model_id: str) -> str:
        info, _ = self.get_model_info(model_id)
        return info.display_name if info is not None else model_id
