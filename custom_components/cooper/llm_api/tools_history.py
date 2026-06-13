"""History tool: query the recorder for an entity's recent state changes.

Read-only. Answers "when did the front door last open?", "has the garage been opened
today?", etc. by summarising recorder history into a compact list the model can reason
over without being flooded with raw rows.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
import re

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool

MAX_CHANGES = 50
# How many "did you mean" candidates to hand back when a guessed id doesn't exist.
MAX_CANDIDATES = 5


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, for loose entity matching."""
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _resolve_entity(hass: HomeAssistant, guessed: str) -> tuple[str | None, list[dict]]:
    """Resolve a possibly-wrong entity_id.

    The model often guesses a plausible id (``binary_sensor.basement_entry_door``) when
    the real one differs slightly (``..._door_2``). Rather than hard-fail — which makes it
    blind-retry round after round — find the closest exposed entities by token overlap.

    Returns ``(resolved_id, candidates)``: an exact/single-dominant match resolves to an id
    with no candidates (zero extra rounds); an ambiguous guess returns ranked candidates so
    the model picks the right id in ONE more round instead of guessing again.
    """
    if hass.states.get(guessed) is not None:
        return guessed, []

    domain = guessed.split(".", 1)[0] if "." in guessed else None
    want = _tokens(guessed.split(".", 1)[-1])
    if not want:
        return None, []

    scored: list[tuple[float, int, str, str]] = []
    for st in hass.states.async_all():
        eid = st.entity_id
        edom = eid.split(".", 1)[0]
        name = str(st.attributes.get("friendly_name") or "")
        have = _tokens(eid.split(".", 1)[-1] + " " + name)
        overlap = len(want & have)
        if not overlap:
            continue
        # Reward token overlap + a same-domain bonus; ratio breaks ties toward tight matches.
        score = overlap + overlap / len(want) + (0.5 if edom == domain else 0.0)
        scored.append((score, overlap, eid, name))

    if not scored:
        return None, []
    scored.sort(key=lambda s: s[0], reverse=True)

    best_score, best_overlap, best_eid, _ = scored[0]
    covers_all = best_overlap == len(want)
    clear_lead = len(scored) == 1 or best_score >= scored[1][0] + 1.0
    if covers_all and clear_lead:
        return best_eid, []

    candidates = [{"entity_id": e, "name": n} for _, _, e, n in scored[:MAX_CANDIDATES]]
    return None, candidates


class HistoryTool(CooperTool):
    """Summarise an entity's recent state history from the recorder."""

    name = "get_history"
    description = (
        "Look up the recent state history of a single entity to answer questions about "
        "what happened and when (e.g. when a door last opened, whether a light has been on "
        "today). Returns the state changes over the requested window."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=720)),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        guessed = str(tool_input.tool_args["entity_id"])
        hours = int(tool_input.tool_args.get("hours", 24))

        entity_id, candidates = _resolve_entity(hass, guessed)
        if entity_id is None:
            if candidates:
                return {
                    "status": "unknown_entity",
                    "guessed": guessed,
                    "did_you_mean": candidates,
                    "hint": (
                        "That exact entity_id does not exist. Retry get_history with one "
                        "of the entity_ids above — do not guess another id."
                    ),
                }
            return {
                "status": "error",
                "reason": f"Unknown entity '{guessed}' and no similar exposed entity found.",
            }
        resolved_from = guessed if entity_id != guessed else None

        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return {"status": "error", "reason": "The recorder is not available."}

        end = dt_util.utcnow()
        start = end - timedelta(hours=hours)
        changes = await get_instance(hass).async_add_executor_job(
            partial(
                history.state_changes_during_period,
                hass,
                start,
                end,
                entity_id,
                no_attributes=True,
                include_start_time_state=True,
            )
        )
        states = changes.get(entity_id, [])
        items = [
            {
                "state": state.state,
                "when": state.last_changed.isoformat(),
            }
            for state in states
        ]
        if len(items) > MAX_CHANGES:
            items = items[-MAX_CHANGES:]
        result: JsonObjectType = {
            "entity_id": entity_id,
            "window_hours": hours,
            "change_count": len(states),
            "changes": items,
        }
        if resolved_from is not None:
            # We corrected a near-miss id; tell the model so it uses this one going forward.
            result["resolved_from"] = resolved_from
        return result
