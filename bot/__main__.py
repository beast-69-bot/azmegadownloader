from __future__ import annotations

import asyncio
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
from .mega_download import download_mega
from .progress import ProgressMessage
from .uploader import upload_path
from .utils import is_mega_link, safe_link_from_text

DOWNLOAD_SEM = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
UPLOAD_SEM = asyncio.Semaphore(CONCURRENT_UPLOADS)


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
    progress = ProgressMessage(status, "Downloading", STATUS_UPDATE_INTERVAL)

    async def progress_cb(done, speed, total):
        await progress.update(done, total, speed)

    dest = DOWNLOAD_DIR / str(message.id)

    await DOWNLOAD_SEM.acquire()
    try:
        await download_mega(link, dest, progress_cb)
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
        await upload_path(client, message.chat.id, dest, status)
        await status.edit_text("Leech complete.")
    except Exception as e:
        LOGGER.error(f"Upload failed: {e}")
        await status.edit_text(f"Upload failed: {e}")
    finally:
        UPLOAD_SEM.release()
        await _cleanup(dest)


async def start_cmd(_, message):
    await message.reply("MEGA leech bot is running. Use /leech <mega link>.")


async def help_cmd(_, message):
    await message.reply(
        "Commands:\n/leech <mega link> - download and upload to Telegram\n/ping - check bot"
    )


async def ping_cmd(_, message):
    await message.reply("pong")


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
    app.add_handler(
        MessageHandler(
            lambda c, m: asyncio.create_task(_run_leech(c, m)),
            filters.command("leech"),
        )
    )

    LOGGER.info("Mega leech bot started")
    app.run()


if __name__ == "__main__":
    main()
