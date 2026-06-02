"""Persisting Cooper-authored automations and scripts to HA's editable YAML files.

HA's UI editor stores automations in ``automations.yaml`` (a list, each item carrying
an ``id``) and scripts in ``scripts.yaml`` (a dict keyed by object id). We write the
same files and then call ``automation.reload`` / ``script.reload`` so the new config
becomes a live, restart-surviving HA entity. Every authored config gets a stable
``cooper_<ulid>`` id so it can be listed, audited, and bulk-removed.

File IO happens in the executor; the in-memory shape is validated first by
``validation.validate_config``.
"""

from __future__ import annotations

from functools import partial
import os
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify, ulid as ulid_util

from ..const import AUTHORED_PREFIX

AUTOMATIONS_FILE = "automations.yaml"
SCRIPTS_FILE = "scripts.yaml"


def _read_yaml(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as file:
        loaded = yaml.safe_load(file)
    return default if loaded is None else loaded


def _write_yaml(path: str, data: Any) -> None:
    tmp = f"{path}.cooper.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)


def new_authored_id() -> str:
    """A stable, auditable id for a Cooper-authored config."""
    return f"{AUTHORED_PREFIX}{ulid_util.ulid_now().lower()}"


def _save_automation_sync(path: str, config: dict[str, Any]) -> None:
    data = _read_yaml(path, [])
    if not isinstance(data, list):
        data = [data]
    config_id = config["id"]
    data = [item for item in data if str(item.get("id")) != config_id]
    data.append(config)
    _write_yaml(path, data)


def _save_script_sync(path: str, object_id: str, config: dict[str, Any]) -> None:
    data = _read_yaml(path, {})
    if not isinstance(data, dict):
        data = {}
    data[object_id] = config
    _write_yaml(path, data)


def _remove_automation_sync(path: str, config_id: str) -> bool:
    data = _read_yaml(path, [])
    if not isinstance(data, list):
        return False
    kept = [item for item in data if str(item.get("id")) != config_id]
    if len(kept) == len(data):
        return False
    _write_yaml(path, kept)
    return True


async def async_save_automation(
    hass: HomeAssistant, config: dict[str, Any]
) -> str:
    """Stamp an id, persist, reload. Returns the resulting automation entity_id."""
    config.setdefault("id", new_authored_id())
    config_id = str(config["id"])
    path = hass.config.path(AUTOMATIONS_FILE)
    await hass.async_add_executor_job(partial(_save_automation_sync, path, config))
    await hass.services.async_call("automation", "reload", blocking=True)
    return f"automation.{config_id}"


async def async_save_script(
    hass: HomeAssistant, alias: str, config: dict[str, Any]
) -> str:
    """Persist a script under a cooper_-prefixed object id, reload. Returns entity_id."""
    object_id = f"{AUTHORED_PREFIX}{slugify(alias)}"
    path = hass.config.path(SCRIPTS_FILE)
    await hass.async_add_executor_job(
        partial(_save_script_sync, path, object_id, config)
    )
    await hass.services.async_call("script", "reload", blocking=True)
    return f"script.{object_id}"


async def async_remove_automation(hass: HomeAssistant, config_id: str) -> bool:
    """Remove an authored automation by id and reload. Returns True if removed."""
    path = hass.config.path(AUTOMATIONS_FILE)
    removed = await hass.async_add_executor_job(
        partial(_remove_automation_sync, path, config_id)
    )
    if removed:
        await hass.services.async_call("automation", "reload", blocking=True)
    return removed


async def async_list_authored_automations(
    hass: HomeAssistant,
) -> list[dict[str, Any]]:
    """Return the raw config of every Cooper-authored automation."""
    path = hass.config.path(AUTOMATIONS_FILE)
    data = await hass.async_add_executor_job(partial(_read_yaml, path, []))
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("id", "")).startswith(AUTHORED_PREFIX)
    ]
