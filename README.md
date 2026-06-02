<p align="center">
  <img src="brand/icon.svg" alt="Cooper" width="118" />
</p>

<h1 align="center">Cooper</h1>

<p align="center">
  <strong>A truly agentic AI for Home Assistant — one you talk to, that does things for you,<br/>
  and that works on <em>your</em> home out of the box, whatever you've got plugged in.</strong>
</p>

You speak (or type) to it. It reasons over your live home, takes the safe stuff itself, asks before the risky stuff, remembers what you like, and can watch things for you proactively. It is not a pile of automations and it is not a rigid voice command parser — it's an agent.

> Status: **running on Home Assistant** → see [`docs/PLAN.md`](docs/PLAN.md) for the architecture and [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) for the build.

---

## Where Cooper came from, and why it fell short

Cooper (v1) was a Claude-powered Home Assistant agent built for exactly one home. It genuinely worked — guardrails, camera vision, presence simulation, native-automation authoring, an end-to-end phone→voice bridge. But it was built _for that house_, and three limits capped it:

1. **It only knew _that_ home.** Cooper grounded itself by dumping its own snapshot of `/states` and `/services` and leaned on hand-curated entity exposure. That meant you couldn't hand it to another Home Assistant user — it had no idea what _their_ entities, areas, or naming were, and re-curating a stranger's home by hand defeats the point.
2. **It was add-on-only.** Cooper ran as a Home Assistant OS add-on. Anyone on HA Core or Container — a big slice of the community — couldn't run it at all.
3. **It was slow, and it was scaffolded.** ~10 seconds before you heard a voice come back, because it processed a large state blob every turn and waited for the whole answer before speaking. And too much of its "intelligence" lived in hand-written use-case prompting rather than in the model.

## Why Cooper is the answer

Cooper is a fresh build (not a refactor) that fixes all three at the root:

1. **It grounds itself in _your_ home — automatically.** Instead of reinventing grounding, Cooper builds on Home Assistant's **built-in LLM Tools API** (the same plumbing the official Anthropic/OpenAI/Gemini conversation integrations use). HA itself generates the description of _your_ exposed devices, areas, and floors and hands the model a curated, exposure-aware tool set — per install, with zero curation from you. Install it on a home it has never seen and it just works.
2. **It runs anywhere.** Cooper ships as a **HACS custom integration** that works on every HA install type — Core, Container, or OS. An **optional add-on** adds an always-on watcher for people on HAOS, but the full agent (including proactivity) works without it.
3. **It's fast, and it streams.** It speaks the first sentence while it's still working the rest, caches the grounding so the first token is quick, and acts on reversible things optimistically. You get a voice back in a beat, not ten seconds.
4. **It's smart by construction, not by enumeration.** One generic reasoning loop over grounded tools and memory — no per-scenario `if/else`, no catalog of hand-written use cases. The only hard rules are mechanical safety tiers (e.g. _confirm before unlocking a door_). Everything else is the model thinking over real state.

## What it can do

- **Talk and act:** "turn the kitchen down to movie level," "is everything okay downstairs?", "why is the office cold?" — it answers from live state and acts within safe bounds.
- **Use judgment:** ambiguous or risky-and-underspecified? It asks one concise question instead of guessing.
- **Stay safe:** reversible actions run immediately; locks, alarms, garages and the like require a yes/no; a denylist is refused outright. Ships with an **observe mode** and a **kill switch**.
- **Be proactive:** ask it to keep an eye on something and it writes a native HA automation that calls back into the agent when it fires — proactivity that survives restarts and works on any install type.
- **Remember you:** learns and recalls your preferences across conversations.
- **Clean up after itself:** every automation/script Cooper authors is stamped with a `cooper_` id. It can list and delete **only its own** — ask "clean up the automations you made that aren't used" and it lists them, asks, and removes the stale ones. Your hand-made automations are off-limits by construction.

## Cleanup & the weekly review

Cooper can prune the automations and scripts **it** authored — never yours.

- `list_cooper_items` — shows every `cooper_`-stamped automation/script with its last-run time.
- `delete_cooper_item` — deletes one, confirm-tier (it asks yes/no; blocked by observe mode / kill switch). A hard rule, enforced in code in two places, refuses any id without the `cooper_` prefix, so your own automations can't be touched.

Deletion is **always on-demand and confirmed** — Cooper never auto-deletes.

**Weekly review — built in, no automation needed.** Cooper runs a cleanup review once a week on its own (Sunday morning): it scans its authored items, flags stale ones, and sends you a *"want me to delete these?"* message — **suggest-only**, it deletes nothing. You confirm later in conversation. Configure it in the agent's options:

- **Weekly cleanup review** — turn the review on/off (on by default).
- **Cleanup review — notify devices** — pick one or more `notify.*` devices for the message (a phone push); leave empty for Home Assistant's notification bell.

Prefer a custom schedule or trigger? The review is just the `cooper.review_cleanup` service — call it from your own automation:

```yaml
actions:
  - action: cooper.review_cleanup
    data:
      notify_target: [notify.mobile_app_your_phone]   # omit for the notification bell
```

> If you wrap it in your own automation, give that automation a **non-`cooper_`** id so the review can't list or delete itself.

To try it now: Developer Tools → Actions → `cooper.review_cleanup` → Run.

## Install (planned)

1. Add this repo to HACS → install **Cooper** → restart Home Assistant.
2. Add the integration, paste your **Anthropic API key** (bring your own), create an agent.
3. Point an Assist pipeline at it (your default assistant stays untouched). Start in observe mode.
4. _Optional (HAOS):_ install the **Cooper Watcher** add-on for always-on watching.

## Best practices — set up your home so Cooper shines

Cooper sees the entities you **expose**, identifies them by **friendly name**, and reasons over
their **areas, floors, and zones**. A little Home Assistant hygiene — clear names, no collisions,
everything placed in an area, people/zones/location sensors set up, cameras named by what they
watch — is the difference between "telepathic" and "guessing."

➡️ **[`docs/BEST_PRACTICES.md`](docs/BEST_PRACTICES.md)** covers exactly what to do for
exposure, naming & aliases, areas/floors, **people · presence · location**, **cameras & vision**,
lock/garage safety tiering, history, the voice pipeline, and a verification checklist.

## Architecture & plan

The full design — verified against current Home Assistant internals — lives in [`docs/PLAN.md`](docs/PLAN.md): the HA LLM-API grounding backbone, the streaming low-latency loop, the portable proactivity model, guardrails, the optional add-on boundary, and the phased build order.

---

_Bring your own Anthropic API key. Claude-only for now, behind a thin provider interface so other models can be added later._
