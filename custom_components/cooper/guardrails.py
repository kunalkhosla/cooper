"""Mechanical, domain-keyed guardrails enforced inside the tools.

The chat-log loop auto-executes every tool call, so guardrails cannot wrap the loop —
they wrap the *tools*. After grounding is resolved we replace the action tools in
``chat_log.llm_api.tools`` with ``GuardedTool`` wrappers that:

* resolve the target of a name-slotted intent (``HassTurnOff`` of "front door") to real
  entities via ``intent.async_match_targets`` so we can tier by domain/device_class,
* honour observe mode and the kill switch,
* gate risky (confirm) and forbidden (never) actions, and
* write an audit record for every decision.

Auto-tier (reversible) actions are *not* gated — they already execute optimistically as
the model streams them, which is the whole latency win.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import hashlib
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from . import _log
from .const import EVENT_TOOL_EXECUTED, LOGGER

if TYPE_CHECKING:
    from homeassistant.components import conversation

    from . import CooperRuntime

CONFIRM_TTL = 120.0  # seconds a pending confirmation stays valid


class CooperTool(llm.Tool):
    """Marker base class for Cooper's own tools, which tier themselves internally."""


class Tier(IntEnum):
    """Action tiers, ordered so ``max`` picks the strictest."""

    AUTO = 0
    CONFIRM = 1
    NEVER = 2


# Intents whose tier depends on the resolved target domain/device-class/direction.
TIER_RESOLVED_INTENTS = frozenset(
    {"HassTurnOn", "HassTurnOff", "HassToggle", "HassSetPosition", "HassStopMoving"}
)
# Intents that change device state but are always reversible/low-risk.
AUTO_ACTION_INTENTS = frozenset(
    {
        "HassLightSet",
        "HassClimateSetTemperature",
        "HassMediaPause",
        "HassMediaUnpause",
        "HassMediaNext",
        "HassMediaPrevious",
        "HassMediaSearchAndPlay",
        "HassSetVolume",
        "HassSetVolumeRelative",
        "HassVacuumStart",
        "HassVacuumReturnToBase",
        "HassVacuumCleanArea",
    }
)
ACTION_INTENTS = TIER_RESOLVED_INTENTS | AUTO_ACTION_INTENTS

# Domains we never act on, even if somehow exposed.
DENY_DOMAINS = frozenset({"update", "backup"})


@dataclass
class PendingConfirmations:
    """Per-conversation set of fingerprints awaiting a 'yes'."""

    by_conversation: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, conversation_id: str, fingerprint: str) -> None:
        self.by_conversation.setdefault(conversation_id, {})[fingerprint] = (
            time.monotonic() + CONFIRM_TTL
        )

    def take(self, conversation_id: str, fingerprint: str) -> bool:
        """Consume a matching, unexpired confirmation. Returns True if found."""
        pend = self.by_conversation.get(conversation_id)
        if not pend:
            return False
        expiry = pend.get(fingerprint)
        if expiry is None or expiry < time.monotonic():
            pend.pop(fingerprint, None)
            return False
        pend.pop(fingerprint, None)
        return True


def base_name(name: str) -> str:
    """Strip the optional ``namespace__`` MergedAPI prefix from a tool name."""
    return name.rsplit("__", 1)[-1]


def _unwrap(tool: llm.Tool) -> llm.Tool:
    return tool.tool if isinstance(tool, llm.NamespacedTool) else tool


def _resolve_states(
    hass: HomeAssistant, tool_args: dict[str, Any], assistant: str | None
) -> list[Any]:
    """Resolve a name/area/floor/domain slot to matching entity states."""
    domains = tool_args.get("domain")
    if isinstance(domains, str):
        domains = [domains]
    device_classes = tool_args.get("device_class")
    if isinstance(device_classes, str):
        device_classes = [device_classes]
    constraints = intent.MatchTargetsConstraints(
        name=tool_args.get("name"),
        area_name=tool_args.get("area"),
        floor_name=tool_args.get("floor"),
        domains=domains,
        device_classes=device_classes,
        assistant=assistant,
    )
    if not constraints.has_constraints:
        return []
    try:
        result = intent.async_match_targets(hass, constraints)
    except Exception:  # noqa: BLE001 - matching is best-effort for tiering
        return []
    return result.states if result.is_match else []


def compute_tier(
    intent_name: str, states: list[Any], bulk_threshold: int
) -> tuple[Tier, str]:
    """Return (tier, human summary) for a resolved action."""
    names = ", ".join(s.name for s in states) if states else "the requested target"

    if any(s.domain in DENY_DOMAINS for s in states):
        return Tier.NEVER, names

    if intent_name not in TIER_RESOLVED_INTENTS:
        if len(states) > bulk_threshold:
            return Tier.CONFIRM, names
        return Tier.AUTO, names

    if not states:
        return Tier.AUTO, names

    tier = Tier.AUTO
    closing = intent_name in ("HassTurnOff", "HassToggle", "HassStopMoving")
    for state in states:
        device_class = state.attributes.get("device_class")
        if state.domain == "lock" and intent_name in ("HassTurnOff", "HassToggle"):
            tier = max(tier, Tier.CONFIRM)  # unlocking
        elif state.domain in ("cover", "valve"):
            if device_class in ("garage", "gate", "door"):
                tier = max(tier, Tier.CONFIRM)
            elif closing:
                tier = max(tier, Tier.CONFIRM)

    if len(states) > bulk_threshold:
        tier = max(tier, Tier.CONFIRM)

    verb = {
        "HassTurnOff": "turn off / close",
        "HassTurnOn": "turn on / open",
        "HassToggle": "toggle",
        "HassSetPosition": "move",
        "HassStopMoving": "stop",
    }.get(intent_name, "act on")
    return tier, f"{verb} {names}"


