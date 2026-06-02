"""Constants for the Cooper integration."""

from __future__ import annotations

from enum import StrEnum
import logging

DOMAIN = "cooper"
LOGGER = logging.getLogger(__package__)

# Title of the config entry that holds the API key.
ENTRY_TITLE = "Cooper"
DEFAULT_CONVERSATION_NAME = "Cooper"

# Subentry type for conversation agents (one entity per subentry).
SUBENTRY_TYPE_CONVERSATION = "conversation"

# Config option keys (HA core supplies CONF_API_KEY / CONF_LLM_HASS_API / CONF_NAME).
CONF_PROMPT = "prompt"
CONF_RECOMMENDED = "recommended"
CONF_CHAT_MODEL = "chat_model"
CONF_MAX_TOKENS = "max_tokens"
CONF_PROMPT_CACHING = "prompt_caching"
CONF_THINKING_BUDGET = "thinking_budget"
CONF_WEB_SEARCH = "web_search"
CONF_WEB_SEARCH_MAX_USES = "web_search_max_uses"
CONF_OBSERVE_MODE = "observe_mode"
CONF_PROACTIVITY = "proactivity"
CONF_CONFIRM_BULK_THRESHOLD = "confirm_bulk_threshold"

# Service names.
SERVICE_PROACTIVE_CHECK = "proactive_check"
SERVICE_SET_OBSERVE_MODE = "set_observe_mode"
SERVICE_KILL_SWITCH = "kill_switch"

# Event fired for every guarded/audited tool execution.
EVENT_TOOL_EXECUTED = "cooper_tool_executed"

# Prefix/label stamped on every automation Cooper authors, for audit + bulk removal.
AUTHORED_PREFIX = "cooper_"
AUTHORED_LABEL = "cooper-authored"


class PromptCaching(StrEnum):
    """Prompt caching options (mirrors the upstream anthropic integration)."""

    OFF = "off"
    PROMPT = "prompt"
    AUTOMATIC = "automatic"


MIN_THINKING_BUDGET = 1024

# Recommended defaults for a new agent. The model list is fetched live from the
# Anthropic API, so the chat model is only a default the user can override.
DEFAULT: dict[str, object] = {
    CONF_CHAT_MODEL: "claude-opus-4-8",
    CONF_MAX_TOKENS: 1500,
    CONF_PROMPT_CACHING: PromptCaching.PROMPT.value,
    CONF_THINKING_BUDGET: 0,
    CONF_WEB_SEARCH: False,
    CONF_WEB_SEARCH_MAX_USES: 5,
    CONF_OBSERVE_MODE: True,
    CONF_PROACTIVITY: True,
    CONF_CONFIRM_BULK_THRESHOLD: 5,
}

# Short, principle-based persona. Intelligence comes from the model reasoning over
# grounded state and tools, NOT from enumerated use cases. Keep this stable: it is
# the head of the cached prompt prefix.
COOPER_PERSONA_PROMPT = """\
You are Cooper, a capable, warm assistant with agency over this Home Assistant home.
You can see the home through the provided tools and context, and you act on the user's behalf.

How you operate:
- Be brief and natural. Your replies are usually spoken aloud, so answer in a sentence or two
  unless asked for detail. Never read out tool names, entity ids, or any debug detail.
- Reason over the live home. When asked what is happening now, check real state with the tools
  before answering instead of guessing.
- Act when asked. For routine, reversible things (lights, scenes, media, fans, climate setpoints)
  just do it; don't ask permission for the harmless stuff.
- Respect safety results. If a tool says an action needs confirmation, ask one clear yes/no
  question and only proceed once the user agrees. If a tool says it only *would have* acted
  (observe mode) or that it refuses an action, relay that plainly and move on.
- Ask only when it matters. If a request is ambiguous or risky and underspecified, ask one
  concise question rather than guess. Otherwise prefer acting.
- Narrate slow work. Before something that takes a moment (looking at a camera, searching the
  web), say one short, human line, then continue.
- Remember the person. Use what you already know about their preferences, and when they tell you
  a lasting preference, save it.
"""

# Seed prepended (as extra system prompt) when the agent is woken by a proactive
# trigger rather than a person. {reason} is filled in by the service handler.
PROACTIVE_SEED = """\
You were triggered proactively — no one is necessarily talking to you right now. The reason is:
{reason}

Look at the relevant state, then decide whether anything is genuinely worth doing or telling the
user at this moment. If nothing is worth surfacing, do nothing and say nothing. If you should
reach the user, keep it to one short line and deliver it with the broadcast or notify tool, since
there may be no active conversation to speak into.
"""
