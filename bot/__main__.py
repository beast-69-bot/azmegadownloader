from __future__ import annotations

import asyncio
import datetime
from contextlib import suppress
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from . import LOGGER
from .config import (
    AUTHORIZED_CHAT_IDS,
    BOT_TOKEN,
    CONCURRENT_DOWNLOADS,
    CONCURRENT_UPLOADS,
    DOWNLOAD_DIR,
    OWNER_ID,
    STATUS_UPDATE_INTERVAL,
    TELEGRAM_API,
    TELEGRAM_HASH,
)
from .mega_download import download_mega_url, get_mega_total_size
from .progress import ProgressMessage
from .uploader import TaskCancelledUpload, upload_path
from .utils import is_mega_link, safe_link_from_text
from .settings_db import get_settings
from .settings_ui import handle_settings_command, register_settings_handlers

DOWNLOAD_SEM = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
UPLOAD_SEM = asyncio.Semaphore(CONCURRENT_UPLOADS)
_TASK_COUNTER_DATE = None
_TASK_COUNTER_VALUE = 0
_TASK_COUNTER_LOCK = asyncio.Lock()
_ACTIVE_TASKS: dict[int, "TaskState"] = {}


class TaskCancelled(Exception):
    pass


class TaskState:
    def __init__(self, task_id: int, owner_user_id: int):
        self.task_id = task_id
        self.owner_user_id = owner_user_id
        self.cancel_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.message = None
        self.dest: Path | None = None


def _authorized(message) -> bool:
    if OWNER_ID and message.from_user and message.from_user.id == OWNER_ID:
        return True
    if not AUTHORIZED_CHAT_IDS:
        return True
    return message.chat.id in AUTHORIZED_CHAT_IDS


async def _cleanup(path: Path):
    if not path.exists():
        return
    for child in path.rglob("*"):
        if child.is_file():
            child.unlink(missing_ok=True)
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    path.rmdir()


async def _next_daily_task_number() -> int:
    global _TASK_COUNTER_DATE, _TASK_COUNTER_VALUE
    async with _TASK_COUNTER_LOCK:
        today = datetime.date.today()
        if _TASK_COUNTER_DATE != today:
            _TASK_COUNTER_DATE = today
            _TASK_COUNTER_VALUE = 0
        _TASK_COUNTER_VALUE += 1
        return _TASK_COUNTER_VALUE


async def _poll_download_progress(progress: ProgressMessage, dest: Path, total: int):
    last = 0
    while True:
        await asyncio.sleep(STATUS_UPDATE_INTERVAL)
        done = 0
        if dest.exists():
            for child in dest.rglob("*"):
                if child.is_file():
                    try:
                        done += child.stat().st_size
                    except FileNotFoundError:
                        continue
        speed = 0
        if STATUS_UPDATE_INTERVAL:
            speed = max(done - last, 0) / STATUS_UPDATE_INTERVAL
        last = done
        await progress.update(done, total, speed)


