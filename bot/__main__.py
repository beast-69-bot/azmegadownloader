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
from .settings_db import (
    add_admin_id,
    get_admin_ids,
    get_global_setting,
    get_settings,
    parse_chat_target,
    remove_admin_id,
    set_global_setting,
)
from .settings_ui import register_settings_handlers, settings_command

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


def _is_admin(message) -> bool:
    if OWNER_ID and message.from_user and message.from_user.id == OWNER_ID:
        return True
    if message.from_user and message.from_user.id in get_admin_ids():
        return True
    return False


async def _resolve_channel_id(client: Client, raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Missing channel id")
    if raw.lstrip("-").isdigit():
        return int(raw)
    if not raw.startswith("@"):
        raw = f"@{raw}"
    chat = await client.get_chat(raw)
    return int(chat.id)


async def _resolve_user_id(client: Client, raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Missing user id")
    if raw.lstrip("-").isdigit():
        return int(raw)
    if not raw.startswith("@"):
        raw = f"@{raw}"
    user = await client.get_users(raw)
    return int(user.id)


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
    task_log_channel = get_global_setting("task_channel_id")
    if task_log_channel:
        try:
            user = message.from_user
            uname = f"@{user.username}" if user and user.username else "unknown"
            await client.send_message(
                int(task_log_channel),
                f"New task from {uname} ({user.id if user else 0}): {link}",
            )
        except Exception:
            pass
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
        await status.edit_text(f"Task {task_number} cancelled by user.")
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
            target_chat_id, topic_id = parse_chat_target(user_settings.get("chat_id", ""))
            await upload_path(
                client,
                target_chat_id or message.chat.id,
                Path(file_path),
                status,
                task_number,
                task_state.cancel_event,
                task_state.owner_user_id,
                topic_id,
            )
        await status.edit_text("Leech complete.")
    except (TaskCancelled, TaskCancelledUpload):
        await status.edit_text(f"Task {task_number} cancelled by user.")
    except Exception as e:
        LOGGER.error(f"Upload failed: {e}")
        await status.edit_text(f"Upload failed: {e}")
    finally:
        UPLOAD_SEM.release()
        await _cleanup(dest)
        _ACTIVE_TASKS.pop(task_number, None)


async def start_cmd(_, message):
    await message.reply("MEGA leech bot is running. Use /leech <mega link>.")
    log_channel = get_global_setting("log_channel_id")
    if log_channel:
        try:
            user = message.from_user
            uname = f"@{user.username}" if user and user.username else "unknown"
            await message._client.send_message(
                int(log_channel),
                f"New user started: {uname} ({user.id if user else 0})",
            )
        except Exception:
            pass


async def help_cmd(_, message):
    await message.reply(
        "Commands:\n/leech <mega link> - download and upload to Telegram\n"
        "/settings - customize leech settings\n"
        "/ping - check bot"
    )


async def ping_cmd(_, message):
    await message.reply("pong")


async def leech_cmd(client, message):
    await _run_leech(client, message)


async def settings_cmd(client, message):
    await settings_command(client, message)


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
        return await message.reply("You can only cancel your own task.")

    task_state.cancel_event.set()
    return


async def setlogchannel_cmd(client, message):
    if not _is_admin(message):
        return await message.reply("Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /setlogchannel <channel_id or @username>")
    try:
        channel_id = await _resolve_channel_id(client, parts[1])
    except Exception:
        return await message.reply("Invalid channel id or username.")
    set_global_setting("log_channel_id", str(channel_id))
    await message.reply(f"OK. Log channel set to {channel_id}")


async def settaskchannel_cmd(client, message):
    if not _is_admin(message):
        return await message.reply("Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /settaskchannel <channel_id or @username>")
    try:
        channel_id = await _resolve_channel_id(client, parts[1])
    except Exception:
        return await message.reply("Invalid channel id or username.")
    set_global_setting("task_channel_id", str(channel_id))
    await message.reply(f"OK. Task channel set to {channel_id}")


async def addadmin_cmd(client, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await message.reply("Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /addadmin <user_id or @username>")
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await message.reply("Invalid user id or username.")
    add_admin_id(user_id)
    await message.reply(f"Admin added: {user_id}")


async def deladmin_cmd(client, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await message.reply("Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /deladmin <user_id or @username>")
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await message.reply("Invalid user id or username.")
    remove_admin_id(user_id)
    await message.reply(f"Admin removed: {user_id}")


async def listadmins_cmd(_, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await message.reply("Unauthorized")
    admins = sorted(get_admin_ids())
    if not admins:
        return await message.reply("No admins set.")
    await message.reply("Admins:\n" + "\n".join(str(x) for x in admins))


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
    app.add_handler(MessageHandler(setlogchannel_cmd, filters.command("setlogchannel")))
    app.add_handler(MessageHandler(settaskchannel_cmd, filters.command("settaskchannel")))
    app.add_handler(MessageHandler(addadmin_cmd, filters.command("addadmin")))
    app.add_handler(MessageHandler(deladmin_cmd, filters.command("deladmin")))
    app.add_handler(MessageHandler(listadmins_cmd, filters.command("listadmins")))
    register_settings_handlers(app)

    LOGGER.info("Mega leech bot started")
    app.run()


if __name__ == "__main__":
    main()
