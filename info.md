# Cooper v2

A truly agentic AI for Home Assistant — one you talk to, that does things for you, and
that works on _your_ home out of the box.

Cooper grounds itself in your home automatically using Home Assistant's built-in LLM
Tools API (the same plumbing the official Anthropic/OpenAI/Gemini agents use), so it
works on a setup it has never seen with zero curation. It streams its reply to TTS as it
works, acts on reversible things immediately, asks before risky ones, remembers your
preferences, and can watch the home proactively.

- Runs on **any** install type (Core / Container / OS) as a HACS integration.
- **Bring your own** Anthropic API key (Claude-only for now).
- Ships with **observe mode** and a **kill switch**; locks, alarms and garages need a yes/no.
- Optional HAOS **Watcher** add-on for always-on watching (no key in the add-on).

## Setup

1. Install **Cooper v2** via HACS, then restart Home Assistant.
2. Add the **Cooper** integration and paste your Anthropic API key.
3. Create an agent and point an Assist pipeline at it (your default assistant is untouched).
   Start in observe mode to build trust.

See the [README](https://github.com/kunalkhosla/cooper-v2) for the full design and the
build plan.
