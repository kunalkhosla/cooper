"""Media tools: put things on a TV via the household TV app.

Two tools over a self-hosted IPTV front-end that exposes a conversational
``/api/assistant`` endpoint (finds + queues a play) and an ``/api/search/all``
endpoint (browse):

* ``search_tv`` — BROWSE: list matches for "show me / what do you have", no play.
* ``watch_tv``  — PLAY: a decisive "put on <X> on the <room> TV". It asks the app
  to pick the best match, then casts the result to the chosen ``media_player`` via
  an HA script (``script.play_iptv`` by default) that resolves a fresh signed
  stream URL. Plays immediately — the household runs one stream at a time and chose
  no confirmation — but the kill switch and observe mode still apply.

Config lives OUTSIDE this (public) repo in ``/config/cooper_media.json`` so the URL
and app credentials aren't committed::

    {
      "url": "http://TV_APP_HOST",
      "user": "<APP_USER>",
      "pass": "<APP_PASS>",
      "play_script": "script.play_iptv"   # optional; HA script that casts a stream
    }

Empty/missing config leaves both tools disabled (they return a clear "not configured").
"""

from __future__ import annotations

import json
import os
from base64 import b64encode

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from ..guardrails import CooperTool, audit
from ._guard import precheck_write

CONFIG_PATH = os.environ.get("COOPER_MEDIA_CONFIG", "/config/cooper_media.json")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _auth_header(cfg: dict) -> dict:
    """HTTP Basic auth header from the app's APP_USER/APP_PASS, if set."""
    user, pw = cfg.get("user", ""), cfg.get("pass", "")
    if not user and not pw:
        return {}
    token = b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _parse_ref(ref: str) -> tuple[str, int] | None:
    """A play ref is "<mode>:<id>:<ext>" (mode live|movie|series; series refs are
    already resolved to an episode id). The cast script derives the ext itself, so
    only mode + id matter. Returns (mode, id) or None for anything malformed."""
    parts = str(ref or "").split(":")
    if len(parts) < 2 or parts[0] not in ("live", "movie", "series"):
        return None
    try:
        sid = int(parts[1])
    except (TypeError, ValueError):
        return None
    return (parts[0], sid) if sid >= 0 else None


