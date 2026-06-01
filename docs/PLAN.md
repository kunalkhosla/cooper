# A portable, truly-agentic AI agent for Home Assistant

## Context

You want an AI agent layered on Home Assistant that you can **talk to** and that **does things for you agentically** — and, crucially, one you can **give to other HA users whose setups you've never seen**. That last requirement is the whole reason to start fresh rather than generalize your existing home agent (Cooper):

- **Cooper is home-specific by design.** It rolls its own grounding (`/states`/`/services` dumps, hand-built `get_live_context`/`get_home_map`, its own `call_service` tool) and leans on *your* hand-curated entity exposure. It never touches HA's built-in LLM tooling. Porting it to a stranger's home means re-curating everything.
- **This is a different product:** zero hardcoding, self-grounding on any install, distributable.

The key architectural bet: **let Home Assistant do the per-home grounding for us.** HA ships a built-in **LLM Tools API** (`homeassistant.helpers.llm`, the "Assist" API — the same one the official Anthropic/OpenAI/Gemini conversation integrations use). When a conversation agent opts in, HA auto-generates the exposed-home prompt + a curated set of intent tools *per install*, respecting each user's exposure settings. We build the smarts on top and add custom tools only for the gaps. That's what makes it work on setups we've never seen with no curation.

### Decisions locked with you
- **Distribution:** HACS **custom integration** (works on all HA install types — Core/Container/OS) as the primary product, **plus an optional add-on** (HAOS/Supervisor only) for an always-on watch "brain." The integration alone delivers agent + proactivity.
- **Grounding:** HA's built-in **LLM Tools API** (`LLM_API_ASSIST`) as the backbone + a small set of custom tools for gaps (history, camera vision, web search, automation authoring, proactivity, memory).
- **v1 scope:** Agent **+ proactivity** from day one.
- **Model:** BYO Anthropic API key, **Claude-only**, behind a thin provider interface so others can be added later. Prompt caching on.

## Two guiding principles (load-bearing)

1. **Smart by construction, not by enumeration.** One generic Claude tool-use loop over (HA's LLM-API tools + a few custom tools + memory). The system prompt is **short and principle-based** — no per-use-case `if/else`, no scenario catalogs, no "if user says X do Y." Intelligence comes from the model reasoning over grounded state and good tools. The *only* hard rules are mechanical **guardrail tiers keyed on service domain** (declarative policy derived from HA's own service registry — not use-case logic).

2. **Latency is a feature — optimize time-to-first-*audio*, not total completion.** Cooper's ~10s lag is the thing to beat. We stream Claude tokens straight into HA's chat-log delta API so TTS speaks sentence one while later tool calls still run; we prompt-cache the grounding block so first token is fast; we execute reversible actions optimistically; and we give *natural spoken* progress only when a tool genuinely takes time — never debug chatter.

---

## Architecture

```
                 ┌─────────────────────────────────────────────┐
 voice / chat ──▶│  HA Assist pipeline (STT → agent → TTS)      │
                 └───────────────┬─────────────────────────────┘
                                 ▼
        ┌────────────────────────────────────────────────────────┐
        │  Integration: ConversationEntity (Claude tool-use loop)  │
        │  • opts into LLM_API_ASSIST  → HA-generated grounding +   │
        │    intent tools (per-home, exposure-aware, free)         │
        │  • + our custom LLM API: history, vision, web search,     │
        │    automation authoring, memory, proactivity             │
        │  • guardrail tiers, observe mode, kill switch (in code)   │
        │  • streams deltas → chat log → TTS (low TTFA)             │
        └───────────────┬───────────────────────────┬─────────────┘
                        │ authors native            │ conversation.process /
                        ▼ HA automations/scripts     ▼ ha_<name>.proactive_check
            ┌───────────────────────┐     ┌────────────────────────────────┐
            │ HA core triggers       │     │ OPTIONAL add-on "watcher"        │
            │ (state/time/event) ────┼────▶│ (HAOS only): always-on watch /   │
            │ call back into agent   │     │ vision / scheduled jobs.          │
            └───────────────────────┘     │ Routes ALL actions back through  │
                                          │ the integration — never bypasses │
                                          └────────────────────────────────┘
```

