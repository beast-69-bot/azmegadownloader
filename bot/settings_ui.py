from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .settings_db import get_settings, save_settings

THUMB_DIR = Path("thumbs")
THUMB_DIR.mkdir(exist_ok=True)


@dataclass
class PendingInput:
    key: str
    message_id: int


PENDING_INPUT: dict[int, PendingInput] = {}


def _format_settings_text(settings: dict) -> str:
    chat_id = settings.get("chat_id") or "Not set"
    caption = settings.get("caption") or "Not set"
    thumb = "Set" if settings.get("thumb_path") else "Not set"
    return (
        "Customize settings for your files...\n\n"
        f"Chat ID : {chat_id}\n"
        f"Caption : {caption}\n"
        f"Thumb   : {thumb}"
    )


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Set Chat ID", callback_data="settings:setchat"),
                InlineKeyboardButton("Set Caption", callback_data="settings:setcaption"),
            ],
            [
                InlineKeyboardButton("Set Thumbnail", callback_data="settings:setthumb"),
                InlineKeyboardButton("Remove Thumbnail", callback_data="settings:remthumb"),
            ],
            [
                InlineKeyboardButton("Reset Settings", callback_data="settings:reset"),
                InlineKeyboardButton("CLOSE", callback_data="settings:close"),
            ],
        ]
    )


async def send_settings_message(message, user_id: int) -> None:
    settings = get_settings(user_id)
    await message.reply_text(_format_settings_text(settings), reply_markup=_keyboard())


async def settings_command(_, message):
    await send_settings_message(message, message.from_user.id)


async def settings_callback(_, cq):
    user_id = cq.from_user.id
    data = cq.data or ""
    if not data.startswith("settings:"):
        return

    settings = get_settings(user_id)
    action = data.split(":", 1)[1]

    if action == "setchat":
        PENDING_INPUT[user_id] = PendingInput(key="chat_id", message_id=cq.message.id)
        await cq.message.reply_text(
            "Send the chat ID (with -100 prefix). For topics: -100CHATID/TOPIC_ID"
        )
        await cq.answer()
        return

    if action == "setcaption":
        PENDING_INPUT[user_id] = PendingInput(key="caption", message_id=cq.message.id)
        await cq.message.reply_text(
            "Send caption template. You can use {filename}, {basename}, {ext}."
        )
        await cq.answer()
        return

    if action == "setthumb":
        PENDING_INPUT[user_id] = PendingInput(key="thumb", message_id=cq.message.id)
        await cq.message.reply_text("Send the photo you want to set as thumbnail.")
        await cq.answer()
        return

    if action == "remthumb":
        if settings.get("thumb_path") and os.path.exists(settings["thumb_path"]):
            os.remove(settings["thumb_path"])
        settings["thumb_path"] = ""
        save_settings(user_id, settings)
        await cq.message.edit_text(_format_settings_text(settings), reply_markup=_keyboard())
        await cq.answer()
        return

    if action == "reset":
        settings["chat_id"] = ""
        settings["caption"] = ""
        if settings.get("thumb_path") and os.path.exists(settings["thumb_path"]):
            os.remove(settings["thumb_path"])
        settings["thumb_path"] = ""
        save_settings(user_id, settings)
        await cq.message.edit_text(_format_settings_text(settings), reply_markup=_keyboard())
        await cq.answer()
        return

    if action == "close":
        await cq.message.delete()
        await cq.answer()
        return

    await cq.answer()


async def settings_input_handler(_, message):
    user_id = message.from_user.id
    pending = PENDING_INPUT.get(user_id)
    if not pending:
        return

    settings = get_settings(user_id)
    if pending.key == "chat_id":
        settings["chat_id"] = (message.text or "").strip()
        save_settings(user_id, settings)
        await message.reply_text("Chat ID set successfully.")
    elif pending.key == "caption":
        settings["caption"] = message.text or ""
        save_settings(user_id, settings)
        await message.reply_text("Caption set successfully.")
    elif pending.key == "thumb":
        if not message.photo:
            await message.reply_text("Please send a photo.")
            return
        thumb_path = THUMB_DIR / f"{user_id}.jpg"
        temp = await message.download()
        if thumb_path.exists():
            thumb_path.unlink()
        os.replace(temp, thumb_path)
        settings["thumb_path"] = str(thumb_path)
        save_settings(user_id, settings)
        await message.reply_text("Thumbnail saved successfully.")

    PENDING_INPUT.pop(user_id, None)
    try:
        settings_message = await message._client.get_messages(
            message.chat.id, pending.message_id
        )
        await settings_message.edit_text(
            _format_settings_text(settings), reply_markup=_keyboard()
        )
    except Exception:
        pass


def register_settings_handlers(app: Client) -> None:
    app.add_handler(
        MessageHandler(
            settings_input_handler,
            filters.private
            & ~filters.command(["settings", "cancel", "start", "help", "leech", "ping"]),
        )
    )
    app.add_handler(CallbackQueryHandler(settings_callback))