def _fingerprint(intent_name: str, states: list[Any]) -> str:
    raw = intent_name + "|" + "|".join(sorted(s.entity_id for s in states))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@callback
def audit(
    hass: HomeAssistant,
    runtime: CooperRuntime,
    record: dict[str, Any],
) -> None:
    """Record a tool decision to the audit ring buffer + an HA event + the log."""
    runtime.audit_log.append(record)
    LOGGER.info("cooper tool %s -> %s", record.get("tool"), record.get("decision"))
    hass.bus.async_fire(EVENT_TOOL_EXECUTED, record)


class GuardedTool(llm.Tool):
    """Wraps an action tool, applying tiers/observe/kill before delegating."""

    def __init__(
        self,
        inner: llm.Tool,
        real: llm.Tool,
        runtime: CooperRuntime,
        conversation_id: str,
        kind: str,
    ) -> None:
        self._inner = inner
        self._real = real
        self._runtime = runtime
        self._conversation_id = conversation_id
        self._kind = kind
        # Preserve the schema the model sees.
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        runtime = self._runtime
        intent_name = base_name(self._real.name)
        record: dict[str, Any] = {
            "tool": self.name,
            "base": intent_name,
            "args": tool_input.tool_args,
            "conversation_id": self._conversation_id,
            "observe": runtime.observe_mode,
            "kill": runtime.kill_switch,
        }

        if runtime.kill_switch:
            record["decision"] = "refused_kill_switch"
            audit(hass, runtime, record)
            return {
                "status": "refused",
                "reason": "Cooper's kill switch is on; no actions can be taken.",
            }

        states = (
            _resolve_states(hass, tool_input.tool_args, llm_context.assistant)
            if self._kind == "intent"
            else []
        )
        tier, summary = compute_tier(
            intent_name, states, runtime.confirm_bulk_threshold
        )
        record["targets"] = [s.entity_id for s in states]
        record["tier"] = tier.name

        if runtime.observe_mode:
            record["decision"] = "observed"
            audit(hass, runtime, record)
            return {
                "status": "observe_mode",
                "would_have": summary,
                "note": "Observe mode is on, so nothing was changed.",
            }

        if tier is Tier.NEVER:
            record["decision"] = "refused_denylist"
            audit(hass, runtime, record)
            return {
                "status": "refused",
                "reason": f"Acting on {summary} is not permitted.",
            }

        if tier is Tier.CONFIRM:
            fingerprint = _fingerprint(intent_name, states)
            if runtime.pending_confirmations.take(self._conversation_id, fingerprint):
                record["decision"] = "confirmed"
                audit(hass, runtime, record)
                return await self._inner.async_call(hass, tool_input, llm_context)
            runtime.pending_confirmations.add(self._conversation_id, fingerprint)
            record["decision"] = "needs_confirmation"
            audit(hass, runtime, record)
            return {
                "status": "needs_confirmation",
                "summary": summary,
                "instructions": (
                    "Ask the user a single yes/no question to confirm. If they agree, "
                    "call this tool again with the same target."
                ),
            }

        record["decision"] = "executed"
        audit(hass, runtime, record)
        return await self._inner.async_call(hass, tool_input, llm_context)


class LoggedTool(llm.Tool):
    """Narrates a tool's call + result to Cooper's activity log, then delegates.

    Sits *outside* GuardedTool/CooperTool so it captures the user-visible call
    signature and the final result for **every** tool — including read-only ones
    (history, vision, recall) that never write an audit record. This is what brings
    back v1's per-tool ``call(args) -> result`` narration.
    """

    def __init__(self, inner: llm.Tool) -> None:
        self._inner = inner
        # Mirror the schema the model/chat-log routing sees.
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        base = base_name(self.name)
        try:
            result = await self._inner.async_call(hass, tool_input, llm_context)
        except Exception as err:  # noqa: BLE001 - narrate, then let HA's loop handle it
            _log.tool_call(base, tool_input.tool_args, {"error": str(err)})
            raise
        _log.tool_call(base, tool_input.tool_args, result)
        return result


@callback
def wrap_tools(
    hass: HomeAssistant, chat_log: conversation.ChatLog, runtime: CooperRuntime
) -> None:
    """Replace action tools with GuardedTool wrappers; narrate every tool.

    Each tool is then wrapped once more in ``LoggedTool`` so its call and result land
    in cooper.log / home-assistant.log (the Log Viewer add-on).
    """
    if chat_log.llm_api is None:
        return

    wrapped: list[llm.Tool] = []
    for tool in chat_log.llm_api.tools:
        real = _unwrap(tool)
        if isinstance(real, CooperTool):
            guarded: llm.Tool = tool  # tiers itself
        elif isinstance(real, llm.ScriptTool):
            guarded = GuardedTool(
                tool, real, runtime, chat_log.conversation_id, "script"
            )
        elif isinstance(real, llm.IntentTool) and real.name in ACTION_INTENTS:
            guarded = GuardedTool(
                tool, real, runtime, chat_log.conversation_id, "intent"
            )
        else:
            guarded = tool  # read-only / unknown → pass through
        wrapped.append(LoggedTool(guarded))

    chat_log.llm_api.tools = wrapped
