# Cooper Watcher

Optional add-on for Home Assistant OS / Supervisor. It is **not required** — the Cooper
integration delivers the full agent and proactivity on its own. This add-on only adds
always-on watching that the event bus cannot do cheaply, and it wakes Cooper through the
`cooper.proactive_check` service. It holds **no Anthropic key** and never controls the
home directly.

## Requirements

Install and configure the **Cooper** integration first (it provides the
`cooper.proactive_check` service).

## Options

| Option | Description |
|---|---|
| `poll_interval` | Seconds between camera polls (10–3600). |
| `cameras` | List of camera entity ids to watch for motion. |
| `motion_threshold` | Mean frame-diff score (0–255) that counts as motion. Higher = less sensitive. |
| `daily_review_time` | Optional `HH:MM` (local) to trigger a daily "review of the home". Empty to disable. |

## How it works

Each cycle the add-on pulls a snapshot of every configured camera through the Supervisor
core proxy, shrinks it to a 64×64 grayscale thumbnail, and compares it to the previous
frame. If the average pixel change crosses `motion_threshold`, it calls
`cooper.proactive_check` with a reason and the camera entity. Cooper then decides — under
its own guardrails — whether anything is worth telling you.
