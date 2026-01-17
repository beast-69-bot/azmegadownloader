from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import time
from urllib.parse import quote_plus

from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram import filters

from .. import LOGGER
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.message_utils import send_message, edit_message
from ..helper.telegram_helper.button_build import ButtonMaker
from ..custom.settings_db import (
    set_premium,
    list_premium_users,
    get_premium_expire_ts,
    create_premium_tokens,
    get_premium_token,
    mark_premium_token_redeemed,
)


PAY_PLANS = {
    "1d": {"label": "1 Day", "days": 1, "amount": 5},
    "1w": {"label": "1 Week", "days": 7, "amount": 30},
    "1m": {"label": "1 Month", "days": 30, "amount": 50},
}


@dataclass
class PaymentRequest:
    user_id: int
    username: str
    plan_key: str
    label: str
    amount: int
    screenshot: str | None = None
    utr: str | None = None
    created_at: int = 0


PENDING_PAYMENTS: dict[int, PaymentRequest] = {}


def _support_button():
    if not Config.SUPPORT_ID:
        return None
    sid = Config.SUPPORT_ID
    if not sid.startswith("@"):
        sid = f"@{sid}"
    buttons = ButtonMaker()
    buttons.url_button("Contact Admin", f"https://t.me/{sid.lstrip('@')}")
    return buttons.build_menu(1)


def _parse_chat_id(value: str):
    if not value:
        return None
    value = str(value).strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _plan_keyboard():
    buttons = ButtonMaker()
    buttons.data_button("🗓 1 Day", "pay:plan:1d")
    buttons.data_button("📅 1 Week", "pay:plan:1w")
    buttons.data_button("📆 1 Month", "pay:plan:1m")
    buttons.data_button("❌ Cancel", "pay:cancel")
    return buttons.build_menu(2)


def _payment_keyboard():
    buttons = ButtonMaker()
    buttons.data_button("📤 Send Screenshot", "pay:send_ss")
    buttons.data_button("🔢 Send UTR", "pay:send_utr")
    buttons.data_button("❌ Cancel", "pay:cancel")
    return buttons.build_menu(2)


def _qr_url(upi: str, amount: int) -> str:
    payload = f"upi://pay?pa={upi}&pn=Premium&am={amount}&cu=INR"
    return f"https://api.qrserver.com/v1/create-qr-code/?size=600x600&data={quote_plus(payload)}"


async def pay(_, message):
    msg = "⭐ <b>Choose Your Premium Plan</b>"
    await send_message(message, msg, _plan_keyboard())


async def pay_callback(_, query):
    data = query.data or ""
    if data == "pay:cancel":
        return await edit_message(query.message, "❌ <b>Payment cancelled.</b>")
    if data.startswith("pay:plan:"):
        plan_key = data.split(":")[-1]
        plan = PAY_PLANS.get(plan_key)
        if not plan:
            return await query.answer("Invalid plan", show_alert=True)
        user = query.from_user
        PENDING_PAYMENTS[user.id] = PaymentRequest(
            user_id=user.id,
            username=f"@{user.username}" if user.username else "unknown",
            plan_key=plan_key,
            label=plan["label"],
            amount=plan["amount"],
            created_at=int(time()),
        )
        msg = (
            "<b>Payment Details</b>\n\n"
            f"Plan: <b>{plan['label']}</b>\n"
            f"Price: <b>{plan['amount']} INR</b>\n\n"
            "Scan the QR code below to pay."
        )
        photo = None
        if Config.PAYMENT_QR:
            photo = Config.PAYMENT_QR
        elif Config.PAYMENT_UPI:
            photo = _qr_url(Config.PAYMENT_UPI, plan["amount"])
        if photo:
            await send_message(query.message, msg, _payment_keyboard(), photo=photo)
        else:
            await send_message(query.message, msg, _payment_keyboard())
        return
    if data == "pay:send_ss":
        return await send_message(query.message, "📤 <b>Send payment screenshot</b>")
    if data == "pay:send_utr":
        return await send_message(query.message, "🔢 <b>Send UTR / Transaction ID</b>")


