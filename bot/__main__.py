from __future__ import annotations

import asyncio
import datetime
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

from pyrogram import Client, StopPropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import LOGGER
from .config import (
    AUTHORIZED_CHAT_IDS,
    BOT_TOKEN,
    CONCURRENT_DOWNLOADS,
    CONCURRENT_UPLOADS,
    DOWNLOAD_DIR,
    OWNER_ID,
    SUDO_USERS,
    STATUS_UPDATE_INTERVAL,
    TELEGRAM_API,
    TELEGRAM_HASH,
    MIN_TOKEN_AGE,
    SHORTLINK_API,
    SHORTLINK_SITE,
    TOKEN_TTL,
    VERIFY_EXPIRE,
    VERIFY_PHOTO,
    VERIFY_TUTORIAL,
)
from .mega_download import download_mega_url, get_mega_total_size
from .progress import ProgressMessage
from .uploader import TaskCancelledUpload, upload_path
from .utils import is_mega_link, safe_link_from_text
from .settings_db import (
    add_admin_id,
    clear_verify_strikes,
    clear_verify_tokens,
    create_verify_token,
    get_admin_ids,
    get_daily_task_count,
    get_global_setting,
    get_settings,
    get_verify_status,
    get_verify_token,
    increment_daily_task_count,
    is_globally_banned,
    is_premium,
    is_user_banned,
    get_premium_expire_ts,
    list_premium_users,
    parse_chat_target,
    record_verify_strike,
    remove_admin_id,
    set_premium,
    set_verify_status,
    set_global_setting,
    delete_verify_token,
)
from .settings_ui import bsettings_command, register_settings_handlers, settings_command

DOWNLOAD_SEM = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
UPLOAD_SEM = asyncio.Semaphore(CONCURRENT_UPLOADS)
_TASK_COUNTER_DATE = None
_TASK_COUNTER_VALUE = 0
_TASK_COUNTER_LOCK = asyncio.Lock()
_ACTIVE_TASKS: dict[int, "TaskState"] = {}
SUDO_USER_IDS: set[int] = set()
_BOT_USERNAME = ""
PAYMENT_PENDING: dict[int, "PaymentRequest"] = {}


class PaymentRequest:
    def __init__(self, plan_key: str, label: str, price: int, seconds: int):
        self.plan_key = plan_key
        self.label = label
        self.price = price
        self.seconds = seconds
        self.screenshot_id: str | None = None
        self.utr: str | None = None


PAYMENT_PLANS = {
    "1d": ("1 Day", 1 * 24 * 60 * 60, 5),
    "1w": ("1 Week", 7 * 24 * 60 * 60, 30),
    "1m": ("1 Month", 30 * 24 * 60 * 60, 50),
}


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


class PrioritySemaphore:
    def __init__(self, value: int):
        self._value = value
        self._waiters = []
        self._counter = 0
        self._lock = asyncio.Lock()

    async def acquire(self, priority: int) -> None:
        async with self._lock:
            if self._value > 0 and not self._waiters:
                self._value -= 1
                return
            event = asyncio.Event()
            self._counter += 1
            self._waiters.append((priority, self._counter, event))
            self._waiters.sort()
        await event.wait()

    async def release(self) -> None:
        async with self._lock:
            if self._waiters:
                _priority, _count, event = self._waiters.pop(0)
                event.set()
            else:
                self._value += 1


DOWNLOAD_SEM = PrioritySemaphore(CONCURRENT_DOWNLOADS)
UPLOAD_SEM = PrioritySemaphore(CONCURRENT_UPLOADS)


def _authorized(message) -> bool:
    if OWNER_ID and message.from_user and message.from_user.id == OWNER_ID:
        return True
    if not AUTHORIZED_CHAT_IDS:
        return True
    return message.chat.id in AUTHORIZED_CHAT_IDS


def _parse_id_list(value: str) -> set[int]:
    ids = set()
    for part in (value or "").replace(",", " ").split():
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


SUDO_USER_IDS = _parse_id_list(SUDO_USERS)


def _is_admin(message) -> bool:
    if OWNER_ID and message.from_user and message.from_user.id == OWNER_ID:
        return True
    if message.from_user and message.from_user.id in SUDO_USER_IDS:
        return True
    if message.from_user and message.from_user.id in get_admin_ids():
        return True
    return False


