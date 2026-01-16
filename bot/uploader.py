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
):
    files = list(iter_files(root))
    if not files:
        raise RuntimeError("No files to upload")

    settings = get_settings(user_id) if user_id else {}
    prefix = settings.get("PREFIX", "")
    suffix = settings.get("SUFFIX", "")
    caption_tmpl = settings.get("CAPTION", "")
    layout = settings.get("LAYOUT", "")
    split_size = int(settings.get("SPLIT_SIZE", 0) or 0)
    equal_split = bool(settings.get("EQUAL", False))
    thumb_path = settings.get("THUMB_PATH") if settings.get("THUMB") else ""
    thumb_file = thumb_path if thumb_path and Path(thumb_path).exists() else None

    for index, file_path in enumerate(files, start=1):
        base = file_path.stem
        ext = file_path.suffix
        new_name = f"{prefix}{base}{suffix}{ext}" if (prefix or suffix) else file_path.name
        if new_name != file_path.name:
            target = file_path.with_name(new_name)
            file_path.rename(target)
            file_path = target

        upload_targets = [file_path]
        if split_size and file_path.stat().st_size > split_size:
            upload_targets = _split_file(file_path, split_size, equal_split)

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

        for upload_path in upload_targets:
            if cancel_event and cancel_event.is_set():
                raise TaskCancelledUpload
            caption = _format_caption(caption_tmpl, upload_path.name, layout)
            try:
                await client.send_document(
                    chat_id=chat_id,
                    document=str(upload_path),
                    caption=caption,
                    thumb=thumb_file,
                    progress=_progress,
                )
            except FloodWait as f:
                await asyncio.sleep(f.value)
                await client.send_document(
                    chat_id=chat_id,
                    document=str(upload_path),
                    caption=caption,
                    thumb=thumb_file,
                    progress=_progress,
                )
            if upload_path != file_path:
                upload_path.unlink(missing_ok=True)


def _format_caption(template: str, filename: str, layout: str) -> str:
    base = Path(filename).stem
    ext = Path(filename).suffix.lstrip(".")
    if template:
        caption = (
            template.replace("{filename}", filename)
            .replace("{basename}", base)
            .replace("{ext}", ext)
        )
    else:
        caption = filename
    if layout:
        caption = f"{caption} | {layout}"
    return caption


def _split_file(path: Path, split_size: int, equal_split: bool) -> list[Path]:
    size = path.stat().st_size
    if equal_split and split_size > 0:
        parts = max(1, (size + split_size - 1) // split_size)
        split_size = max(1, size // parts)

    outputs = []
    with path.open("rb") as src:
        index = 1
        while True:
            chunk = src.read(split_size)
            if not chunk:
                break
            part_path = path.with_suffix(path.suffix + f".part{index:02d}")
            with part_path.open("wb") as dst:
                dst.write(chunk)
            outputs.append(part_path)
            index += 1
    path.unlink(missing_ok=True)
    return outputs
