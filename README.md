# Cooper v2

**A truly agentic AI for Home Assistant — one you talk to, that does things for you, and that works on _your_ home out of the box, whatever you've got plugged in.**

You speak (or type) to it. It reasons over your live home, takes the safe stuff itself, asks before the risky stuff, remembers what you like, and can watch things for you proactively. It is not a pile of automations and it is not a rigid voice command parser — it's an agent.

> Status: **planning** → see [`docs/PLAN.md`](docs/PLAN.md) for the full architecture and build order.

---

## Where Cooper came from, and why it fell short

Cooper (v1) was a Claude-powered Home Assistant agent built for exactly one home. It genuinely worked — guardrails, camera vision, presence simulation, native-automation authoring, an end-to-end phone→voice bridge. But it was built _for that house_, and three limits capped it:

1. **It only knew _that_ home.** Cooper grounded itself by dumping its own snapshot of `/states` and `/services` and leaned on hand-curated entity exposure. That meant you couldn't hand it to another Home Assistant user — it had no idea what _their_ entities, areas, or naming were, and re-curating a stranger's home by hand defeats the point.
2. **It was add-on-only.** Cooper ran as a Home Assistant OS add-on. Anyone on HA Core or Container — a big slice of the community — couldn't run it at all.
3. **It was slow, and it was scaffolded.** ~10 seconds before you heard a voice come back, because it processed a large state blob every turn and waited for the whole answer before speaking. And too much of its "intelligence" lived in hand-written use-case prompting rather than in the model.

## Why Cooper v2 is the answer

Cooper v2 is a fresh build (not a refactor) that fixes all three at the root:

1. **It grounds itself in _your_ home — automatically.** Instead of reinventing grounding, Cooper v2 builds on Home Assistant's **built-in LLM Tools API** (the same plumbing the official Anthropic/OpenAI/Gemini conversation integrations use). HA itself generates the description of _your_ exposed devices, areas, and floors and hands the model a curated, exposure-aware tool set — per install, with zero curation from you. Install it on a home it has never seen and it just works.
2. **It runs anywhere.** Cooper v2 ships as a **HACS custom integration** that works on every HA install type — Core, Container, or OS. An **optional add-on** adds an always-on watcher for people on HAOS, but the full agent (including proactivity) works without it.
3. **It's fast, and it streams.** It speaks the first sentence while it's still working the rest, caches the grounding so the first token is quick, and acts on reversible things optimistically. You get a voice back in a beat, not ten seconds.
4. **It's smart by construction, not by enumeration.** One generic reasoning loop over grounded tools and memory — no per-scenario `if/else`, no catalog of hand-written use cases. The only hard rules are mechanical safety tiers (e.g. _confirm before unlocking a door_). Everything else is the model thinking over real state.

## What it can do

- **Talk and act:** "turn the kitchen down to movie level," "is everything okay downstairs?", "why is the office cold?" — it answers from live state and acts within safe bounds.
- **Use judgment:** ambiguous or risky-and-underspecified? It asks one concise question instead of guessing.
- **Stay safe:** reversible actions run immediately; locks, alarms, garages and the like require a yes/no; a denylist is refused outright. Ships with an **observe mode** and a **kill switch**.
- **Be proactive:** ask it to keep an eye on something and it writes a native HA automation that calls back into the agent when it fires — proactivity that survives restarts and works on any install type.
- **Remember you:** learns and recalls your preferences across conversations.

## Install (planned)

1. Add this repo to HACS → install **Cooper v2** → restart Home Assistant.
2. Add the integration, paste your **Anthropic API key** (bring your own), create an agent.
3. Point an Assist pipeline at it (your default assistant stays untouched). Start in observe mode.
4. _Optional (HAOS):_ install the **Cooper v2 Watcher** add-on for always-on watching.

## Architecture & plan

The full design — verified against current Home Assistant internals — lives in [`docs/PLAN.md`](docs/PLAN.md): the HA LLM-API grounding backbone, the streaming low-latency loop, the portable proactivity model, guardrails, the optional add-on boundary, and the phased build order.

---

_Bring your own Anthropic API key. Claude-only for now, behind a thin provider interface so other models can be added later._
