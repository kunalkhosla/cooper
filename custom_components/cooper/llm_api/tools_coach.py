"""Coach tool: FITTR-style macro/meal coaching, plus weight/training/alcohol logging.

Read-only coaching context + write-logging, mirroring two existing patterns:
- ``get_coach_context`` is shaped exactly like ``tools_swim.py``'s ``get_swim_info`` — a
  single rich data snapshot loaded from an out-of-repo config, which the model reasons
  over rather than Cooper hand-coding meal logic (no per-use-case prompt rules).
- ``log_weight`` / ``log_training`` / ``log_alcohol`` mirror ``tools_memory.py``'s
  remember/forget: internal personal data, not home control, so they're gated only by
  the kill switch (no observe-mode simulation, no confirm y/n) — logging a daily weight
  shouldn't need a yes/no every time.

Config lives OUTSIDE this (public) repo in ``/config/cooper_coach.json`` so personal
macros/targets/plan details aren't committed::

    {
      "default_user": "kunal",
      "profile": {
        "goal": "190lb / 86.2kg at 13% body fat",
        "targets": {
          "active_phase": "phase2",
          "phase1": {"kcal": 2450, "protein_g": 180, "carb_g": 216, "fat_g": 96},
          "phase2": {"kcal": 2100, "protein_g": 180, "carb_g": 173, "fat_g": 77}
        },
        "base_template": { "...": "per-item macros + cooked/raw conversion factors" },
        "rotation": { "...": "current week's dishes + shopping list" },
        "rules": ["report cooked weight by default, raw only for Sunday prep", "..."]
      }
    }

Weight is logged (and stored) in kilograms; ``log_weight`` accepts either unit and
converts, since the user thinks in both lb and kg depending on context.
"""

from __future__ import annotations

import json
import os
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

CONFIG_PATH = os.environ.get("COOPER_COACH_CONFIG", "/config/cooper_coach.json")
LB_PER_KG = 2.20462
BODY_WEIGHT_ENTITY = "input_number.body_weight"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _user_id(llm_context: llm.LLMContext) -> str | None:
    context = llm_context.context
    return context.user_id if context is not None else None


def _to_kg(value: float, unit: str) -> float:
    return value / LB_PER_KG if unit == "lb" else value


class GetCoachContextTool(CooperTool):
    """Return the active FITTR/Get-Shredded plan snapshot for coaching Q&A."""

    name = "get_coach_context"
    description = (
        "Get the user's current fat-loss/nutrition plan: active macro targets (calories/"
        "protein/carb/fat), the meal base-template with per-item macros and cooked-vs-raw "
        "conversion factors, this week's meal-prep rotation + shopping list, and the coach's "
        "own rules (e.g. report COOKED weight by default, raw only for Sunday prep). Use this "
        "before answering ANY question about meals, calories, macros, ingredient swaps, "
        "what's-for-lunch, or 'why am I not losing fat' — reason over the returned data, "
        "don't guess numbers."
    )
    parameters = vol.Schema({})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        cfg = await hass.async_add_executor_job(_load_config)
        profile = cfg.get("profile")
        if not profile:
            return {
                "status": "error",
                "reason": f"coach plan not configured ({CONFIG_PATH} missing 'profile')",
            }
        return {"status": "ok", "profile": profile}


class LogWeightTool(CooperTool):
    """Log a body-weight reading."""

    name = "log_weight"
    description = (
        "Log today's body weight. Pass 'weight' as a number and 'unit' as 'kg' or 'lb' "
        "(default 'lb'). Also mirrors the value onto the input_number.body_weight helper "
        "if it exists, so it shows on dashboards. Use whenever the user tells you their "
        "weight (e.g. 'I weighed 211 this morning')."
    )
    parameters = vol.Schema(
        {
            vol.Required("weight"): vol.Coerce(float),
            vol.Optional("unit", default="lb"): vol.In(["kg", "lb"]),
            vol.Optional("note"): str,
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
        if runtime.kill_switch:
            return {"status": "refused", "reason": "Cooper's kill switch is on."}

        args = tool_input.tool_args
        kg = round(_to_kg(float(args["weight"]), args.get("unit", "lb")), 1)
        entry = await runtime.fitness.log_weight(
            _user_id(llm_context), kg, source="voice", note=args.get("note", "")
        )

        # Best-effort dashboard mirror; missing helper shouldn't fail the log itself.
        try:
            await hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": BODY_WEIGHT_ENTITY, "value": kg},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            pass

        return {"status": "logged", "entry": entry}


class LogTrainingTool(CooperTool):
    """Log a completed (or partial) training session."""

    name = "log_training"
    description = (
        "Log a training session. Pass 'day' (e.g. 'Day 1' or 'Chest/Shoulders') and "
        "optionally 'completed' (e.g. '5 of 7 exercises') and a 'note'. Use whenever the "
        "user says they did (or partially did) a workout."
    )
    parameters = vol.Schema(
        {
            vol.Required("day"): str,
            vol.Optional("completed"): str,
            vol.Optional("note"): str,
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
        if runtime.kill_switch:
            return {"status": "refused", "reason": "Cooper's kill switch is on."}

        args = tool_input.tool_args
        entry = await runtime.fitness.log_training(
            _user_id(llm_context),
            str(args["day"]),
            completed=args.get("completed", ""),
            note=args.get("note", ""),
        )
        return {"status": "logged", "entry": entry}


class LogAlcoholTool(CooperTool):
    """Log drinks consumed (the plan's highest-leverage lever)."""

    name = "log_alcohol"
    description = (
        "Log drinks consumed today. Pass 'drinks' as a count. Use whenever the user "
        "mentions having a drink or drinks — this tracks progress on their goal of "
        "cutting from daily to about once a month."
    )
    parameters = vol.Schema({vol.Required("drinks"): vol.Coerce(int), vol.Optional("note"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        if runtime.kill_switch:
            return {"status": "refused", "reason": "Cooper's kill switch is on."}

        args = tool_input.tool_args
        entry = await runtime.fitness.log_alcohol(
            _user_id(llm_context), int(args["drinks"]), note=args.get("note", "")
        )
        return {"status": "logged", "entry": entry}


class GetFitnessSummaryTool(CooperTool):
    """Return the rolling weight trend, recent training, and monthly drink count."""

    name = "get_fitness_summary"
    description = (
        "Get the user's fitness progress summary: rolling weekly weight trend (this "
        "week's average vs last week's, not a single noisy reading), the last few "
        "training sessions logged, and this month's drink count. Use for 'how am I "
        "doing' / 'what's my weight trend' / 'am I on track' questions."
    )
    parameters = vol.Schema({})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        summary = await runtime.fitness.summary(_user_id(llm_context))
        return {"status": "ok", **summary}
