"""Human-readable activity narration for Cooper turns and tool calls.

In v1 the brains lived in a TypeScript add-on whose Log tab narrated every goal
evaluation: a header per turn, then an indented, emoji-tagged line per tool call
showing ``call(args) -> result``. v2's brains are here in the integration, so we
reproduce that narration through the standard logger.

Where it goes / disk safety
---------------------------
Cooper gets its **own** log file, ``/config/cooper.log``, written through a
``RotatingFileHandler`` (``maxBytes`` x ``backupCount`` = a hard ceiling that rotates
by size and auto-prunes old files) so it can never eat the disk no matter how long HA
runs between restarts. Writes happen on a ``QueueListener`` background thread, so the
event loop never blocks on file I/O.

With ``MIRROR_TO_HA_LOG`` (default on) the logger also propagates, so the same lines
land in ``home-assistant.log`` — the only file the Log Viewer add-on can tail — and show
up live in the UI. Set it False to keep the narration in cooper.log only and home-
assistant.log lean.

Colour + emoji
--------------
Colour lives in this module's *handler formatter*, so only ``cooper.log`` carries the
ANSI (blue turn headers, cyan tool calls, magenta notifications, green done, red
errors) — ``tail -f /config/cooper.log`` in the SSH/Web Terminal add-on renders it. The
copy that propagates to home-assistant.log stays plain (no escape codes to pollute
grep), but keeps the emoji, so the Log Viewer add-on view is still readable.
"""

from __future__ import annotations

import contextvars
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import queue
from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOGGER

# Per-turn tool tally. A ContextVar is isolated per asyncio task, so concurrent
# conversations each count their own tool calls without stepping on each other.
_turn = contextvars.ContextVar("cooper_turn", default=None)  # type: ignore[var-annotated]

# Mirror the narration into home-assistant.log too, so the Log Viewer add-on (which
# can only tail that file) shows Cooper live in the UI. Set False for cooper.log only.
MIRROR_TO_HA_LOG = True

# --- rotation / disk caps ------------------------------------------------------
LOG_FILE = "cooper.log"
_MAX_BYTES = 1_000_000  # 1 MB per file …
_BACKUPS = 3            # … + 3 rotated copies => ~4 MB hard ceiling, auto-pruned

# --- ANSI palette (rendered by the Log Viewer add-on / terminal `ha core logs`) -
_RESET = "\x1b[0m"
_GRAY = "\x1b[90m"
_COLORS = {
    "header": "\x1b[1;34m",  # bold blue — turn boundary
    "tool": "\x1b[36m",      # cyan — a tool call
    "notify": "\x1b[35m",    # magenta — reaching the user
    "done": "\x1b[1;32m",    # bold green — turn finished
    "error": "\x1b[31m",     # red — a tool errored / was refused
}

# Emoji per tool, keyed by base (un-namespaced) name — mirrors v1's agent.ts tags.
_TOOL_EMOJI = {
    # Cooper's own tools
    "get_history": "🕘",
    "look_at_camera": "📷",
    "remember": "🧠",
    "recall": "🧠",
    "forget": "🧠",
    "author_automation": "🤖",
    "list_cooper_items": "📋",
    "delete_cooper_item": "🗑",
    "create_watch": "👁",
    "list_watches": "👁",
    "remove_watch": "👁",
    # built-in HA intents Cooper drives
    "HassTurnOn": "💡",
    "HassTurnOff": "💡",
    "HassToggle": "🔀",
    "HassLightSet": "💡",
    "HassClimateSetTemperature": "🌡",
    "HassSetPosition": "↕",
    "HassStopMoving": "✋",
    "HassMediaPause": "⏸",
    "HassMediaUnpause": "▶",
    "HassMediaNext": "⏭",
    "HassMediaPrevious": "⏮",
    "HassSetVolume": "🔊",
    "HassBroadcast": "📢",
    "HassVacuumStart": "🧹",
}
_NOTIFY_TOOLS = frozenset({"notify", "HassBroadcast"})

_listener = None  # type: QueueListener | None
_queue_handler = None  # type: QueueHandler | None


def _emoji(base: str) -> str:
    return _TOOL_EMOJI.get(base, "🎬" if base.startswith("script") else "⚙")