def _get_verif_int(key: str, default: int) -> int:
    raw = (get_global_setting(key) or "").strip()
    if not raw:
        return int(default or 0)
    try:
        return int(raw)
    except ValueError:
        return int(default or 0)


def _get_verif_str(key: str, default: str) -> str:
    raw = (get_global_setting(key) or "").strip()
    if raw:
        return raw
    return default or ""


def _parse_validity(value: str) -> int:
    value = (value or "").strip().lower()
    if not value:
        return 0
    if value[-1] not in {"w", "m", "y"}:
        return 0
    num = value[:-1]
    if not num.isdigit():
        return 0
    count = int(num)
    if count <= 0:
        return 0
    if value.endswith("w"):
        return count * 7 * 24 * 60 * 60
    if value.endswith("m"):
        return count * 30 * 24 * 60 * 60
    return count * 365 * 24 * 60 * 60


def _is_verified_user(user_id: int) -> bool:
    if not user_id:
        return False
    ts = get_verify_status(user_id)
    if not ts:
        return False
    verify_expire = _get_verif_int("VERIFY_EXPIRE", VERIFY_EXPIRE)
    if verify_expire and int(time.time()) - int(ts) > verify_expire:
        return False
    return True


async def _get_bot_username(client: Client) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    me = await client.get_me()
    _BOT_USERNAME = me.username or ""
    return _BOT_USERNAME


def _shorten_url(url: str, site: str, api_key: str) -> str:
    if not site or not api_key:
        return url
    api = (
        f"{site.rstrip('/')}/api?api="
        f"{urllib.parse.quote(api_key)}&url={urllib.parse.quote(url)}&format=text"
    )
    try:
        with urllib.request.urlopen(api, timeout=10) as resp:
            data = resp.read().decode().strip()
        if data.startswith("http"):
            return data
    except Exception:
        return url
    return url


async def _reply(message, text, **kwargs):
    if "parse_mode" not in kwargs:
        kwargs["parse_mode"] = ParseMode.HTML
    return await message.reply(text, **kwargs)


async def _edit(message, text, **kwargs):
    return await message.edit_text(text, parse_mode=ParseMode.HTML, **kwargs)


async def _send_verification_prompt(client: Client, message) -> None:
    user = message.from_user
    if not user:
        return
    if is_user_banned(user.id):
        await _reply(
            message,
            "❌ <b>You are banned from verification</b>",
            reply_markup=_support_button(),
        )
        return
    ttl = _get_verif_int("TOKEN_TTL", TOKEN_TTL) or 600
    token_info = create_verify_token(user.id, ttl)
    token = token_info["token"]
    username = await _get_bot_username(client)
    if not username:
        await _reply(message, "⚠️ <b>Verification unavailable.</b> Please try again later.")
        return
    start_param = f"verify-{user.id}-{token}"
    deep_link = f"https://t.me/{username}?start={urllib.parse.quote(start_param)}"
    short_site = _get_verif_str("SHORTLINK_SITE", SHORTLINK_SITE)
    short_api = _get_verif_str("SHORTLINK_API", SHORTLINK_API)
    short_link = await asyncio.to_thread(_shorten_url, deep_link, short_site, short_api)
    buttons = [[InlineKeyboardButton("Get Token", url=short_link)]]
    tutorial_url = _get_verif_str("VERIFY_TUTORIAL", VERIFY_TUTORIAL)
    if tutorial_url:
        buttons.append([InlineKeyboardButton("Tutorial", url=tutorial_url)])
    markup = InlineKeyboardMarkup(buttons)
    text = (
        "🔒 <b>Verification required</b>\n"
        "<i>Click Get Token and return to this chat to verify.</i>"
    )
    photo_url = _get_verif_str("VERIFY_PHOTO", VERIFY_PHOTO)
    if photo_url:
        await message.reply_photo(photo_url, caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, reply_markup=markup)


def _support_button() -> InlineKeyboardMarkup | None:
    support_id = _get_verif_str("SUPPORT_ID", "")
    if not support_id:
        return None
    if not support_id.startswith("@"):
        support_id = f"@{support_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Contact Admin", url=f"https://t.me/{support_id.lstrip('@')}")]]
    )


def _payment_button() -> InlineKeyboardMarkup | None:
    support_id = _get_verif_str("SUPPORT_ID", "")
    if not support_id:
        return None
    if not support_id.startswith("@"):
        support_id = f"@{support_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{support_id.lstrip('@')}")]]
    )


