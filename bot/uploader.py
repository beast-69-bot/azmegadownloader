from __future__ import annotations

import asyncio
from pathlib import Path

from pyrogram.errors import FloodWait

from .config import STATUS_UPDATE_INTERVAL
from .progress import ProgressMessage
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
):
    files = list(iter_files(root))
    if not files:
        raise RuntimeError("No files to upload")

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

        try:
            await client.send_document(
                chat_id=chat_id,
                document=str(file_path),
                caption=file_path.name,
                progress=_progress,
            )
        except FloodWait as f:
            await asyncio.sleep(f.value)
            await client.send_document(
                chat_id=chat_id,
                document=str(file_path),
                caption=file_path.name,
                progress=_progress,
            )