def _trim(text: Any, limit: int = 220) -> str:
    """One-line, length-bounded rendering of arbitrary text."""
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _summarize_args(args: dict[str, Any] | None) -> str:
    """Compact ``k=v, k=v`` rendering of tool arguments."""
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, (dict, list)):
            value = "<%s:%d>" % (type(value).__name__, len(value))
        parts.append("%s=%s" % (key, _trim(value, 60)))
    return _trim(", ".join(parts), 180)


def _summarize_result(result: Any) -> str:
    """Compact rendering of a tool result, surfacing status/decision first."""
    if isinstance(result, dict):
        priority = ("status", "decision", "would_have", "reason", "error")
        head = ["%s=%s" % (k, result[k]) for k in priority if k in result]
        extra = [k for k in result if k not in priority]
        if extra:
            head.append("+" + ",".join(extra[:4]))
        return _trim(" ".join(head) or "ok", 180)
    return _trim(result, 180)


def _result_is_bad(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if "error" in result:
        return True
    flag = str(result.get("status") or result.get("decision") or "")
    return flag.startswith("refused") or flag in ("error", "failed")


# --- narration -----------------------------------------------------------------
def turn_start(user_text: str, mode: str) -> None:
    """Header marking the start of a Cooper turn (the rule line, v1-style)."""
    _turn.set({"tools": 0})
    LOGGER.info(
        "──── turn [%s] ◀ %s",
        mode,
        _trim(user_text),
        extra={"cooper_color": "header"},
    )


def tool_call(base: str, args: dict[str, Any] | None, result: Any) -> None:
    """One narrated line per tool call: ``emoji base(args) -> result``."""
    state = _turn.get()
    if state is not None:
        state["tools"] += 1
    if _result_is_bad(result):
        color = "error"
    elif base in _NOTIFY_TOOLS:
        color = "notify"
    else:
        color = "tool"
    LOGGER.info(
        "  %s %s(%s) → %s",
        _emoji(base),
        base,
        _summarize_args(args),
        _summarize_result(result),
        extra={"cooper_color": color},
    )


def turn_end(reply: str | None, *, rounds: int) -> None:
    """Footer with the final spoken reply and how much work the turn took."""
    state = _turn.get()
    tool_count = state["tools"] if state is not None else 0
    LOGGER.info(
        "──── done (%d tool call%s, %d round%s) ▶ %s",
        tool_count,
        "" if tool_count == 1 else "s",
        rounds,
        "" if rounds == 1 else "s",
        _trim(reply or "(no speech)"),
        extra={"cooper_color": "done"},
    )


# --- handler lifecycle ---------------------------------------------------------
class _CooperFormatter(logging.Formatter):
    """Colourises each line by its per-record hint; message itself stays plain."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%H:%M:%S")
        message = record.getMessage()
        color = _COLORS.get(getattr(record, "cooper_color", ""), "")
        body = "%s%s%s" % (color, message, _RESET) if color else message
        return "%s%s%s %s" % (_GRAY, stamp, _RESET, body)


def install_file_handler(hass: HomeAssistant) -> None:
    """Attach the capped, rotating, colourised Cooper log file (idempotent).

    Safe to call from the event loop: the file is opened lazily (``delay=True``) and
    every write runs on the ``QueueListener`` thread, never on the loop.
    """
    global _listener, _queue_handler
    if _queue_handler is not None:
        return
    try:
        handler = RotatingFileHandler(
            hass.config.path(LOG_FILE),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
    except OSError as err:  # never let logging setup break the integration
        LOGGER.warning("Cooper file logging disabled (%s)", err)
        return
    handler.setFormatter(_CooperFormatter())
    handler.setLevel(logging.INFO)

    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    listener = QueueListener(log_queue, handler, respect_handler_level=True)
    listener.start()

    queue_handler = QueueHandler(log_queue)
    LOGGER.addHandler(queue_handler)
    if LOGGER.level == logging.NOTSET or LOGGER.level > logging.INFO:
        LOGGER.setLevel(logging.INFO)
    # propagate=True (default) mirrors plain lines into home-assistant.log for the
    # Log Viewer add-on; the colourised copy stays in cooper.log via this handler.
    LOGGER.propagate = MIRROR_TO_HA_LOG

    _listener, _queue_handler = listener, queue_handler


def remove_file_handler() -> None:
    """Detach the Cooper log file and stop its writer thread."""
    global _listener, _queue_handler
    if _queue_handler is not None:
        LOGGER.removeHandler(_queue_handler)
        _queue_handler = None
    if _listener is not None:
        _listener.stop()
        _listener = None
    LOGGER.propagate = True
