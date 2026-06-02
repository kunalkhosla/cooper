"""Shared write-gate for Cooper's own (self-tiering) action tools.

The built-in intent tools are gated by ``guardrails.GuardedTool`` after grounding is
resolved. Cooper's own tools pass through that wrapping untouched (they subclass
``guardrails.CooperTool``), so the ones that *change the home* — authoring an
automation/script, creating or removing a watch — gate themselves here instead.

Because these are our own tools we can carry an explicit ``confirm`` parameter, so we
do not need the fingerprint dance the built-in intents require: an unconfirmed call
returns ``needs_confirmation`` and the model asks a yes/no, then re-calls with
``confirm=True``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from ..guardrails import audit

if TYPE_CHECKING:
    from .. import CooperRuntime


def precheck_write(
    hass: HomeAssistant,
    runtime: CooperRuntime,
    *,
    tool: str,
    summary: str,
    args: dict[str, Any],
    confirmed: bool,
) -> dict[str, Any] | None:
    """Apply kill switch / observe mode / confirmation to a Cooper write tool.

    Returns a result dict to hand straight back to the model when the action is
    blocked, or ``None`` when the caller should proceed (and then audit the result).
    """
    record: dict[str, Any] = {
        "tool": tool,
        "base": tool,
        "args": args,
        "observe": runtime.observe_mode,
        "kill": runtime.kill_switch,
        "tier": "CONFIRM",
    }

    if runtime.kill_switch:
        record["decision"] = "refused_kill_switch"
        audit(hass, runtime, record)
        return {
            "status": "refused",
            "reason": "Cooper's kill switch is on; nothing was changed.",
        }

    if runtime.observe_mode:
        record["decision"] = "observed"
        audit(hass, runtime, record)
        return {
            "status": "observe_mode",
            "would_have": summary,
            "note": "Observe mode is on, so nothing was created or changed.",
        }

    if not confirmed:
        record["decision"] = "needs_confirmation"
        audit(hass, runtime, record)
        return {
            "status": "needs_confirmation",
            "summary": summary,
            "instructions": (
                "Ask the user a single yes/no question to confirm. If they agree, "
                "call this tool again with the same arguments and confirm=true."
            ),
        }

    return None