async def verification_gate(client: Client, message):
    if not message.from_user:
        return
    if _is_admin(message):
        return
    if message.command:
        cmd = message.command[0]
        if cmd in {"start", "help", "ping", "settings", "leech"}:
            return
    if is_user_banned(message.from_user.id):
        await _reply(message, 
            "❌ <b>You are banned from verification</b>", reply_markup=_support_button()
        )
        raise StopPropagation
    if not is_premium(message.from_user.id) and not _is_verified_user(message.from_user.id):
        await _send_verification_prompt(client, message)
        raise StopPropagation


async def _notify_ban(client: Client, user_id: int) -> None:
    targets = set(get_admin_ids()) | set(SUDO_USER_IDS)
    if OWNER_ID:
        targets.add(OWNER_ID)
    for admin_id in targets:
        if not admin_id:
            continue
        try:
            await client.send_message(
                int(admin_id),
                f"User banned for bypass: {user_id}",
            )
        except Exception:
            continue


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
        return await _reply(message, "⛔ <b>Unauthorized</b>")

    link = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            link = parts[1].strip()
    if not link and message.reply_to_message and message.reply_to_message.text:
        link = safe_link_from_text(message.reply_to_message.text)

    if not is_mega_link(link):
        return await _reply(message, "❗ <b>Send a MEGA link with /leech</b>")

    user_id = message.from_user.id if message.from_user else 0
    if user_id and is_globally_banned(user_id):
        return await _reply(message, "⛔ <b>You are banned from using this bot.</b>")

    premium = user_id and is_premium(user_id)
    if user_id and not premium:
        if is_user_banned(user_id):
            return await _reply(
                message,
                "❌ <b>You are banned from verification</b>",
                reply_markup=_support_button(),
            )
        if not _is_verified_user(user_id):
            await _send_verification_prompt(client, message)
            return

    today = datetime.date.today().isoformat()
    if user_id and not premium:
        current = get_daily_task_count(user_id, today)
        if current >= 3:
            return await _reply(
                message,
                "❌ <b>Free plan daily limit is 3 tasks.</b> Upgrade to Premium.",
            )

    status = await _reply(message, "⬇️ <b>Starting download...</b>")
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
    task_state = TaskState(task_number, user_id)
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

    if user_id and not premium:
        if total_size <= 0 or total_size > 20 * 1024 * 1024 * 1024:
            return await _edit(
                status,
                "❌ <b>Free plan Mega size limit is 20GB.</b> Upgrade to Premium.",
            )
        increment_daily_task_count(user_id, today)

    await DOWNLOAD_SEM.acquire(0 if premium else 1)
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
        await _edit(status, f"🚫 <b>Task {task_number} cancelled by user.</b>")
        await _cleanup(dest)
        _ACTIVE_TASKS.pop(task_number, None)
        return
    except Exception as e:
        LOGGER.error(f"Download failed: {e}")
        await _edit(status, f"❌ <b>Download failed:</b> {e}")
        await _cleanup(dest)
        return
    finally:
        await DOWNLOAD_SEM.release()

    await progress.finalize("Download complete. Uploading...")

    await UPLOAD_SEM.acquire(0 if premium else 1)
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
        await _edit(status, "✅ <b>Leech complete.</b>")
    except (TaskCancelled, TaskCancelledUpload):
        await _edit(status, f"🚫 <b>Task {task_number} cancelled by user.</b>")
    except Exception as e:
        LOGGER.error(f"Upload failed: {e}")
        await _edit(status, f"❌ <b>Upload failed:</b> {e}")
    finally:
        await UPLOAD_SEM.release()
        await _cleanup(dest)
        _ACTIVE_TASKS.pop(task_number, None)


