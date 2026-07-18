"""Structured fitness/nutrition log, backed by helpers.storage.Store.

Distinct from memory.py's free-text, capped preference store: this holds STRUCTURED,
append-only, UNBOUNDED logs (weigh-ins, training sessions, drinks) per user. It's a real
history, not a rotating preference list — used by the coach tools (llm_api/tools_coach.py)
to answer "what's my weight trend" / "how's training going" questions.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

STORAGE_VERSION = 1
STORAGE_KEY = "cooper.fitness"


def _scope(user_id: str | None) -> str:
    return user_id or "global"


def _today() -> str:
    return dt_util.now().date().isoformat()


def _weekly_trend(weights: list[dict[str, Any]]) -> dict[str, Any]:
    """Rolling this-week vs prior-week average, from raw dated weigh-ins.

    Mirrors the /fittr skill's own rule: judge the trend, not any single reading
    (day-to-day swings of 1-2kg from water/sodium/glycogen are expected noise).
    """
    if not weights:
        return {"status": "no_data"}
    ordered = sorted(weights, key=lambda e: e["date"])
    today = dt_util.now().date()
    this_week = [e for e in ordered if (today - date.fromisoformat(e["date"])).days < 7]
    prior_week = [
        e for e in ordered if 7 <= (today - date.fromisoformat(e["date"])).days < 14
    ]
    latest = ordered[-1]
    if len(this_week) < 2:
        return {
            "status": "insufficient_data",
            "latest_kg": latest["kg"],
            "latest_date": latest["date"],
            "entries_this_week": len(this_week),
        }
    avg_this = sum(e["kg"] for e in this_week) / len(this_week)
    result: dict[str, Any] = {
        "status": "ok",
        "avg_this_week_kg": round(avg_this, 2),
        "latest_kg": latest["kg"],
        "latest_date": latest["date"],
        "entries_this_week": len(this_week),
    }
    if prior_week:
        avg_prior = sum(e["kg"] for e in prior_week) / len(prior_week)
        result["avg_prior_week_kg"] = round(avg_prior, 2)
        result["delta_kg"] = round(avg_this - avg_prior, 2)
    return result


class FitnessStore:
    """Per-user structured fitness log: weigh-ins, training sessions, drinks."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, dict[str, list[dict[str, Any]]]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._data: dict[str, dict[str, list[dict[str, Any]]]] | None = None

    async def _load(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        if self._data is None:
            self._data = await self._store.async_load() or {}
        return self._data

    async def _append(
        self, user_id: str | None, key: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        data = await self._load()
        bucket = data.setdefault(_scope(user_id), {}).setdefault(key, [])
        bucket.append(entry)
        await self._store.async_save(data)
        return entry

    async def log_weight(
        self, user_id: str | None, kg: float, source: str = "voice", note: str = ""
    ) -> dict[str, Any]:
        return await self._append(
            user_id,
            "weights",
            {"date": _today(), "kg": round(kg, 1), "source": source, "note": note},
        )

    async def log_training(
        self, user_id: str | None, day: str, completed: str = "", note: str = ""
    ) -> dict[str, Any]:
        return await self._append(
            user_id,
            "training",
            {"date": _today(), "day": day, "completed": completed, "note": note},
        )

    async def log_alcohol(
        self, user_id: str | None, drinks: int, note: str = ""
    ) -> dict[str, Any]:
        return await self._append(
            user_id, "alcohol", {"date": _today(), "drinks": drinks, "note": note}
        )

    async def log_meal(
        self,
        user_id: str | None,
        description: str,
        kcal: float,
        protein_g: float = 0.0,
        carb_g: float = 0.0,
        fat_g: float = 0.0,
    ) -> dict[str, Any]:
        return await self._append(
            user_id,
            "meals",
            {
                "id": uuid.uuid4().hex[:8],
                "date": _today(),
                "description": description,
                "kcal": round(kcal, 1),
                "protein_g": round(protein_g, 1),
                "carb_g": round(carb_g, 1),
                "fat_g": round(fat_g, 1),
            },
        )

    async def delete_meal(self, user_id: str | None, entry_id: str) -> bool:
        """Remove a meal logged TODAY, by id. Returns False if no entry matched.

        Scoped to today only, matching the tool's own description ("already logged
        today") - a stray/hallucinated id can't reach into older history. Meals logged
        before this field existed have no 'id' and can't be targeted - harmless, they
        just age out as old history.
        """
        data = await self._load()
        meals = data.get(_scope(user_id), {}).get("meals", [])
        today = _today()
        for i, meal in enumerate(meals):
            if meal.get("id") == entry_id and meal.get("date") == today:
                del meals[i]
                await self._store.async_save(data)
                return True
        return False

    async def correct_meal(
        self,
        user_id: str | None,
        entry_id: str,
        description: str,
        kcal: float,
        protein_g: float = 0.0,
        carb_g: float = 0.0,
        fat_g: float = 0.0,
    ) -> dict[str, Any]:
        """Atomically replace a meal logged today: one load/save covers both the
        removal of entry_id (if it matches a meal from today) and the append of the
        corrected entry, so a failure/interruption between two separate calls can't
        silently drop the meal (the failure mode a delete_meal + log_meal pair has).
        If entry_id doesn't match, the corrected entry is still logged (old_entry_found
        comes back False so the caller can flag it).
        """
        data = await self._load()
        meals = data.setdefault(_scope(user_id), {}).setdefault("meals", [])
        today = _today()
        old_entry_found = False
        for i, meal in enumerate(meals):
            if meal.get("id") == entry_id and meal.get("date") == today:
                del meals[i]
                old_entry_found = True
                break
        new_entry = {
            "id": uuid.uuid4().hex[:8],
            "date": today,
            "description": description,
            "kcal": round(kcal, 1),
            "protein_g": round(protein_g, 1),
            "carb_g": round(carb_g, 1),
            "fat_g": round(fat_g, 1),
        }
        meals.append(new_entry)
        await self._store.async_save(data)
        return {"entry": new_entry, "old_entry_found": old_entry_found}

    async def today_totals(self, user_id: str | None) -> dict[str, Any]:
        """Sum today's logged meals. Does not know the day's TARGET - callers combine
        this with the coach config's active-phase macros to compute what's remaining.
        """
        data = await self._load()
        meals = [
            m
            for m in data.get(_scope(user_id), {}).get("meals", [])
            if m["date"] == _today()
        ]
        totals = {
            "kcal": round(sum(m["kcal"] for m in meals), 1),
            "protein_g": round(sum(m["protein_g"] for m in meals), 1),
            "carb_g": round(sum(m["carb_g"] for m in meals), 1),
            "fat_g": round(sum(m["fat_g"] for m in meals), 1),
        }
        return {"consumed": totals, "meals_logged": meals}

    async def summary(
        self, user_id: str | None, extra_weights: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Rolling weight trend + recent training + this-month drink count.

        ``extra_weights`` lets a caller (the get_fitness_summary tool) fold in
        weigh-ins from OTHER sources — e.g. a phone's Health-Connect sensor synced
        straight into HA — so the trend isn't blind to weight that was never voice-
        logged through log_weight.
        """
        data = await self._load()
        scope = data.get(_scope(user_id), {})
        voice_weights = scope.get("weights", [])
        extra_weights = list(extra_weights or [])
        training = scope.get("training", [])
        alcohol = scope.get("alcohol", [])

        this_month = _today()[:7]
        drinks_this_month = sum(
            int(e.get("drinks", 0)) for e in alcohol if e["date"][:7] == this_month
        )

        return {
            "weight_trend": _weekly_trend(voice_weights + extra_weights),
            "weight_sources": {
                "voice_logged": len(voice_weights),
                "auto_captured": len(extra_weights),
            },
            "recent_training": training[-5:],
            "drinks_this_month": drinks_this_month,
            "total_logged": {
                "weights": len(voice_weights),
                "training": len(training),
                "alcohol": len(alcohol),
            },
        }