async def _run_leech(client: Client, message):
    if not _authorized(message):
        return await message.reply("Unauthorized")

    link = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            link = parts[1].strip()
    if not link and message.reply_to_message and message.reply_to_message.text:
        link = safe_link_from_text(message.reply_to_message.text)

    if not is_mega_link(link):
        return await message.reply("Send a MEGA link with /leech")

    status = await message.reply("Starting download...")
    task_number = await _next_daily_task_number()
    task_state = TaskState(task_number, message.from_user.id if message.from_user else 0)
    task_state.message = status
    _ACTIVE_TASKS[task_number] = task_state

    task_label = f"{task_number} | Downloading"
    progress = ProgressMessage(
        status,
        task_label,
        "Downloading",
        "#Mega -> #Leech",
        task_number,
        STATUS_UPDATE_INTERVAL,
    )

    user_settings = get_settings(task_state.owner_user_id)
    destination = user_settings.get("DESTINATION", "").strip()
    if destination:
        dest = DOWNLOAD_DIR / destination / str(message.id)
    else:
        dest = DOWNLOAD_DIR / str(message.id)
    task_state.dest = dest
    total_size = 0
    try:
        total_size = await get_mega_total_size(link)
    except Exception as e:
        LOGGER.warning(f"Unable to get MEGA size: {e}")

    await DOWNLOAD_SEM.acquire()
    try:
        poll_task = asyncio.create_task(_poll_download_progress(progress, dest, total_size))
        try:
            download_task = asyncio.create_task(download_mega_url(link, str(dest)))
            task_state.task = download_task
            cancel_wait = asyncio.create_task(task_state.cancel_event.wait())
            done, _pending = await asyncio.wait(
                {download_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait in done:
                download_task.cancel()
                with suppress(asyncio.CancelledError):
                    await download_task
                raise TaskCancelled
            files = await download_task
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
    except TaskCancelled:
        await status.edit_text(f"❌ Task {task_number} cancelled by user.")
        await _cleanup(dest)
        _ACTIVE_TASKS.pop(task_number, None)
        return
    except Exception as e:
        LOGGER.error(f"Download failed: {e}")
        await status.edit_text(f"Download failed: {e}")
        await _cleanup(dest)
        return
    finally:
        DOWNLOAD_SEM.release()

    await progress.finalize("Download complete. Uploading...")

    await UPLOAD_SEM.acquire()
    try:
        for file_path in files:
            if task_state.cancel_event.is_set():
                raise TaskCancelled
            await upload_path(
                client,
                message.chat.id,
                Path(file_path),
                status,
                task_number,
                task_state.cancel_event,
                task_state.owner_user_id,
            )
        await status.edit_text("Leech complete.")
    except (TaskCancelled, TaskCancelledUpload):
        await status.edit_text(f"❌ Task {task_number} cancelled by user.")
    except Exception as e:
        LOGGER.error(f"Upload failed: {e}")
        await status.edit_text(f"Upload failed: {e}")
    finally:
        UPLOAD_SEM.release()
        await _cleanup(dest)
        _ACTIVE_TASKS.pop(task_number, None)


async def start_cmd(_, message):
    await message.reply("MEGA leech bot is running. Use /leech <mega link>.")


async def help_cmd(_, message):
    await message.reply(
        "Commands:\n/leech <mega link> - download and upload to Telegram\n"
        "/settings - leech settings panel\n"
        "/ping - check bot"
    )


async def ping_cmd(_, message):
    await message.reply("pong")


async def leech_cmd(client, message):
    await _run_leech(client, message)


async def settings_cmd(client, message):
    await handle_settings_command(client, message)


async def cancel_cmd(_, message):
    task_id = 0
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].isdigit():
            task_id = int(parts[1])

    task_state = _ACTIVE_TASKS.get(task_id)
    if not task_state:
        return await message.reply("No active task found.")
    if message.from_user and task_state.owner_user_id != message.from_user.id:
        return await message.reply("❌ You can only cancel your own task.")

    task_state.cancel_event.set()
    return


def main():
    app = Client(
        "mega_leech_bot",
        api_id=TELEGRAM_API,
        api_hash=TELEGRAM_HASH,
        bot_token=BOT_TOKEN,
    )

    app.add_handler(MessageHandler(start_cmd, filters.command("start")))
    app.add_handler(MessageHandler(help_cmd, filters.command("help")))
    app.add_handler(MessageHandler(ping_cmd, filters.command("ping")))
    app.add_handler(MessageHandler(leech_cmd, filters.command("leech")))
    app.add_handler(MessageHandler(cancel_cmd, filters.command("cancel")))
    app.add_handler(MessageHandler(settings_cmd, filters.command("settings")))
    register_settings_handlers(app)

    LOGGER.info("Mega leech bot started")
    app.run()


if __name__ == "__main__":
    main()
