from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import (
    OWNER_ID,
    SUDO_USERS,
    MIN_TOKEN_AGE,
    SHORTLINK_API,
    SHORTLINK_SITE,
    TOKEN_TTL,
    VERIFY_EXPIRE,
    VERIFY_PHOTO,
    VERIFY_TUTORIAL,
)
from .settings_db import get_admin_ids, get_global_setting, get_settings, save_settings, set_global_setting

THUMB_DIR = Path("thumbs")
THUMB_DIR.mkdir(exist_ok=True)


@dataclass
class PendingInput:
    key: str
    message_id: int


PENDING_INPUT: dict[int, PendingInput] = {}
BSETTING_PENDING: dict[int, PendingInput] = {}
BSETTING_KEYS = [
    "VERIFY_EXPIRE",
    "TOKEN_TTL",
    "MIN_TOKEN_AGE",
    "VERIFY_PHOTO",
    "VERIFY_TUTORIAL",
    "SHORTLINK_SITE",
    "SHORTLINK_API",
    "SUPPORT_ID",
]


def _parse_id_list(value: str) -> set[int]:
    ids = set()
    for part in (value or "").replace(",", " ").split():
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


SUDO_USER_IDS = _parse_id_list(SUDO_USERS)


def _format_settings_text(settings: dict) -> str:
    chat_id = settings.get("chat_id") or "Not set"
    caption = settings.get("caption") or "Not set"
    thumb = "Set" if settings.get("thumb_path") else "Not set"
    return (
        "⚙️ <b>Leech Settings</b>\n\n"
        f"• <b>Chat ID</b> : {chat_id}\n"
        f"• <b>Caption</b> : {caption}\n"
        f"• <b>Thumb</b>   : {thumb}"
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


def _is_admin(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    if user_id in SUDO_USER_IDS:
        return True
    if user_id in get_admin_ids():
        return True
    return False


def _get_verif_value(key: str) -> str:
    raw = (get_global_setting(key) or "").strip()
    if raw:
        return raw
    defaults = {
        "VERIFY_EXPIRE": str(VERIFY_EXPIRE),
        "TOKEN_TTL": str(TOKEN_TTL),
        "MIN_TOKEN_AGE": str(MIN_TOKEN_AGE),
        "VERIFY_PHOTO": VERIFY_PHOTO,
        "VERIFY_TUTORIAL": VERIFY_TUTORIAL,
        "SHORTLINK_SITE": SHORTLINK_SITE,
        "SHORTLINK_API": SHORTLINK_API,
        "SUPPORT_ID": "",
    }
    return defaults.get(key, "")


def _format_bsetting_text() -> str:
    lines = ["🧩 <b>Verification Settings</b>"]
    for key in BSETTING_KEYS:
        value = _get_verif_value(key)
        lines.append(f"{key}: {value or 'none'}")
    lines.append("")
    lines.append("Tap a key to set. Send <code>clear</code> to unset.")
    return "\n".join(lines)


def _bsetting_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("VERIFY_EXPIRE", callback_data="bsetting:VERIFY_EXPIRE"),
            InlineKeyboardButton("TOKEN_TTL", callback_data="bsetting:TOKEN_TTL"),
        ],
        [
            InlineKeyboardButton("MIN_TOKEN_AGE", callback_data="bsetting:MIN_TOKEN_AGE"),
        ],
        [
            InlineKeyboardButton("VERIFY_PHOTO", callback_data="bsetting:VERIFY_PHOTO"),
            InlineKeyboardButton("VERIFY_TUTORIAL", callback_data="bsetting:VERIFY_TUTORIAL"),
        ],
        [
            InlineKeyboardButton("SHORTLINK_SITE", callback_data="bsetting:SHORTLINK_SITE"),
            InlineKeyboardButton("SHORTLINK_API", callback_data="bsetting:SHORTLINK_API"),
        ],
        [
            InlineKeyboardButton("SUPPORT_ID", callback_data="bsetting:SUPPORT_ID"),
        ],
        [InlineKeyboardButton("CLOSE", callback_data="bsetting:close")],
    ]
    return InlineKeyboardMarkup(rows)


