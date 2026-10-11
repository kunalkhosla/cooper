"""ListAutomationsTool must find automations by anything in their config, not just alias."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from custom_components.cooper.llm_api import autoconfig
from custom_components.cooper.llm_api.tools_lifecycle import ListAutomationsTool

AUTOMATIONS = [
    {
        "id": "1111",
        "alias": "Evening lights",
        "description": "Scare off the deer at dusk",
        "trigger": [{"platform": "sun", "event": "sunset"}],
        "condition": [],
        "action": [{"service": "switch.turn_on", "target": {"entity_id": "switch.sprinkler_zone_3"}}],
    },
    {
        "id": "2222",
        "alias": "Garage reminder",
        "triggers": [{"trigger": "state", "entity_id": "cover.garage_door"}],
        "conditions": [],
        "actions": [{"action": "notify.mobile_app_phone"}],
    },
    {"id": "3333", "alias": "Porch light", "trigger": [], "action": []},
]


class FakeState(SimpleNamespace):
    pass


def make_hass(tmp_path, automations=AUTOMATIONS):
    (tmp_path / "automations.yaml").write_text(yaml.safe_dump(automations))
    states = [
        FakeState(entity_id="automation.evening_lights", state="on",
                  attributes={"friendly_name": "Evening lights", "id": "1111"}),
        FakeState(entity_id="automation.garage_reminder", state="off",
                  attributes={"friendly_name": "Garage reminder", "id": "2222"}),
        FakeState(entity_id="automation.porch_light", state="on",
                  attributes={"friendly_name": "Porch light", "id": "3333"}),
    ]
    hass = MagicMock()
    hass.states.async_all.return_value = states
    hass.states.get.side_effect = lambda eid: next((s for s in states if s.entity_id == eid), None)
    hass.config.path.side_effect = lambda f: str(tmp_path / f)

    async def run(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = run
    return hass


async def call(hass, **args):
    tool_input = SimpleNamespace(tool_args=args)
    return await ListAutomationsTool().async_call(hass, tool_input, None)


def names(result):
    return sorted(a["name"] for a in result["automations"])


async def test_no_filter_lists_all(tmp_path):
    assert (await call(make_hass(tmp_path)))["count"] == 3


async def test_alias_match_still_works_and_attaches_config(tmp_path):
    res = await call(make_hass(tmp_path), name="evening")
    assert names(res) == ["Evening lights"]
    assert res["automations"][0]["config"]["trigger"][0]["platform"] == "sun"


async def test_matches_entity_id_in_actions(tmp_path):
    res = await call(make_hass(tmp_path), name="sprinkler")
    assert names(res) == ["Evening lights"]


async def test_matches_description(tmp_path):
    assert names(await call(make_hass(tmp_path), name="deer")) == ["Evening lights"]


async def test_matches_new_style_triggers_keys(tmp_path):
    res = await call(make_hass(tmp_path), name="garage_door")
    assert names(res) == ["Garage reminder"]
    assert res["automations"][0]["config"]["trigger"][0]["entity_id"] == "cover.garage_door"


async def test_match_is_case_insensitive(tmp_path):
    assert names(await call(make_hass(tmp_path), name="SPRINKLER")) == ["Evening lights"]


async def test_matches_automation_entity_id(tmp_path):
    assert names(await call(make_hass(tmp_path), name="automation.porch")) == ["Porch light"]


async def test_no_match_returns_empty(tmp_path):
    assert (await call(make_hass(tmp_path), name="zzz-nothing"))["count"] == 0


async def test_missing_yaml_falls_back_to_alias_only(tmp_path):
    hass = make_hass(tmp_path)
    (tmp_path / "automations.yaml").unlink()
    assert names(await call(hass, name="porch")) == ["Porch light"]
    assert (await call(hass, name="sprinkler"))["count"] == 0