async def pay_input(_, message):
    if not message.from_user:
        return
    pending = PENDING_PAYMENTS.get(message.from_user.id)
    if not pending:
        return
    if message.photo:
        pending.screenshot = message.photo.file_id
        await send_message(message, "✅ <b>Screenshot received</b>")
    elif message.text:
        pending.utr = message.text.strip()
        await send_message(message, "✅ <b>UTR received</b>")

    if pending.screenshot and pending.utr:
        await send_message(
            message,
            "⏳ <b>Your payment is under verification.</b>\nPlease wait for admin approval.",
        )
        await _notify_admin_payment(pending)
        PENDING_PAYMENTS.pop(message.from_user.id, None)


async def _notify_admin_payment(pending: PaymentRequest):
    buttons = ButtonMaker()
    buttons.data_button("✅ Approve", f"payadmin:approve:{pending.user_id}")
    buttons.data_button("❌ Decline", f"payadmin:reject:{pending.user_id}")
    kb = buttons.build_menu(2)

    text = (
        "💰 <b>New Premium Payment Request</b>\n\n"
        f"User: {pending.username} ({pending.user_id})\n"
        f"Plan: {pending.label}\n"
        f"UTR: {pending.utr}"
    )

    if Config.PAYMENT_CHANNEL:
        try:
            chat_id = _parse_chat_id(Config.PAYMENT_CHANNEL)
            await TgClient.bot.send_photo(
                chat_id=chat_id,
                photo=pending.screenshot,
                caption=text,
                reply_markup=kb,
            )
            return
        except Exception as e:
            LOGGER.error(f"Payment notify failed for {Config.PAYMENT_CHANNEL}: {e}")

    for uid in TgClient.bot.me.id, *():  # no-op, fallback below
        pass
    for sudo_id in TgClient.bot.me.id if False else []:
        pass
    # fallback: send to sudo users
    for sudo_id in (Config.OWNER_ID, *Config.SUDO_USERS.split()):
        try:
            if not sudo_id:
                continue
            await TgClient.bot.send_photo(
                chat_id=int(str(sudo_id).strip()),
                photo=pending.screenshot,
                caption=text,
                reply_markup=kb,
            )
        except Exception:
            continue


def _add_premium_days(user_id: int, days: int):
    now = int(time())
    current = get_premium_expire_ts(user_id)
    base = max(now, current)
    expire = base + (days * 86400)
    set_premium(user_id, True, expire)
    return expire


async def pay_admin_callback(_, query):
    if not await CustomFilters.sudo(_, query):
        return await query.answer("Unauthorized", show_alert=True)
    data = query.data.split(":")
    if len(data) != 3:
        return
    action, user_id = data[1], int(data[2])
    pending = PENDING_PAYMENTS.get(user_id)
    if not pending:
        return await query.answer("No pending payment", show_alert=True)

    if action == "approve":
        expire = _add_premium_days(user_id, PAY_PLANS[pending.plan_key]["days"])
        valid_till = datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M")
        await edit_message(
            query.message,
            f"✅ <b>Payment approved.</b>\nUser: {user_id}\nPlan: {pending.label}",
        )
        await TgClient.bot.send_message(
            user_id,
            "🎉 <b>Premium Activated!</b>\n\n"
            f"Plan: {pending.label}\n"
            f"Valid Till: {valid_till}\n\n"
            "Enjoy unlimited leech & priority 🚀",
        )
    else:
        await edit_message(
            query.message,
            f"❌ <b>Payment rejected.</b>\nUser: {user_id}\nPlan: {pending.label}",
        )
        await TgClient.bot.send_message(
            user_id,
            "❌ <b>Payment verification failed.</b>\nPlease contact admin for support.",
            reply_markup=_support_button(),
        )
    PENDING_PAYMENTS.pop(user_id, None)


async def setpremium(_, message):
    if not await CustomFilters.sudo(_, message):
        return await send_message(message, "⛔ Unauthorized")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await send_message(message, "Usage: /setpremium <user_id> <validity>")
    user_id = int(parts[1])
    validity = parts[2].lower()
    days = 0
    if validity.endswith("d"):
        days = int(validity[:-1])
    elif validity.endswith("w"):
        days = int(validity[:-1]) * 7
    elif validity.endswith("m"):
        days = int(validity[:-1]) * 30
    elif validity.endswith("y"):
        days = int(validity[:-1]) * 365
    else:
        return await send_message(message, "Invalid validity. Use 1d/1w/1m/1y.")
    expire = _add_premium_days(user_id, days)
    valid_till = datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M")
    await send_message(message, f"✅ Premium enabled: {user_id}\nValid till: {valid_till}")