async def send_settings_message(message, user_id: int) -> None:
    settings = get_settings(user_id)
    await message.reply_text(
        _format_settings_text(settings),
        reply_markup=_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def settings_command(_, message):
    await send_settings_message(message, message.from_user.id)


async def bsettings_command(_, message):
    if not _is_admin(message.from_user.id):
        return await message.reply_text(
            "⛔ <b>Unauthorized</b>", parse_mode=ParseMode.HTML
        )
    await message.reply_text(
        _format_bsetting_text(),
        reply_markup=_bsetting_keyboard(),
        parse_mode=ParseMode.HTML,
    )


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
            "📨 <b>Send the chat ID</b> (with -100 prefix).\n"
            "For topics: <code>-100CHATID/TOPIC_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if action == "setcaption":
        PENDING_INPUT[user_id] = PendingInput(key="caption", message_id=cq.message.id)
        await cq.message.reply_text(
            "📝 <b>Send caption template</b>\n"
            "You can use <code>{filename}</code>, <code>{basename}</code>, <code>{ext}</code>.",
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if action == "setthumb":
        PENDING_INPUT[user_id] = PendingInput(key="thumb", message_id=cq.message.id)
        await cq.message.reply_text(
            "🖼️ <b>Send the photo</b> you want to set as thumbnail.",
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if action == "remthumb":
        if settings.get("thumb_path") and os.path.exists(settings["thumb_path"]):
            os.remove(settings["thumb_path"])
        settings["thumb_path"] = ""
        save_settings(user_id, settings)
        await cq.message.edit_text(
            _format_settings_text(settings),
            reply_markup=_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if action == "reset":
        settings["chat_id"] = ""
        settings["caption"] = ""
        if settings.get("thumb_path") and os.path.exists(settings["thumb_path"]):
            os.remove(settings["thumb_path"])
        settings["thumb_path"] = ""
        save_settings(user_id, settings)
        await cq.message.edit_text(
            _format_settings_text(settings),
            reply_markup=_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if action == "close":
        await cq.message.delete()
        await cq.answer()
        return

    await cq.answer()


async def bsettings_callback(_, cq):
    user_id = cq.from_user.id
    data = cq.data or ""
    if not data.startswith("bsetting:"):
        return
    if not _is_admin(user_id):
        await cq.answer("Unauthorized", show_alert=True)
        return

    action = data.split(":", 1)[1]
    if action == "close":
        await cq.message.delete()
        await cq.answer()
        return

    if action in BSETTING_KEYS:
        BSETTING_PENDING[user_id] = PendingInput(key=action, message_id=cq.message.id)
        await cq.message.reply_text(
            f"🧩 <b>Send value for {action}</b>\nType <code>clear</code> to unset.",
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    await cq.answer()


async def settings_input_handler(_, message):
    user_id = message.from_user.id
    pending_b = BSETTING_PENDING.get(user_id)
    if pending_b:
        key = pending_b.key
        value = (message.text or "").strip()
        if value.lower() in {"clear", "unset", "remove", "none"}:
            set_global_setting(key, "")
            await message.reply_text(
                f"🧹 <b>{key} cleared.</b>", parse_mode=ParseMode.HTML
            )
        else:
            set_global_setting(key, value)
            await message.reply_text(
                f"✅ <b>{key} updated.</b>", parse_mode=ParseMode.HTML
            )
        BSETTING_PENDING.pop(user_id, None)
        try:
            settings_message = await message._client.get_messages(
                message.chat.id, pending_b.message_id
            )
            await settings_message.edit_text(
                _format_bsetting_text(),
                reply_markup=_bsetting_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    pending = PENDING_INPUT.get(user_id)
    if not pending:
        return

    settings = get_settings(user_id)
    if pending.key == "chat_id":
        settings["chat_id"] = (message.text or "").strip()
        save_settings(user_id, settings)
        await message.reply_text(
            "✅ <b>Chat ID set successfully.</b>", parse_mode=ParseMode.HTML
        )
    elif pending.key == "caption":
        settings["caption"] = message.text or ""
        save_settings(user_id, settings)
        await message.reply_text(
            "✅ <b>Caption set successfully.</b>", parse_mode=ParseMode.HTML
        )
    elif pending.key == "thumb":
        if not message.photo:
            await message.reply_text(
                "❗ <b>Please send a photo.</b>", parse_mode=ParseMode.HTML
            )
            return
        thumb_path = THUMB_DIR / f"{user_id}.jpg"
        temp = await message.download()
        if thumb_path.exists():
            thumb_path.unlink()
        os.replace(temp, thumb_path)
        settings["thumb_path"] = str(thumb_path)
        save_settings(user_id, settings)
        await message.reply_text(
            "✅ <b>Thumbnail saved successfully.</b>", parse_mode=ParseMode.HTML
        )

    PENDING_INPUT.pop(user_id, None)
    try:
        settings_message = await message._client.get_messages(
            message.chat.id, pending.message_id
        )
        await settings_message.edit_text(
            _format_settings_text(settings),
            reply_markup=_keyboard(),
            parse_mode=ParseMode.HTML,
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
    app.add_handler(CallbackQueryHandler(settings_callback, filters.regex("^settings:")))
    app.add_handler(CallbackQueryHandler(bsettings_callback, filters.regex("^bsetting:")))
