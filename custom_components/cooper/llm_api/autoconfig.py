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
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify, ulid as ulid_util

from ..const import AUTHORED_ALIAS_PREFIX, AUTHORED_PREFIX

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


def _tag_alias(config: dict[str, Any]) -> None:
    """Prefix the friendly name with [Cooper] so authored items are visible in the UI."""
    alias = config.get("alias")
    if isinstance(alias, str) and alias and "cooper" not in alias.lower():
        config["alias"] = AUTHORED_ALIAS_PREFIX + alias


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


def _remove_script_sync(path: str, object_id: str) -> bool:
    data = _read_yaml(path, {})
    if not isinstance(data, dict) or object_id not in data:
        return False
    del data[object_id]
    _write_yaml(path, data)
    return True


def _automation_entity_id(hass: HomeAssistant, config_id: str) -> str:
    """Real entity_id for an authored automation.

    HA derives an automation's entity_id from its alias (slugified), not from the
    config ``id``; it registers the entity with ``unique_id == id``. So after a reload
    we look the entity up by unique_id. Falls back to ``automation.<id>`` if not found
    yet (e.g. registry not populated), which is only used as a best-effort handle.
    """
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "automation" and entry.unique_id == config_id:
            return entry.entity_id
    return f"automation.{config_id}"


async def async_save_automation(
    hass: HomeAssistant, config: dict[str, Any]
) -> str:
    """Stamp an id, persist, reload. Returns the resulting automation entity_id."""
    config.setdefault("id", new_authored_id())
    _tag_alias(config)
    config_id = str(config["id"])
    path = hass.config.path(AUTOMATIONS_FILE)
    await hass.async_add_executor_job(partial(_save_automation_sync, path, config))
    await hass.services.async_call("automation", "reload", blocking=True)
    # entity_id is alias-slugified, not automation.<id> — resolve the real one so
    # callers (e.g. run_now's trigger) target the entity that actually exists.
    return _automation_entity_id(hass, config_id)


async def async_save_script(
    hass: HomeAssistant, alias: str, config: dict[str, Any]
) -> str:
    """Persist a script under a cooper_-prefixed object id, reload. Returns entity_id."""
    object_id = f"{AUTHORED_PREFIX}{slugify(alias)}"
    config.setdefault("alias", alias)
    _tag_alias(config)
    path = hass.config.path(SCRIPTS_FILE)
    await hass.async_add_executor_job(
        partial(_save_script_sync, path, object_id, config)
    )
    await hass.services.async_call("script", "reload", blocking=True)
    return f"script.{object_id}"


async def async_remove_automation(hass: HomeAssistant, config_id: str) -> bool:
    """Remove an authored automation by id and reload. Returns True if removed.

    Storage-layer hard gate: refuses any id not stamped with ``AUTHORED_PREFIX`` so
    Cooper can never delete a user's hand-made automation, even if a caller asks it to.
    """
    if not str(config_id).startswith(AUTHORED_PREFIX):
        return False
    path = hass.config.path(AUTOMATIONS_FILE)
    removed = await hass.async_add_executor_job(
        partial(_remove_automation_sync, path, config_id)
    )
    if removed:
        await hass.services.async_call("automation", "reload", blocking=True)
    return removed


async def async_remove_script(hass: HomeAssistant, object_id: str) -> bool:
    """Remove an authored script by object id and reload. Returns True if removed.

    Same storage-layer hard gate as automations: only ``AUTHORED_PREFIX`` keys go.
    """
    if not str(object_id).startswith(AUTHORED_PREFIX):
        return False
    path = hass.config.path(SCRIPTS_FILE)
    removed = await hass.async_add_executor_job(
        partial(_remove_script_sync, path, object_id)
    )
    if removed:
        await hass.services.async_call("script", "reload", blocking=True)
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


async def async_list_authored_scripts(
    hass: HomeAssistant,
) -> dict[str, dict[str, Any]]:
    """Return ``{object_id: config}`` for every Cooper-authored script."""
    path = hass.config.path(SCRIPTS_FILE)
    data = await hass.async_add_executor_job(partial(_read_yaml, path, {}))
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if str(key).startswith(AUTHORED_PREFIX) and isinstance(value, dict)
    }