async def start_cmd(client, message):
    user = message.from_user
    param = ""
    if message.command and len(message.command) > 1:
        param = message.command[1].strip()

    if user and param.startswith("verify-"):
        parts = param.split("-", 2)
        if len(parts) != 3:
            return await _reply(message, "❌ <b>Invalid verification token.</b>")
        try:
            token_user_id = int(parts[1])
        except ValueError:
            return await _reply(message, "❌ <b>Invalid verification token.</b>")
        token_value = parts[2].strip()
        if token_user_id != user.id:
            return await _reply(message, "❗ <b>This token is not for your account.</b>")
        if is_user_banned(user.id):
            return await _reply(
                message,
                "❌ <b>You are banned from verification</b>",
                reply_markup=_support_button(),
            )
        token_info = get_verify_token(user.id, token_value)
        if not token_info:
            return await _reply(message, "❌ <b>Invalid or expired token.</b>")
        now = int(time.time())
        if token_info["expire_at"] and now > int(token_info["expire_at"]):
            delete_verify_token(user.id, token_value)
            return await _reply(message, "⌛ <b>Token expired.</b> Please request a new one.")
        min_age = _get_verif_int("MIN_TOKEN_AGE", MIN_TOKEN_AGE)
        if min_age and now - int(token_info["created_at"]) < min_age:
            strikes, banned = record_verify_strike(user.id)
            if banned:
                await _notify_ban(client, user.id)
                await _reply(
                    message,
                    "❌ <b>You are banned from verification</b>",
                    reply_markup=_support_button(),
                )
                return
            delete_verify_token(user.id, token_value)
            ttl = _get_verif_int("TOKEN_TTL", TOKEN_TTL) or 600
            new_token = create_verify_token(user.id, ttl)
            username = await _get_bot_username(client)
            start_param = f"verify-{user.id}-{new_token['token']}"
            deep_link = f"https://t.me/{username}?start={urllib.parse.quote(start_param)}"
            short_site = _get_verif_str("SHORTLINK_SITE", SHORTLINK_SITE)
            short_api = _get_verif_str("SHORTLINK_API", SHORTLINK_API)
            short_link = await asyncio.to_thread(_shorten_url, deep_link, short_site, short_api)
            buttons = [[InlineKeyboardButton("Get New Token", url=short_link)]]
            markup = InlineKeyboardMarkup(buttons)
            warning_count = strikes if strikes <= 2 else 2
            return await _reply(
                message,
                "Nice try champ. Ab jaake YouTube se 'How to bypass' dekh. "
                f"Warning {warning_count}/2.",
                reply_markup=markup,
            )
        set_verify_status(user.id, now)
        clear_verify_strikes(user.id)
        delete_verify_token(user.id, token_value)
        clear_verify_tokens(user.id)
        await _reply(message, "✅ <b>Verification successful.</b>")
        return

    if user and not _is_admin(message) and not _is_verified_user(user.id):
        await _send_verification_prompt(client, message)
        return

    await _reply(message, "✅ <b>MEGA leech bot is running.</b> Use /leech &lt;mega link&gt;.")
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
    await _reply(
        message,
        "\U0001f916 AZ MEGA DOWNLOADER BOT\n"
        "Fast \u2022 Secure \u2022 Simple\n\n"
        "\U0001f4cc User Commands\n"
        "\u2022 /leech &lt;mega link&gt;  \u2192 download + upload\n"
        "\u2022 /cancel &lt;task_id&gt;   \u2192 cancel your task\n"
        "\u2022 /settings           \u2192 chat id, caption, thumbnail\n"
        "\u2022 /ping               \u2192 bot status\n"
        "\u2022 /start              \u2192 start bot\n"
        "\u2022 /help               \u2192 this menu\n\n"
        "\U0001f6e1 Admin / Sudo\n"
        "\u2022 /setlogchannel &lt;channel&gt;\n"
        "\u2022 /settaskchannel &lt;channel&gt;\n"
        "\u2022 /addadmin &lt;user&gt;\n"
        "\u2022 /deladmin &lt;user&gt;\n"
        "\u2022 /listadmins\n"
        "\u2022 /bsetting\n"
        "\u2022 /setpremium &lt;user&gt; &lt;validity&gt;\n"
        "\u2022 /delpremium &lt;user&gt;\n"
        "\u2022 /listpremium\n\n"
        "\U0001f4a1 Premium: unlimited tasks + priority \U0001f680",
    )


async def ping_cmd(_, message):
    await _reply(message, "🏓 <b>pong</b>")


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
        return await _reply(message, "⚠️ <b>No active task found.</b>")
    if message.from_user and task_state.owner_user_id != message.from_user.id:
        return await _reply(message, "⚠️ <b>You can only cancel your own task.</b>")

    task_state.cancel_event.set()
    return


async def setlogchannel_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /setlogchannel &lt;channel_id or @username&gt;\nExample: /setlogchannel -1001234567890"
        )
    try:
        channel_id = await _resolve_channel_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid channel id or username.</b>")
    set_global_setting("log_channel_id", str(channel_id))
    await _reply(message, f"✅ <b>Log channel set to</b> {channel_id}")


