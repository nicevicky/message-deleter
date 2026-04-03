import os
import logging
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Response
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    MessageEntity, ChatPermissions
)
from telegram.constants import ChatType, ChatMemberStatus
from telegram.error import BadRequest, RetryAfter, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ChatMemberHandler, ChatJoinRequestHandler,
)
from supabase import create_client, Client
from dotenv import load_dotenv
import asyncio
import requests as http_requests
import json

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPABASE_URL       = os.getenv("SUPABASE_URL")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL        = os.getenv("WEBHOOK_URL")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")

supabase: Client   = create_client(SUPABASE_URL, SUPABASE_KEY)
app                = FastAPI()
ptb_application    = None

# ════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ════════════════════════════════════════════════════════════

def is_forwarded_or_channel_message(message) -> bool:
    if message.forward_origin is not None:
        return True
    if message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        return True
    if message.entities:
        e = message.entities[0]
        if e.offset == 0 and e.type == "text_link" and "t.me" in (e.url or ""):
            return True
    return False


def is_deleted_account(user) -> bool:
    if user is None:
        return False
    return (
        getattr(user, "first_name", "") == "Deleted"
        and getattr(user, "username", None) is None
        and not getattr(user, "is_bot", False)
    )


def get_user_mention_html(user) -> str:
    """
    Has username  -> clickable @username
    No username   -> clickable first_name via tg://user?id=
    """
    name = (user.first_name or "User").strip()
    if getattr(user, "username", None):
        return f'<a href="https://t.me/{user.username}">@{user.username}</a>'
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def parse_duration(s: str) -> int:
    s = s.lower().strip()
    try:
        if s.endswith("m"):   return int(s[:-1]) * 60
        elif s.endswith("h"): return int(s[:-1]) * 3600
        elif s.endswith("d"): return int(s[:-1]) * 86400
        elif s.endswith("w"): return int(s[:-1]) * 604800
        else:                 return int(s) * 60
    except ValueError:
        return 0


# ════════════════════════════════════════════════════════════
# DATABASE — GROUPS
# ════════════════════════════════════════════════════════════

async def get_group_settings(chat_id: int):
    try:
        r = supabase.table("groups").select("*").eq("chat_id", chat_id).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_group_settings: {e}")
        return None


async def add_group_to_db(chat_id, chat_title, added_by, username, bot_is_admin):
    try:
        existing = await get_group_settings(chat_id)
        defaults = {
            "delete_promotions": False, "delete_links": False,
            "warning_timer": 30, "max_word_count": 0,
            "welcome_message": None, "welcome_timer": 0,
            "delete_join_messages": False, "max_warnings": 3,
            "require_approval": False, "auto_approve": False,
            "sticker_protect": False, "force_sub_message_timer": 60,
        }
        if existing:
            for k in defaults:
                defaults[k] = existing.get(k, defaults[k])
        defaults.update({
            "chat_id": chat_id, "chat_title": chat_title,
            "added_by": added_by, "added_by_username": username,
            "bot_is_admin": bot_is_admin,
        })
        supabase.table("groups").upsert(defaults, on_conflict="chat_id").execute()
    except Exception as e:
        logger.error(f"add_group_to_db: {e}")


async def delete_group_and_words(chat_id: int):
    try:
        for t in ("banned_words", "group_members", "notes", "force_sub",
                  "warnings", "bans", "mutes", "reports",
                  "pending_deletions", "join_requests"):
            supabase.table(t).delete().eq("chat_id", chat_id).execute()
        supabase.table("groups").delete().eq("chat_id", chat_id).execute()
    except Exception as e:
        logger.error(f"delete_group_and_words: {e}")


async def get_user_groups(user_id: int):
    try:
        r = supabase.table("groups").select("*").eq("added_by", user_id).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"get_user_groups: {e}")
        return []


async def update_setting(chat_id: int, **kwargs):
    try:
        supabase.table("groups").update(kwargs).eq("chat_id", chat_id).execute()
    except Exception as e:
        logger.error(f"update_setting: {e}")


# ════════════════════════════════════════════════════════════
# DATABASE — MEMBER ACTIVITY
# ════════════════════════════════════════════════════════════

async def track_member_activity(chat_id: int, user_id: int,
                                 username: str = None, first_name: str = None):
    try:
        r = supabase.table("group_members").select("message_count") \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
        count = (r.data[0]["message_count"] + 1) if r.data else 1
        supabase.table("group_members").upsert({
            "chat_id": chat_id, "user_id": user_id,
            "username": username, "first_name": first_name,
            "last_active": datetime.now(timezone.utc).isoformat(),
            "message_count": count,
        }, on_conflict="chat_id,user_id").execute()
    except Exception as e:
        logger.error(f"track_member_activity: {e}")


async def get_group_members(chat_id: int):
    try:
        r = supabase.table("group_members").select("*").eq("chat_id", chat_id).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"get_group_members: {e}")
        return []


async def remove_member(chat_id: int, user_id: int):
    try:
        supabase.table("group_members").delete() \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"remove_member: {e}")


# ════════════════════════════════════════════════════════════
# DATABASE — WARNINGS / BANS / MUTES
# ════════════════════════════════════════════════════════════

async def add_warning(chat_id, user_id, warned_by, reason, username=None):
    try:
        supabase.table("warnings").insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "warned_by": warned_by, "reason": reason,
            "warned_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"add_warning: {e}")


async def get_user_warnings(chat_id, user_id):
    try:
        r = supabase.table("warnings").select("*") \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
        return r.data or []
    except Exception:
        return []


async def clear_user_warnings(chat_id: int, user_id: int):
    try:
        supabase.table("warnings").delete() \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"clear_user_warnings: {e}")


async def add_ban(chat_id, user_id, banned_by, reason, username=None):
    try:
        supabase.table("bans").insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "banned_by": banned_by, "reason": reason,
            "banned_at": datetime.now(timezone.utc).isoformat(), "is_active": True,
        }).execute()
    except Exception as e:
        logger.error(f"add_ban: {e}")


async def unban_user_in_db(chat_id, user_id):
    try:
        supabase.table("bans").update({"is_active": False}) \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"unban_user_in_db: {e}")


async def add_mute(chat_id, user_id, muted_by, reason, duration_minutes, username=None):
    try:
        mute_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        supabase.table("mutes").insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "muted_by": muted_by, "reason": reason,
            "muted_at": datetime.now(timezone.utc).isoformat(),
            "mute_until": mute_until.isoformat(), "is_active": True,
        }).execute()
    except Exception as e:
        logger.error(f"add_mute: {e}")


async def unmute_user_in_db(chat_id, user_id):
    try:
        supabase.table("mutes").update({"is_active": False}) \
            .eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"unmute_user_in_db: {e}")