class SearchTvTool(CooperTool):
    """Browse the household TV app — list matches without playing anything."""

    name = "search_tv"
    description = (
        "BROWSE the household TV app — list what's available WITHOUT playing anything. Use for "
        "'show me…', 'what movies/channels do you have', 'find me…', 'is there any…'. Read a few "
        "of the matches aloud and offer to put one on (the user then says 'put on <title>' and you "
        "call watch_tv). 'query' is the search terms (a title, person, or genre like 'funny kids "
        "movies'); optional 'kind' restricts to live/movie/series."
    )
    parameters = vol.Schema(
        {
            vol.Required("query"): str,
            vol.Optional("kind"): vol.In(["live", "movie", "series"]),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        cfg = await hass.async_add_executor_job(_load_config)
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            return {"status": "error", "reason": f"TV app not configured ({CONFIG_PATH} missing 'url')"}
        query = str(tool_input.tool_args.get("query") or "").strip()
        if not query:
            return {"status": "error", "reason": "empty query — what should I look for?"}
        kind = tool_input.tool_args.get("kind")

        session = async_get_clientsession(hass)
        try:
            async with session.get(
                f"{url}/api/search/all",
                params={"q": query},
                headers=_auth_header(cfg),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return {"status": "error", "reason": f"search returned {resp.status}"}
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"search failed: {err}"}

        kinds = [kind] if kind else ["live", "movie", "series", "disk"]
        results: list[dict] = []
        for k in kinds:
            items = data.get(k) if isinstance(data, dict) else None
            for it in (items or [])[:4]:
                if isinstance(it, dict) and it.get("name"):
                    entry = {"title": it["name"], "kind": k}
                    if it.get("year"):
                        entry["year"] = it["year"]
                    results.append(entry)

        if not results:
            return {"status": "ok", "results": [], "note": f'No matches for "{query}".'}
        return {
            "status": "ok",
            "results": results,
            "note": (
                "Read a FEW of these aloud naturally and offer to put one on (the user says "
                "'put on <title>' → watch_tv). Do NOT play anything now."
            ),
        }


class WatchTvTool(CooperTool):
    """Play something on a TV via the household TV app."""

    name = "watch_tv"
    description = (
        "PLAY something on a TV via the household TV app. Use for a decisive 'put on / play <X> "
        "[on the <room> TV]' — a channel, movie, show, or sports match. NOT for 'show me / what do "
        "you have' (that's search_tv). It plays IMMEDIATELY. 'request' is the natural-language ask "
        "('the cricket', 'a funny movie', 'the news'). 'target' is the media_player entity_id of "
        "the TV and is REQUIRED: if the user did not name a TV, ASK which one first, then resolve "
        "that name to a REAL media_player entity_id — never guess a default. If the app can't decide "
        "from an ambiguous request it returns a question instead of playing — relay it and ask the "
        "user to be more specific."
    )
    parameters = vol.Schema(
        {
            vol.Required("request"): str,
            vol.Required("target"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        runtime = get_runtime(hass)
        cfg = await hass.async_add_executor_job(_load_config)
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            return {
                "status": "error",
                "reason": f"TV app not configured ({CONFIG_PATH} missing 'url') — tell the user, don't claim you put anything on",
            }
        request = str(tool_input.tool_args.get("request") or "").strip()
        target = str(tool_input.tool_args.get("target") or "").strip()
        if not request:
            return {"status": "error", "reason": "empty request — what should I put on?"}
        if not target.startswith("media_player."):
            return {"status": "error", "reason": f'target must be a media_player entity_id, got "{target}"'}
        if hass.states.get(target) is None:
            return {
                "status": "error",
                "reason": f"no such entity: {target} — resolve the TV name to a REAL media_player entity_id first, don't invent one",
            }

        # A real cast is a write: honor the kill switch + observe mode. The household
        # chose immediate play, so there is no confirmation gate (confirmed=True).
        blocked = precheck_write(
            hass,
            runtime,
            tool=self.name,
            summary=f'play "{request}" on {target}',
            args={"request": request, "target": target},
            confirmed=True,
        )
        if blocked is not None:
            return blocked

        # Ask the app to decide + play. Frame decisively so it PLAYS rather than
        # asking us to confirm; a genuinely empty match comes back as a no-action reply.
        utterance = (
            "Play this now — pick the single best match and play it immediately, "
            f"do not ask me to confirm: {request}"
        )
        session = async_get_clientsession(hass)
        try:
            async with session.post(
                f"{url}/api/assistant",
                json={"utterance": utterance},
                headers=_auth_header(cfg),
                timeout=aiohttp.ClientTimeout(total=35),  # the app's model loop can take a while
            ) as resp:
                if resp.status != 200:
                    return {"status": "error", "reason": f"TV app returned {resp.status} — tell the user it didn't work"}
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"couldn't reach the TV app: {err} — tell the user it didn't work"}

        action = data.get("action") if isinstance(data, dict) else None
        reply = str((data or {}).get("reply") or "") if isinstance(data, dict) else ""
        if not (isinstance(action, dict) and action.get("type") == "play"):
            return {
                "status": "not_played",
                "reply": reply or "nothing found",
                "note": (
                    "The app didn't pick something to play (ambiguous request or no match). Relay "
                    "its reply and ask the user to be more specific; do NOT claim you put anything on."
                ),
            }
        parsed = _parse_ref(action.get("ref"))
        if not parsed:
            return {
                "status": "error",
                "reason": f"the app returned an unplayable result{f' ({reply})' if reply else ''} — tell the user it couldn't play that",
            }
        mode, sid = parsed

        # "script.play_iptv" -> domain "script", service "play_iptv" (accept a bare
        # service name too). The script resolves a fresh signed stream and casts it.
        play_script = cfg.get("play_script") or "script.play_iptv"
        if "." not in play_script:
            play_script = f"script.{play_script}"
        pdomain, psvc = play_script.split(".", 1)
        try:
            await hass.services.async_call(
                pdomain, psvc, {"mode": mode, "stream_id": sid, "target": target}, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"cast failed: {err} — tell the user it didn't work"}

        title = action.get("title") or request
        audit(
            hass,
            runtime,
            {
                "tool": self.name,
                "base": self.name,
                "args": {"request": request, "target": target, "ref": action.get("ref")},
                "tier": "AUTO",
                "decision": "executed",
                "observe": runtime.observe_mode,
                "kill": runtime.kill_switch,
            },
        )
        return {"status": "done", "title": title, "target": target, "note": f'Now playing "{title}".'}