async def settaskchannel_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /settaskchannel &lt;channel_id or @username&gt;\nExample: /settaskchannel @mychannel"
        )
    try:
        channel_id = await _resolve_channel_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid channel id or username.</b>")
    set_global_setting("task_channel_id", str(channel_id))
    await _reply(message, f"✅ <b>Task channel set to</b> {channel_id}")


async def addadmin_cmd(client, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /addadmin &lt;user_id or @username&gt;\nExample: /addadmin 123456789"
        )
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid user id or username.</b>")
    add_admin_id(user_id)
    await _reply(message, f"👑 <b>Admin added:</b> {user_id}")


async def deladmin_cmd(client, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /deladmin &lt;user_id or @username&gt;\nExample: /deladmin 123456789"
        )
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid user id or username.</b>")
    remove_admin_id(user_id)
    await _reply(message, f"👑 <b>Admin removed:</b> {user_id}")


async def listadmins_cmd(_, message):
    if not (OWNER_ID and message.from_user and message.from_user.id == OWNER_ID):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    admins = sorted(get_admin_ids())
    if not admins:
        return await _reply(message, "ℹ️ <b>No admins set.</b>")
    await _reply(message, "Admins:\n" + "\n".join(str(x) for x in admins))


async def setpremium_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await _reply(message, 
            "Usage: /setpremium &lt;user_id or @username&gt; &lt;validity&gt;\n"
            "Validity: 1w, 1m, 1y\nExample: /setpremium 123456789 1m"
        )
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid user id or username.</b>")
    seconds = _parse_validity(parts[2])
    if not seconds:
        return await _reply(message, "❌ <b>Invalid validity.</b> Use 1w, 1m, or 1y.")
    expire_ts = int(time.time()) + seconds
    set_premium(user_id, True, expire_ts)
    await _reply(message, f"⭐ <b>Premium enabled:</b> {user_id}")


async def delpremium_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /delpremium &lt;user_id or @username&gt;\nExample: /delpremium 123456789"
        )
    try:
        user_id = await _resolve_user_id(client, parts[1])
    except Exception:
        return await _reply(message, "❌ <b>Invalid user id or username.</b>")
    set_premium(user_id, False)
    await _reply(message, f"⭐ <b>Premium disabled:</b> {user_id}")