async def cleanup_expired_mutes(bot):
    """Called from cron — receives bot object directly, not a context."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        r   = supabase.table("mutes").select("*").eq("is_active", True).lte("mute_until", now).execute()
        count = 0
        for m in (r.data or []):
            try:
                perms = ChatPermissions(
                    can_send_messages=True, can_send_photos=True,
                    can_send_videos=True, can_send_documents=True,
                    can_send_audios=True, can_send_voice_notes=True,
                    can_send_video_notes=True, can_send_polls=True,
                )
                await bot.restrict_chat_member(m["chat_id"], m["user_id"], perms, until_date=0)
                await unmute_user_in_db(m["chat_id"], m["user_id"])
                count += 1
            except Exception as e:
                logger.error(f"auto-unmute {m['user_id']}: {e}")
        return count
    except Exception as e:
        logger.error(f"cleanup_expired_mutes: {e}")
        return 0


# ════════════════════════════════════════════════════════════
# DATABASE — REPORTS / BANNED WORDS / NOTES / FORCE SUB
# ════════════════════════════════════════════════════════════

async def add_report(chat_id, reporter_id, reported_user_id, reason,
                     reporter_username=None, reported_username=None):
    try:
        supabase.table("reports").insert({
            "chat_id": chat_id, "reporter_id": reporter_id,
            "reporter_username": reporter_username,
            "reported_user_id": reported_user_id,
            "reported_username": reported_username, "reason": reason,
            "reported_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"add_report: {e}")


async def get_banned_words(chat_id):
    try:
        r = supabase.table("banned_words").select("word").eq("chat_id", chat_id).execute()
        return [i["word"] for i in (r.data or [])]
    except Exception:
        return []


async def add_banned_word(chat_id, word, added_by):
    try:
        supabase.table("banned_words").insert({
            "chat_id": chat_id, "word": word.lower(), "added_by": added_by,
        }).execute()
    except Exception as e:
        logger.error(f"add_banned_word: {e}")


async def remove_banned_word(chat_id, word):
    try:
        supabase.table("banned_words").delete() \
            .eq("chat_id", chat_id).eq("word", word.lower()).execute()
    except Exception as e:
        logger.error(f"remove_banned_word: {e}")


async def save_note(chat_id: int, name: str, content: str, added_by: int):
    try:
        supabase.table("notes").upsert({
            "chat_id": chat_id, "name": name.lower(), "content": content,
            "added_by": added_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="chat_id,name").execute()
    except Exception as e:
        logger.error(f"save_note: {e}")


async def get_note(chat_id: int, name: str):
    try:
        r = supabase.table("notes").select("*") \
            .eq("chat_id", chat_id).eq("name", name.lower()).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None


async def delete_note(chat_id: int, name: str):
    try:
        supabase.table("notes").delete() \
            .eq("chat_id", chat_id).eq("name", name.lower()).execute()
    except Exception as e:
        logger.error(f"delete_note: {e}")


async def get_all_notes(chat_id: int):
    try:
        r = supabase.table("notes").select("name").eq("chat_id", chat_id).execute()
        return [i["name"] for i in (r.data or [])]
    except Exception:
        return []


async def get_force_sub_channels(chat_id: int):
    try:
        r = supabase.table("force_sub").select("*") \
            .eq("chat_id", chat_id).eq("is_active", True).execute()
        return r.data or []
    except Exception:
        return []


async def add_force_sub_channel(chat_id, channel_id, channel_title, channel_username, added_by):
    try:
        supabase.table("force_sub").upsert({
            "chat_id": chat_id, "channel_id": channel_id,
            "channel_title": channel_title, "channel_username": channel_username,
            "added_by": added_by, "is_active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="chat_id,channel_id").execute()
    except Exception as e:
        logger.error(f"add_force_sub_channel: {e}")


async def remove_force_sub_channel(chat_id: int, channel_id):
    try:
        supabase.table("force_sub").update({"is_active": False}) \
            .eq("chat_id", chat_id).eq("channel_id", channel_id).execute()
    except Exception as e:
        logger.error(f"remove_force_sub_channel: {e}")


async def schedule_message_deletion(chat_id, message_id, delay_seconds):
    try:
        delete_time = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        supabase.table("pending_deletions").insert({
            "chat_id": chat_id, "message_id": message_id,
            "delete_at": delete_time.isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"schedule_message_deletion: {e}")


async def get_due_deletions():
    try:
        now = datetime.now(timezone.utc).isoformat()
        r   = supabase.table("pending_deletions").select("*").lte("delete_at", now).execute()
        return r.data or []
    except Exception:
        return []


async def remove_pending_deletion(row_id):
    try:
        supabase.table("pending_deletions").delete().eq("id", row_id).execute()
    except Exception as e:
        logger.error(f"remove_pending_deletion: {e}")


async def save_join_request(chat_id, user_id, username, first_name):
    try:
        supabase.table("join_requests").upsert({
            "chat_id": chat_id, "user_id": user_id,
            "username": username, "first_name": first_name,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }, on_conflict="chat_id,user_id").execute()
    except Exception as e:
        logger.error(f"save_join_request: {e}")


# ════════════════════════════════════════════════════════════
# ADMIN CHECK HELPERS
# ════════════════════════════════════════════════════════════

async def is_user_admin(chat_id, user_id, context) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False


async def is_sender_admin(chat_id, message, context) -> bool:
    try:
        if message.sender_chat and message.sender_chat.id == chat_id:
            return True
        if message.from_user:
            return await is_user_admin(chat_id, message.from_user.id, context)
        return False
    except Exception:
        return False


async def verify_callback_admin(chat_id, query, context):
    try:
        if query.from_user:
            ok = await is_user_admin(chat_id, query.from_user.id, context)
            return (True, query.from_user.id, None) if ok \
                else (False, query.from_user.id, "Admins only.")
        return False, None, "Admins only."
    except Exception:
        return False, None, "Admins only."


# ════════════════════════════════════════════════════════════
# GEMINI AI
# ════════════════════════════════════════════════════════════

async def generate_mute_reason_with_gemini(warning_count, recent_warnings, offense_type) -> str:
    try:
        summary = "\n".join(
            f"- {w['reason']}" for w in recent_warnings[-5:]
        ) if recent_warnings else "None"
        prompt = (
            f"Write a concise (max 150 chars) professional mute reason for Telegram moderation.\n"
            f"Warnings: {warning_count} | Offense: {offense_type}\n"
            f"History: {summary}\nPlain text only, no markdown."
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 80},
        }
        resp = http_requests.post(
            url, headers={"Content-Type": "application/json"},
            data=json.dumps(payload), timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
    return f"Repeated violations ({offense_type})"


# ════════════════════════════════════════════════════════════
# FORCE SUBSCRIBE
# ════════════════════════════════════════════════════════════

async def check_force_sub(chat_id: int, user_id: int, context) -> list:
    channels   = await get_force_sub_channels(chat_id)
    not_joined = []
    for fc in channels:
        try:
            m = await context.bot.get_chat_member(fc["channel_id"], user_id)
            if m.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                not_joined.append(fc)
        except Exception:
            not_joined.append(fc)
    return not_joined


async def send_force_sub_message(chat, user, not_joined: list, context, warning_timer=60):
    try:
        user_link = get_user_mention_html(user)
        ch_names  = ", ".join(f"<b>{fc['channel_title']}</b>" for fc in not_joined)
        text      = (
            f"{user_link}, you must join the required channel(s) before chatting here.\n\n"
            f"Please join: {ch_names}\n\nThen send your message again."
        )
        kb = []
        for fc in not_joined:
            link = (f"https://t.me/{fc['channel_username']}"
                    if fc.get("channel_username")
                    else f"https://t.me/c/{str(fc['channel_id']).replace('-100','')}")
            kb.append([InlineKeyboardButton(f"Join {fc['channel_title']}", url=link)])
        msg = await chat.send_message(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None,
        )
        if warning_timer > 0:
            await schedule_message_deletion(chat.id, msg.message_id, warning_timer)
    except Exception as e:
        logger.error(f"send_force_sub_message: {e}")


# ════════════════════════════════════════════════════════════
# AUTO-MUTE (shared helper)
# ════════════════════════════════════════════════════════════

async def auto_mute_user(chat, user_id, username, count, warnings, max_w, context):
    reason     = await generate_mute_reason_with_gemini(count, warnings, "Max warnings")
    until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    try:
        await context.bot.restrict_chat_member(
            chat.id, user_id,
            ChatPermissions(
                can_send_messages=False, can_send_photos=False,
                can_send_videos=False, can_send_documents=False,
                can_send_audios=False, can_send_voice_notes=False,
                can_send_video_notes=False, can_send_polls=False,
            ),
            until_date=until_date,
        )
        await add_mute(chat.id, user_id, 0, reason, 60, username)
        mention = f"@{username}" if username else f"User {user_id}"
        kb = [[InlineKeyboardButton("Unmute User",
                callback_data=f"unmute_user_{user_id}_{chat.id}")]]
        await chat.send_message(
            f"<b>AUTO-MUTED</b>\nUser: {mention}\nDuration: 1 hour\n"
            f"Reason: {reason}\nReached {max_w} warnings.",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"auto_mute_user: {e}")


async def send_warning_with_count(chat, user_id, username, reason, context,
                                   offense_type="general"):
    s          = await get_group_settings(chat.id) or {}
    max_w      = s.get("max_warnings", 3)
    warn_timer = s.get("warning_timer", 30)
    mention    = f"@{username}" if username else f"User {user_id}"

    if offense_type == "banned_word":
        m = await chat.send_message(f"{mention}, your message was removed (banned word).")
        if warn_timer > 0:
            await schedule_message_deletion(chat.id, m.message_id, warn_timer)
        return

    await add_warning(chat.id, user_id, 0, reason, username)
    warnings = await get_user_warnings(chat.id, user_id)
    count    = len(warnings)
    text     = (
        f"<b>WARNING #{count}/{max_w}</b>\n"
        f"User: {mention}\nReason: {reason}\nIssued by: Bot"
    )
    msg = await chat.send_message(text, parse_mode="HTML")
    if warn_timer > 0:
        await schedule_message_deletion(chat.id, msg.message_id, warn_timer)
    if count >= max_w:
        await auto_mute_user(chat, user_id, username, count, warnings, max_w, context)


# ════════════════════════════════════════════════════════════
# RESOLVE TARGET HELPER
# ════════════════════════════════════════════════════════════

async def resolve_target(message, context):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u, u.username
    if context.args:
        raw = context.args[0].replace("@", "")
        try:
            m = await context.bot.get_chat_member(message.chat.id, int(raw))
            return m.user, m.user.username
        except Exception:
            pass
    return None, None


# ════════════════════════════════════════════════════════════
# MODERATION COMMANDS
# ════════════════════════════════════════════════════════════

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args or len(context.args) < 2:
        await msg.reply_text("Usage: /warn <@user or ID> <reason>")
        return
    reason = " ".join(context.args[1:])
    target, _ = await resolve_target(msg, context)
    if not target:
        await msg.reply_text("User not found. Reply to their message or give user ID.")
        return
    warned_by = msg.from_user.id if msg.from_user else 0
    await add_warning(chat.id, target.id, warned_by, reason, target.username)
    warnings = await get_user_warnings(chat.id, target.id)
    count    = len(warnings)
    s        = await get_group_settings(chat.id) or {}
    max_w    = s.get("max_warnings", 3)
    mention  = get_user_mention_html(target)
    adm_men  = "Anonymous Admin" if not msg.from_user else msg.from_user.mention_html()
    kb       = [
        [InlineKeyboardButton("Ban User",  callback_data=f"ban_from_warn_{target.id}_{chat.id}")],
        [InlineKeyboardButton("Mute User", callback_data=f"mute_from_warn_{target.id}_{chat.id}")],
    ]
    await msg.reply_html(
        f"<b>WARNING #{count}/{max_w}</b>\nUser: {mention}\nAdmin: {adm_men}\nReason: {reason}",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    if count >= max_w:
        await auto_mute_user(chat, target.id, target.username, count, warnings, max_w, context)


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /ban <@user or ID> [reason]")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
    target, _ = await resolve_target(msg, context)
    if not target:
        await msg.reply_text("User not found.")
        return
    banned_by = msg.from_user.id if msg.from_user else 0
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
        await add_ban(chat.id, target.id, banned_by, reason, target.username)
        mention = get_user_mention_html(target)
        kb = [[InlineKeyboardButton("Unban User",
                callback_data=f"unban_user_{target.id}_{chat.id}")]]
        await msg.reply_html(
            f"<b>BANNED</b>\nUser: {mention}\nReason: {reason}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as e:
        await msg.reply_text(f"Failed to ban: {e}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /unban <ID>")
        return
    try:
        raw = context.args[0].replace("@", "")
        uid = int(raw)
        await context.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
        await unban_user_in_db(chat.id, uid)
        await msg.reply_text("User unbanned.")
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args or len(context.args) < 2:
        await msg.reply_text("Usage: /mute <@user or ID> <duration> [reason]\nEx: /mute @user 1h Spamming")
        return
    dur_str = context.args[1]
    reason  = " ".join(context.args[2:]) if len(context.args) > 2 else "No reason"
    secs    = parse_duration(dur_str)
    if secs == 0:
        await msg.reply_text("Invalid duration. Use: 10m, 1h, 1d, 1w")
        return
    target, _ = await resolve_target(msg, context)
    if not target:
        await msg.reply_text("User not found.")
        return
    muted_by   = msg.from_user.id if msg.from_user else 0
    until_date = int((datetime.now(timezone.utc) + timedelta(seconds=secs)).timestamp())
    try:
        await context.bot.restrict_chat_member(
            chat.id, target.id,
            ChatPermissions(can_send_messages=False, can_send_photos=False,
                            can_send_videos=False, can_send_documents=False),
            until_date=until_date,
        )
        await add_mute(chat.id, target.id, muted_by, reason, secs // 60, target.username)
        mention = get_user_mention_html(target)
        kb = [[InlineKeyboardButton("Unmute User",
                callback_data=f"unmute_user_{target.id}_{chat.id}")]]
        await msg.reply_html(
            f"<b>MUTED</b>\nUser: {mention}\nDuration: {dur_str}\nReason: {reason}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    target, _ = await resolve_target(msg, context)
    if not target:
        await msg.reply_text("Reply to user's message or provide their ID.")
        return
    perms = ChatPermissions(
        can_send_messages=True, can_send_photos=True, can_send_videos=True,
        can_send_documents=True, can_send_audios=True, can_send_voice_notes=True,
        can_send_video_notes=True, can_send_polls=True,
    )
    try:
        await context.bot.restrict_chat_member(chat.id, target.id, perms, until_date=0)
        await unmute_user_in_db(chat.id, target.id)
        await clear_user_warnings(chat.id, target.id)   # ← reset warnings on unmute
        mention = get_user_mention_html(target)
        await msg.reply_html(f"<b>Unmuted</b>\n{mention} unmuted. Warnings reset to 0.")
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not msg.from_user:
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Reply to the message you want to report.")
        return
    reported = msg.reply_to_message.from_user
    if await is_user_admin(chat.id, reported.id, context):
        await msg.reply_text("You cannot report admins.")
        return
    reason = " ".join(context.args) if context.args else "No reason"
    await add_report(chat.id, msg.from_user.id, reported.id, reason,
                     msg.from_user.username, reported.username)
    await msg.reply_text("Report submitted. Thank you.")


async def show_admin_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    kb = [
        [InlineKeyboardButton("Warn",    callback_data=f"cmd_warn_{chat.id}"),
         InlineKeyboardButton("Mute",    callback_data=f"cmd_mute_{chat.id}"),
         InlineKeyboardButton("Ban",     callback_data=f"cmd_ban_{chat.id}")],
        [InlineKeyboardButton("Unmute",  callback_data=f"cmd_unmute_{chat.id}"),
         InlineKeyboardButton("Unban",   callback_data=f"cmd_unban_{chat.id}")],
        [InlineKeyboardButton("Reports", callback_data=f"cmd_reports_{chat.id}"),
         InlineKeyboardButton("Tag All", callback_data=f"cmd_tagall_{chat.id}")],
    ]
    await msg.reply_html("<b>Admin Panel</b>", reply_markup=InlineKeyboardMarkup(kb))


# ════════════════════════════════════════════════════════════
# TAG ALL / NOTES / FORCE SUB / FILTER DELETED
# ════════════════════════════════════════════════════════════

async def tag_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    note_text = " ".join(context.args) if context.args else "Attention everyone!"
    members   = await get_group_members(chat.id)
    if not members:
        await msg.reply_text("No members tracked yet. Members are tracked as they send messages.")
        return
    mentions = []
    for m in members:
        uid   = m["user_id"]
        uname = m.get("username")
        fname = m.get("first_name") or "User"
        if uname:
            mentions.append(f'<a href="https://t.me/{uname}">@{uname}</a>')
        else:
            mentions.append(f'<a href="tg://user?id={uid}">{fname}</a>')
    chunks = [mentions[i:i+30] for i in range(0, len(mentions), 30)]
    for idx, chunk in enumerate(chunks):
        text = (note_text + "\n\n" if idx == 0 else "") + " ".join(chunk)
        try:
            await chat.send_message(text, parse_mode="HTML")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"tagall chunk {idx}: {e}")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args or len(context.args) < 2:
        await msg.reply_text("Usage: /note <name> <content>")
        return
    name     = context.args[0].lower()
    content  = " ".join(context.args[1:])
    added_by = msg.from_user.id if msg.from_user else 0
    await save_note(chat.id, name, content, added_by)
    await msg.reply_html(f"Note <b>{name}</b> saved.")


async def get_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not context.args:
        await msg.reply_text("Usage: /get <name>")
        return
    note = await get_note(chat.id, context.args[0].lower())
    if note:
        await msg.reply_html(f"<b>{context.args[0]}</b>\n\n{note['content']}")
    else:
        await msg.reply_text(f"No note named '{context.args[0]}'.")


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    chat  = msg.chat
    names = await get_all_notes(chat.id)
    if names:
        lines = "\n".join(f"- <code>{n}</code>" for n in names)
        await msg.reply_html(f"<b>Notes ({len(names)}):</b>\n{lines}\n\nUse /get &lt;name&gt;")
    else:
        await msg.reply_text("No notes yet. Use /note <name> <content>.")


async def delnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /delnote <name>")
        return
    await delete_note(chat.id, context.args[0].lower())
    await msg.reply_html(f"Note <b>{context.args[0]}</b> deleted.")


async def forcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args:
        channels = await get_force_sub_channels(chat.id)
        if not channels:
            await msg.reply_text("No force-subscribe channels.\nUsage: /forcesub @channel")
        else:
            lines = "\n".join(f"- {c['channel_title']} (@{c.get('channel_username') or c['channel_id']})"
                              for c in channels)
            await msg.reply_html(f"<b>Force Subscribe Channels:</b>\n{lines}")
        return
    try:
        co  = await context.bot.get_chat(context.args[0])
        bm  = await context.bot.get_chat_member(co.id, context.bot.id)
        if bm.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await msg.reply_text("I must be a member of that channel first.")
            return
        await add_force_sub_channel(
            chat.id, co.id, co.title or context.args[0],
            co.username, msg.from_user.id if msg.from_user else 0,
        )
        await msg.reply_html(
            f"Force subscribe enabled for <b>{co.title}</b>.\n"
            f"Members must join before chatting."
        )
    except Exception as e:
        logger.error(f"forcesub: {e}")
        await msg.reply_text("Could not access that channel. Make sure I'm a member.")


async def removeforcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /removeforcesub @channel")
        return
    try:
        co = await context.bot.get_chat(context.args[0])
        await remove_force_sub_channel(chat.id, co.id)
        await msg.reply_html(f"Force subscribe removed for <b>{co.title}</b>.")
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")


async def filter_deleted_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("Admins only.")
        return
    members = await get_group_members(chat.id)
    removed = 0
    for m in members:
        try:
            cm   = await context.bot.get_chat_member(chat.id, m["user_id"])
            user = cm.user
            if is_deleted_account(user):
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await context.bot.unban_chat_member(chat.id, user.id)
                except Exception:
                    pass
                await remove_member(chat.id, user.id)
                removed += 1
        except (Forbidden, BadRequest):
            await remove_member(chat.id, m["user_id"])
            removed += 1
        except Exception:
            pass
    await msg.reply_html(f"Done. Removed <b>{removed}</b> deleted/ghost accounts.")


# ════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ════════════════════════════════════════════════════════════

async def unban_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = q.data.split("_")   # unban_user_<uid>_<cid>
    uid, cid = int(p[2]), int(p[3])
    ok, _, err = await verify_callback_admin(cid, q, context)
    if not ok: await q.answer(err, show_alert=True); return
    try:
        await context.bot.unban_chat_member(cid, uid, only_if_banned=True)
        await unban_user_in_db(cid, uid)
        await q.message.edit_reply_markup(reply_markup=None)
        await q.answer("Unbanned.", show_alert=True)
    except Exception as e:
        await q.answer(f"Failed: {e}", show_alert=True)


async def unmute_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = q.data.split("_")   # unmute_user_<uid>_<cid>
    uid, cid = int(p[2]), int(p[3])
    ok, _, err = await verify_callback_admin(cid, q, context)
    if not ok: await q.answer(err, show_alert=True); return
    perms = ChatPermissions(
        can_send_messages=True, can_send_photos=True, can_send_videos=True,
        can_send_documents=True, can_send_audios=True, can_send_voice_notes=True,
        can_send_video_notes=True, can_send_polls=True,
    )
    try:
        await context.bot.restrict_chat_member(cid, uid, perms, until_date=0)
        await unmute_user_in_db(cid, uid)
        await clear_user_warnings(cid, uid)    # ← reset warnings
        await q.message.edit_reply_markup(reply_markup=None)
        await q.answer("Unmuted. Warnings reset.", show_alert=True)
    except Exception as e:
        await q.answer(f"Failed: {e}", show_alert=True)


async def ban_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = q.data.split("_")   # ban_from_warn_<uid>_<cid>
    uid, cid = int(p[3]), int(p[4])
    ok, clicker, err = await verify_callback_admin(cid, q, context)
    if not ok: await q.answer(err, show_alert=True); return
    try:
        await context.bot.ban_chat_member(cid, uid)
        await add_ban(cid, uid, clicker or 0, "Banned from warn panel")
        await q.message.edit_reply_markup(reply_markup=None)
        await q.answer("Banned.", show_alert=True)
    except Exception as e:
        await q.answer(f"Failed: {e}", show_alert=True)


async def mute_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    p = q.data.split("_")   # mute_from_warn_<uid>_<cid>
    uid, cid = int(p[3]), int(p[4])
    ok, clicker, err = await verify_callback_admin(cid, q, context)
    if not ok: await q.answer(err, show_alert=True); return
    until = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    try:
        await context.bot.restrict_chat_member(
            cid, uid,
            ChatPermissions(can_send_messages=False, can_send_photos=False,
                            can_send_videos=False, can_send_documents=False),
            until_date=until,
        )
        await add_mute(cid, uid, clicker or 0, "Muted from warn panel", 60)
        await q.message.edit_reply_markup(reply_markup=None)
        await q.answer("Muted 1 hour.", show_alert=True)
    except Exception as e:
        await q.answer(f"Failed: {e}", show_alert=True)


# ════════════════════════════════════════════════════════════
# SETTINGS CALLBACKS
# ════════════════════════════════════════════════════════════

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    cid   = int(query.data.split("_")[2])
    s     = await get_group_settings(cid)
    if not s:
        await query.message.edit_text("Group not found!"); return
    bw  = await get_banned_words(cid)
    fcs = await get_force_sub_channels(cid)
    tv  = s.get("warning_timer", 30)
    td  = f"{tv//60}m" if tv >= 60 else f"{tv}s"
    text = (
        f"<b>Group Settings — {s['chat_title']}</b>\n\n"
        f"Delete Links: {'ON' if s.get('delete_links') else 'OFF'}\n"
        f"Delete Promotions: {'ON' if s.get('delete_promotions') else 'OFF'}\n"
        f"Delete Join Msgs: {'ON' if s.get('delete_join_messages') else 'OFF'}\n"
        f"Sticker Protect: {'ON' if s.get('sticker_protect') else 'OFF'}\n"
        f"Require Approval: {'ON' if s.get('require_approval') else 'OFF'}\n"
        f"Auto Approve: {'ON' if s.get('auto_approve') else 'OFF'}\n"
        f"Max Warnings: {s.get('max_warnings', 3)}\n"
        f"Max Words: {s.get('max_word_count') or 'Unlimited'}\n"
        f"Warning Timer: {td}\n"
        f"Force Sub: {', '.join(c['channel_title'] for c in fcs) or 'None'}\n"
        f"Banned Words: {', '.join(bw) if bw else 'None'}"
    )
    kb = [
        [InlineKeyboardButton("Set Welcome",         callback_data=f"set_welcome_{cid}")],
        [InlineKeyboardButton("+ Word",              callback_data=f"add_word_{cid}"),
         InlineKeyboardButton("- Word",              callback_data=f"remove_word_{cid}")],
        [InlineKeyboardButton("Word Limit",          callback_data=f"set_word_limit_{cid}"),
         InlineKeyboardButton("Warn Timer",          callback_data=f"set_timer_{cid}")],
        [InlineKeyboardButton("Toggle Promos",       callback_data=f"toggle_promo_{cid}"),
         InlineKeyboardButton("Toggle Links",        callback_data=f"toggle_links_{cid}")],
        [InlineKeyboardButton("Max Warnings",        callback_data=f"set_max_warnings_{cid}"),
         InlineKeyboardButton("Toggle Join Delete",  callback_data=f"toggle_join_delete_{cid}")],
        [InlineKeyboardButton("Toggle Stickers",     callback_data=f"toggle_sticker_{cid}"),
         InlineKeyboardButton("Toggle Auto Approve", callback_data=f"toggle_autoapprove_{cid}")],
        [InlineKeyboardButton("Back to Groups",      callback_data="my_groups")],
    ]
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.error(f"group_settings_handler edit: {e}")


async def _toggle(update, context, field):
    q = update.callback_query; await q.answer()
    cid = int(q.data.rsplit("_", 1)[-1])
    s   = await get_group_settings(cid) or {}
    nv  = not s.get(field, False)
    await update_setting(cid, **{field: nv})
    await q.answer(f"{field.replace('_',' ').title()}: {'ON' if nv else 'OFF'}", show_alert=True)
    q.data = f"group_settings_{cid}"
    await group_settings_handler(update, context)


async def toggle_promo_handler(u, c):        await _toggle(u, c, "delete_promotions")
async def toggle_links_handler(u, c):        await _toggle(u, c, "delete_links")
async def toggle_join_delete_handler(u, c):  await _toggle(u, c, "delete_join_messages")
async def toggle_sticker_handler(u, c):      await _toggle(u, c, "sticker_protect")
async def toggle_autoapprove_handler(u, c):  await _toggle(u, c, "auto_approve")


async def _prompt(query, context, cid, action, text):
    context.user_data["awaiting_input"] = cid
    context.user_data["action"]         = action
    try:   await query.message.edit_text(text)
    except BadRequest: pass


async def set_welcome_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[2])
    await _prompt(q, context, cid, "set_welcome",
        "Send your HTML welcome message.\nVariables: {USER_NAME} {CHAT_TITLE} {BOT_NAME} {USER_ID}\n\n/cancel to abort.")

async def add_word_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[2])
    await _prompt(q, context, cid, "add_word", "Send word to ban:\n\n/cancel to abort.")

async def remove_word_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[2])
    words = await get_banned_words(cid)
    if not words:
        await q.answer("No banned words!", show_alert=True); return
    await _prompt(q, context, cid, "remove_word",
        f"Current: {', '.join(words)}\n\nSend word to remove:\n\n/cancel to abort.")

async def set_timer_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[2])
    await _prompt(q, context, cid, "set_timer", "Send warning timer (e.g. 30 or 1m):\n\n/cancel.")

async def set_word_limit_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[3])
    await _prompt(q, context, cid, "set_word_limit", "Send max word count (0 = unlimited):\n\n/cancel.")

async def set_max_warnings_handler(update, context):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[3])
    await _prompt(q, context, cid, "set_max_warnings", "Send max warnings (3–31):\n\n/cancel.")


# ════════════════════════════════════════════════════════════
# PRIVATE TEXT INPUT
# ════════════════════════════════════════════════════════════

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "awaiting_input" not in context.user_data:
        return
    cid    = context.user_data["awaiting_input"]
    action = context.user_data.get("action", "")
    txt    = update.message.text.strip()
    reply  = ""

    if action == "set_welcome":
        context.user_data["welcome_message_html"] = txt
        context.user_data["action"] = "set_welcome_timer"
        await update.message.reply_text(
            "Welcome message saved!\nNow send auto-delete timer (0 = never, e.g. 30 or 2m):"
        )
        return
    elif action == "set_welcome_timer":
        m = re.match(r"^(\d+)\s*(s|m)?$", txt)
        if m:
            val  = int(m.group(1))
            secs = val * 60 if m.group(2) == "m" else val
            wm   = context.user_data.get("welcome_message_html", "")
            await update_setting(cid, welcome_message=wm, welcome_timer=secs)
            reply = f"Welcome message set! Timer: {val}{'m' if m.group(2)=='m' else 's'}."
        else:
            await update.message.reply_text("Invalid. Use '0', '30', or '2m'."); return
    elif action == "add_word":
        word = txt.lower()
        await add_banned_word(cid, word, update.effective_user.id)
        reply = f"Word <b>{word}</b> banned!"
    elif action == "remove_word":
        word = txt.lower()
        await remove_banned_word(cid, word)
        reply = f"Word <b>{word}</b> removed!"
    elif action == "set_timer":
        m = re.match(r"^(\d+)\s*(s|m)?$", txt)
        if m:
            secs = int(m.group(1)) * 60 if m.group(2) == "m" else int(m.group(1))
            await update_setting(cid, warning_timer=secs)
            reply = f"Warning timer set to {txt}."
        else:
            await update.message.reply_text("Invalid. Use '30' or '1m'."); return
    elif action == "set_word_limit":
        if txt.isdigit():
            await update_setting(cid, max_word_count=int(txt))
            reply = f"Word limit: {txt}." if int(txt) > 0 else "Word limit disabled."
        else:
            await update.message.reply_text("Send a number."); return
    elif action == "set_max_warnings":
        if txt.isdigit() and 3 <= int(txt) <= 31:
            await update_setting(cid, max_warnings=int(txt))
            reply = f"Max warnings set to {txt}."
        else:
            await update.message.reply_text("Must be 3–31."); return

    for k in ("awaiting_input", "action", "welcome_message_html"):
        context.user_data.pop(k, None)
    kb = [[InlineKeyboardButton("Back to Settings", callback_data=f"group_settings_{cid}")]]
    await update.message.reply_html(reply, reply_markup=InlineKeyboardMarkup(kb))


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("awaiting_input", "action", "welcome_message_html"):
        context.user_data.pop(k, None)
    await update.message.reply_text("Cancelled.")


# ════════════════════════════════════════════════════════════
# WELCOME MESSAGE
# ════════════════════════════════════════════════════════════

def parse_welcome_template(html_tmpl, bot_name, user_name, user_id, chat_title):
    msg = (html_tmpl
           .replace("{BOT_NAME}", bot_name)
           .replace("{USER_NAME}", user_name)
           .replace("{USER_ID}", str(user_id))
           .replace("{CHAT_TITLE}", chat_title))
    buttons = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", msg)
    msg     = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", "", msg).strip()
    return msg, buttons


async def send_welcome_message(chat, new_member, context, settings):
    try:
        bot_name  = context.bot.username or "GroupPilot"
        user_name = (new_member.first_name or new_member.username or "Member").strip()
        if settings and settings.get("welcome_message"):
            text, buttons = parse_welcome_template(
                settings["welcome_message"], bot_name, user_name,
                new_member.id, chat.title,
            )
            kb = []
            for i in range(0, len(buttons), 2):
                row = [InlineKeyboardButton(buttons[i][0], url=buttons[i][1])]
                if i + 1 < len(buttons):
                    row.append(InlineKeyboardButton(buttons[i+1][0], url=buttons[i+1][1]))
                kb.append(row)
            try:
                wm = await chat.send_message(
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb) if kb else None,
                )
                wt = settings.get("welcome_timer", 0)
                if wt > 0:
                    await schedule_message_deletion(chat.id, wm.message_id, wt)
            except BadRequest as e:
                logger.error(f"send_welcome_message template: {e}")
                await chat.send_message(f"Welcome {user_name} to {chat.title}!")
        else:
            await chat.send_message(f"Welcome to {chat.title}, {user_name}!")
    except Exception as e:
        logger.error(f"send_welcome_message: {e}")


# ════════════════════════════════════════════════════════════
# JOIN REQUEST HANDLER
# ════════════════════════════════════════════════════════════

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr   = update.chat_join_request
    if not jr:
        return
    chat = jr.chat
    user = jr.from_user
    s    = await get_group_settings(chat.id)
    if not s:
        return
    await save_join_request(chat.id, user.id, user.username, user.first_name)
    if s.get("auto_approve", False):
        try:
            await context.bot.approve_chat_join_request(chat.id, user.id)
        except Exception as e:
            logger.error(f"auto-approve: {e}")


# ════════════════════════════════════════════════════════════
# MEMBER JOIN/LEAVE TRACKING
# ════════════════════════════════════════════════════════════

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mc   = update.my_chat_member
    chat = mc.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    new = mc.new_chat_member
    old = mc.old_chat_member
    if old.status == ChatMemberStatus.LEFT and new.status != ChatMemberStatus.LEFT:
        adder = mc.from_user
        try:
            m = await chat.get_member(adder.id)
            if m.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                await chat.send_message("Only group admins can add me.")
                await chat.leave()
                return
        except Exception:
            await chat.leave()
            return
        try:
            bm           = await chat.get_member(context.bot.id)
            bot_is_admin = bm.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            bot_is_admin = False
        if not bot_is_admin:
            await chat.send_message(
                "Please make me an admin with Delete Messages permission.\n"
                "I'll leave now. Add me again after granting admin rights."
            )
            await chat.leave()
            return
        uname = adder.username or f"user_{adder.id}"
        await add_group_to_db(chat.id, chat.title, adder.id, uname, bot_is_admin)
        await chat.send_message(
            "<b>GroupPilot is now active!</b>\n\n"
            "<b>Admin Commands:</b>\n"
            "/warn /mute /unmute /ban /unban\n"
            "/tagall /note /get /notes /delnote\n"
            "/forcesub /removeforcesub /filterdeleted\n"
            "/admin\n\n"
            "<b>Members:</b>\n"
            "/report /get &lt;note&gt;\n\n"
            "Open the Mini App to configure settings.",
            parse_mode="HTML",
        )


async def user_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cu   = update.chat_member
    if not cu:
        return
    chat = cu.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    new = cu.new_chat_member
    old = cu.old_chat_member
    if (old.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
            and new.status == ChatMemberStatus.MEMBER):
        user = new.user
        if is_deleted_account(user):
            try:
                await context.bot.ban_chat_member(chat.id, user.id)
                await context.bot.unban_chat_member(chat.id, user.id)
            except Exception:
                pass
            return
        await track_member_activity(chat.id, user.id, user.username, user.first_name)
        s = await get_group_settings(chat.id)
        await send_welcome_message(chat, user, context, s)
    elif new.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        await remove_member(chat.id, new.user.id)


# ════════════════════════════════════════════════════════════
# CHECK MESSAGE — MAIN FILTER
# ════════════════════════════════════════════════════════════

def caption_has_link(caption, entities):
    if not caption:
        return False
    if entities:
        for e in entities:
            if e.type in [MessageEntity.TEXT_LINK, MessageEntity.URL]:
                return True
    for pat in [r"https?://\S+", r"www\.\S+", r"t\.me/\S+"]:
        if re.search(pat, caption, re.IGNORECASE):
            return True
    return False


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    if not msg:
        return
    chat = msg.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    s = await get_group_settings(chat.id)
    if not s:
        return

    # ── Determine exempt status ──────────────────────────────
    exempt = False
    if msg.sender_chat and msg.sender_chat.id == chat.id:
        exempt = True
    elif msg.from_user:
        if msg.from_user.id == 1087968824:
            exempt = True
        else:
            try:
                m = await chat.get_member(msg.from_user.id)
                if m.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                    exempt = True
            except Exception:
                pass
    if msg.sender_chat and msg.sender_chat.type == ChatType.CHANNEL:
        exempt = True

    uid  = msg.from_user.id       if msg.from_user else 0
    uname = ((msg.from_user.username or msg.from_user.first_name)
             if msg.from_user else None)

    if not exempt and msg.from_user:
        user = msg.from_user
        # Deleted account
        if is_deleted_account(user):
            try:
                await msg.delete()
                await context.bot.ban_chat_member(chat.id, user.id)
                await context.bot.unban_chat_member(chat.id, user.id)
            except Exception:
                pass
            return
        # Track activity
        await track_member_activity(chat.id, uid, user.username, user.first_name)
        # Force sub
        not_joined = await check_force_sub(chat.id, uid, context)
        if not_joined:
            try: await msg.delete()
            except Exception: pass
            await send_force_sub_message(
                chat, user, not_joined, context,
                warning_timer=s.get("force_sub_message_timer", 60),
            )
            return
        # Sticker protect
        if s.get("sticker_protect") and msg.sticker:
            try:
                await msg.delete()
                wm = await chat.send_message(
                    f"@{uname or uid}, stickers are not allowed here."
                )
                wt = s.get("warning_timer", 30)
                if wt > 0:
                    await schedule_message_deletion(chat.id, wm.message_id, wt)
            except Exception as e:
                logger.error(f"sticker protect: {e}")
            return

    if exempt:
        return

    # Photo caption link
    if s.get("delete_links") and msg.photo and msg.caption:
        if caption_has_link(msg.caption, msg.caption_entities):
            try:
                await msg.delete()
                await send_warning_with_count(chat, uid, uname,
                    "Links in photo captions are not allowed", context, "photo_caption_link")
            except Exception as e:
                logger.error(f"photo link: {e}")
            return

    if not msg.text:
        return

    # Word count
    max_wc = s.get("max_word_count", 0)
    if max_wc > 0:
        wc = len(msg.text.split())
        if wc > max_wc:
            try:
                await msg.delete()
                await send_warning_with_count(chat, uid, uname,
                    f"Message too long ({wc} words, max {max_wc})", context, "word_limit")
            except Exception as e:
                logger.error(f"word count: {e}")
            return

    # Promotions
    if s.get("delete_promotions"):
        reason = None
        if is_forwarded_or_channel_message(msg):    reason = "forwarded/channel message"
        elif msg.via_bot:                           reason = "sent via bot"
        elif msg.from_user and msg.from_user.is_bot:reason = "bot message"
        else:
            ep = r"[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]"
            em = re.findall(ep, msg.text)
            if len(em) > 15 or (len(msg.text) > 10 and len(em) / len(msg.text) > 0.4):
                reason = "too many emojis"
        if reason:
            try:
                await msg.delete()
                await send_warning_with_count(chat, uid, uname,
                    f"{reason} not allowed", context, reason.replace(" ","_"))
            except Exception as e:
                logger.error(f"promo: {e}")
            return

    # Links
    if s.get("delete_links"):
        has_link = bool(re.search(r"(https?://\S+|www\.\S+|t\.me/\S+)", msg.text))
        if not has_link and msg.entities:
            for e in msg.entities:
                if e.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                    has_link = True
        if has_link:
            try:
                await msg.delete()
                await send_warning_with_count(chat, uid, uname,
                    "Links are not allowed", context, "link")
            except Exception as e:
                logger.error(f"link: {e}")
            return

    # Banned words
    bwords = await get_banned_words(chat.id)
    if bwords:
        low = msg.text.lower()
        for word in bwords:
            if re.search(r"\b" + re.escape(word) + r"\b", low):
                try:
                    await msg.delete()
                    await send_warning_with_count(chat, uid, uname,
                        "banned word", context, "banned_word")
                except Exception as e:
                    logger.error(f"banned word: {e}")
                return


# ════════════════════════════════════════════════════════════
# /start /help /mygroups  (private chat)
# ════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb   = [
        [InlineKeyboardButton("Add to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("My Groups",    callback_data="my_groups")],
        [InlineKeyboardButton("Help",         callback_data="help")],
    ]
    text = (
        f"Welcome {user.mention_html()}!\n\n"
        "<b>GroupPilot</b> — Intelligent Group Moderation\n\n"
        "Add me to your group to get started."
    )
    if update.message:
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    elif update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
            )
        except BadRequest:
            pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>GroupPilot Commands</b>\n\n"
        "<b>Admin (Group):</b>\n"
        "/warn /mute /unmute /ban /unban\n"
        "/tagall [msg] — tag all tracked members\n"
        "/note &lt;name&gt; &lt;text&gt; — save note\n"
        "/get &lt;name&gt; — retrieve note\n"
        "/notes — list all notes\n"
        "/delnote &lt;name&gt; — delete note\n"
        "/forcesub @ch — add force subscribe\n"
        "/removeforcesub @ch — remove it\n"
        "/filterdeleted — kick deleted accounts\n"
        "/admin — show admin keyboard\n\n"
        "<b>Members (Group):</b>\n"
        "/report — reply to user to report them\n"
        "/get &lt;name&gt; — read a note\n\n"
        "<b>Private:</b>\n"
        "/start /mygroups /help"
    )
    if update.message:
        await update.message.reply_html(text)
    elif update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML")
        except BadRequest:
            pass


async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    groups = await get_user_groups(uid)
    bot_u  = context.bot.username or "GroupPilotBot"
    if not groups:
        text = "You haven't added me to any groups yet!"
        kb   = [[InlineKeyboardButton("Add to Group",
                    url=f"https://t.me/{bot_u}?startgroup=true")]]
    else:
        text = "<b>Your Groups:</b>\nSelect a group to manage:"
        kb   = [[InlineKeyboardButton(g["chat_title"],
                    callback_data=f"group_settings_{g['chat_id']}")] for g in groups]
        kb.append([InlineKeyboardButton("Back", callback_data="back_to_main")])
    rm = InlineKeyboardMarkup(kb)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=rm, parse_mode="HTML")
        except BadRequest:
            pass
    else:
        await update.message.reply_html(text, reply_markup=rm)


async def admin_keyboard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_")
    if len(parts) < 3: return
    cmd, cid = parts[1], int(parts[2])
    ok, _, err = await verify_callback_admin(cid, q, context)
    if not ok: await q.answer(err, show_alert=True); return
    tips = {
        "warn":    "/warn @user reason",
        "mute":    "/mute @user 1h reason",
        "ban":     "/ban @user reason",
        "unmute":  "/unmute @user  (resets warnings to 0)",
        "unban":   "/unban <user_id>",
        "reports": "Check Mini App → Reports tab.",
        "tagall":  "/tagall Your announcement",
    }
    await q.message.reply_html(f"<b>{cmd.title()} usage:</b>\n{tips.get(cmd,'?')}")


# ════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ════════════════════════════════════════════════════════════

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if   data == "my_groups":                    await my_groups_handler(update, context)
    elif data == "help":                         await help_command(update, context)
    elif data == "back_to_main":                 await start(update, context)
    elif data.startswith("group_settings_"):     await group_settings_handler(update, context)
    elif data.startswith("set_welcome_"):        await set_welcome_handler(update, context)
    elif data.startswith("add_word_"):           await add_word_handler(update, context)
    elif data.startswith("remove_word_"):        await remove_word_handler(update, context)
    elif data.startswith("set_timer_"):          await set_timer_handler(update, context)
    elif data.startswith("set_word_limit_"):     await set_word_limit_handler(update, context)
    elif data.startswith("toggle_promo_"):       await toggle_promo_handler(update, context)
    elif data.startswith("toggle_links_"):       await toggle_links_handler(update, context)
    elif data.startswith("toggle_join_delete_"): await toggle_join_delete_handler(update, context)
    elif data.startswith("toggle_sticker_"):     await toggle_sticker_handler(update, context)
    elif data.startswith("toggle_autoapprove_"): await toggle_autoapprove_handler(update, context)
    elif data.startswith("set_max_warnings_"):   await set_max_warnings_handler(update, context)
    elif data.startswith("unban_user_"):         await unban_callback_handler(update, context)
    elif data.startswith("unmute_user_"):        await unmute_callback_handler(update, context)
    elif data.startswith("ban_from_warn_"):      await ban_from_warn_callback_handler(update, context)
    elif data.startswith("mute_from_warn_"):     await mute_from_warn_callback_handler(update, context)
    elif data.startswith("cmd_"):                await admin_keyboard_callback_handler(update, context)
    else:                                        await update.callback_query.answer()


# ════════════════════════════════════════════════════════════
# FASTAPI STARTUP + ENDPOINTS
# ════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    global ptb_application
    if ptb_application is not None:
        return
    ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    gf = filters.ChatType.GROUP | filters.ChatType.SUPERGROUP

    # Private
    ptb_application.add_handler(CommandHandler("start",         start,                 filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("help",          help_command,          filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("mygroups",      my_groups_handler,     filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("cancel",        cancel_handler,        filters.ChatType.PRIVATE))

    # Group moderation
    ptb_application.add_handler(CommandHandler("warn",          warn_command,          gf))
    ptb_application.add_handler(CommandHandler("mute",          mute_command,          gf))
    ptb_application.add_handler(CommandHandler("unmute",        unmute_command,        gf))
    ptb_application.add_handler(CommandHandler("ban",           ban_command,           gf))
    ptb_application.add_handler(CommandHandler("unban",         unban_command,         gf))
    ptb_application.add_handler(CommandHandler("report",        report_command,        gf))
    ptb_application.add_handler(CommandHandler("admin",         show_admin_keyboard,   gf))

    # Group new features
    ptb_application.add_handler(CommandHandler("tagall",        tag_all_command,       gf))
    ptb_application.add_handler(CommandHandler("note",          note_command,          gf))
    ptb_application.add_handler(CommandHandler("get",           get_note_command,      gf))
    ptb_application.add_handler(CommandHandler("notes",         notes_command,         gf))
    ptb_application.add_handler(CommandHandler("delnote",       delnote_command,       gf))
    ptb_application.add_handler(CommandHandler("forcesub",      forcesub_command,      gf))
    ptb_application.add_handler(CommandHandler("removeforcesub",removeforcesub_command,gf))
    ptb_application.add_handler(CommandHandler("filterdeleted", filter_deleted_command,gf))

    # Callbacks + private input
    ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
    ptb_application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_input,
    ))

    # Member tracking
    ptb_application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    ptb_application.add_handler(ChatMemberHandler(user_chat_member,  ChatMemberHandler.CHAT_MEMBER))

    # Join requests
    ptb_application.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Group messages
    ptb_application.add_handler(MessageHandler(filters.PHOTO       & gf, check_message))
    ptb_application.add_handler(MessageHandler(filters.Sticker.ALL & gf, check_message))
    ptb_application.add_handler(MessageHandler(filters.TEXT        & gf, check_message))

    await ptb_application.initialize()
    await ptb_application.start()

    if WEBHOOK_URL:
        try:
            await ptb_application.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=[
                    "message", "edited_message", "callback_query",
                    "my_chat_member", "chat_member", "chat_join_request",
                ],
            )
            logger.info(f"Webhook set → {WEBHOOK_URL}")
        except RetryAfter as e:
            logger.warning(f"Rate-limited on webhook: {e}")
        except Exception as e:
            logger.error(f"set_webhook: {e}")
    else:
        logger.error("WEBHOOK_URL env var not set!")


@app.post("/webhook/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = Update.de_json(data, ptb_application.bot)
        await ptb_application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"webhook: {e}")
        return Response(status_code=500)


@app.api_route("/", methods=["GET", "POST"])
async def health_check():
    return {"status": "ok", "bot": "GroupPilot"}


@app.post("/approve-join")
async def approve_join_api(request: Request):
    try:
        d = await request.json()
        await ptb_application.bot.approve_chat_join_request(int(d["chat_id"]), int(d["user_id"]))
        supabase.table("join_requests").update({"status": "approved"}) \
            .eq("chat_id", d["chat_id"]).eq("user_id", d["user_id"]).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/reject-join")
async def reject_join_api(request: Request):
    try:
        d = await request.json()
        await ptb_application.bot.decline_chat_join_request(int(d["chat_id"]), int(d["user_id"]))
        supabase.table("join_requests").update({"status": "rejected"}) \
            .eq("chat_id", d["chat_id"]).eq("user_id", d["user_id"]).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/run-cleanup")
async def run_cleanup_job():
    if ptb_application is None:
        await startup_event()
    unmuted = deleted = 0
    try:
        unmuted = await cleanup_expired_mutes(ptb_application.bot)
    except Exception as e:
        logger.error(f"mute cleanup: {e}")
    try:
        for item in await get_due_deletions():
            try:
                await ptb_application.bot.delete_message(
                    chat_id=item["chat_id"], message_id=item["message_id"],
                )
                deleted += 1
            except Exception:
                pass
            await remove_pending_deletion(item["id"])
    except Exception as e:
        logger.error(f"deletion cleanup: {e}")
    return {"status": "ok", "deleted_count": deleted, "unmuted_count": unmuted}


@app.get("/run-group-cleanup")
async def run_group_cleanup():
    if ptb_application is None:
        await startup_event()
    try:
        result    = supabase.table("groups").select("chat_id").execute()
        group_ids = [g["chat_id"] for g in result.data]
    except Exception:
        return {"status": "error"}
    removed = []
    for cid in group_ids:
        try:
            await ptb_application.bot.get_chat(cid)
        except Forbidden:
            await delete_group_and_words(cid)
            removed.append(cid)
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                await delete_group_and_words(cid)
                removed.append(cid)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            pass
    return {"status": "ok", "removed": removed}
