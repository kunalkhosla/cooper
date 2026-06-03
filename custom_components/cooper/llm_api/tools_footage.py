"""Recorded-footage vision tool: look at a camera at a PAST moment, not live.

The live ``look_at_camera`` tool only grabs a current frame. This one reaches into the
camera's recordings: it walks Home Assistant's Reolink ``media_source`` tree
(CAM -> RES(main) -> day -> 5-minute file), extracts frame(s) with ffmpeg, and hands them
to Claude vision.

Two modes:
- **single frame** — pass ``time`` only.
- **window scan** — pass ``time`` + ``end``. It is **detection-driven**, not time-sampled:
  it reads the camera's own ``person`` / ``vehicle`` / ``animal`` / ``motion`` detection
  history over the window (recorder) and only grabs a frame at each moment something fired.
  So an empty window costs **zero** vision calls, and a 2-second car is still caught
  (blind 15s sampling would miss it). Frames are capped and described with a small/cheap
  vision model. If the camera has no detection sensors, it falls back to a capped, evenly
  sampled set (with a note that brief events may be missed).

ffmpeg can't use the caller's session, so the resolved recording URL is **signed**
(``async_sign_path``) — otherwise the Reolink proxy returns 401.

**Provider-specific by nature.** Supports **Reolink** today; degrades gracefully on a home
without Reolink recordings. Read-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import partial
import os
import tempfile
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import (
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from ..const import LOGGER
from ..guardrails import CooperTool

_ROOT = "media-source://reolink"
_DEFAULT_QUESTION = "Describe what you see in this frame concisely."
_INTERNAL_BASE = "http://127.0.0.1:8123"
_FILE_TS = "%Y%m%d%H%M%S"

# Token frugality.
_MAX_FRAMES = 8  # hard cap on frames per call, however busy the window
_DETECT_TYPES = ("person", "vehicle", "animal", "pet", "motion")
_CLUSTER_S = 15  # merge detections within this many seconds into one frame
_TARGET_INTERVAL_S = 120  # fallback even-sampling spacing (no detection sensors)
_FRAME_MODEL = "claude-haiku-4-5-20251001"  # cheap vision per frame
_URL_TTL = timedelta(seconds=180)


def _parse_when(raw: str) -> datetime | None:
    """Parse a time into a naive LOCAL datetime (file times are local)."""
    parsed = dt_util.parse_datetime(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed)
    return parsed.replace(tzinfo=None)


def _norm(text: str) -> str:
    return text.lower().removeprefix("camera.").replace("_", " ").strip()


def _write_file(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _even_samples(start: datetime, end: datetime) -> list[datetime]:
    span = (end - start).total_seconds()
    if span <= 0:
        return [start]
    n = min(_MAX_FRAMES, max(2, int(span // _TARGET_INTERVAL_S) + 1))
    return [start + timedelta(seconds=span * i / (n - 1)) for i in range(n)]


class LookAtFootageTool(CooperTool):
    """Describe a frame, or detection-driven scan a window, of a camera's recording."""

    name = "look_at_recorded_footage"
    description = (
        "Look at a camera's RECORDED footage at a PAST time (not live). For one moment, pass "
        "camera + time. To find what happened over a WINDOW (e.g. 'anyone on the driveway "
        "between 6:00 and 6:15am?'), also pass 'end': it checks the camera's own person/"
        "vehicle/animal/motion detections in that window and only looks at those moments, so "
        "an empty window is free and brief events (a passing car) are still caught. Args: "
        "camera (area name, e.g. 'Driveway'), time (ISO local like '2026-06-01T06:00:00'), "
        "optional end (ISO), optional question. Live views: use look_at_camera. Reolink only."
    )
    parameters = vol.Schema(
        {
            vol.Required("camera"): str,
            vol.Required("time"): str,
            vol.Optional("end"): str,
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

        args = tool_input.tool_args
        camera = str(args["camera"])
        start = _parse_when(str(args["time"]))
        if start is None:
            return {"status": "error", "reason": "Could not parse 'time' — use ISO 8601."}
        end = _parse_when(str(args["end"])) if args.get("end") else None
        question = args.get("question") or _DEFAULT_QUESTION
        window = bool(end and end > start)

        note = None
        if window:
            picks = await self._detections(hass, camera, start, end)
            if picks is None:
                picks = [(t, None) for t in _even_samples(start, end)]
                note = "No detection sensors for this camera — sampled evenly; brief events may be missed."
            elif not picks:
                return {
                    "status": "ok",
                    "camera": camera,
                    "window": {
                        "start": start.isoformat(timespec="seconds"),
                        "end": end.isoformat(timespec="seconds"),
                    },
                    "frames_sampled": 0,
                    "frames": [],
                    "note": "No person, vehicle, animal, or motion detected in that window.",
                }
            else:
                note = "Frames taken at detection moments (capped)."
        else:
            picks = [(start, None)]

        runtime = get_runtime(hass)
        frames: list[dict] = []
        first_error: dict | None = None
        clips: dict[str, str] = {}  # file_id -> downloaded temp path (reused across frames)
        try:
            for when, label in picks:
                frame, shown = await self._frame_bytes(hass, camera, when, clips)
                if isinstance(frame, dict):
                    first_error = first_error or frame
                    continue
                if frame is None:
                    continue
                try:
                    desc = await runtime.provider.describe_image(
                        frame, "image/jpeg", question, model=_FRAME_MODEL
                    )
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("footage: describe failed: %s", err)
                    continue
                item = {"time": shown.isoformat(timespec="seconds"), "description": desc}
                if label:
                    item["detected"] = label
                frames.append(item)
        finally:
            for path in clips.values():
                try:
                    os.unlink(path)
                except OSError:
                    pass

        if not frames:
            if first_error is not None:
                return first_error
            return {
                "status": "error",
                "reason": "Found the recording but couldn't extract any frame (ffmpeg).",
            }

        if not window:
            return {
                "status": "ok",
                "camera": camera,
                "shown_time": frames[0]["time"],
                "description": frames[0]["description"],
            }
        return {
            "status": "ok",
            "camera": camera,
            "window": {
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
            },
            "frames_sampled": len(frames),
            "frames": frames,
            "note": note,
        }

    async def _detections(self, hass, camera, start, end):
        """Detection moments in [start,end]. None = no sensors; [] = sensors but nothing."""
        slug = _norm(camera).replace(" ", "_")
        sensors = {
            s.entity_id: t
            for s in hass.states.async_all("binary_sensor")
            for t in _DETECT_TYPES
            if s.entity_id == f"binary_sensor.{slug}_{t}"
        }
        if not sensors:
            return None
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return None

        tz = dt_util.DEFAULT_TIME_ZONE
        start_utc = dt_util.as_utc(start.replace(tzinfo=tz))
        end_utc = dt_util.as_utc(end.replace(tzinfo=tz))
        states = await get_instance(hass).async_add_executor_job(
            partial(
                history.get_significant_states,
                hass,
                start_utc,
                end_utc,
                list(sensors),
                None,
                True,
                False,
            )
        )
        events: list[tuple[datetime, str]] = []
        for eid, sts in (states or {}).items():
            for st in sts:
                if getattr(st, "state", None) == "on" and getattr(st, "last_changed", None):
                    local = dt_util.as_local(st.last_changed).replace(tzinfo=None)
                    if start <= local <= end:
                        events.append((local, sensors[eid]))
        events.sort(key=lambda e: e[0])

        merged: list[tuple[datetime, str]] = []
        for tm, lbl in events:
            if merged and (tm - merged[-1][0]).total_seconds() <= _CLUSTER_S:
                labels = merged[-1][1].split(",")
                if lbl not in labels:
                    merged[-1] = (merged[-1][0], merged[-1][1] + "," + lbl)
            else:
                merged.append((tm, lbl))
        if len(merged) > _MAX_FRAMES:  # busy window — spread the cap across it
            step = len(merged) / _MAX_FRAMES
            merged = [merged[int(i * step)] for i in range(_MAX_FRAMES)]
        return merged

    async def _frame_bytes(self, hass, camera, when, clips):
        """Resolve the clip covering `when`; return (jpeg|err|None, shown_time).

        The Reolink proxy rejects HTTP Range, so ffmpeg can't seek over the network
        (input-seek -> 400 'got text/html'). Instead download the (low-res) clip whole
        once, cache it per file, and seek the LOCAL file — fast and reliable.
        """
        file_id, shown = await self._find_file(hass, camera, when)
        if isinstance(file_id, dict):
            return file_id, None
        path = clips.get(file_id)
        if path is None:
            path = await self._download_clip(hass, file_id)
            if isinstance(path, dict):
                return path, None
            if path is None:
                return None, shown
            clips[file_id] = path
        offset = max(0.0, (when - shown).total_seconds())
        return await self._extract_frame(hass, path, offset), shown

    async def _download_clip(self, hass, file_id) -> str | dict | None:
        """Download a recording clip whole (no Range) to a temp file; return its path."""
        try:
            media = await async_resolve_media(hass, file_id, None)
        except Exception as err:  # noqa: BLE001
            return {"status": "error", "reason": f"Could not resolve the recording: {err}"}
        parts = urlsplit(media.url)
        rel = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            url = _INTERNAL_BASE + async_sign_path(hass, rel, _URL_TTL)
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("footage: could not sign url (%s); using raw", err)
            url = media.url if media.url.startswith("http") else _INTERNAL_BASE + media.url
        try:
            session = async_get_clientsession(hass)
            async with session.get(url) as resp:  # plain GET, no Range
                if resp.status != 200:
                    LOGGER.warning("footage: clip download HTTP %s", resp.status)
                    return None
                data = await resp.read()
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("footage: clip download failed: %s", err)
            return None
        fd, tmp = tempfile.mkstemp(suffix=".mp4", prefix="cooper_footage_")
        try:
            await hass.async_add_executor_job(_write_file, fd, data)
        except OSError as err:
            LOGGER.warning("footage: temp write failed: %s", err)
            return None
        return tmp

    async def _find_file(self, hass, camera, when):
        """Walk CAM -> RES(main) -> day -> file. Returns (file_id, file_start) or (err, None)."""
        want = _norm(camera)
        try:
            root = await async_browse_media(hass, _ROOT)
        except Exception:  # noqa: BLE001
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

        # Prefer the low-res 'sub' stream — small clips (~8 MB vs ~240 MB), plenty for
        # describing a person/car/animal, and far cheaper to download per frame.
        res = await async_browse_media(hass, cam.media_content_id)
        stream = next(
            (c for c in res.children if c.media_content_id.endswith("|sub")),
            next(
                (c for c in res.children if c.media_content_id.endswith("|main")),
                res.children[0] if res.children else None,
            ),
        )
        if stream is None:
            return {"status": "error", "reason": "Camera has no recordings."}, None

        days = await async_browse_media(hass, stream.media_content_id)
        suffix = f"|{when.year}|{when.month}|{when.day}"
        day = next((c for c in days.children if c.media_content_id.endswith(suffix)), None)
        if day is None:
            return {"status": "error", "reason": f"No recordings for {when.date()} on {cam.title}."}, None

        files = (await async_browse_media(hass, day.media_content_id)).children
        best = None
        for f in files:
            parts = f.media_content_id.split("|")
            try:
                fstart = datetime.strptime(parts[-2], _FILE_TS)
                fend = datetime.strptime(parts[-1], _FILE_TS)
            except (ValueError, IndexError):
                continue
            if fstart <= when < fend:
                return f.media_content_id, fstart
            if best is None or abs((fstart - when).total_seconds()) < abs((best[1] - when).total_seconds()):
                best = (f.media_content_id, fstart)
        if best is not None:
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