async def listpremium_cmd(_, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    users = list_premium_users()
    if not users:
        return await _reply(message, "ℹ️ <b>No premium users.</b>")
    lines = ["Premium users:"]
    for user_id in users:
        exp = get_premium_expire_ts(user_id)
        if exp:
            lines.append(f"{user_id} (expires {datetime.datetime.fromtimestamp(exp)})")
        else:
            lines.append(f"{user_id}")
    await _reply(message, "\n".join(lines))


async def bsetting_cmd(_, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) == 1 or parts[1].lower() == "show":
        return await bsettings_command(message._client, message)
    allowed = {
        "VERIFY_EXPIRE",
        "TOKEN_TTL",
        "MIN_TOKEN_AGE",
        "VERIFY_PHOTO",
        "VERIFY_TUTORIAL",
        "SHORTLINK_SITE",
        "SHORTLINK_API",
        "SUPPORT_ID",
    }
    if len(parts) == 1 or parts[1].lower() == "show":
        values = [
            f"VERIFY_EXPIRE: {_get_verif_int('VERIFY_EXPIRE', VERIFY_EXPIRE)}",
            f"TOKEN_TTL: {_get_verif_int('TOKEN_TTL', TOKEN_TTL)}",
            f"MIN_TOKEN_AGE: {_get_verif_int('MIN_TOKEN_AGE', MIN_TOKEN_AGE)}",
            f"VERIFY_PHOTO: {_get_verif_str('VERIFY_PHOTO', VERIFY_PHOTO) or 'none'}",
            f"VERIFY_TUTORIAL: {_get_verif_str('VERIFY_TUTORIAL', VERIFY_TUTORIAL) or 'none'}",
            f"SHORTLINK_SITE: {_get_verif_str('SHORTLINK_SITE', SHORTLINK_SITE) or 'none'}",
            f"SHORTLINK_API: {_get_verif_str('SHORTLINK_API', SHORTLINK_API) or 'none'}",
            f"SUPPORT_ID: {_get_verif_str('SUPPORT_ID', '') or 'none'}",
        ]
        return await _reply(message, "🧩 <b>Verification settings:</b>\n" + "\n".join(values))

    action = parts[1].lower()
    if action in {"set", "unset"}:
        if len(parts) < 3:
            return await _reply(message, 
                "Usage: /bsetting set &lt;KEY&gt; &lt;VALUE&gt; or /bsetting unset &lt;KEY&gt;\n"
                "Example: /bsetting set TOKEN_TTL 600"
            )
        rest = parts[2].strip()
        if action == "set":
            key_value = rest.split(maxsplit=1)
            if len(key_value) < 2:
                return await _reply(message, 
                    "Usage: /bsetting set &lt;KEY&gt; &lt;VALUE&gt;\nExample: /bsetting set MIN_TOKEN_AGE 10"
                )
            key, value = key_value[0].upper(), key_value[1].strip()
            if key not in allowed:
                return await _reply(message, "Invalid key.")
            set_global_setting(key, value)
            return await _reply(message, f"✅ <b>{key} updated.</b>")
        key = rest.upper()
        if key not in allowed:
            return await _reply(message, "Invalid key.")
        set_global_setting(key, "")
        return await _reply(message, f"🧹 <b>{key} cleared.</b>")

    if len(parts) < 2:
        return await _reply(message, 
            "Usage: /bsetting &lt;KEY&gt; &lt;VALUE&gt; or /bsetting show\nExample: /bsetting TOKEN_TTL 600"
        )
    if len(parts) < 3:
        return await _reply(message, 
            "Usage: /bsetting &lt;KEY&gt; &lt;VALUE&gt;\nExample: /bsetting VERIFY_EXPIRE 86400"
        )
    key = parts[1].upper()
    value = parts[2].strip()
    if key not in allowed:
        return await _reply(message, "Invalid key.")
    set_global_setting(key, value)
    return await _reply(message, f"✅ <b>{key} updated.</b>")


async def pay_cmd(_, message):
    buttons = [
        [
            InlineKeyboardButton("🗓 1 Day", callback_data="pay:plan:1d"),
            InlineKeyboardButton("📅 1 Week", callback_data="pay:plan:1w"),
        ],
        [InlineKeyboardButton("📆 1 Month", callback_data="pay:plan:1m")],
        [InlineKeyboardButton("❌ Cancel", callback_data="pay:cancel")],
    ]
    await _reply(
        message,
        "⭐ <b>Choose Your Premium Plan</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def pay_callback(client, cq):
    user_id = cq.from_user.id
    data = cq.data or ""
    if not data.startswith("pay:"):
        return

    if data == "pay:cancel":
        PAYMENT_PENDING.pop(user_id, None)
        await cq.message.edit_text("❌ <b>Payment cancelled.</b>", parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    if data.startswith("pay:plan:"):
        plan_key = data.split(":", 2)[2]
        if plan_key not in PAYMENT_PLANS:
            await cq.answer("Invalid plan", show_alert=True)
            return
        label, seconds, price = PAYMENT_PLANS[plan_key]
        PAYMENT_PENDING[user_id] = PaymentRequest(plan_key, label, price, seconds)

        caption = (
            "💳 <b>Payment Details</b>\n\n"
            f"Plan: <b>{label}</b>\n"
            f"Price: <b>{price}</b>\n\n"
            "Scan the QR code below to pay."
        )
        buttons = [
            [
                InlineKeyboardButton("📤 Send Screenshot", callback_data="pay:send_ss"),
                InlineKeyboardButton("🔢 Send UTR", callback_data="pay:send_utr"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="pay:cancel")],
        ]
        qr = _get_verif_str("PAYMENT_QR", "")
        upi = _get_verif_str("PAYMENT_UPI", "")
        if upi:
            pay_link = (
                f"upi://pay?pa={urllib.parse.quote(upi)}"
                f"&am={price}&cu=INR&tn={urllib.parse.quote(label)}"
            )
            qr_url = (
                "https://api.qrserver.com/v1/create-qr-code/?size=512x512&data="
                + urllib.parse.quote(pay_link)
            )
            await cq.message.reply_photo(
                qr_url,
                caption=caption + f"\n\nUPI: <code>{upi}</code>",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        elif qr:
            await cq.message.reply_photo(
                qr,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        else:
            await cq.message.reply_text(
                "⚠️ <b>Payment settings not configured.</b> Please contact admin.",
                reply_markup=_payment_button(),
                parse_mode=ParseMode.HTML,
            )
        await cq.answer()
        return

    if data == "pay:send_ss":
        await cq.message.reply_text("📤 <b>Send payment screenshot</b>", parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    if data == "pay:send_utr":
        await cq.message.reply_text("🔢 <b>Send UTR / Transaction ID</b>", parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    await cq.answer()


async def pay_input_handler(client, message):
    user_id = message.from_user.id if message.from_user else 0
    if not user_id or user_id not in PAYMENT_PENDING:
        return
    pending = PAYMENT_PENDING[user_id]

    if message.photo:
        pending.screenshot_id = message.photo.file_id
        await _reply(message, "✅ <b>Screenshot received</b>")
    elif message.text:
        pending.utr = (message.text or "").strip()
        await _reply(message, "✅ <b>UTR received</b>")
    else:
        return

    if pending.screenshot_id and pending.utr:
        await _reply(
            message,
            "⏳ <b>Your payment is under verification.</b>\nPlease wait for admin approval.",
        )
        await _notify_payment_request(client, message, pending)
    raise StopPropagation


async def _notify_payment_request(client, message, pending: PaymentRequest) -> None:
    user = message.from_user
    uname = f"@{user.username}" if user and user.username else "unknown"
    text = (
        "💰 <b>New Premium Payment Request</b>\n\n"
        f"User: {uname} ({user.id if user else 0})\n"
        f"Plan: {pending.label}\n"
        f"UTR: {pending.utr or 'N/A'}"
    )

    channel = _get_verif_str("PAYMENT_CHANNEL", "")
    targets = []
    if channel:
        try:
            channel_id = await _resolve_channel_id(client, channel)
            targets = [channel_id]
        except Exception:
            targets = []
    if not targets:
        targets = list(set(get_admin_ids()) | set(SUDO_USER_IDS) | ({OWNER_ID} if OWNER_ID else set()))

    for target in targets:
        try:
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Approve",
                            callback_data=f"payadmin:approve:{user.id if user else 0}",
                        ),
                        InlineKeyboardButton(
                            "❌ Decline",
                            callback_data=f"payadmin:reject:{user.id if user else 0}",
                        ),
                    ]
                ]
            )
            if pending.screenshot_id:
                await client.send_photo(
                    int(target),
                    pending.screenshot_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons,
                )
            else:
                await client.send_message(
                    int(target),
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons,
                )
        except Exception:
            continue


async def pay_admin_callback(client, cq):
    if not _is_admin(cq):
        await cq.answer("Unauthorized", show_alert=True)
        return
    data = cq.data or ""
    if not data.startswith("payadmin:"):
        return
    parts = data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await cq.answer("Invalid request", show_alert=True)
        return
    action = parts[1]
    user_id = int(parts[2])
    pending = PAYMENT_PENDING.pop(user_id, None)
    if not pending:
        await cq.answer("No pending payment found.", show_alert=True)
        return

    if action == "approve":
        expire_ts = int(time.time()) + pending.seconds
        set_premium(user_id, True, expire_ts)
        await cq.message.edit_text(
            f"✅ <b>Payment approved.</b>\nUser: {user_id}\nPlan: {pending.label}",
            parse_mode=ParseMode.HTML,
        )
        try:
            await client.send_message(
                user_id,
                "🎉 <b>Premium Activated!</b>\n\n"
                f"Plan: <b>{pending.label}</b>\n"
                f"Valid Till: <b>{datetime.datetime.fromtimestamp(expire_ts)}</b>\n\n"
                "Enjoy unlimited leech & priority access 🚀",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await cq.answer("Approved")
        return

    if action == "reject":
        await cq.message.edit_text(
            f"❌ <b>Payment rejected.</b>\nUser: {user_id}\nPlan: {pending.label}",
            parse_mode=ParseMode.HTML,
        )
        try:
            await client.send_message(
                user_id,
                "❌ <b>Payment verification failed.</b>\n"
                "Please contact admin for support.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await cq.answer("Rejected")
        return

    await cq.answer()


async def payapprove_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, "Usage: /payapprove <user_id>")
    if not parts[1].isdigit():
        return await _reply(message, "❌ <b>Invalid user id.</b>")
    user_id = int(parts[1])
    pending = PAYMENT_PENDING.pop(user_id, None)
    if not pending:
        return await _reply(message, "⚠️ <b>No pending payment found.</b>")
    expire_ts = int(time.time()) + pending.seconds
    set_premium(user_id, True, expire_ts)
    await _reply(message, f"✅ <b>Payment approved.</b> Premium enabled: {user_id}")
    try:
        await client.send_message(
            user_id,
            "🎉 <b>Premium Activated!</b>\n\n"
            f"Plan: <b>{pending.label}</b>\n"
            f"Valid Till: <b>{datetime.datetime.fromtimestamp(expire_ts)}</b>\n\n"
            "Enjoy unlimited leech & priority access 🚀",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def payreject_cmd(client, message):
    if not _is_admin(message):
        return await _reply(message, "⛔ <b>Unauthorized</b>")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await _reply(message, "Usage: /payreject <user_id>")
    if not parts[1].isdigit():
        return await _reply(message, "❌ <b>Invalid user id.</b>")
    user_id = int(parts[1])
    pending = PAYMENT_PENDING.pop(user_id, None)
    if not pending:
        return await _reply(message, "⚠️ <b>No pending payment found.</b>")
    await _reply(message, f"❌ <b>Payment rejected.</b> User: {user_id}")
    try:
        await client.send_message(
            user_id,
            "❌ <b>Payment verification failed.</b>\n"
            "Please contact admin for support.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def main():
    app = Client(
        "mega_leech_bot",
        api_id=TELEGRAM_API,
        api_hash=TELEGRAM_HASH,
        bot_token=BOT_TOKEN,
    )

    app.add_handler(
        MessageHandler(
            verification_gate,
            filters.command(
                [
                    "start",
                    "help",
                    "ping",
                    "leech",
                    "cancel",
                    "settings",
                    "setlogchannel",
                    "settaskchannel",
                    "addadmin",
                    "deladmin",
                    "listadmins",
                    "setpremium",
                    "delpremium",
                    "listpremium",
                    "pay",
                    "payapprove",
                    "payreject",
                    "bsetting",
                ]
            ),
        ),
        group=0,
    )
    app.add_handler(MessageHandler(start_cmd, filters.command("start")), group=1)
    app.add_handler(MessageHandler(help_cmd, filters.command("help")), group=1)
    app.add_handler(MessageHandler(ping_cmd, filters.command("ping")), group=1)
    app.add_handler(MessageHandler(leech_cmd, filters.command("leech")), group=1)
    app.add_handler(MessageHandler(cancel_cmd, filters.command("cancel")), group=1)
    app.add_handler(MessageHandler(settings_cmd, filters.command("settings")), group=1)
    app.add_handler(MessageHandler(setlogchannel_cmd, filters.command("setlogchannel")), group=1)
    app.add_handler(MessageHandler(settaskchannel_cmd, filters.command("settaskchannel")), group=1)
    app.add_handler(MessageHandler(addadmin_cmd, filters.command("addadmin")), group=1)
    app.add_handler(MessageHandler(deladmin_cmd, filters.command("deladmin")), group=1)
    app.add_handler(MessageHandler(listadmins_cmd, filters.command("listadmins")), group=1)
    app.add_handler(MessageHandler(setpremium_cmd, filters.command("setpremium")), group=1)
    app.add_handler(MessageHandler(delpremium_cmd, filters.command("delpremium")), group=1)
    app.add_handler(MessageHandler(listpremium_cmd, filters.command("listpremium")), group=1)
    app.add_handler(MessageHandler(pay_cmd, filters.command("pay")), group=1)
    app.add_handler(MessageHandler(payapprove_cmd, filters.command("payapprove")), group=1)
    app.add_handler(MessageHandler(payreject_cmd, filters.command("payreject")), group=1)
    app.add_handler(CallbackQueryHandler(pay_callback, filters.regex("^pay:")), group=1)
    app.add_handler(
        CallbackQueryHandler(pay_admin_callback, filters.regex("^payadmin:")),
        group=1,
    )
    app.add_handler(
        MessageHandler(
            pay_input_handler,
            filters.private
            & ~filters.command(
                [
                    "start",
                    "help",
                    "ping",
                    "leech",
                    "cancel",
                    "settings",
                    "setlogchannel",
                    "settaskchannel",
                    "addadmin",
                    "deladmin",
                    "listadmins",
                    "setpremium",
                    "delpremium",
                    "listpremium",
                    "bsetting",
                    "pay",
                    "payapprove",
                    "payreject",
                ]
            ),
        ),
        group=1,
    )
    app.add_handler(MessageHandler(bsetting_cmd, filters.command("bsetting")), group=1)
    register_settings_handlers(app)

    LOGGER.info("Mega leech bot started")
    app.run()


if __name__ == "__main__":
    main()
