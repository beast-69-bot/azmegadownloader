from __future__ import annotations

import asyncio
from pathlib import Path

from pyrogram.errors import FloodWait

from .config import STATUS_UPDATE_INTERVAL
from .progress import ProgressMessage
from .settings_db import get_settings
from .utils import iter_files


class TaskCancelledUpload(Exception):
    pass


async def upload_path(
    client,
    chat_id: int,
    root: Path,
    status_message,
    task_id: int,
    cancel_event: asyncio.Event | None = None,
    user_id: int | None = None,
    topic_id: int | None = None,
):
    files = list(iter_files(root))
    if not files:
        raise RuntimeError("No files to upload")

    settings = get_settings(user_id) if user_id else {}
    caption_tmpl = settings.get("caption", "")
    thumb_path = settings.get("thumb_path", "")
    thumb_file = thumb_path if thumb_path and Path(thumb_path).exists() else None

    for index, file_path in enumerate(files, start=1):
        label = f"{task_id} | Uploading {file_path.name} ({index}/{len(files)})"
        stage = f"Uploading {file_path.name} ({index}/{len(files)})"
        progress = ProgressMessage(
            status_message,
            label,
            stage,
            "#Upload -> #Telegram",
            task_id,
            STATUS_UPDATE_INTERVAL,
        )

        async def _progress(current, total):
            if cancel_event and cancel_event.is_set():
                raise TaskCancelledUpload
            speed = 0
            if total:
                speed = current / max(1, STATUS_UPDATE_INTERVAL)
            await progress.update(current, total, speed)

        caption = _format_caption(caption_tmpl, file_path.name)
        send_kwargs = {"chat_id": chat_id, "document": str(file_path), "caption": caption, "progress": _progress}
        if thumb_file:
            send_kwargs["thumb"] = thumb_file
        if topic_id:
            send_kwargs["message_thread_id"] = topic_id
        try:
            await client.send_document(**send_kwargs)
        except FloodWait as f:
            await asyncio.sleep(f.value)
            await client.send_document(**send_kwargs)


def _format_caption(template: str, filename: str) -> str:
    base = Path(filename).stem
    ext = Path(filename).suffix.lstrip(".")
    if not template:
        return filename
    return (
        template.replace("{filename}", filename)
        .replace("{basename}", base)
        .replace("{ext}", ext)
    )
