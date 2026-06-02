"""Recorded-footage vision tool: look at a camera at a PAST moment, not live.

The live ``look_at_camera`` tool only grabs a current frame. This one reaches into the
camera's recordings: it walks Home Assistant's Reolink ``media_source`` tree
(CAM -> RES(main) -> day -> 5-minute file), finds the clip covering the requested
timestamp, resolves it to a (token-authed) URL, extracts the single frame at the right
offset with ffmpeg, and hands that frame to Claude vision — the same ``describe_image``
path the live tool uses.

**Provider-specific by nature.** Every NVR exposes recordings with a different
media_source tree and time encoding, so "frame at time T" can't be universal. This
supports **Reolink** today (the layout above); on a home without Reolink recordings it
degrades gracefully with a clear message rather than failing. Adding another NVR (UniFi
Protect, Frigate, …) means adding a sibling finder — keep the tool/ffmpeg/vision parts,
swap the media_source walk. Read-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging

import voluptuous as vol

from homeassistant.components.media_source import (
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from ..const import CONF_CHAT_MODEL, DEFAULT, LOGGER
from ..guardrails import CooperTool

_ROOT = "media-source://reolink"
_DEFAULT_QUESTION = "Describe what you see in this frame concisely."
_INTERNAL_BASE = "http://127.0.0.1:8123"
_FILE_TS = "%Y%m%d%H%M%S"  # the start/end encoding in a FILE media id


def _parse_when(raw: str) -> datetime | None:
    """Parse the requested time into a naive LOCAL datetime (file times are local)."""
    parsed = dt_util.parse_datetime(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed)
    return parsed.replace(tzinfo=None)


def _norm(text: str) -> str:
    return text.lower().removeprefix("camera.").replace("_", " ").strip()


class LookAtFootageTool(CooperTool):
    """Describe a single frame from a camera's recording at a past time."""

    name = "look_at_recorded_footage"
    description = (
        "Look at a camera's RECORDED footage at a specific PAST time (not live) and describe "
        "the frame. Use for questions like 'what was on the driveway at 6:53am yesterday?'. "
        "Pass: camera (the camera's area name, e.g. 'Driveway' or 'Side Yard'), time (the moment "
        "to view, as an ISO 8601 local datetime like '2026-06-01T06:53:00'), and optionally a "
        "question about the scene. Returns the described frame plus the exact timestamp shown. "
        "For live views use look_at_camera instead. Works with Reolink NVR recordings; if the "
        "home has no supported recordings it will say so."
    )
    parameters = vol.Schema(
        {
            vol.Required("camera"): str,
            vol.Required("time"): str,
            vol.Optional("question"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        from .. import get_runtime

        camera = str(tool_input.tool_args["camera"])
        when = _parse_when(str(tool_input.tool_args["time"]))
        if when is None:
            return {"status": "error", "reason": "Could not parse 'time' — use ISO 8601."}

        file_id, shown = await self._find_file(hass, camera, when)
        if isinstance(file_id, dict):
            return file_id  # an error dict from the walk

        try:
            media = await async_resolve_media(hass, file_id, None)
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"Could not resolve the recording: {err}"}

        offset = max(0.0, (when - shown).total_seconds())
        url = media.url if media.url.startswith("http") else _INTERNAL_BASE + media.url
        frame = await self._extract_frame(hass, url, offset)
        if frame is None:
            return {
                "status": "error",
                "reason": "Found the recording but couldn't extract a frame (ffmpeg).",
            }

        question = tool_input.tool_args.get("question") or _DEFAULT_QUESTION
        runtime = get_runtime(hass)
        description = await runtime.provider.describe_image(
            frame, "image/jpeg", question, model=str(DEFAULT[CONF_CHAT_MODEL])
        )
        return {
            "status": "ok",
            "camera": camera,
            "shown_time": when.isoformat(timespec="seconds"),
            "description": description,
        }

    async def _find_file(self, hass, camera, when):
        """Walk CAM -> RES(main) -> day -> file. Returns (file_id, file_start) or (err, None)."""
        want = _norm(camera)
        try:
            root = await async_browse_media(hass, _ROOT)
        except Exception:  # noqa: BLE001 - Reolink media_source not present on this HA
            return {
                "status": "unsupported",
                "reason": (
                    "Recorded-footage lookup currently supports Reolink NVR recordings, and "
                    "none were found on this Home Assistant. Live views still work via "
                    "look_at_camera."
                ),
            }, None
        cam = next(
            (c for c in root.children if want in c.title.lower() or c.title.lower() in want),
            None,
        )
        if cam is None:
            names = ", ".join(c.title for c in root.children)
            return {"status": "error", "reason": f"No camera matching '{camera}'. Have: {names}."}, None

        res = await async_browse_media(hass, cam.media_content_id)
        main = next(
            (c for c in res.children if c.media_content_id.endswith("|main")),
            res.children[0] if res.children else None,
        )
        if main is None:
            return {"status": "error", "reason": "Camera has no recordings."}, None

        days = await async_browse_media(hass, main.media_content_id)
        suffix = f"|{when.year}|{when.month}|{when.day}"
        day = next((c for c in days.children if c.media_content_id.endswith(suffix)), None)
        if day is None:
            return {"status": "error", "reason": f"No recordings for {when.date()} on {cam.title}."}, None

        files = (await async_browse_media(hass, day.media_content_id)).children
        best = None  # nearest file as a fallback
        for f in files:
            parts = f.media_content_id.split("|")
            try:
                start = datetime.strptime(parts[-2], _FILE_TS)
                end = datetime.strptime(parts[-1], _FILE_TS)
            except (ValueError, IndexError):
                continue
            if start <= when < end:
                return f.media_content_id, start
            if best is None or abs((start - when).total_seconds()) < abs((best[1] - when).total_seconds()):
                best = (f.media_content_id, start)
        if best is not None:
            LOGGER.debug("footage: no exact clip for %s, using nearest at %s", when, best[1])
            return best
        return {"status": "error", "reason": f"No recording clip near {when.time()} on {cam.title}."}, None

    async def _extract_frame(self, hass, url: str, offset: float) -> bytes | None:
        """One JPEG frame at `offset` seconds into the recording, via ffmpeg."""
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            binary = get_ffmpeg_manager(hass).binary
        except Exception:  # noqa: BLE001
            binary = "ffmpeg"
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-nostdin", "-loglevel", "error",
                "-ss", str(offset), "-i", url,
                "-frames:v", "1", "-f", "mjpeg", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, OSError) as exc:
            LOGGER.warning("footage: ffmpeg failed: %s", exc)
            return None
        if proc.returncode != 0 or not out:
            LOGGER.warning("footage: ffmpeg rc=%s err=%s", proc.returncode, err[:200] if err else b"")
            return None
        return out
