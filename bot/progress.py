from __future__ import annotations

import asyncio
import time


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


class ProgressMessage:
    def __init__(self, message, label: str, update_interval: int = 5):
        self._message = message
        self._label = label
        self._update_interval = update_interval
        self._last_update = 0.0
        self._start = time.time()
        self._lock = asyncio.Lock()

    async def update(self, done: int, total: int, speed: float):
        now = time.time()
        if now - self._last_update < self._update_interval:
            return
        async with self._lock:
            if now - self._last_update < self._update_interval:
                return
            self._last_update = now
            percent = f"{(done / total * 100):.1f}%" if total else "--"
            text = (
                f"{self._label}\n"
                f"- Done: {_fmt_bytes(done)} / {_fmt_bytes(total)} ({percent})\n"
                f"- Speed: {_fmt_bytes(speed)}/s\n"
                f"- ETA: {_fmt_eta(done, total, speed)}"
            )
            await self._message.edit_text(text)

    async def finalize(self, text: str):
        async with self._lock:
            await self._message.edit_text(text)
