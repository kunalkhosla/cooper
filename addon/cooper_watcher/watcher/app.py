"""Watch loop: poll cameras for motion (cheap frame-diff) and run a daily review.

Talks to Home Assistant only through the Supervisor core proxy using SUPERVISOR_TOKEN.
The one and only state-changing call it makes is ``cooper.proactive_check``.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np
import requests
from PIL import Image

LOGGER = logging.getLogger("cooper_watcher")

SUPERVISOR_CORE = "http://supervisor/core/api"
PROACTIVE_CHECK = "/services/cooper/proactive_check"
THUMB = (64, 64)


@dataclass
class Config:
    """Runtime config from add-on options (env), with sane fallbacks."""

    poll_interval: int = 60
    motion_threshold: float = 12.0
    daily_review_time: str = ""
    cameras: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        cameras = [c.strip() for c in os.environ.get("CAMERAS", "").split(",") if c.strip()]
        return cls(
            poll_interval=int(os.environ.get("POLL_INTERVAL", "60")),
            motion_threshold=float(os.environ.get("MOTION_THRESHOLD", "12.0")),
            daily_review_time=os.environ.get("DAILY_REVIEW_TIME", "").strip(),
            cameras=cameras,
        )


class HAClient:
    """Minimal Supervisor-proxy client. Reads cameras; writes only proactive_check."""

    def __init__(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN missing; run inside Supervisor.")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def camera_snapshot(self, entity_id: str) -> bytes | None:
        try:
            resp = self._session.get(
                f"{SUPERVISOR_CORE}/camera_proxy/{entity_id}", timeout=15
            )
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as err:
            LOGGER.warning("snapshot failed for %s: %s", entity_id, err)
            return None

    def proactive_check(self, reason: str, context_entities: list[str]) -> None:
        payload = {"reason": reason, "context_entities": context_entities}
        try:
            resp = self._session.post(
                f"{SUPERVISOR_CORE}{PROACTIVE_CHECK}", json=payload, timeout=30
            )
            resp.raise_for_status()
            LOGGER.info("woke Cooper: %s", reason)
        except requests.RequestException as err:
            LOGGER.error("proactive_check failed: %s", err)


def _thumbnail(jpeg: bytes) -> np.ndarray | None:
    try:
        image = Image.open(io.BytesIO(jpeg)).convert("L").resize(THUMB)
        return np.asarray(image, dtype=np.float32)
    except Exception as err:  # noqa: BLE001 - any decode error just skips this frame
        LOGGER.warning("could not decode frame: %s", err)
        return None


def _motion_score(prev: np.ndarray, curr: np.ndarray) -> float:
    return float(np.mean(np.abs(curr - prev)))


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    client = HAClient()
    last_frames: dict[str, np.ndarray] = {}
    last_review_day: str | None = None

    LOGGER.info(
        "watching %d camera(s); motion threshold %.1f",
        len(config.cameras),
        config.motion_threshold,
    )

    while True:
        for camera in config.cameras:
            jpeg = client.camera_snapshot(camera)
            if jpeg is None:
                continue
            curr = _thumbnail(jpeg)
            if curr is None:
                continue
            prev = last_frames.get(camera)
            last_frames[camera] = curr
            if prev is None:
                continue
            score = _motion_score(prev, curr)
            if score >= config.motion_threshold:
                client.proactive_check(
                    f"possible motion on {camera} (score {score:.0f})",
                    [camera],
                )

        if config.daily_review_time:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            hhmm = time.strftime("%H:%M", now)
            if hhmm == config.daily_review_time and last_review_day != today:
                last_review_day = today
                client.proactive_check(
                    "scheduled daily review of the home", []
                )

        time.sleep(config.poll_interval)