async def delpremium(_, message):
    if not await CustomFilters.sudo(_, message):
        return await send_message(message, "⛔ Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await send_message(message, "Usage: /delpremium <user_id>")
    user_id = int(parts[1])
    set_premium(user_id, False, 0)
    await send_message(message, f"✅ Premium disabled: {user_id}")


async def listpremium(_, message):
    if not await CustomFilters.sudo(_, message):
        return await send_message(message, "⛔ Unauthorized")
    users = list_premium_users()
    if not users:
        return await send_message(message, "No premium users.")
    lines = []
    for uid in users:
        exp = get_premium_expire_ts(uid)
        if exp:
            lines.append(f"{uid} (expires {datetime.fromtimestamp(exp)})")
        else:
            lines.append(str(uid))
    await send_message(message, "\n".join(lines))


async def generate(_, message):
    if not await CustomFilters.sudo(_, message):
        return await send_message(message, "⛔ Unauthorized")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        return await send_message(message, "Usage: /generate <qty>")
    qty = int(parts[1])
    tokens = create_premium_tokens(qty, message.from_user.id)
    body = "\n".join(tokens)
    text = (
        "✅ Tokens Generated Successfully\n\n"
        "⏳ Validity: 1 hour\n"
        "🔐 Single-use only\n\n"
        "🔑 Generated Tokens:\n"
        "```\n"
        f"{body}\n"
        "```\n"
        "Share one token with one user only."
    )
    await message.reply_text(text, parse_mode="markdown")


async def redeem(_, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await send_message(message, "Usage: /redeem <token>")
    token = parts[1].strip().upper()
    token_info = get_premium_token(token)
    if not token_info:
        return await send_message(message, "❌ Invalid token.")
    now = int(time())
    if token_info.get("expires_at") and now > int(token_info["expires_at"]):
        return await send_message(
            message, "⏳ This token has expired. Please contact admin."
        )
    if token_info.get("redeemed_by"):
        return await send_message(
            message, "❌ This token has already been redeemed. Try a new one."
        )
    mark_premium_token_redeemed(token, message.from_user.id, now)
    expire = _add_premium_days(message.from_user.id, 1)
    valid_till = datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M")
    text = (
        "🎉 Premium Activated!\n\n"
        "Plan      : 1 Day Premium\n"
        f"Token     : `{token}`\n"
        f"Valid Till: {valid_till}\n\n"
        "Enjoy unlimited leech & priority 🚀"
    )
    await message.reply_text(text, parse_mode="markdown")


def get_pay_handlers():
    return [
        MessageHandler(
            pay, filters=filters.command(BotCommands.PayCommand) & CustomFilters.authorized
        ),
        MessageHandler(
            pay_input,
            filters=filters.private
            & ~filters.command(_flatten_commands())
            & CustomFilters.authorized,
        ),
        CallbackQueryHandler(pay_callback, filters=filters.regex("^pay:")),
        CallbackQueryHandler(
            pay_admin_callback, filters=filters.regex("^payadmin:") & CustomFilters.sudo
        ),
        MessageHandler(
            setpremium,
            filters=filters.command(BotCommands.SetPremiumCommand) & CustomFilters.sudo,
        ),
        MessageHandler(
            delpremium,
            filters=filters.command(BotCommands.DelPremiumCommand) & CustomFilters.sudo,
        ),
        MessageHandler(
            listpremium,
            filters=filters.command(BotCommands.ListPremiumCommand) & CustomFilters.sudo,
        ),
        MessageHandler(
            generate,
            filters=filters.command(BotCommands.GenerateCommand) & CustomFilters.sudo,
        ),
        MessageHandler(
            redeem,
            filters=filters.command(BotCommands.RedeemCommand) & CustomFilters.authorized,
        ),
    ]


def _flatten_commands():
    cmds = []
    for items in BotCommands.get_commands().values():
        if isinstance(items, list):
            cmds.extend(items)
        else:
            cmds.append(items)
    return [c for c in cmds if c]
