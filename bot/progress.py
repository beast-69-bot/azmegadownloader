from __future__ import annotations

import asyncio
import time

from pyrogram.enums import ParseMode


def _fmt_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def _fmt_eta(done: int, total: int, speed: float) -> str:
    if speed <= 0 or total <= 0:
        return "--"
    remaining = max(total - done, 0)
    return _fmt_time(remaining / speed)


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "░" * width
    ratio = min(max(done / total, 0), 1)
    filled = int(ratio * width)
    return ("█" * filled) + ("░" * (width - filled))


class ProgressMessage:
    def __init__(
        self,
        message,
        label: str,
        stage: str,
        action: str,
        task_id: int,
        update_interval: int = 5,
    ):
        self._message = message
        self._label = label
        self._stage = stage
        self._action = action
        self._task_id = task_id
        self._update_interval = update_interval
        self._last_update = 0.0
        self._start = time.time()
        self._lock = asyncio.Lock()
        self._last_text = ""

    async def update(self, done: int, total: int, speed: float):
        now = time.time()
        if now - self._last_update < self._update_interval:
            return
        async with self._lock:
            if now - self._last_update < self._update_interval:
                return
            self._last_update = now
            percent_value = (done / total * 100) if total else 0.0
            percent = f"{percent_value:05.2f}"
            bar = _progress_bar(done, total)
            elapsed = _fmt_time(time.time() - self._start)
            est_total = "--"
            if speed > 0 and total > 0:
                est_total = _fmt_time(total / speed)
            text = (
                "<pre>"
                f"🚀 1. Task {self._label}\n"
                "\n"
                f"[ {self._stage}... ]\n"
                f"[ {bar} ] {percent}%\n"
                f"Progress   : {percent}%\n"
                f"Processed  : {_fmt_bytes(done)} / {_fmt_bytes(total)}\n"
                f"Speed      : {_fmt_bytes(speed)}/s\n"
                f"Time       : {elapsed} / {est_total}  (ETA {_fmt_eta(done, total, speed)})\n"
                "\n"
                f"Action     : {self._action}\n"
                f"Cancel     : /cancel {self._task_id}"
                "</pre>"
            )
            if text == self._last_text:
                return
            self._last_text = text
            await self._message.edit_text(text, parse_mode=ParseMode.HTML)

    async def finalize(self, text: str):
        async with self._lock:
            await self._message.edit_text(text)
