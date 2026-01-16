from __future__ import annotations

# Adapted from WZML-X (wzv3) user settings patterns:
# https://github.com/WZML-X/WZML/tree/wzv3

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .settings_db import DEFAULT_SETTINGS, get_settings, save_settings

THUMB_DIR = Path("thumbnails")
THUMB_DIR.mkdir(exist_ok=True)


@dataclass
class PendingInput:
    key: str
    message_id: int


PENDING_INPUT: dict[int, PendingInput] = {}
PENDING_LOCK = asyncio.Lock()


def _fmt_bool(value: bool) -> str:
    return "✅" if value else "❌"


def _format_split_size(size_bytes: int) -> str:
    if not size_bytes:
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f}PB"


def _parse_size(value: str) -> int:
    value = value.strip().lower()
    if value.isdigit():
        return int(value)
    multipliers = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    for unit, mul in multipliers.items():
        if value.endswith(unit):
            num = float(value[: -len(unit)].strip())
            return int(num * mul)
    raise ValueError("Invalid size. Use bytes or kb/mb/gb/tb.")


def build_settings_text(settings: dict) -> str:
    return (
        "<b>LEECH SETTINGS</b>\n\n"
        f"TYPE        : {_fmt_bool(settings['TYPE'])}\n"
        f"THUMB       : {_fmt_bool(settings['THUMB'])}\n"
        f"SPLIT SIZE  : {_format_split_size(int(settings['SPLIT_SIZE']))}\n"
        f"EQUAL       : {_fmt_bool(settings['EQUAL'])}\n"
        f"GROUP       : {_fmt_bool(settings['GROUP'])}\n"
        f"DESTINATION : {settings['DESTINATION'] or 'None'}\n"
        f"PREFIX      : {settings['PREFIX'] or 'None'}\n"
        f"SUFFIX      : {settings['SUFFIX'] or 'None'}\n"
        f"CAPTION     : {settings['CAPTION'] or 'None'}\n"
        f"LAYOUT      : {settings['LAYOUT'] or 'None'}"
    )


def build_keyboard(user_id: int, settings: dict) -> InlineKeyboardMarkup:
    def cb(action: str) -> str:
        return f"settings:{user_id}:{action}"

    rows = [
        [
            InlineKeyboardButton(f"TYPE {_fmt_bool(settings['TYPE'])}", callback_data=cb("toggle:TYPE")),
            InlineKeyboardButton(f"THUMB {_fmt_bool(settings['THUMB'])}", callback_data=cb("toggle:THUMB")),
        ],
        [
            InlineKeyboardButton(f"EQUAL {_fmt_bool(settings['EQUAL'])}", callback_data=cb("toggle:EQUAL")),
            InlineKeyboardButton(f"GROUP {_fmt_bool(settings['GROUP'])}", callback_data=cb("toggle:GROUP")),
        ],
        [
            InlineKeyboardButton("THUMBNAIL", callback_data=cb("menu:THUMBNAIL")),
            InlineKeyboardButton("SPLIT SIZE", callback_data=cb("menu:SPLIT_SIZE")),
        ],
        [
            InlineKeyboardButton("DESTINATION", callback_data=cb("menu:DESTINATION")),
            InlineKeyboardButton("PREFIX", callback_data=cb("menu:PREFIX")),
        ],
        [
            InlineKeyboardButton("SUFFIX", callback_data=cb("menu:SUFFIX")),
            InlineKeyboardButton("CAPTION", callback_data=cb("menu:CAPTION")),
        ],
        [
            InlineKeyboardButton("LAYOUT", callback_data=cb("menu:LAYOUT")),
        ],
        [
            InlineKeyboardButton("BACK", callback_data=cb("back")),
            InlineKeyboardButton("CLOSE", callback_data=cb("close")),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def show_settings(message, user_id: int) -> None:
    settings = get_settings(user_id)
    text = build_settings_text(settings)
    await message.edit_text(text, reply_markup=build_keyboard(user_id, settings), parse_mode=ParseMode.HTML)


async def handle_settings_command(client: Client, message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    text = build_settings_text(settings)
    await message.reply_text(
        text, reply_markup=build_keyboard(user_id, settings), parse_mode=ParseMode.HTML
    )


async def handle_settings_callback(client: Client, query) -> None:
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "settings":
        return
    user_id = int(parts[1])
    if query.from_user and query.from_user.id != user_id:
        return await query.answer("Not yours.", show_alert=True)

    action = parts[2] if len(parts) > 2 else ""
    settings = get_settings(user_id)

    if action.startswith("toggle:"):
        key = action.split(":", 1)[1]
        settings[key] = not bool(settings.get(key, False))
        save_settings(user_id, settings)
        await query.answer()
        return await show_settings(query.message, user_id)

    if action.startswith("menu:"):
        key = action.split(":", 1)[1]
        async with PENDING_LOCK:
            PENDING_INPUT[user_id] = PendingInput(key=key, message_id=query.message.id)
        await query.answer()
        prompt = {
            "THUMBNAIL": "Send a photo to set thumbnail or /remove to clear.",
            "SPLIT_SIZE": "Send split size (e.g. 1.95GB or 2000MB). Send 0 to disable.",
            "DESTINATION": "Send destination folder name or /reset.",
            "PREFIX": "Send filename prefix or /reset.",
            "SUFFIX": "Send filename suffix or /reset.",
            "CAPTION": "Send caption template or /reset. Use {filename}, {basename}, {ext}.",
            "LAYOUT": "Send layout name or /reset.",
        }[key]
        return await query.message.edit_text(
            f"<b>Set {key}</b>\n{prompt}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("BACK", callback_data=f"settings:{user_id}:back")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    if action == "back":
        await query.answer()
        return await show_settings(query.message, user_id)

    if action == "close":
        await query.answer()
        return await query.message.delete()


async def handle_settings_input(client: Client, message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    async with PENDING_LOCK:
        pending = PENDING_INPUT.get(user_id)
    if not pending:
        return

    settings = get_settings(user_id)
    key = pending.key

    if key == "THUMBNAIL":
        if message.text and message.text.strip().lower() == "/remove":
            settings["THUMB"] = False
            settings["THUMB_PATH"] = ""
            save_settings(user_id, settings)
        elif message.photo:
            thumb_path = THUMB_DIR / f"{user_id}.jpg"
            await message.download(file_name=str(thumb_path))
            settings["THUMB"] = True
            settings["THUMB_PATH"] = str(thumb_path)
            save_settings(user_id, settings)
        else:
            await message.reply_text("Send a photo or /remove.")
            return
    else:
        value = message.text or ""
        if value.strip().lower() == "/reset":
            settings[key] = DEFAULT_SETTINGS[key]
            save_settings(user_id, settings)
        else:
            if key == "SPLIT_SIZE":
                settings[key] = _parse_size(value)
            else:
                settings[key] = value.strip()
            save_settings(user_id, settings)

    async with PENDING_LOCK:
        PENDING_INPUT.pop(user_id, None)
    settings_message = await client.get_messages(message.chat.id, pending.message_id)
    await show_settings(settings_message, user_id)
    await message.delete()


def register_settings_handlers(app: Client) -> None:
    app.add_handler(MessageHandler(handle_settings_input))
    app.add_handler(CallbackQueryHandler(handle_settings_callback))