**The integration is the product. The add-on is purely additive** (a trigger source that still routes every action through the integration's guardrails). Core/Container users get the full agent + proactivity; HAOS users can add the always-on watcher.

> **Naming:** repo/domain placeholder below is `ha_brain` — pick the real name before build (your house style: Cooper/Jarvis/Bellhop/Khouch). Domain must be a valid HA slug; replace `ha_brain` everywhere.

---

## Verified HA integration pattern

Confirmed against `home-assistant/core` **`dev`, fetched 2026-06-01** (via the official `anthropic` integration + `helpers/llm.py` + `components/conversation/chat_log.py`). A few internal names flagged **[verify on clone]** in Phase 0.

- **Entity:** subclass `conversation.ConversationEntity` (+ `AbstractConversationAgent`); set `_attr_supports_streaming = True`; `supported_languages = MATCH_ALL`; advertise `ConversationEntityFeature.CONTROL` when an LLM HASS API is configured.
- **Registration:** one **config entry** holds the API key; one **`ConfigSubentry`** per agent persona → one entity each (current HA pattern).
- **Message handling:** `_async_handle_message(user_input, chat_log)`:
  1. `chat_log.async_provide_llm_data(llm_context, user_llm_hass_api=[LLM_API_ASSIST, "ha_brain"], user_llm_prompt, user_extra_system_prompt)` — resolves both APIs into one merged `APIInstance` (`chat_log.llm_api`), auto-builds the exposed-home prompt, loads intent tools **and** our tools.
  2. `await self._async_handle_chat_log(chat_log)` — the loop.
  3. `return conversation.async_get_result_from_chat_log(user_input, chat_log)`.
- **The loop (heart of it):** `chat_log.async_add_delta_content_stream(self.entity_id, _transform(stream))` — we feed it our provider's normalized deltas (`AssistantContentDeltaDict`); it accumulates text/thinking/tool_calls, **auto-executes tool calls via `chat_log.llm_api.async_call_tool(...)`**, and yields `ToolResultContent`. Loop until no unresponded tool results, max ~10 iterations. **We do not write our own tool executor or grounding prompt.**
- **Custom tools:** `llm.async_register_api(hass, HaBrainAPI(...))` in `__init__.py`; pass both API ids to `async_provide_llm_data`. `async_get_api([...])` merges them; `async_call_tool` dispatches by name. Each tool = `llm.Tool` subclass (`name`, `description`, `parameters: vol.Schema`, `async_call(hass, tool_input, llm_context)`).
- **Prompt caching:** Anthropic `cache_control: ephemeral` breakpoints; HA's pattern formats `vol.Schema` → JSON schema and strips unsupported `anyOf`/`oneOf`.
- **Proactivity hook:** `conversation.process` service (`text`, `agent_id`, `conversation_id`) lets any HA automation call back into the agent.

---

## Agent loop & latency design

**Single per-turn flow:** build `system` = short persona prompt + merged `llm_api.api_prompt` (HA's grounding) + compact memory block; `tools` = `[_format_tool(t) for t in llm_api.tools]`; `messages` = converted chat-log content → `client.messages.create(stream=True)` → map Anthropic SSE events to HA deltas → push through `async_add_delta_content_stream`. Multi-step "planning" is just the native tool-use loop (no custom planner). **Clarifying questions fall out for free:** when the model needs info it emits a plain text turn with no tool calls → loop ends → that text *is* the spoken question (persona prompt nudges: "if ambiguous or risky-and-underspecified, ask one concise question instead of guessing").

**Latency budget — concrete techniques:**
- **Stream to TTS sentence-by-sentence.** `_attr_supports_streaming = True` + delta stream means the pipeline speaks sentence one immediately. The spoken answer *is* the feedback.
- **Prompt-cache the stable blocks** (persona → tool defs → HA grounding `api_prompt` → memory), longest-stable first, so first token after cache warm is fast and cheap. This directly kills Cooper's "8k-token state dump billed/processed every turn" stall.
- **Default to a fast Claude model** (e.g. a Sonnet tier for true agentic reasoning; Haiku selectable for snappiest). One model, no per-use-case tiering. Model id is a config option.
- **Optimistic execution** of reversible (auto-tier) actions — fire the service call as the model emits it; don't wait for a final confirmation turn.
- **Natural spoken progress only when warranted** — if a tool will take time (camera vision, web search), the model narrates one short human line ("Let me check the back camera…") then continues. Never emit debug/tool logs to voice.
- **Avoid extra round-trips:** simple Q&A and single commands resolve in one model call → one stream. Don't gate trivial answers behind tool calls.

**Memory:** short-term = HA's per-`conversation_id` `ChatLog` (free). Long-term preferences = `RememberTool`/`RecallTool` backed by `helpers.storage.Store`, namespaced per user; a compact "known preferences" block is injected into the (cached) system prompt and summarized when it grows.

---

## Proactivity (portable, all install types)

The agent **authors native HA automations/scripts** whose trigger is anything HA supports and whose action calls back into the agent via a custom service `ha_brain.proactive_check` (or `conversation.process`). Example the agent writes:

```yaml
trigger: {platform: numeric_state, entity_id: sensor.basement_humidity, above: 70}
action:
  - service: ha_brain.proactive_check
    data: {reason: "basement humidity high", context_entities: [sensor.basement_humidity, fan.basement]}
```

`ha_brain.proactive_check` opens a fresh session, seeds "you were triggered proactively because…", runs the **same loop** under the same guardrails, and delivers via an exposed `notify`/TTS service the model chooses. This gives Core/Container users full proactivity with **no add-on**. A **cooldown/max-frequency guard** in the authoring tool prevents runaway proactive loops.

**The optional add-on** (HAOS only) adds what HA's event bus/cron can't do cheaply: continuous/event-driven **camera-vision watch loops**, debounced/batched event watching, and heavier **scheduled reasoning** ("review of the day," history anomaly scans). It authenticates via the Supervisor token and, when it decides to act/speak, calls `ha_brain.proactive_check` — so **all guardrails/exposure/audit live in the integration and apply uniformly.** The add-on is a trigger source, never a second brain with its own authority.

---

## Safety & guardrails (mechanical, not use-case logic)

HA's LLM API already gates *which entities exist to the model* (exposure) and validates intent targets. On top, in `guardrails.py` (generic, no per-home lists):

- **Tiers by domain + service verb** (from HA's own service registry):
  - *Auto (reversible):* lights, switches, scenes, media, climate setpoint, opening covers → execute (optimistically).
  - *Confirm (risky):* `lock.*`, garage/gates, `alarm_control_panel.*`, closing covers, destructive automation edits, bulk actions over a threshold → tool returns "needs confirmation"; model surfaces a yes/no; only a confirmed follow-up turn proceeds (token keyed to `conversation_id`).
  - *Never:* configurable denylist (`homeassistant.stop`, `hassio.*`, `update.*` installs, config/host ops) → deterministic refusal.
- **Observe mode** (default on at onboarding): acting tools return "would have done X"; read tools still run. Builds trust on a new home.
- **Kill switch:** service + toggle → instant refuse-all.
- **Deterministic validation of authored automations** (`validation.py`): every `entity_id` exists in the state machine, every `service` exists via `hass.services.has_service`, areas exist, YAML matches HA's voluptuous schema. Reject invalid config with a structured error the model can fix. Tag agent-authored automations with a known prefix/label for audit + bulk removal.
- **Audit log** of every tool execution (tier, args, result, observe/kill state).

---

## Config & onboarding

`config_flow.py` mirrors the verified `anthropic` structure:
- `async_step_user`: enter Anthropic key → validate (`client.models.list`) → create main entry (key only).
- `ConfigSubentryFlow` per persona: name, short persona prompt, `CONF_LLM_HASS_API` (default `[assist, ha_brain]`), control toggle, observe-mode default, proactivity toggle, recommended-vs-advanced (model, max_tokens, caching, thinking, web_search).
- Options flow to change later.

**A stranger installs by:** HACS → add repo → install → restart → add integration → paste key → create agent subentry (Control on, Observe on) → point a **new Assist pipeline** at it (default agent untouched). HAOS users optionally install the watcher add-on from the same repo.

---

## Repo / distribution layout

```
/  (single GitHub repo)
  hacs.json
  custom_components/ha_brain/        # the integration (HACS installs only this)
  addon/ha_brain_watcher/            # optional add-on (config.yaml, Dockerfile, build.yaml, watcher/)
  repository.yaml                    # makes the repo an add-on repository too
  .github/workflows/validate.yml     # hassfest + HACS validation + add-on lint
  README.md  info.md  LICENSE
```
HACS needs `manifest.json` (`version`, `documentation`, `issue_tracker`, `codeowners`), `hacs.json`, semver release tags, passing hassfest. Add-on multi-arch images built in CI → GHCR; users add the repo URL in the Add-on Store. Follow your existing GH-Actions/GHCR conventions; reuse the public/private mirror pattern if you want a private canonical.

### Integration file tree
```
custom_components/ha_brain/
  manifest.json          const.py            __init__.py        config_flow.py
  coordinator.py         entity.py           conversation.py    guardrails.py
  memory.py              services.yaml       strings.json       translations/en.json
  provider/  base.py (LLMProvider protocol)  anthropic_client.py (Claude streaming + delta mapping)
  llm_api/   custom_api.py (HaBrainAPI)       validation.py
             tools_history.py  tools_vision.py  tools_websearch.py
             tools_automation.py  tools_memory.py  tools_proactivity.py
```
**Most critical files:** `conversation.py`, `entity.py`, `llm_api/custom_api.py`, `config_flow.py`, `guardrails.py`, `provider/anthropic_client.py`.

---

## Verification plan (real HAOS at 192.168.0.194, side-by-side, non-disruptive)

Principle: **never touch the default agent/pipeline.** Use a separate test agent + test pipeline.
1. Copy `custom_components/ha_brain/` to `/config/custom_components/` (SSH add-on port 22220 / Samba), restart, add integration with your key.
2. New Assist pipeline "Brain Test" → conversation agent = HA Brain. Default stays default.
3. **Grounding proof (observe on):** `POST /api/conversation/process {text, agent_id}` (or the HA MCP tools) → ask "what lights are in the living room?" — answer must reflect *this* home with zero hardcoding.
4. **Custom tools:** history ("when did the front door last open?"), vision ("what's on the driveway camera?"), web search.
5. **Multi-step + latency:** "if the basement's humid, tell me and suggest a fix" → confirm multiple tool calls in one turn; eyeball time-to-first-token.
6. **Guardrails:** observe-on "turn off all lights" → "would have…"; observe-off reversible action → executes; "unlock front door" → confirmation prompt, not action.
7. **Authoring + proactivity:** create a proactive automation → bad entity rejected, good one written/tagged → force the trigger via REST → agent runs and notifies, all on the test path.
8. **Add-on (last, HAOS):** install watcher → confirm Supervisor-token auth and that its actions route through `ha_brain.proactive_check`.
9. **Regression:** default agent + pipeline still answer normally.
Rollback: remove test pipeline, delete subentry, remove folder, restart.

---

## Phased build order (to reach the v1 you want: agent + proactivity)

- **Phase 0 — Spike/verify:** shallow-clone `home-assistant/core`; confirm flagged names (`unresponded_tool_results`, `_async_handle_chat_log` internals, tool-call `id`/`external` placement, list-merge in `async_provide_llm_data`, current Anthropic SDK + model ids). Stand up a no-op `ConversationEntity` that echoes via the delta stream on the real box. *Smallest installable proof.*
- **Phase 1 — Grounded conversational agent:** manifest/const/config_flow (key + one subentry), Claude streaming provider, entity loop, `async_provide_llm_data([assist])`. A grounded Claude agent that controls the home via Assist, streaming to TTS. HACS-installable.
- **Phase 2 — Custom tools + memory:** register `ha_brain` API; history, vision, web search, memory tools; prompt-cache breakpoints; latency tuning.
- **Phase 3 — Guardrails:** tiers, observe mode, kill switch, audit log, automation `validation.py`.
- **Phase 4 — Proactivity (portable):** authoring tools + `ha_brain.proactive_check` callback + cooldown guard. **← this completes the v1 product on all install types.**
- **Phase 5 — Add-on:** watcher container, Supervisor-token client, watch/vision/scheduled loops routing through the integration.
- **Phase 6 — Distribution polish:** translations, hassfest/HACS CI, docs, release tags, optional HACS listing.

## Open items / risks
- **Name** the product/domain before Phase 1.
- Confirm Phase-0 flagged HA internals + Anthropic SDK pin/model ids.
- Prompt-cache churn if a home's exposure changes often (acceptable; recompute api_prompt).
- Cost of vision/agentic loops (mitigated by caching + observe default + model choice).
- Prevent proactive-automation loops (cooldown/max-frequency guard — built into the authoring tool).
- HACS listing + add-on multi-arch CI have lead time; not blockers for personal/side-load install.
