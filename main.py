import os
import logging
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, ChatPermissions
from telegram.constants import ChatType, ChatMemberStatus
from telegram.error import BadRequest, RetryAfter, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ChatMemberHandler,
    ChatJoinRequestHandler,
)
from supabase import create_client, Client
from dotenv import load_dotenv
import asyncio
import requests as http_requests
import json

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL       = os.getenv("WEBHOOK_URL")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
ptb_application = None

# ── In-memory caches ──────────────────────────────────────────────────────────
_force_sub_cache:  dict = {}
_membership_cache: dict = {}
_FORCE_SUB_TTL   = 120
_MEMBERSHIP_TTL  = 90

# ═══════════════════════════════════════════════════════════════════════════════
# SAFE CHAT-ID PARSER  — always use rsplit("_",1)[-1] so we never crash on
# callback prefixes that contain underscores (toggle_join_delete_, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

def _cid(data: str) -> int:
    """Parse chat_id from ANY callback_data string safely."""
    return int(data.rsplit("_", 1)[-1])


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_forwarded_or_channel_message(message) -> bool:
    if message.forward_origin is not None:
        return True
    if message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        return True
    if message.entities:
        first_entity = message.entities[0]
        if first_entity.offset == 0 and first_entity.type in ('bold', 'text_link'):
            if first_entity.type == 'text_link' and 't.me' in (first_entity.url or ''):
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
    name = (user.first_name or "User").strip()
    if getattr(user, "username", None):
        return f'<a href="https://t.me/{user.username}">@{user.username}</a>'
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def parse_welcome_template(template: str, bot_name: str, user_name: str,
                            user_id: int, chat_title: str) -> tuple:
    """Replace {variables} and extract [Button](url) pairs."""
    msg = (template
           .replace('{BOT_NAME}',    bot_name)
           .replace('{USER_NAME}',   user_name)
           .replace('{FIRST_NAME}',  user_name)
           .replace('{USER_ID}',     str(user_id))
           .replace('{CHAT_TITLE}',  chat_title)
           .replace('{CHANNEL_TITLE}', chat_title))
    btn_pat = r'\[([^\]]+)\]\(([^)]+)\)'
    buttons = re.findall(btn_pat, msg)
    msg = re.sub(btn_pat, '', msg).strip()
    return msg, buttons


def build_inline_keyboard(buttons: list):
    """Build InlineKeyboardMarkup from [(text,url)] list, 2 per row."""
    if not buttons:
        return None
    kb = []
    for i in range(0, len(buttons), 2):
        row = [InlineKeyboardButton(buttons[i][0], url=buttons[i][1])]
        if i + 1 < len(buttons):
            row.append(InlineKeyboardButton(buttons[i + 1][0], url=buttons[i + 1][1]))
        kb.append(row)
    return InlineKeyboardMarkup(kb)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE — GROUPS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_group_settings(chat_id: int):
    try:
        r = supabase.table('groups').select("*").eq('chat_id', chat_id).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_group_settings: {e}"); return None


async def is_user_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False


async def is_sender_admin(chat_id: int, message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        if message.sender_chat and message.sender_chat.id == chat_id:
            return True
        if message.from_user:
            return await is_user_admin(chat_id, message.from_user.id, context)
        return False
    except Exception:
        return False


async def verify_callback_admin(chat_id: int, query, context) -> tuple:
    try:
        if query.from_user:
            ok = await is_user_admin(chat_id, query.from_user.id, context)
            if ok:
                return (True, query.from_user.id, None)
            return (False, query.from_user.id,
                    "❌ This button is for admins only.")
        return (False, None, "❌ This button is for admins only.")
    except Exception as e:
        logger.error(f"verify_callback_admin: {e}")
        return (False, None, "❌ This button is for admins only.")


async def get_chat_admins(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return [a.user.id for a in admins]
    except Exception as e:
        logger.error(f"get_chat_admins: {e}"); return []


async def add_warning(chat_id, user_id, warned_by, reason, username=None):
    try:
        return supabase.table('warnings').insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "warned_by": warned_by, "reason": reason,
            "warned_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"add_warning: {e}"); return None


async def get_user_warnings(chat_id, user_id):
    try:
        return supabase.table('warnings').select("*").eq('chat_id', chat_id).eq('user_id', user_id).execute().data
    except Exception as e:
        logger.error(f"get_user_warnings: {e}"); return []


async def clear_user_warnings(chat_id, user_id):
    try:
        supabase.table('warnings').delete().eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"clear_user_warnings: {e}")


async def add_ban(chat_id, user_id, banned_by, reason, username=None):
    try:
        return supabase.table('bans').insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "banned_by": banned_by, "reason": reason,
            "banned_at": datetime.now(timezone.utc).isoformat(), "is_active": True
        }).execute()
    except Exception as e:
        logger.error(f"add_ban: {e}"); return None


async def get_active_ban(chat_id, user_id):
    try:
        r = supabase.table('bans').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_active_ban: {e}"); return None


async def unban_user_in_db(chat_id, user_id):
    try:
        return supabase.table('bans').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"unban_user_in_db: {e}"); return None


async def add_mute(chat_id, user_id, muted_by, reason, duration_minutes, username=None):
    try:
        mute_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        return supabase.table('mutes').insert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "muted_by": muted_by, "reason": reason,
            "muted_at": datetime.now(timezone.utc).isoformat(),
            "mute_until": mute_until.isoformat(), "is_active": True
        }).execute()
    except Exception as e:
        logger.error(f"add_mute: {e}"); return None


async def get_active_mute(chat_id, user_id):
    try:
        r = supabase.table('mutes').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        if r.data:
            md = r.data[0]
            if datetime.now(timezone.utc) > datetime.fromisoformat(md['mute_until']):
                await unmute_user_in_db(chat_id, user_id); return None
            return md
        return None
    except Exception as e:
        logger.error(f"get_active_mute: {e}"); return None


async def unmute_user_in_db(chat_id, user_id):
    try:
        return supabase.table('mutes').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"unmute_user_in_db: {e}"); return None


async def cleanup_expired_mutes(bot):
    try:
        now = datetime.now(timezone.utc).isoformat()
        r = supabase.table('mutes').select("*").eq('is_active', True).lte('mute_until', now).execute()
        count = 0
        for md in (r.data or []):
            try:
                await bot.restrict_chat_member(
                    md['chat_id'], md['user_id'],
                    ChatPermissions(can_send_messages=True, can_send_photos=True,
                                    can_send_videos=True, can_send_documents=True,
                                    can_send_audios=True, can_send_voice_notes=True,
                                    can_send_video_notes=True, can_send_polls=True),
                    until_date=0)
                await unmute_user_in_db(md['chat_id'], md['user_id'])
                count += 1
            except Exception as e:
                logger.error(f"auto-unmute {md['user_id']}: {e}")
        return count
    except Exception as e:
        logger.error(f"cleanup_expired_mutes: {e}"); return 0


async def add_report(chat_id, reporter_id, reported_user_id, reason,
                     reporter_username=None, reported_username=None):
    try:
        return supabase.table('reports').insert({
            "chat_id": chat_id, "reporter_id": reporter_id,
            "reporter_username": reporter_username,
            "reported_user_id": reported_user_id,
            "reported_username": reported_username, "reason": reason,
            "reported_at": datetime.now(timezone.utc).isoformat(), "status": "pending"
        }).execute()
    except Exception as e:
        logger.error(f"add_report: {e}"); return None


async def get_pending_reports(chat_id):
    try:
        return supabase.table('reports').select("*").eq('chat_id', chat_id).eq('status', 'pending').execute().data
    except Exception as e:
        logger.error(f"get_pending_reports: {e}"); return []


async def add_group_to_db(chat_id, chat_title, added_by, username, bot_is_admin, chat_username=None):
    try:
        ex = await get_group_settings(chat_id)
        def _g(k, d): return ex.get(k, d) if ex else d
        data = {
            "chat_id": chat_id, "chat_title": chat_title, "chat_username": chat_username,
            "added_by": added_by, "added_by_username": username, "bot_is_admin": bot_is_admin,
            "delete_promotions": _g('delete_promotions', False),
            "delete_links": _g('delete_links', False),
            "warning_timer": _g('warning_timer', 30),
            "max_word_count": _g('max_word_count', 0),
            "welcome_message": _g('welcome_message', None),
            "welcome_timer": _g('welcome_timer', 0),
            "delete_join_messages": _g('delete_join_messages', False),
            "max_warnings": _g('max_warnings', 3),
            "require_approval": _g('require_approval', False),
            "auto_approve": _g('auto_approve', False),
            "sticker_protect": _g('sticker_protect', False),
            "force_sub_channel": _g('force_sub_channel', None),
            "force_sub_message_timer": _g('force_sub_message_timer', 60),
            "member_count": _g('member_count', 0),
        }
        return supabase.table('groups').upsert(data, on_conflict='chat_id').execute()
    except Exception as e:
        logger.error(f"add_group_to_db: {e}"); return None


async def get_user_groups(user_id):
    try:
        return supabase.table('groups').select("*").eq('added_by', user_id).execute().data
    except Exception as e:
        logger.error(f"get_user_groups: {e}"); return []


async def add_banned_word(chat_id, word, added_by):
    try:
        return supabase.table('banned_words').insert({"chat_id": chat_id, "word": word.lower(), "added_by": added_by}).execute()
    except Exception as e:
        logger.error(f"add_banned_word: {e}"); return None


async def remove_banned_word(chat_id, word):
    try:
        return supabase.table('banned_words').delete().eq('chat_id', chat_id).eq('word', word.lower()).execute()
    except Exception as e:
        logger.error(f"remove_banned_word: {e}"); return None


async def get_banned_words(chat_id):
    try:
        return [i['word'] for i in supabase.table('banned_words').select("word").eq('chat_id', chat_id).execute().data]
    except Exception as e:
        logger.error(f"get_banned_words: {e}"); return []


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE — USERS / MEMBERS / NOTES / JOIN-REQ / FORCE-SUB
# ═══════════════════════════════════════════════════════════════════════════════

async def upsert_user(telegram_id, username=None, first_name=None, last_name=None):
    try:
        supabase.table('users').upsert({
            "telegram_id": telegram_id, "username": username,
            "first_name": first_name, "last_name": last_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }, on_conflict='telegram_id').execute()
    except Exception as e:
        logger.error(f"upsert_user: {e}")


async def upsert_group_member(chat_id, user_id, username=None, first_name=None):
    try:
        ex = supabase.table('group_members').select("*").eq('chat_id', chat_id).eq('user_id', user_id).execute()
        if ex.data:
            supabase.table('group_members').update({
                "username": username, "first_name": first_name,
                "last_active": datetime.now(timezone.utc).isoformat(),
                "message_count": ex.data[0].get('message_count', 0) + 1,
            }).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        else:
            supabase.table('group_members').insert({
                "chat_id": chat_id, "user_id": user_id, "username": username,
                "first_name": first_name, "last_active": datetime.now(timezone.utc).isoformat(),
                "message_count": 1, "joined_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception as e:
        logger.error(f"upsert_group_member: {e}")


async def get_group_members(chat_id):
    try:
        return supabase.table('group_members').select("*").eq('chat_id', chat_id).execute().data
    except Exception as e:
        logger.error(f"get_group_members: {e}"); return []


async def remove_group_member(chat_id, user_id):
    try:
        supabase.table('group_members').delete().eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"remove_group_member: {e}")


async def add_note(chat_id, name, content, added_by):
    try:
        supabase.table('notes').upsert(
            {"chat_id": chat_id, "name": name.lower(), "content": content, "added_by": added_by},
            on_conflict='chat_id,name').execute()
    except Exception as e:
        logger.error(f"add_note: {e}")


async def get_note(chat_id, name):
    try:
        r = supabase.table('notes').select("*").eq('chat_id', chat_id).eq('name', name.lower()).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_note: {e}"); return None


async def get_all_notes(chat_id):
    try:
        return supabase.table('notes').select("*").eq('chat_id', chat_id).execute().data
    except Exception as e:
        logger.error(f"get_all_notes: {e}"); return []


async def delete_note(chat_id, name):
    try:
        supabase.table('notes').delete().eq('chat_id', chat_id).eq('name', name.lower()).execute()
    except Exception as e:
        logger.error(f"delete_note: {e}")


async def add_join_request(chat_id, user_id, username=None, first_name=None):
    try:
        supabase.table('join_requests').upsert({
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "first_name": first_name, "requested_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }, on_conflict='chat_id,user_id').execute()
    except Exception as e:
        logger.error(f"add_join_request: {e}")


async def get_pending_join_requests(chat_id):
    try:
        return supabase.table('join_requests').select("*").eq('chat_id', chat_id).eq('status', 'pending').execute().data
    except Exception as e:
        logger.error(f"get_pending_join_requests: {e}"); return []


async def update_join_request_status(chat_id, user_id, status, reviewed_by):
    try:
        supabase.table('join_requests').update({
            "status": status, "reviewed_by": reviewed_by,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"update_join_request_status: {e}")


async def add_force_sub(chat_id, channel_id, channel_title=None, channel_username=None, added_by=0):
    try:
        supabase.table('force_sub').upsert({
            "chat_id": chat_id, "channel_id": channel_id,
            "channel_title": channel_title, "channel_username": channel_username,
            "added_by": added_by, "is_active": True,
        }, on_conflict='chat_id,channel_id').execute()
        _force_sub_cache.pop(chat_id, None)
    except Exception as e:
        logger.error(f"add_force_sub: {e}")


async def get_active_force_subs(chat_id):
    try:
        return supabase.table('force_sub').select("*").eq('chat_id', chat_id).eq('is_active', True).execute().data
    except Exception as e:
        logger.error(f"get_active_force_subs: {e}"); return []


async def remove_force_sub(chat_id, channel_id):
    try:
        supabase.table('force_sub').update({"is_active": False}).eq('chat_id', chat_id).eq('channel_id', channel_id).execute()
        _force_sub_cache.pop(chat_id, None)
    except Exception as e:
        logger.error(f"remove_force_sub: {e}")


async def check_user_force_sub(chat_id, user_id, context) -> tuple:
    force_subs = await get_active_force_subs(chat_id)
    if not force_subs:
        return True, []
    missing = []
    for fs in force_subs:
        try:
            m = await context.bot.get_chat_member(fs['channel_id'], user_id)
            if m.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                missing.append(fs)
        except Exception:
            missing.append(fs)
    return len(missing) == 0, missing


async def increment_member_count(chat_id):
    try:
        s = await get_group_settings(chat_id)
        if s:
            supabase.table('groups').update({"member_count": s.get('member_count', 0) + 1}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"increment_member_count: {e}")


async def decrement_member_count(chat_id):
    try:
        s = await get_group_settings(chat_id)
        if s:
            supabase.table('groups').update({"member_count": max(0, s.get('member_count', 0) - 1)}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"decrement_member_count: {e}")


async def update_promotion_setting(chat_id, v):
    supabase.table('groups').update({"delete_promotions": v}).eq('chat_id', chat_id).execute()

async def update_link_setting(chat_id, v):
    supabase.table('groups').update({"delete_links": v}).eq('chat_id', chat_id).execute()

async def update_warning_timer(chat_id, seconds):
    supabase.table('groups').update({"warning_timer": seconds}).eq('chat_id', chat_id).execute()

async def update_word_limit(chat_id, limit):
    supabase.table('groups').update({"max_word_count": limit}).eq('chat_id', chat_id).execute()

async def update_welcome_message(chat_id, welcome_html, timer):
    supabase.table('groups').update({"welcome_message": welcome_html, "welcome_timer": timer}).eq('chat_id', chat_id).execute()

async def update_delete_join_messages(chat_id, v):
    supabase.table('groups').update({"delete_join_messages": v}).eq('chat_id', chat_id).execute()

async def update_max_warnings(chat_id, max_warnings):
    if not (3 <= max_warnings <= 31):
        raise ValueError("Max warnings must be between 3 and 31")
    supabase.table('groups').update({"max_warnings": max_warnings}).eq('chat_id', chat_id).execute()

async def update_sticker_protect(chat_id, v):
    supabase.table('groups').update({"sticker_protect": v}).eq('chat_id', chat_id).execute()

async def update_auto_approve(chat_id, v):
    supabase.table('groups').update({"auto_approve": v}).eq('chat_id', chat_id).execute()

async def update_setting(chat_id, **kwargs):
    try:
        supabase.table('groups').update(kwargs).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"update_setting: {e}")


async def schedule_message_deletion(chat_id, message_id, delay_seconds):
    try:
        delete_time = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        supabase.table('pending_deletions').insert({
            "chat_id": chat_id, "message_id": message_id,
            "delete_at": delete_time.isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"schedule_message_deletion: {e}")


async def get_due_deletions():
    try:
        r = supabase.table('pending_deletions').select("*").lte('delete_at', datetime.now(timezone.utc).isoformat()).execute()
        return r.data if r.data else []
    except Exception as e:
        logger.error(f"get_due_deletions: {e}"); return []


async def remove_pending_deletion(row_id):
    try:
        supabase.table('pending_deletions').delete().eq('id', row_id).execute()
    except Exception as e:
        logger.error(f"remove_pending_deletion: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE — CHANNELS  (NEW)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_channel_settings(channel_id: int):
    try:
        r = supabase.table('channel_settings').select("*").eq('channel_id', channel_id).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"get_channel_settings: {e}"); return None


async def upsert_channel_settings(channel_id: int, data: dict):
    try:
        data['channel_id'] = channel_id
        supabase.table('channel_settings').upsert(data, on_conflict='channel_id').execute()
    except Exception as e:
        logger.error(f"upsert_channel_settings: {e}")


async def get_user_channels(user_id: int):
    try:
        return supabase.table('channel_settings').select("*").eq('added_by', user_id).execute().data
    except Exception as e:
        logger.error(f"get_user_channels: {e}"); return []


async def record_channel_join(channel_id, user_id, username=None, first_name=None, invite_source=None):
    try:
        supabase.table('channel_members').upsert({
            "channel_id": channel_id, "user_id": user_id,
            "username": username, "first_name": first_name,
            "invite_source": invite_source,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict='channel_id,user_id').execute()
    except Exception as e:
        logger.error(f"record_channel_join: {e}")


async def get_channel_analytics(channel_id: int) -> dict:
    try:
        today    = datetime.now(timezone.utc).date().isoformat()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        total = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).execute()
        t_day = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).gte('joined_at', today).execute()
        t_wk  = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).gte('joined_at', week_ago).execute()
        return {"total_members": total.count or 0, "joined_today": t_day.count or 0, "joined_this_week": t_wk.count or 0}
    except Exception as e:
        logger.error(f"get_channel_analytics: {e}")
        return {"total_members": 0, "joined_today": 0, "joined_this_week": 0}


async def save_scheduled_post(channel_id, content, scheduled_at, added_by, parse_mode="HTML", buttons_json=None):
    try:
        supabase.table('scheduled_posts').insert({
            "channel_id": channel_id, "content": content, "scheduled_at": scheduled_at,
            "added_by": added_by, "parse_mode": parse_mode,
            "buttons_json": buttons_json, "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"save_scheduled_post: {e}")


async def get_due_scheduled_posts():
    try:
        return supabase.table('scheduled_posts').select("*").eq('status', 'pending').lte('scheduled_at', datetime.now(timezone.utc).isoformat()).execute().data or []
    except Exception as e:
        logger.error(f"get_due_scheduled_posts: {e}"); return []


async def mark_scheduled_post_sent(post_id):
    try:
        supabase.table('scheduled_posts').update({"status": "sent"}).eq('id', post_id).execute()
    except Exception as e:
        logger.error(f"mark_scheduled_post_sent: {e}")


async def record_user_onboarded(channel_id, user_id):
    try:
        supabase.table('channel_members').update({
            "onboarded": True, "onboarded_at": datetime.now(timezone.utc).isoformat()
        }).eq('channel_id', channel_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"record_user_onboarded: {e}")


async def is_user_onboarded(channel_id, user_id) -> bool:
    try:
        r = supabase.table('channel_members').select("onboarded").eq('channel_id', channel_id).eq('user_id', user_id).execute()
        return bool(r.data[0].get('onboarded', False)) if r.data else False
    except Exception as e:
        logger.error(f"is_user_onboarded: {e}"); return False


# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI
# ═══════════════════════════════════════════════════════════════════════════════

async def generate_mute_reason_with_gemini(warning_count, recent_warnings, offense_type) -> str:
    try:
        ws = "\n".join(f"- {w['reason']} ({w['warned_at']})" for w in (recent_warnings or [])[-5:]) or "None"
        prompt = (f"Generate a concise professional mute reason (2-3 sentences) for Telegram moderation.\n"
                  f"Warning count: {warning_count}\nOffense: {offense_type}\nRecent warnings: {ws}\n"
                  f"Under 150 chars. Plain text, no markdown.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        r = http_requests.post(url, headers={"Content-Type": "application/json"},
                               data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                                "generationConfig": {"maxOutputTokens": 100}}), timeout=5)
        if r.status_code == 200:
            return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        return f"Multiple violations ({offense_type})"
    except Exception as e:
        logger.error(f"Gemini: {e}"); return f"Repeated violations ({offense_type})"


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_duration(s: str) -> int:
    s = s.lower().strip()
    try:
        if s.endswith('m'): return int(s[:-1]) * 60
        if s.endswith('h'): return int(s[:-1]) * 3600
        if s.endswith('d'): return int(s[:-1]) * 86400
        if s.endswith('w'): return int(s[:-1]) * 604800
        return int(s) * 60
    except ValueError:
        return 0


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


async def auto_mute_user(chat, user_id, username, count, warnings, max_w, context):
    reason = await generate_mute_reason_with_gemini(count, warnings, "Max warnings")
    until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    try:
        await context.bot.restrict_chat_member(
            chat.id, user_id,
            ChatPermissions(can_send_messages=False, can_send_photos=False,
                            can_send_videos=False, can_send_documents=False,
                            can_send_audios=False, can_send_voice_notes=False,
                            can_send_video_notes=False, can_send_polls=False),
            until_date=until_date)
        await add_mute(chat.id, user_id, 0, reason, 60, username)
        mention = f"@{username}" if username else f"User {user_id}"
        kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat.id}")]]
        await chat.send_message(
            f"🔇 <b>AUTO-MUTED</b>\nUser: {mention}\nDuration: 1 hour\n"
            f"Reason: {reason}\nReached {max_w} warnings.",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as e:
        logger.error(f"auto_mute_user: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL WELCOME DM
# ═══════════════════════════════════════════════════════════════════════════════

async def send_channel_welcome_dm(bot, user, channel_id: int,
                                  channel_title: str, settings: dict) -> bool:
    """Send private welcome DM to a new channel member."""
    try:
        template = settings.get('welcome_message') or (
            f"👋 Welcome to <b>{channel_title}</b>, {{USER_NAME}}!\n\n"
            f"We're glad to have you. Enjoy the content! 🎉"
        )
        text, buttons = parse_welcome_template(
            template, bot.username or "Bot",
            user.first_name or user.username or "Member",
            user.id, channel_title)
        # Default channel button if none set
        if not buttons and settings.get('channel_username'):
            buttons = [(f"📢 Open {channel_title}", f"https://t.me/{settings['channel_username']}")]
        rm = build_inline_keyboard(buttons)
        await bot.send_message(chat_id=user.id, text=text, parse_mode='HTML', reply_markup=rm)
        logger.info(f"Channel welcome DM sent → user {user.id}")
        return True
    except Forbidden:
        logger.info(f"User {user.id} hasn't started the bot — DM skipped.")
        return False
    except Exception as e:
        logger.error(f"send_channel_welcome_dm: {e}"); return False


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL JOIN REQUEST HANDLER  (NEW — handles channels AND groups)
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified handler: channels get full DM flow; groups get simple auto-approve."""
    jr = update.chat_join_request
    if not jr:
        return
    chat = jr.chat
    user = jr.from_user

    # ── CHANNEL FLOW ──────────────────────────────────────────────────────────
    if chat.type == ChatType.CHANNEL:
        logger.info(f"Channel join request: user {user.id} → {chat.id} ({chat.title})")
        await add_join_request(chat.id, user.id, user.username, user.first_name)
        await upsert_user(user.id, user.username, user.first_name, getattr(user, 'last_name', None))

        settings = await get_channel_settings(chat.id)

        if not settings:
            # Channel not registered — still auto-approve + basic DM
            try:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
                await record_channel_join(chat.id, user.id, user.username, user.first_name, "direct")
                try:
                    await context.bot.send_message(
                        user.id,
                        f"👋 Welcome to <b>{chat.title}</b>, {user.first_name or 'Member'}!\n\nEnjoy! 🎉",
                        parse_mode='HTML')
                except Forbidden:
                    pass
            except Exception as e:
                logger.error(f"Channel join (no settings): {e}")
            return

        auto_approve   = settings.get('auto_approve', True)
        approval_delay = settings.get('approval_delay', 0)

        if auto_approve:
            if approval_delay > 0:
                await asyncio.sleep(approval_delay)
            try:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
                await record_channel_join(chat.id, user.id, user.username, user.first_name, "join_request")
                already = await is_user_onboarded(chat.id, user.id)
                if not already:
                    sent = await send_channel_welcome_dm(context.bot, user, chat.id, chat.title, settings)
                    if sent:
                        await record_user_onboarded(chat.id, user.id)
            except Exception as e:
                logger.error(f"Channel auto-approve: {e}")
        else:
            admin_id = settings.get('added_by')
            if admin_id:
                try:
                    kb = [[
                        InlineKeyboardButton("✅ Approve", callback_data=f"ch_approve_{chat.id}_{user.id}"),
                        InlineKeyboardButton("❌ Reject",  callback_data=f"ch_reject_{chat.id}_{user.id}")
                    ]]
                    await context.bot.send_message(
                        admin_id,
                        f"🔔 <b>New Channel Join Request</b>\n\n"
                        f"Channel: <b>{chat.title}</b>\n"
                        f"User: {get_user_mention_html(user)}\n"
                        f"ID: <code>{user.id}</code>",
                        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
                except Exception as e:
                    logger.error(f"Notify admin of join: {e}")
        return

    # ── GROUP FLOW ────────────────────────────────────────────────────────────
    s = await get_group_settings(chat.id)
    if not s:
        return
    await add_join_request(chat.id, user.id, user.username, user.first_name)
    if s.get("auto_approve", False):
        try:
            await context.bot.approve_chat_join_request(chat.id, user.id)
            await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
        except Exception as e:
            logger.error(f"Group auto-approve: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL APPROVE / REJECT CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def channel_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts      = q.data.split("_")   # ch_approve_{cid}_{uid}
    channel_id = int(parts[2])
    user_id    = int(parts[3])
    try:
        await context.bot.approve_chat_join_request(channel_id, user_id)
        await update_join_request_status(channel_id, user_id, "approved", q.from_user.id)
        await record_channel_join(channel_id, user_id, None, None, "manual_approve")
        settings = await get_channel_settings(channel_id)
        ch_info  = await context.bot.get_chat(channel_id)
        try:
            u = await context.bot.get_chat(user_id)
            if settings:
                await send_channel_welcome_dm(context.bot, u, channel_id, ch_info.title, settings)
        except Exception:
            pass
        try:
            await q.message.edit_text(f"✅ User {user_id} approved for channel {channel_id}.", reply_markup=None)
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


async def channel_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts      = q.data.split("_")
    channel_id = int(parts[2])
    user_id    = int(parts[3])
    try:
        await context.bot.decline_chat_join_request(channel_id, user_id)
        await update_join_request_status(channel_id, user_id, "rejected", q.from_user.id)
        try:
            await q.message.edit_text(f"❌ User {user_id} rejected.", reply_markup=None)
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


# ═══════════════════════════════════════════════════════════════════════════════
# /addchannel COMMAND  — also handles the startchannel deep-link flow
# ═══════════════════════════════════════════════════════════════════════════════

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addchannel @username  OR  the bot receives a startchannel payload
    after the admin adds it via the deep-link button.
    Works for public AND private channels as long as the bot is admin.
    """
    message = update.message
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Please use /addchannel in private chat.")
        return

    if not context.args:
        bot_username = context.bot.username
        await message.reply_html(
            "❌ <b>Usage:</b> /addchannel @channelUsername\n\n"
            "<b>Steps:</b>\n"
            "1. Add me as admin to your channel (use the button below)\n"
            "2. Give me <b>Invite Users</b> + <b>Manage Channel</b> permissions\n"
            "3. Enable <b>Join Requests</b> in channel settings\n"
            "4. Then send /addchannel @yourchannel\n\n"
            "<i>Private channels work too — just make sure I'm already inside.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "➕ Add Me to Channel",
                    url=f"https://t.me/{bot_username}?startchannel=true"
                        f"&admin=post_messages+edit_messages+delete_messages+invite_users"
                )
            ]])
        )
        return

    channel_ref = context.args[0]
    try:
        channel_chat = await context.bot.get_chat(channel_ref)
    except Exception as e:
        await message.reply_text(f"❌ Could not find channel: {e}")
        return

    if channel_chat.type != ChatType.CHANNEL:
        await message.reply_text("❌ That is not a channel!")
        return

    try:
        bm = await context.bot.get_chat_member(channel_chat.id, context.bot.id)
        if bm.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_html(
                "❌ I'm not an admin in that channel!\n\n"
                "Please give me admin rights with:\n"
                "• <b>Invite Users</b>\n• <b>Manage Channel</b>"
            )
            return
    except Exception as e:
        await message.reply_text(f"❌ Error checking permissions: {e}")
        return

    await upsert_channel_settings(channel_chat.id, {
        "channel_title":    channel_chat.title,
        "channel_username": channel_chat.username,   # None for private channels
        "added_by":         message.from_user.id,
        "auto_approve":     True,
        "approval_delay":   0,
        "welcome_message":  None,
        "welcome_timer":    0,
        "created_at":       datetime.now(timezone.utc).isoformat(),
    })

    deep_link = f"https://t.me/{context.bot.username}?start=channel_{channel_chat.id}"
    is_private = not channel_chat.username

    kb = [
        [InlineKeyboardButton("⚙️ Channel Settings", callback_data=f"ch_settings_{channel_chat.id}")],
        [InlineKeyboardButton("💌 Set Welcome DM",   callback_data=f"ch_set_welcome_{channel_chat.id}")],
        [InlineKeyboardButton("📊 Analytics",         callback_data=f"ch_analytics_{channel_chat.id}")],
    ]
    await message.reply_html(
        f"✅ <b>Channel Registered!</b>\n\n"
        f"📢 <b>{channel_chat.title}</b>\n"
        f"🆔 ID: <code>{channel_chat.id}</code>\n"
        f"🔒 Type: {'Private' if is_private else 'Public'}\n\n"
        f"<b>I will now automatically:</b>\n"
        f"• Approve join requests\n"
        f"• Send private welcome DMs to new members\n"
        f"• Track analytics\n\n"
        f"<b>Deep-link (share so users can receive DMs):</b>\n"
        f"<code>{deep_link}</code>\n"
        f"<i>Share this link — when users click it they start the bot and "
        f"immediately receive their welcome message.</i>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL SETTINGS / ANALYTICS / TOGGLE CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def channel_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channel_id = _cid(q.data)
    settings   = await get_channel_settings(channel_id)
    if not settings:
        try:
            await q.message.edit_text("❌ Channel not found. Use /addchannel to register.")
        except BadRequest:
            pass
        return

    auto  = "✅ ON" if settings.get('auto_approve', True) else "❌ OFF"
    wel   = "✅ Set" if settings.get('welcome_message') else "❌ Not Set"
    delay = settings.get('approval_delay', 0)
    ch_u  = settings.get('channel_username', '')
    link  = f"https://t.me/{ch_u}" if ch_u else "Private Channel"
    dl    = f"https://t.me/{context.bot.username}?start=channel_{channel_id}"

    text = (
        f"⚙️ <b>Channel Settings</b>\n\n"
        f"📢 <b>{settings.get('channel_title', 'Unknown')}</b>\n"
        f"🔗 {link}\n\n"
        f"✅ Auto Approve: {auto}\n"
        f"⏱ Approval Delay: {delay}s\n"
        f"💌 Welcome DM: {wel}\n\n"
        f"<b>Welcome DM deep-link:</b>\n<code>{dl}</code>"
    )
    kb = [
        [InlineKeyboardButton(f"Auto Approve: {auto}", callback_data=f"ch_toggle_approve_{channel_id}")],
        [InlineKeyboardButton("💌 Set Welcome DM",     callback_data=f"ch_set_welcome_{channel_id}")],
        [InlineKeyboardButton("⏱ Set Approval Delay",  callback_data=f"ch_set_delay_{channel_id}")],
        [InlineKeyboardButton("📊 Analytics",           callback_data=f"ch_analytics_{channel_id}")],
        [InlineKeyboardButton("🔙 My Channels",         callback_data="my_channels")],
    ]
    try:
        await q.message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest:
        pass


async def channel_analytics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channel_id = _cid(q.data)
    settings   = await get_channel_settings(channel_id)
    analytics  = await get_channel_analytics(channel_id)
    title = settings.get('channel_title', 'Unknown') if settings else 'Unknown'
    text = (
        f"📊 <b>Channel Analytics</b>\n\n"
        f"📢 {title}\n\n"
        f"👥 Total Members (tracked): <b>{analytics['total_members']}</b>\n"
        f"📈 Joined Today: <b>{analytics['joined_today']}</b>\n"
        f"📆 Joined This Week: <b>{analytics['joined_this_week']}</b>"
    )
    kb = [[InlineKeyboardButton("🔙 Back", callback_data=f"ch_settings_{channel_id}")]]
    try:
        await q.message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest:
        pass


async def channel_toggle_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channel_id = _cid(q.data)
    settings   = await get_channel_settings(channel_id)
    if not settings:
        return
    new_val = not settings.get('auto_approve', True)
    await upsert_channel_settings(channel_id, {"auto_approve": new_val})
    await q.answer(f"Auto Approve: {'ON' if new_val else 'OFF'}", show_alert=True)
    q.data = f"ch_settings_{channel_id}"
    await channel_settings_handler(update, context)


async def channel_set_welcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channel_id = _cid(q.data)
    context.user_data['awaiting_input'] = channel_id
    context.user_data['action']         = 'ch_set_welcome'
    text = (
        "💌 <b>Set Channel Welcome DM</b>\n\n"
        "Sent privately to every new member who joins.\n\n"
        "<b>Variables:</b>\n"
        "• <code>{USER_NAME}</code> — first name\n"
        "• <code>{USER_ID}</code> — Telegram ID\n"
        "• <code>{CHANNEL_TITLE}</code> — channel name\n"
        "• <code>{BOT_NAME}</code> — bot username\n\n"
        "<b>Inline Buttons:</b> <code>[Text](https://link)</code>\n\n"
        "<b>HTML tags:</b> &lt;b&gt; &lt;i&gt; &lt;code&gt; &lt;u&gt; &lt;s&gt;\n\n"
        "<b>Example:</b>\n"
        "<code>👋 Welcome &lt;b&gt;{USER_NAME}&lt;/b&gt; to {CHANNEL_TITLE}!\n\n"
        "🎉 Enjoy exclusive content.\n\n"
        "[📢 Open Channel](https://t.me/yourchannel) "
        "[📋 Rules](https://t.me/yourchannel/5)</code>\n\n"
        "✏️ Send your welcome message now:"
    )
    try:
        await q.message.edit_text(text, parse_mode='HTML')
    except BadRequest:
        pass


async def channel_set_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channel_id = _cid(q.data)
    context.user_data['awaiting_input'] = channel_id
    context.user_data['action']         = 'ch_set_delay'
    try:
        await q.message.edit_text(
            "⏱ <b>Set Approval Delay</b>\n\nSeconds before approving:\n"
            "<code>0</code> Instant  <code>5</code> 5s  <code>30</code> 30s\n\n✏️ Send number:",
            parse_mode='HTML')
    except BadRequest:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MY CHANNELS COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

async def my_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id      = update.effective_user.id
    channels     = await get_user_channels(user_id)
    bot_username = context.bot.username

    add_btn = InlineKeyboardButton(
        "➕ Add Bot to Channel",
        url=f"https://t.me/{bot_username}?startchannel=true"
            f"&admin=post_messages+edit_messages+delete_messages+invite_users"
    )

    if not channels:
        text = (
            "📢 <b>You have no registered channels yet.</b>\n\n"
            "<b>How to add a channel:</b>\n"
            "1. Click the button below to add me as admin\n"
            "2. Give me <b>Invite Users</b> + <b>Manage Channel</b> permissions\n"
            "3. Enable <b>Join Requests</b> in your channel settings\n"
            "4. Then use /addchannel @yourchannel\n\n"
            "<i>Works with private channels too!</i>"
        )
        kb = [
            [add_btn],
            [InlineKeyboardButton("❓ Help", callback_data="how_to_add_channel")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")],
        ]
    else:
        text = "📢 <b>Your Channels:</b>\n\nSelect one to manage:"
        kb = [[InlineKeyboardButton(
            f"📢 {ch.get('channel_title', 'Unknown')}",
            callback_data=f"ch_settings_{ch['channel_id']}"
        )] for ch in channels]
        kb.append([add_btn])
        kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")])

    rm = InlineKeyboardMarkup(kb)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode='HTML', reply_markup=rm)
        except BadRequest:
            pass
    else:
        await update.message.reply_html(text, reply_markup=rm)


# ═══════════════════════════════════════════════════════════════════════════════
# /schedulepost
# ═══════════════════════════════════════════════════════════════════════════════

async def schedule_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Please use /schedulepost in private chat.")
        return
    if not context.args or len(context.args) < 4:
        await message.reply_html(
            "❌ <b>Usage:</b>\n"
            "/schedulepost @channel YYYY-MM-DD HH:MM Your message\n\n"
            "<b>Example:</b>\n"
            "/schedulepost @mychannel 2025-12-25 09:00 Merry Christmas! 🎄"
        )
        return
    channel_ref, date_str, time_str = context.args[0], context.args[1], context.args[2]
    content = " ".join(context.args[3:])
    try:
        sdt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.reply_text("❌ Invalid date/time. Use YYYY-MM-DD HH:MM (UTC)")
        return
    if sdt <= datetime.now(timezone.utc):
        await message.reply_text("❌ Scheduled time must be in the future!")
        return
    try:
        channel_chat = await context.bot.get_chat(channel_ref)
    except Exception as e:
        await message.reply_text(f"❌ Channel not found: {e}")
        return
    settings = await get_channel_settings(channel_chat.id)
    if not settings or settings.get('added_by') != message.from_user.id:
        await message.reply_text("❌ You don't manage this channel. Use /addchannel first.")
        return
    await save_scheduled_post(channel_chat.id, content, sdt.isoformat(), message.from_user.id)
    await message.reply_html(
        f"✅ <b>Post Scheduled!</b>\n\n"
        f"📢 Channel: <b>{channel_chat.title}</b>\n"
        f"⏰ Time: <b>{date_str} {time_str} UTC</b>\n\n"
        f"📝 Preview:\n<i>{content[:200]}</i>"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MODERATION COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /warn <username/ID> <reason>\nExample: /warn @user123 Spamming")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:])
    target_user, target_username = None, None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user    = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user    = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            tid = int(target)
            cm  = await context.bot.get_chat_member(chat.id, tid)
            target_user    = cm.user
            target_username = cm.user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    warned_by = message.from_user.id if message.from_user else 0
    await add_warning(chat.id, target_user.id, warned_by, reason, target_username)
    warnings      = await get_user_warnings(chat.id, target_user.id)
    warning_count = len(warnings)
    settings      = await get_group_settings(chat.id)
    max_warnings  = settings.get('max_warnings', 3) if settings else 3
    user_mention  = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    warn_msg = (
        f"⚠️ <b>WARNING #{warning_count}/{max_warnings}</b>\n"
        f"👤 User: {user_mention}\n🛡️ Admin: {admin_mention}\n📝 Reason: {reason}\n\n"
        f"This user has been warned by admin."
    )
    if warning_count >= max_warnings:
        mute_reason = await generate_mute_reason_with_gemini(warning_count, warnings, "Maximum warnings reached")
        until_date  = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        try:
            await context.bot.restrict_chat_member(
                chat.id, target_user.id,
                ChatPermissions(can_send_messages=False, can_send_photos=False,
                                can_send_videos=False, can_send_documents=False,
                                can_send_audios=False, can_send_voice_notes=False,
                                can_send_video_notes=False, can_send_polls=False),
                until_date=until_date)
            await add_mute(chat.id, target_user.id, warned_by, mute_reason, 60, target_username)
            kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{target_user.id}_{chat.id}")]]
            await message.reply_html(
                f"🔇 <b>USER MUTED (AUTO)</b>\n👤 {user_mention}\n🛡️ Admin\n"
                f"⏱ 1 hour\n📝 {mute_reason}\n\nReached {max_warnings} warnings.",
                reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logger.error(f"Auto-mute: {e}")
    kb = [
        [InlineKeyboardButton("🚫 Ban User",   callback_data=f"ban_from_warn_{target_user.id}_{chat.id}")],
        [InlineKeyboardButton("🔇 Mute User",  callback_data=f"mute_from_warn_{target_user.id}_{chat.id}")]
    ]
    await message.reply_html(warn_msg, reply_markup=InlineKeyboardMarkup(kb))


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ Admins only!")
        return
    if not context.args:
        await message.reply_text("❌ Usage: /ban <username/ID> <reason>")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
    target_user, target_username = None, None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user    = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            raw = target.lstrip("@")
            cm  = await context.bot.get_chat_member(chat.id, int(raw) if raw.isdigit() else raw)
            target_user    = cm.user
            target_username = cm.user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    if await get_active_ban(chat.id, target_user.id):
        await message.reply_text("❌ Already banned!")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target_user.id)
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}"); return
    banned_by = message.from_user.id if message.from_user else 0
    await add_ban(chat.id, target_user.id, banned_by, reason, target_username)
    kb = [[InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{target_user.id}_{chat.id}")]]
    await message.reply_html(
        f"🚫 <b>USER BANNED</b>\n👤 {target_user.mention_html()}\n"
        f"🛡️ {'Anonymous Admin' if not message.from_user else message.from_user.mention_html()}\n"
        f"📝 {reason}",
        reply_markup=InlineKeyboardMarkup(kb))


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ Admins only!")
        return
    if not context.args:
        await message.reply_text("❌ Usage: /unban <username/ID>")
        return
    target = context.args[0]
    uid    = None
    if target.startswith("@"):
        r = supabase.table('bans').select("*").eq('chat_id', chat.id).eq('username', target[1:]).eq('is_active', True).execute()
        if r.data: uid = r.data[0]['user_id']
    else:
        try: uid = int(target)
        except ValueError: pass
    if not uid or not await get_active_ban(chat.id, uid):
        await message.reply_text("❌ User not found or not banned!"); return
    try:
        await context.bot.unban_chat_member(chat.id, uid)
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}"); return
    await unban_user_in_db(chat.id, uid)
    await message.reply_text("✅ User has been unbanned successfully!")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ Admins only!")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /mute <username/ID> <duration> <reason>\nDuration: 10m, 1h, 1d, 1w")
        return
    target       = context.args[0]
    duration_str = context.args[1]
    reason       = " ".join(context.args[2:]) if len(context.args) > 2 else "No reason provided"
    duration_sec = parse_duration(duration_str)
    if duration_sec == 0:
        await message.reply_text("❌ Invalid duration! Use 10m, 1h, 1d, 1w"); return
    if duration_sec > 366 * 86400:
        await message.reply_text("❌ Duration exceeds 366 days!"); return
    target_user, target_username = None, None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user    = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            raw = target.lstrip("@")
            cm  = await context.bot.get_chat_member(chat.id, int(raw) if raw.isdigit() else raw)
            target_user    = cm.user
            target_username = cm.user.username
        except Exception:
            await message.reply_text("❌ Could not find user."); return
    if not target_user:
        await message.reply_text("❌ User not found."); return
    if await get_active_mute(chat.id, target_user.id):
        await message.reply_text("❌ Already muted!"); return
    until_date = int((datetime.now(timezone.utc) + timedelta(seconds=duration_sec)).timestamp())
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_user.id,
            ChatPermissions(can_send_messages=False, can_send_photos=False,
                            can_send_videos=False, can_send_documents=False,
                            can_send_audios=False, can_send_voice_notes=False,
                            can_send_video_notes=False, can_send_polls=False),
            until_date=until_date)
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}"); return
    muted_by  = message.from_user.id if message.from_user else 0
    warnings  = await get_user_warnings(chat.id, target_user.id)
    mute_reason = await generate_mute_reason_with_gemini(len(warnings), warnings, f"Manual mute: {reason}")
    await add_mute(chat.id, target_user.id, muted_by, mute_reason, duration_sec // 60, target_username)
    if duration_sec >= 604800:
        w = duration_sec // 604800; disp = f"{w} week{'s' if w>1 else ''}"
    elif duration_sec >= 86400:
        d = duration_sec // 86400;  disp = f"{d} day{'s' if d>1 else ''}"
    elif duration_sec >= 3600:
        h = duration_sec // 3600; m = (duration_sec % 3600) // 60
        disp = f"{h}h {m}m" if m else f"{h}h"
    else:
        disp = f"{duration_sec//60}m"
    kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{target_user.id}_{chat.id}")]]
    await message.reply_html(
        f"🔇 <b>USER MUTED</b>\n👤 {target_user.mention_html()}\n"
        f"🛡️ {'Anonymous Admin' if not message.from_user else message.from_user.mention_html()}\n"
        f"⏱ {disp}\n📝 {mute_reason}",
        reply_markup=InlineKeyboardMarkup(kb))


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ Admins only!")
        return
    if not context.args:
        await message.reply_text("❌ Usage: /unmute <username/ID>")
        return
    target = context.args[0]
    uid    = None
    if target.startswith("@"):
        r = supabase.table('mutes').select("*").eq('chat_id', chat.id).eq('username', target[1:]).eq('is_active', True).execute()
        if r.data: uid = r.data[0]['user_id']
    else:
        try: uid = int(target)
        except ValueError: pass
    if not uid or not await get_active_mute(chat.id, uid):
        await message.reply_text("❌ User not muted!"); return
    try:
        await context.bot.restrict_chat_member(
            chat.id, uid,
            ChatPermissions(can_send_messages=True, can_send_photos=True,
                            can_send_videos=True, can_send_documents=True,
                            can_send_audios=True, can_send_voice_notes=True,
                            can_send_video_notes=True, can_send_polls=True),
            until_date=0)
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}"); return
    await unmute_user_in_db(chat.id, uid)
    try:
        m = await context.bot.get_chat_member(chat.id, uid)
        mention = f"@{m.user.username}" if m.user and m.user.username else m.user.mention_html() if m.user else f"User {uid}"
    except Exception:
        mention = f"User {uid}"
    await message.reply_html(f"✅ <b>User Unmuted</b>\n\n{mention} can now send messages.")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /report <username> <reason>\n\nYour report will be sent to admins privately.")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:])
    target_user, target_username = None, None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user    = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user    = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            tid = int(target)
            cm  = await context.bot.get_chat_member(chat.id, tid)
            target_user    = cm.user
            target_username = cm.user.username
        except Exception:
            await message.reply_text("❌ Could not find user."); return
    if not target_user:
        await message.reply_text("❌ User not found."); return
    try:
        tm = await context.bot.get_chat_member(chat.id, target_user.id)
        if tm.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text("❌ You cannot report an admin!"); return
    except Exception:
        pass
    await add_report(chat.id, message.from_user.id, target_user.id, reason,
                     message.from_user.username or message.from_user.first_name, target_username)
    await message.reply_text("✅ Report sent to admins. Thank you for helping keep the group safe!")
    admins = await get_chat_admins(chat.id, context)
    report_msg = (
        f"🚨 <b>NEW REPORT</b>\n📱 Group: {chat.title}\n"
        f"👤 Reported: {target_user.mention_html()}\n"
        f"📝 Reporter: {message.from_user.mention_html()}\n"
        f"📋 Reason: {reason}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    for aid in admins:
        try:
            await context.bot.send_message(aid, report_msg, parse_mode='HTML')
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# MODERATION CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def unban_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); user_id = int(parts[2]); chat_id = int(parts[3])
    ok, _, msg = await verify_callback_admin(chat_id, q, context)
    if not ok: await q.answer(msg, show_alert=True); return
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        await unban_user_in_db(chat_id, user_id)
        try:
            m = await context.bot.get_chat_member(chat_id, user_id)
            mention = f"@{m.user.username}" if m.user and m.user.username else f"User {user_id}"
        except Exception:
            mention = f"User {user_id}"
        try:
            await q.message.edit_text(f"✅ <b>User Unbanned</b>\n\n{mention} can rejoin.", reply_markup=None, parse_mode='HTML')
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


async def unmute_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); user_id = int(parts[2]); chat_id = int(parts[3])
    ok, _, msg = await verify_callback_admin(chat_id, q, context)
    if not ok: await q.answer(msg, show_alert=True); return
    try:
        await context.bot.restrict_chat_member(
            chat_id, user_id,
            ChatPermissions(can_send_messages=True, can_send_photos=True,
                            can_send_videos=True, can_send_documents=True,
                            can_send_audios=True, can_send_voice_notes=True,
                            can_send_video_notes=True, can_send_polls=True), until_date=0)
        await unmute_user_in_db(chat_id, user_id)
        try:
            m = await context.bot.get_chat_member(chat_id, user_id)
            mention = f"@{m.user.username}" if m.user and m.user.username else f"User {user_id}"
        except Exception:
            mention = f"User {user_id}"
        try:
            await q.message.edit_text(f"✅ <b>User Unmuted</b>\n\n{mention} can send messages.", reply_markup=None, parse_mode='HTML')
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


async def ban_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); user_id = int(parts[3]); chat_id = int(parts[4])
    ok, clicker, msg = await verify_callback_admin(chat_id, q, context)
    if not ok: await q.answer(msg, show_alert=True); return
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await add_ban(chat_id, user_id, clicker or 0, "Banned from warning")
        kb = [[InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user_id}_{chat_id}")]]
        try:
            await q.message.edit_text(f"🚫 User {user_id} banned!", reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


async def mute_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); user_id = int(parts[3]); chat_id = int(parts[4])
    ok, clicker, msg = await verify_callback_admin(chat_id, q, context)
    if not ok: await q.answer(msg, show_alert=True); return
    try:
        until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        await context.bot.restrict_chat_member(
            chat_id, user_id,
            ChatPermissions(can_send_messages=False, can_send_photos=False,
                            can_send_videos=False, can_send_documents=False,
                            can_send_audios=False, can_send_voice_notes=False,
                            can_send_video_notes=False, can_send_polls=False), until_date=until_date)
        warnings    = await get_user_warnings(chat_id, user_id)
        mute_reason = await generate_mute_reason_with_gemini(len(warnings), warnings, "Muted from warning")
        await add_mute(chat_id, user_id, clicker or 0, mute_reason, 60)
        kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat_id}")]]
        try:
            await q.message.edit_text(f"🔇 User muted 1h!\nReason: {mute_reason}", reply_markup=InlineKeyboardMarkup(kb))
        except BadRequest:
            pass
    except Exception as e:
        await q.answer(f"❌ Error: {e}", show_alert=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN KEYBOARD
# ═══════════════════════════════════════════════════════════════════════════════

async def show_admin_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat    = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ Admins only!")
        return
    kb = [
        [InlineKeyboardButton("⚠️ Warn",    callback_data=f"cmd_warn_{chat.id}"),
         InlineKeyboardButton("🔇 Mute",    callback_data=f"cmd_mute_{chat.id}"),
         InlineKeyboardButton("🚫 Ban",     callback_data=f"cmd_ban_{chat.id}")],
        [InlineKeyboardButton("🔊 Unmute",  callback_data=f"cmd_unmute_{chat.id}"),
         InlineKeyboardButton("✅ Unban",   callback_data=f"cmd_unban_{chat.id}")],
        [InlineKeyboardButton("📋 Reports", callback_data=f"cmd_reports_{chat.id}"),
         InlineKeyboardButton("📢 Tag All", callback_data=f"cmd_tagall_{chat.id}")]
    ]
    await message.reply_html(
        "🛡️ <b>Admin Commands</b>\n\n"
        "⚠️ Warn · 🔇 Mute · 🚫 Ban\n🔊 Unmute · ✅ Unban\n📋 Reports · 📢 Tag All\n\n"
        "<i>This keyboard is only visible to admins.</i>",
        reply_markup=InlineKeyboardMarkup(kb))


async def admin_keyboard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts   = q.data.split("_")
    if len(parts) < 3: return
    command = parts[1]
    chat_id = int(parts[2])
    ok, _, msg = await verify_callback_admin(chat_id, q, context)
    if not ok: await q.answer(msg, show_alert=True); return
    instructions = {
        "warn":    "/warn @user reason",
        "mute":    "/mute @user 1h reason",
        "ban":     "/ban @user reason",
        "unmute":  "/unmute @user  (resets warnings to 0)",
        "unban":   "/unban <user_id>",
        "reports": "Reports are sent privately to admins. Check your DMs.",
        "tagall":  "/tagall Your announcement",
    }
    try:
        await q.message.reply_html(f"<b>{command.title()} usage:</b>\n{instructions.get(command, 'Unknown')}")
    except Exception as e:
        logger.error(f"admin_keyboard_callback: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TAG ALL / NOTES / FORCE SUB / FILTER DELETED
# ═══════════════════════════════════════════════════════════════════════════════

async def tag_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    note_text = " ".join(context.args) if context.args else "Attention everyone!"
    members   = await get_group_members(chat.id)
    if not members:
        await msg.reply_text("No members tracked yet."); return
    mentions = []
    for m in members:
        if m.get("username"):
            mentions.append(f'<a href="https://t.me/{m["username"]}">@{m["username"]}</a>')
        else:
            mentions.append(f'<a href="tg://user?id={m["user_id"]}">{m.get("first_name") or "User"}</a>')
    for idx, chunk in enumerate([mentions[i:i+30] for i in range(0, len(mentions), 30)]):
        text = (note_text + "\n\n" if idx == 0 else "") + " ".join(chunk)
        try:
            await chat.send_message(text, parse_mode="HTML")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"tagall chunk {idx}: {e}")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    if not context.args or len(context.args) < 2:
        await msg.reply_text("Usage: /note <name> <content>"); return
    await add_note(chat.id, context.args[0].lower(), " ".join(context.args[1:]), msg.from_user.id if msg.from_user else 0)
    await msg.reply_html(f"📝 Note <b>{context.args[0]}</b> saved.")


async def get_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not context.args:
        await msg.reply_text("Usage: /get <name>"); return
    note = await get_note(chat.id, context.args[0].lower())
    if note: await msg.reply_html(f"📝 <b>{context.args[0]}</b>\n\n{note['content']}")
    else:    await msg.reply_text(f"❌ No note named '{context.args[0]}'.")


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message; chat = msg.chat
    notes = await get_all_notes(chat.id)
    if notes:
        lines = "\n".join(f"- <code>{n['name']}</code>" for n in notes)
        await msg.reply_html(f"<b>Notes ({len(notes)}):</b>\n{lines}\n\nUse /get &lt;name&gt; to retrieve.")
    else:
        await msg.reply_text("No notes yet. Use /note <name> <content>.")


async def delnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    if not context.args:
        await msg.reply_text("Usage: /delnote <name>"); return
    await delete_note(chat.id, context.args[0].lower())
    await msg.reply_html(f"🗑 Note <b>{context.args[0]}</b> deleted.")


async def forcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    if not context.args:
        channels = await get_active_force_subs(chat.id)
        if not channels: await msg.reply_text("No force-subscribe channels.\nUsage: /forcesub @channel")
        else:
            lines = "\n".join(f"- {c['channel_title']} (@{c.get('channel_username') or c['channel_id']})" for c in channels)
            await msg.reply_html(f"<b>Force Subscribe Channels:</b>\n{lines}")
        return
    try:
        co = await context.bot.get_chat(context.args[0])
        bm = await context.bot.get_chat_member(co.id, context.bot.id)
        if bm.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await msg.reply_text("I must be a member of that channel first."); return
        await add_force_sub(chat.id, co.id, co.title or context.args[0], co.username, msg.from_user.id if msg.from_user else 0)
        await msg.reply_html(f"✅ Force subscribe enabled for <b>{co.title}</b>.\nMembers must join before chatting.")
    except Exception as e:
        logger.error(f"forcesub: {e}")
        await msg.reply_text("Could not access that channel. Make sure I'm a member.")


async def removeforcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    if not context.args:
        await msg.reply_text("Usage: /removeforcesub @channel"); return
    try:
        co = await context.bot.get_chat(context.args[0])
        await remove_force_sub(chat.id, co.id)
        await msg.reply_html(f"✅ Force subscribe removed for <b>{co.title}</b>.")
    except Exception as e:
        await msg.reply_text(f"❌ Failed: {e}")


async def filter_deleted_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    members = await get_group_members(chat.id)
    removed = 0
    for m in members:
        try:
            cm = await context.bot.get_chat_member(chat.id, m["user_id"])
            if is_deleted_account(cm.user):
                try:
                    await context.bot.ban_chat_member(chat.id, cm.user.id)
                    await context.bot.unban_chat_member(chat.id, cm.user.id)
                except Exception:
                    pass
                await remove_group_member(chat.id, cm.user.id)
                removed += 1
        except (Forbidden, BadRequest):
            await remove_group_member(chat.id, m["user_id"])
            removed += 1
        except Exception:
            pass
    await msg.reply_html(f"✅ Done. Removed <b>{removed}</b> deleted/ghost accounts.")


# ═══════════════════════════════════════════════════════════════════════════════
# /setwelcome  — set welcome message directly inside group
# ═══════════════════════════════════════════════════════════════════════════════

async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a group welcome message directly in the group with a live preview."""
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    if not context.args:
        await msg.reply_html(
            "📝 <b>Set Welcome Message</b>\n\n"
            "<b>Usage:</b> /setwelcome Your message\n\n"
            "<b>Variables:</b>\n"
            "• <code>{USER_NAME}</code> — first name\n"
            "• <code>{USER_ID}</code> — Telegram ID\n"
            "• <code>{CHAT_TITLE}</code> — group name\n"
            "• <code>{BOT_NAME}</code> — bot username\n\n"
            "<b>Buttons:</b> <code>[Text](https://link)</code>\n\n"
            "<b>HTML tags:</b> &lt;b&gt; &lt;i&gt; &lt;code&gt; &lt;u&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/setwelcome 👋 Welcome &lt;b&gt;{USER_NAME}&lt;/b&gt; to {CHAT_TITLE}!\n\n"
            "[📋 Rules](https://t.me/c/123/5) [💬 Support](https://t.me/support)</code>"
        )
        return
    welcome_text = " ".join(context.args)
    await update_welcome_message(chat.id, welcome_text, 0)
    bot_name  = context.bot.username or "Bot"
    user_name = msg.from_user.first_name if msg.from_user else "Member"
    user_id   = msg.from_user.id if msg.from_user else 0
    preview, buttons = parse_welcome_template(welcome_text, bot_name, user_name, user_id, chat.title)
    rm = build_inline_keyboard(buttons)
    await msg.reply_html(f"✅ <b>Welcome message saved!</b>\n\n<b>Preview:</b>\n\n{preview}", reply_markup=rm)


async def clearwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message; chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only."); return
    await update_welcome_message(chat.id, None, 0)
    await msg.reply_text("✅ Welcome message cleared. Default welcome will be used.")


# ═══════════════════════════════════════════════════════════════════════════════
# FORCE SUB CHECK + MESSAGE
# ═══════════════════════════════════════════════════════════════════════════════

async def check_force_sub(chat_id: int, user_id: int, context) -> list:
    import time
    now = time.monotonic()
    cached = _force_sub_cache.get(chat_id)
    if cached and (now - cached[1]) < _FORCE_SUB_TTL:
        channels = cached[0]
    else:
        channels = await get_active_force_subs(chat_id)
        _force_sub_cache[chat_id] = (channels, now)
    if not channels:
        return []
    not_joined = []
    async def _check(fc):
        key = (chat_id, user_id, fc["channel_id"])
        cm = _membership_cache.get(key)
        if cm and (now - cm[1]) < _MEMBERSHIP_TTL:
            is_mem = cm[0]
        else:
            try:
                m = await context.bot.get_chat_member(fc["channel_id"], user_id)
                is_mem = m.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
            except Exception:
                is_mem = False
            _membership_cache[key] = (is_mem, now)
        if not is_mem:
            not_joined.append(fc)
    await asyncio.gather(*[_check(fc) for fc in channels])
    return not_joined


async def send_force_sub_message(chat, user, not_joined: list, context, warning_timer=60):
    try:
        user_link = get_user_mention_html(user)
        ch_names  = ", ".join(f"<b>{fc['channel_title']}</b>" for fc in not_joined)
        text      = (f"{user_link}, you must join the required channel(s) before chatting.\n\n"
                     f"Please join: {ch_names}\n\nThen send your message again.")
        kb = []
        for fc in not_joined:
            link = (f"https://t.me/{fc['channel_username']}"
                    if fc.get("channel_username")
                    else f"https://t.me/c/{str(fc['channel_id']).replace('-100', '')}")
            kb.append([InlineKeyboardButton(f"Join {fc['channel_title']}", url=link)])
        msg = await chat.send_message(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        if warning_timer > 0:
            await schedule_message_deletion(chat.id, msg.message_id, warning_timer)
    except Exception as e:
        logger.error(f"send_force_sub_message: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TOGGLE / PROMPT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _toggle(update, context, field):
    q = update.callback_query; await q.answer()
    cid = _cid(q.data)
    s   = await get_group_settings(cid) or {}
    nv  = not s.get(field, False)
    await update_setting(cid, **{field: nv})
    await q.answer(f"{field.replace('_', ' ').title()}: {'ON' if nv else 'OFF'}", show_alert=True)
    q.data = f"group_settings_{cid}"
    await group_settings_handler(update, context)


async def toggle_sticker_handler(update, context):
    await _toggle(update, context, "sticker_protect")

async def toggle_autoapprove_handler(update, context):
    await _toggle(update, context, "auto_approve")


# ═══════════════════════════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user         = update.effective_user
    bot_username = context.bot.username or "GroupPilotBot"

    # ── Deep-link: ?start=channel_{id}  sent after bot is started from channel link
    if context.args:
        payload = context.args[0]
        if payload.startswith("channel_"):
            try:
                channel_id = int(payload.split("_", 1)[1])
                settings   = await get_channel_settings(channel_id)
                if settings:
                    already = await is_user_onboarded(channel_id, user.id)
                    if not already:
                        ch = await context.bot.get_chat(channel_id)
                        sent = await send_channel_welcome_dm(context.bot, user, channel_id, ch.title, settings)
                        if sent:
                            await record_user_onboarded(channel_id, user.id)
                    return   # don't show main menu
            except Exception as e:
                logger.error(f"Deep-link start error: {e}")

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Add to Group",
                url=f"https://t.me/{bot_username}?startgroup=true"
            ),
            InlineKeyboardButton(
                "📢 Add to Channel",
                url=f"https://t.me/{bot_username}?startchannel=true"
                    f"&admin=post_messages+edit_messages+delete_messages+invite_users"
            ),
        ],
        [
            InlineKeyboardButton("📋 My Groups",   callback_data="my_groups"),
            InlineKeyboardButton("📢 My Channels", callback_data="my_channels"),
        ],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    welcome_text = (
        f"👋 Welcome {user.mention_html()}!\n\n"
        "<b>GroupPilot</b> — Intelligent Group &amp; Channel Management\n\n"
        "<b>Group Features:</b>\n"
        "✅ Auto-delete banned words, links, promotions\n"
        "✅ Warn / Mute / Ban with inline buttons\n"
        "✅ Auto-mute at configurable warning limit (3–31)\n"
        "✅ Custom welcome messages with HTML &amp; buttons\n"
        "✅ Force-subscribe channel enforcement\n"
        "✅ Tag all members, notes system\n"
        "✅ Sticker protection, join-request management\n\n"
        "<b>Channel Features (NEW):</b>\n"
        "✅ Auto-approve join requests instantly\n"
        "✅ Private welcome DM to every new member\n"
        "✅ Manual approval mode with admin buttons\n"
        "✅ Analytics (daily / weekly joins)\n"
        "✅ Scheduled posts\n"
        "✅ Works with private &amp; public channels\n\n"
        "🚀 Click a button below to get started!"
    )
    if update.message:
        await update.message.reply_html(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except BadRequest:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════════════════════════

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>GroupPilot Commands</b>\n\n"
        "<b>Group Admin Commands:</b>\n"
        "/warn @user reason — Warn user (auto-mutes at limit)\n"
        "/mute @user dur reason — Mute (10m, 1h, 1d, 1w)\n"
        "/unmute @user — Unmute\n"
        "/ban @user reason — Ban\n"
        "/unban ID — Unban\n"
        "/admin — Admin keyboard\n"
        "/setwelcome message — Set welcome (with live preview)\n"
        "/clearwelcome — Remove welcome message\n"
        "/tagall [msg] — Mention all tracked members\n"
        "/note name text — Save note\n"
        "/get name — Read note\n"
        "/notes — List notes\n"
        "/delnote name — Delete note\n"
        "/forcesub @ch — Require subscription\n"
        "/removeforcesub @ch — Remove requirement\n"
        "/filterdeleted — Kick ghost accounts\n\n"
        "<b>Welcome Message Variables:</b>\n"
        "<code>{USER_NAME}</code> <code>{USER_ID}</code> "
        "<code>{CHAT_TITLE}</code> <code>{BOT_NAME}</code>\n"
        "Buttons: <code>[Text](https://link)</code>\n\n"
        "<b>Member Commands:</b>\n"
        "/report @user reason — Report to admins\n"
        "/get name — Read a note\n\n"
        "<b>Channel Commands (private chat):</b>\n"
        "/addchannel @channel — Register channel (supports private)\n"
        "/mychannels — View your channels\n"
        "/schedulepost @ch DATE TIME msg — Schedule post\n\n"
        "<b>Private Chat:</b>\n"
        "/start — Main menu\n"
        "/mygroups — Manage groups\n"
        "/help — This message"
    )
    if update.message:
        await update.message.reply_html(help_text)
    elif update.callback_query:
        try:
            await update.callback_query.message.edit_text(help_text, parse_mode="HTML")
        except BadRequest:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# MY GROUPS HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id      = update.effective_user.id
    groups       = await get_user_groups(user_id)
    bot_username = context.bot.username or "GroupPilotBot"

    if not groups:
        text = "❌ You haven't added me to any groups yet!"
        kb   = [
            [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
        ]
    else:
        text = "📋 <b>Your Groups:</b>\n\nSelect a group to manage settings:"
        kb   = [[InlineKeyboardButton(f"🔧 {g['chat_title']}", callback_data=f"group_settings_{g['chat_id']}")] for g in groups]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main")])

    rm = InlineKeyboardMarkup(kb)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=rm, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"my_groups_handler: {e}")
    else:
        await update.message.reply_html(text, reply_markup=rm)


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP SETTINGS HANDLER  — uses _cid() everywhere so no more ValueError
# ═══════════════════════════════════════════════════════════════════════════════

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id  = _cid(q.data)          # ← FIXED: always uses rsplit last segment
    settings = await get_group_settings(chat_id)
    if not settings:
        try: await q.message.edit_text("❌ Group not found!")
        except BadRequest: pass
        return

    banned_words = await get_banned_words(chat_id)
    bw_text = ", ".join(banned_words) if banned_words else "None"
    def yn(v): return "✅ ON" if v else "❌ OFF"
    wl  = settings.get('max_word_count', 0)
    tv  = settings.get('warning_timer', 30)
    wt  = settings.get('welcome_timer', 0)
    mw  = settings.get('max_warnings', 3)

    text = (
        f"⚙️ <b>Group Settings</b>\n"
        f"📱 {settings['chat_title']}\n"
        f"👤 Added by: @{settings.get('added_by_username', 'N/A')}\n"
        f"👥 Members: {settings.get('member_count', 0)}\n\n"
        f"🎉 Welcome Msg: {yn(settings.get('welcome_message'))} "
        f"(Delete: {f'{wt}s' if wt else 'Never'})\n"
        f"🚫 Banned Words: {bw_text}\n"
        f"📝 Word Limit: {f'{wl} words' if wl else '❌ OFF'}\n"
        f"📨 Del Promotions: {yn(settings.get('delete_promotions'))}\n"
        f"🌐 Del Links: {yn(settings.get('delete_links'))}\n"
        f"⏱ Warn Timer: {f'{tv//60}m' if tv>=60 else f'{tv}s'}\n"
        f"⚠️ Max Warnings: {mw} (auto-mute at limit)\n"
        f"👋 Del Join Msgs: {yn(settings.get('delete_join_messages'))}\n"
        f"🔐 Require Approval: {yn(settings.get('require_approval'))}\n"
        f"✅ Auto Approve: {yn(settings.get('auto_approve'))}\n"
        f"🎭 Sticker Protect: {yn(settings.get('sticker_protect'))}\n"
        f"📢 Force Sub: {f\"@{settings['force_sub_channel']}\" if settings.get('force_sub_channel') else '❌ Not Set'}\n"
    )
    keyboard = [
        [InlineKeyboardButton("🎉 Set Welcome Message",   callback_data=f"set_welcome_{chat_id}")],
        [InlineKeyboardButton("➕ Add Banned Word",        callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("➖ Remove Word",            callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("📝 Word Count Limit",       callback_data=f"set_word_limit_{chat_id}"),
         InlineKeyboardButton("⏱ Warning Timer",           callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton("📨 Toggle Promotions",      callback_data=f"toggle_promo_{chat_id}"),
         InlineKeyboardButton("🌐 Toggle Links",            callback_data=f"toggle_links_{chat_id}")],
        [InlineKeyboardButton("⚠️ Max Warnings",           callback_data=f"set_max_warnings_{chat_id}"),
         # FIXED: renamed to toggle_joindel_ (no extra underscores) to avoid _cid crash
         InlineKeyboardButton("👋 Toggle Join Delete",     callback_data=f"toggle_joindel_{chat_id}")],
        [InlineKeyboardButton("🎭 Sticker Protect",        callback_data=f"toggle_sticker_{chat_id}"),
         InlineKeyboardButton("✅ Auto Approve",            callback_data=f"toggle_autoapprove_{chat_id}")],
        [InlineKeyboardButton("🔙 Back to Groups",         callback_data="my_groups")]
    ]
    try:
        await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"group_settings_handler edit: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS INPUT HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def set_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = _cid(q.data)
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'set_welcome'
    text = (
        "🎉 <b>Set Group Welcome Message</b>\n\n"
        "<b>Variables:</b>\n"
        "• <code>{USER_NAME}</code> — first name\n"
        "• <code>{USER_ID}</code> — Telegram ID\n"
        "• <code>{CHAT_TITLE}</code> — group name\n"
        "• <code>{BOT_NAME}</code> — bot username\n\n"
        "<b>Inline Buttons:</b> <code>[Text](https://link)</code>\n\n"
        "<b>HTML tags:</b> &lt;b&gt; &lt;i&gt; &lt;code&gt; &lt;u&gt; &lt;s&gt;\n\n"
        "<b>Example:</b>\n"
        "<code>👋 Hey &lt;b&gt;{USER_NAME}&lt;/b&gt;, welcome to {CHAT_TITLE}!\n\n"
        "[📋 Rules](https://t.me/c/123/5) [💬 Support](https://t.me/support)</code>\n\n"
        "⏱ After sending I'll ask for the auto-delete timer.\n"
        "✏️ Send your message now:"
    )
    try: await q.message.edit_text(text, parse_mode='HTML')
    except BadRequest: pass


async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = _cid(q.data)
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'add_word'
    try: await q.message.edit_text("✏️ Send the word you want to ban.\n\n💡 /cancel to cancel.")
    except BadRequest: pass


async def remove_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id      = _cid(q.data)
    banned_words = await get_banned_words(chat_id)
    if not banned_words:
        await q.answer("No banned words to remove!", show_alert=True); return
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'remove_word'
    try:
        await q.message.edit_text(
            f"✏️ Banned words: {', '.join(banned_words)}\n\nSend the word to remove.\n\n💡 /cancel")
    except BadRequest: pass


async def set_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = _cid(q.data)
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'set_timer'
    try:
        await q.message.edit_text(
            "⏱ <b>Warning Deletion Timer</b>\nExamples: <code>5s</code> <code>1m</code> <code>30</code>\n\n✏️ Send duration:",
            parse_mode='HTML')
    except BadRequest: pass


async def set_word_limit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = _cid(q.data)
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'set_word_limit'
    try:
        await q.message.edit_text(
            "📝 <b>Max Word Count</b>\nExamples: <code>100</code> <code>35</code> <code>0</code> (unlimited)\n\n✏️ Send number:",
            parse_mode='HTML')
    except BadRequest: pass


async def set_max_warnings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = _cid(q.data)
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action']         = 'set_max_warnings'
    try:
        await q.message.edit_text(
            "⚠️ <b>Max Warnings Before Auto-Mute (3–31)</b>\n\n✏️ Send a number (3–31):",
            parse_mode='HTML')
    except BadRequest: pass


async def toggle_promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id  = _cid(q.data)
    settings = await get_group_settings(chat_id)
    new_val  = not settings.get('delete_promotions', False)
    await update_promotion_setting(chat_id, new_val)
    await q.answer(f"Promotion deletion {'enabled' if new_val else 'disabled'}!", show_alert=True)
    q.data = f"group_settings_{chat_id}"
    await group_settings_handler(update, context)


async def toggle_links_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id  = _cid(q.data)
    settings = await get_group_settings(chat_id)
    new_val  = not settings.get('delete_links', False)
    await update_link_setting(chat_id, new_val)
    await q.answer(f"Link deletion {'enabled' if new_val else 'disabled'}!", show_alert=True)
    q.data = f"group_settings_{chat_id}"
    await group_settings_handler(update, context)


async def toggle_join_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles callback toggle_joindel_{chat_id}  — no ambiguous underscores."""
    q = update.callback_query; await q.answer()
    chat_id  = _cid(q.data)      # ← safe; last segment is always the int
    settings = await get_group_settings(chat_id)
    new_val  = not settings.get('delete_join_messages', False)
    await update_delete_join_messages(chat_id, new_val)
    await q.answer(f"Join message deletion {'enabled' if new_val else 'disabled'}!", show_alert=True)
    q.data = f"group_settings_{chat_id}"
    await group_settings_handler(update, context)


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE CHAT TEXT INPUT HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' not in context.user_data:
        return
    chat_id   = context.user_data['awaiting_input']
    action    = context.user_data['action']
    user_text = update.message.text.strip()
    is_ch     = action.startswith('ch_')
    text      = None

    if action == 'set_welcome':
        context.user_data['welcome_message_html'] = user_text
        context.user_data['action']               = 'set_welcome_timer'
        await update.message.reply_html(
            "⏱ <b>Set Welcome Auto-Delete Timer</b>\n"
            "Examples: <code>0</code> (never) <code>30</code> (30s) <code>1m</code>\n\n✏️ Send time:")
        return

    elif action == 'set_welcome_timer':
        welcome_html = context.user_data.get('welcome_message_html', '')
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text.strip())
        if match:
            value = int(match.group(1))
            unit  = match.group(2)
            ts    = value * 60 if unit == 'm' else value
            du    = "minutes" if unit == 'm' else "seconds"
            await update_welcome_message(chat_id, welcome_html, ts)
            # Show preview
            bot_name  = context.bot.username or "Bot"
            user_name = update.effective_user.first_name or "Member"
            preview, buttons = parse_welcome_template(welcome_html, bot_name, user_name, update.effective_user.id, "Your Group")
            rm = build_inline_keyboard(buttons)
            kb = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
            for k in ['awaiting_input', 'action', 'welcome_message_html']:
                context.user_data.pop(k, None)
            await update.message.reply_html(
                f"✅ Welcome message saved! Auto-delete: <b>{value} {du}</b>\n\n"
                f"<b>Preview:</b>\n\n{preview}",
                reply_markup=rm or InlineKeyboardMarkup(kb))
            return
        else:
            await update.message.reply_html("❌ Invalid format! Use '0', '30', or '1m'"); return

    elif action == 'add_word':
        await add_banned_word(chat_id, user_text.lower(), update.effective_user.id)
        text = f"✅ Word '<b>{user_text.lower()}</b>' added to banned words!"

    elif action == 'remove_word':
        await remove_banned_word(chat_id, user_text.lower())
        text = f"✅ Word '<b>{user_text.lower()}</b>' removed from banned words!"

    elif action == 'set_timer':
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text)
        if match:
            value = int(match.group(1)); unit = match.group(2)
            await update_warning_timer(chat_id, value * 60 if unit == 'm' else value)
            text = f"✅ Warning timer set to <b>{value} {'minutes' if unit=='m' else 'seconds'}</b>!"
        else:
            await update.message.reply_html("❌ Invalid! Use '10s' or '1m'"); return

    elif action == 'set_word_limit':
        if user_text.isdigit():
            limit = int(user_text)
            await update_word_limit(chat_id, limit)
            text = "✅ Word limit disabled." if limit == 0 else f"✅ Max words: <b>{limit}</b>!"
        else:
            await update.message.reply_html("❌ Send a number."); return

    elif action == 'set_max_warnings':
        if user_text.isdigit() and 3 <= int(user_text) <= 31:
            await update_max_warnings(chat_id, int(user_text))
            text = f"✅ Max warnings: <b>{user_text}</b>!"
        else:
            await update.message.reply_html("❌ Must be 3–31."); return

    elif action == 'ch_set_welcome':
        await upsert_channel_settings(chat_id, {"welcome_message": user_text})
        bot_name  = context.bot.username or "Bot"
        user_name = update.effective_user.first_name or "Member"
        preview, buttons = parse_welcome_template(user_text, bot_name, user_name, update.effective_user.id, "Your Channel")
        rm = build_inline_keyboard(buttons)
        kb = [[InlineKeyboardButton("🔙 Back", callback_data=f"ch_settings_{chat_id}")]]
        for k in ['awaiting_input', 'action']:
            context.user_data.pop(k, None)
        await update.message.reply_html(
            f"✅ Channel welcome DM saved!\n\n<b>Preview:</b>\n\n{preview}",
            reply_markup=rm or InlineKeyboardMarkup(kb))
        return

    elif action == 'ch_set_delay':
        if user_text.isdigit():
            await upsert_channel_settings(chat_id, {"approval_delay": int(user_text)})
            text = f"✅ Approval delay: <b>{user_text} seconds</b>!"
        else:
            await update.message.reply_html("❌ Send a number like 0, 5, or 30."); return

    for k in ['awaiting_input', 'action', 'welcome_message_html']:
        context.user_data.pop(k, None)

    if text:
        back_cb  = f"ch_settings_{chat_id}" if is_ch else f"group_settings_{chat_id}"
        back_lbl = "🔙 Back to Channel Settings" if is_ch else "🔙 Back to Settings"
        await update.message.reply_html(
            text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(back_lbl, callback_data=back_cb)]]))


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ['awaiting_input', 'action', 'welcome_message_html']:
        context.user_data.pop(k, None)
    await update.message.reply_text("✅ Operation cancelled.")


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP WELCOME MESSAGE SENDER
# ═══════════════════════════════════════════════════════════════════════════════

async def send_welcome_message(chat, new_member, context: ContextTypes.DEFAULT_TYPE, settings: dict):
    try:
        if settings and settings.get('welcome_message'):
            welcome_html = settings['welcome_message']
            bot_name  = context.bot.username or "Bot"
            user_name = new_member.first_name or new_member.username or "Member"
            msg_text, buttons = parse_welcome_template(welcome_html, bot_name, user_name, new_member.id, chat.title)
            # Auto-translate for non-English users
            user_lang = getattr(new_member, 'language_code', None) or 'en'
            if user_lang != 'en' and GEMINI_API_KEY:
                try:
                    texts  = [msg_text] + [b[0] for b in buttons]
                    joined = "\n---\n".join(texts)
                    prompt = (f"Translate to {user_lang}, preserving HTML tags, emojis. "
                              f"Sections by --- → same order separated by ---:\n{joined}")
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
                    resp = http_requests.post(url, headers={"Content-Type": "application/json"},
                                              data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                                               "generationConfig": {"maxOutputTokens": 500}}))
                    if resp.status_code == 200:
                        parts = resp.json()['candidates'][0]['content']['parts'][0]['text'].split("\n---\n")
                        if len(parts) == len(texts):
                            msg_text = parts[0]
                            buttons  = [(parts[i + 1], buttons[i][1]) for i in range(len(buttons))]
                except Exception as e:
                    logger.error(f"Translation error: {e}")
            rm = build_inline_keyboard(buttons)
            try:
                wm = await chat.send_message(msg_text, reply_markup=rm, parse_mode='HTML')
                wt = settings.get('welcome_timer', 0)
                if wt > 0:
                    await schedule_message_deletion(chat.id, wm.message_id, wt)
            except BadRequest as e:
                logger.error(f"Welcome send error: {e}")
                try:
                    await chat.send_message(f"👋 Welcome {new_member.mention_html()} to {chat.title}!", parse_mode='HTML')
                except Exception:
                    pass
        else:
            try:
                await chat.send_message(f"👋 Welcome {new_member.mention_html()} to {chat.title}!", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Default welcome error: {e}")
    except Exception as e:
        logger.error(f"send_welcome_message: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# BOT ADDED TO GROUP
# ═══════════════════════════════════════════════════════════════════════════════

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mcm  = update.my_chat_member
    chat = mcm.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    new_m = mcm.new_chat_member
    old_m = mcm.old_chat_member
    if old_m.status == ChatMemberStatus.LEFT and new_m.status != ChatMemberStatus.LEFT:
        added_by = mcm.from_user
        try:
            member = await chat.get_member(added_by.id)
            if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                await chat.send_message("⚠️ Only group admins can add me!")
                await chat.leave(); return
        except Exception as e:
            logger.error(f"track_chat_member admin check: {e}")
            await chat.leave(); return
        try:
            bm = await chat.get_member(context.bot.id)
            bot_is_admin = bm.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            bot_is_admin = False
        if not bot_is_admin:
            await chat.send_message("⚠️ Please make me an admin with 'Delete Messages' permission!\n\nI'll leave now.")
            await chat.leave(); return
        username     = added_by.username or f"user_{added_by.id}"
        chat_username = getattr(chat, 'username', None)
        await add_group_to_db(chat.id, chat.title, added_by.id, username, bot_is_admin, chat_username)
        await chat.send_message(
            f"🎉 <b>Thank you for adding me!</b>\n\n"
            f"✅ I'm now protecting this group!\n"
            f"👤 Added by: @{username}\n\n"
            f"<b>Admin Commands:</b>\n"
            f"/warn /mute /ban /unmute /unban /admin\n"
            f"/setwelcome — Set custom welcome message\n"
            f"/note /get /notes — Notes system\n"
            f"/tagall — Tag all members\n\n"
            f"<b>Member Commands:</b>\n"
            f"/report — Report a user\n\n"
            f"⚙️ Full settings → private chat → My Groups",
            parse_mode='HTML')


# ═══════════════════════════════════════════════════════════════════════════════
# USER JOINS / LEAVES GROUP
# ═══════════════════════════════════════════════════════════════════════════════

async def user_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu: return
    chat  = cmu.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]: return
    new_m = cmu.new_chat_member
    old_m = cmu.old_chat_member
    user  = new_m.user

    if old_m.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED] and new_m.status == ChatMemberStatus.MEMBER:
        logger.info(f"New member {user.id} joined {chat.id}")
        settings = await get_group_settings(chat.id)
        await upsert_user(user.id, user.username, user.first_name, getattr(user, 'last_name', None))
        await upsert_group_member(chat.id, user.id, user.username, user.first_name)
        await increment_member_count(chat.id)
        # Clear membership cache for this user
        keys = [k for k in _membership_cache if k[0] == chat.id and k[1] == user.id]
        for k in keys: del _membership_cache[k]
        if settings and settings.get('auto_approve', False):
            try:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                await update_join_request_status(chat.id, user.id, 'approved', context.bot.id)
            except Exception: pass
        await send_welcome_message(chat, user, context, settings)

    elif new_m.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        logger.info(f"Member {user.id} left/banned from {chat.id}")
        await remove_group_member(chat.id, user.id)
        await decrement_member_count(chat.id)
        keys = [k for k in _membership_cache if k[0] == chat.id and k[1] == user.id]
        for k in keys: del _membership_cache[k]


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE MODERATION
# ═══════════════════════════════════════════════════════════════════════════════

async def send_warning_with_count(chat, user_id, username, reason, context, offense_type="general"):
    warnings     = await get_user_warnings(chat.id, user_id)
    settings     = await get_group_settings(chat.id)
    max_warnings = settings.get('max_warnings', 3) if settings else 3
    user_mention = f"@{username}" if username else f"User {user_id}"
    if offense_type == "banned_word":
        await chat.send_message(f"⚠️ {user_mention}, your message was hidden (banned word)."); return
    await add_warning(chat.id, user_id, 0, reason, username)
    warning_count = len(warnings) + 1
    warn_msg = (f"⚠️ <b>WARNING #{warning_count}/{max_warnings}</b>\n"
                f"👤 {user_mention}\n📝 Reason: {reason}\n\nThis user has been warned by the bot.")
    if warning_count >= max_warnings:
        updated     = await get_user_warnings(chat.id, user_id)
        mute_reason = await generate_mute_reason_with_gemini(warning_count, updated, f"Max warnings: {offense_type}")
        until_date  = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        try:
            await context.bot.restrict_chat_member(
                chat.id, user_id,
                ChatPermissions(can_send_messages=False, can_send_photos=False,
                                can_send_videos=False, can_send_documents=False,
                                can_send_audios=False, can_send_voice_notes=False,
                                can_send_video_notes=False, can_send_polls=False), until_date=until_date)
            await add_mute(chat.id, user_id, 0, mute_reason, 60, username)
            kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat.id}")]]
            await chat.send_message(
                f"🔇 <b>AUTO-MUTED</b>\n👤 {user_mention}\n⏱ 1 hour\n📝 {mute_reason}\n"
                f"Reached {max_warnings} warnings.",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        except Exception as e:
            logger.error(f"Auto-mute: {e}")
    await chat.send_message(warn_msg, parse_mode='HTML')


def contains_link_in_caption(caption: str, caption_entities: list) -> bool:
    if not caption: return False
    if caption_entities:
        for e in caption_entities:
            if e.type in [MessageEntity.TEXT_LINK, MessageEntity.URL]: return True
    for p in [r'https?://\S+', r'www\.\S+', r't\.me/\S+']:
        if re.search(p, caption, re.IGNORECASE): return True
    return False


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message: return
    chat = message.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]: return
    settings = await get_group_settings(chat.id)
    if not settings: return

    # Exempt check
    is_admin_or_exempt = False
    if message.sender_chat and message.sender_chat.id == chat.id:
        is_admin_or_exempt = True
    elif message.from_user and message.from_user.id == 1087968824:
        is_admin_or_exempt = True
    elif message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        is_admin_or_exempt = True
    else:
        try:
            if message.from_user:
                m = await chat.get_member(message.from_user.id)
                if m.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                    is_admin_or_exempt = True
        except Exception: pass
    if is_admin_or_exempt: return
    if not message.from_user: return

    user     = message.from_user
    user_id  = user.id
    username = user.username or user.first_name or str(user_id)

    # Deleted account
    if is_deleted_account(user):
        try:
            await message.delete()
            await context.bot.ban_chat_member(chat.id, user_id)
            await context.bot.unban_chat_member(chat.id, user_id)
        except Exception: pass
        return

    # Track activity
    await upsert_user(user_id, user.username, user.first_name, getattr(user, 'last_name', None))
    await upsert_group_member(chat.id, user_id, user.username, user.first_name)

    # Force subscribe check (cached)
    not_joined = await check_force_sub(chat.id, user_id, context)
    if not_joined:
        keyboard      = []
        channel_names = []
        for fc in not_joined:
            title = fc.get('channel_title') or fc.get('channel_username') or str(fc['channel_id'])
            channel_names.append(f"<b>{title}</b>")
            link = (f"https://t.me/{fc['channel_username']}" if fc.get('channel_username')
                    else f"https://t.me/c/{str(fc['channel_id']).replace('-100', '')}")
            keyboard.append([InlineKeyboardButton(f"➕ Join {title}", url=link)])
        user_mention  = get_user_mention_html(user)
        channels_text = " and ".join(channel_names)
        warn_text = (f"⚠️ {user_mention}, you must join {channels_text} "
                     f"before sending messages here.\n\nJoin below, then send again.")
        timer = settings.get('force_sub_message_timer', 120)
        async def _del():
            try: await message.delete()
            except Exception: pass
        async def _warn():
            try:
                wm = await chat.send_message(warn_text, parse_mode="HTML",
                                              reply_markup=InlineKeyboardMarkup(keyboard))
                if timer > 0: await schedule_message_deletion(chat.id, wm.message_id, timer)
            except Exception as e:
                logger.error(f"Force sub warn: {e}")
        await asyncio.gather(_del(), _warn())
        return

    # Sticker protection
    if settings.get('sticker_protect', False) and message.sticker:
        try:
            await message.delete()
            wt   = settings.get('warning_timer', 30)
            warn = await chat.send_message(f"⚠️ @{username}, stickers are not allowed here.")
            if wt > 0: await schedule_message_deletion(chat.id, warn.message_id, wt)
        except Exception as e:
            logger.error(f"Sticker protect: {e}")
        return

    # Photo caption link
    if settings.get('delete_links', False) and message.photo and message.caption:
        if contains_link_in_caption(message.caption, message.caption_entities):
            try:
                await message.delete()
                await send_warning_with_count(chat, user_id, username,
                                              "Links in photo captions not allowed", context, "photo_caption_link")
            except Exception as e:
                logger.error(f"Photo caption link: {e}")
            return

    if not message.text: return

    # Word count
    max_wc = settings.get('max_word_count', 0)
    if max_wc > 0:
        wc = len(message.text.split())
        if wc > max_wc:
            try:
                await message.delete()
                await send_warning_with_count(chat, user_id, username,
                                              f"Too long ({wc} words, max {max_wc})", context, "word_limit")
            except Exception as e:
                logger.error(f"Word count: {e}")
            return

    # Promotions
    if settings.get('delete_promotions', False):
        reason = None
        if is_forwarded_or_channel_message(message): reason = "forwarded or channel message"
        elif message.via_bot:                         reason = "sent via bot"
        elif message.from_user and message.from_user.is_bot: reason = "bot message"
        else:
            ep = (r'[\U0001F000-\U0001FFFF]|[\U00002600-\U000027BF]|[\U0001F600-\U0001F64F]'
                  r'|[\U0001F300-\U0001F5FF]|[\U0001F680-\U0001F6FF]|[\u200d\u2600-\u26FF\u2700-\u27BF]')
            ems = re.findall(ep, message.text); tl = len(message.text)
            if len(ems) > 15 or (tl > 10 and len(ems) / tl > 0.4): reason = "too many emojis"
        if reason:
            try:
                await message.delete()
                await send_warning_with_count(chat, user_id, username,
                                              f"{reason} is not allowed", context, reason.replace(" ", "_"))
            except Exception as e:
                logger.error(f"Promo delete: {e}")
            return

    # Links
    if settings.get('delete_links', False):
        has_link = bool(re.search(r'(https?://\S+|www\.\S+|t\.me/\S+)', message.text))
        if not has_link and message.entities:
            for ent in message.entities:
                if ent.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                    has_link = True; break
        if has_link:
            try:
                await message.delete()
                await send_warning_with_count(chat, user_id, username,
                                              "Links are not allowed", context, "link")
            except Exception as e:
                logger.error(f"Link delete: {e}")
            return

    # Banned words
    banned_words = await get_banned_words(chat.id)
    if banned_words:
        msg_lower = message.text.lower()
        for word in banned_words:
            if re.search(r'\b' + re.escape(word) + r'\b', msg_lower):
                try:
                    await message.delete()
                    await send_warning_with_count(chat, user_id, username, "banned word", context, "banned_word")
                except Exception as e:
                    logger.error(f"Banned word: {e}")
                return


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data

    if   data == "my_groups":                     await my_groups_handler(update, context)
    elif data == "my_channels":                    await my_channels_command(update, context)
    elif data == "how_to_add_channel":
        await q.answer()
        bot_u = context.bot.username
        try:
            await q.message.edit_text(
                "📢 <b>How to Add a Channel</b>\n\n"
                "1. Click the button below → add me as admin\n"
                "2. Give me <b>Invite Users</b> + <b>Manage Channel</b> permissions\n"
                "3. Enable <b>Join Requests</b> in channel settings\n"
                "4. Come back and use /addchannel @yourchannel\n\n"
                "<i>Private channels work too!</i>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "➕ Add Me to Channel",
                        url=f"https://t.me/{bot_u}?startchannel=true"
                            f"&admin=post_messages+edit_messages+delete_messages+invite_users"
                    )],
                    [InlineKeyboardButton("🔙 Back", callback_data="my_channels")]
                ]))
        except BadRequest: pass
    elif data == "help":                           await help_command(update, context)
    elif data == "back_to_main":                   await start(update, context)
    elif data.startswith("group_settings_"):       await group_settings_handler(update, context)
    elif data.startswith("set_welcome_"):          await set_welcome_handler(update, context)
    elif data.startswith("add_word_"):             await add_word_handler(update, context)
    elif data.startswith("remove_word_"):          await remove_word_handler(update, context)
    elif data.startswith("set_timer_"):            await set_timer_handler(update, context)
    elif data.startswith("set_word_limit_"):       await set_word_limit_handler(update, context)
    elif data.startswith("toggle_promo_"):         await toggle_promo_handler(update, context)
    elif data.startswith("toggle_links_"):         await toggle_links_handler(update, context)
    # FIXED: renamed callback prefix → toggle_joindel_ (no ambiguous underscores)
    elif data.startswith("toggle_joindel_"):       await toggle_join_delete_handler(update, context)
    elif data.startswith("toggle_sticker_"):       await toggle_sticker_handler(update, context)
    elif data.startswith("toggle_autoapprove_"):   await toggle_autoapprove_handler(update, context)
    elif data.startswith("set_max_warnings_"):     await set_max_warnings_handler(update, context)
    elif data.startswith("unban_user_"):           await unban_callback_handler(update, context)
    elif data.startswith("unmute_user_"):          await unmute_callback_handler(update, context)
    elif data.startswith("ban_from_warn_"):        await ban_from_warn_callback_handler(update, context)
    elif data.startswith("mute_from_warn_"):       await mute_from_warn_callback_handler(update, context)
    elif data.startswith("cmd_"):                  await admin_keyboard_callback_handler(update, context)
    # Channel callbacks
    elif data.startswith("ch_settings_"):          await channel_settings_handler(update, context)
    elif data.startswith("ch_analytics_"):         await channel_analytics_handler(update, context)
    elif data.startswith("ch_toggle_approve_"):    await channel_toggle_approve_callback(update, context)
    elif data.startswith("ch_set_welcome_"):       await channel_set_welcome_callback(update, context)
    elif data.startswith("ch_set_delay_"):         await channel_set_delay_callback(update, context)
    elif data.startswith("ch_approve_"):           await channel_approve_callback(update, context)
    elif data.startswith("ch_reject_"):            await channel_reject_callback(update, context)
    else:
        try: await q.answer()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI / WEBHOOK
# ═══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    global ptb_application
    if ptb_application is not None:
        return
    ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    gf = filters.ChatType.GROUP | filters.ChatType.SUPERGROUP

    # Private commands
    ptb_application.add_handler(CommandHandler("start",        start,               filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("help",         help_command,        filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("mygroups",     my_groups_handler,   filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("mychannels",   my_channels_command, filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("addchannel",   add_channel_command, filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("schedulepost", schedule_post_command, filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("cancel",       cancel_handler,      filters.ChatType.PRIVATE))

    # Group moderation
    ptb_application.add_handler(CommandHandler("warn",           warn_command,           gf))
    ptb_application.add_handler(CommandHandler("mute",           mute_command,           gf))
    ptb_application.add_handler(CommandHandler("unmute",         unmute_command,         gf))
    ptb_application.add_handler(CommandHandler("ban",            ban_command,            gf))
    ptb_application.add_handler(CommandHandler("unban",          unban_command,          gf))
    ptb_application.add_handler(CommandHandler("report",         report_command,         gf))
    ptb_application.add_handler(CommandHandler("admin",          show_admin_keyboard,    gf))
    ptb_application.add_handler(CommandHandler("tagall",         tag_all_command,        gf))
    ptb_application.add_handler(CommandHandler("note",           note_command,           gf))
    ptb_application.add_handler(CommandHandler("get",            get_note_command,       gf))
    ptb_application.add_handler(CommandHandler("notes",          notes_command,          gf))
    ptb_application.add_handler(CommandHandler("delnote",        delnote_command,        gf))
    ptb_application.add_handler(CommandHandler("forcesub",       forcesub_command,       gf))
    ptb_application.add_handler(CommandHandler("removeforcesub", removeforcesub_command, gf))
    ptb_application.add_handler(CommandHandler("filterdeleted",  filter_deleted_command, gf))
    ptb_application.add_handler(CommandHandler("setwelcome",     setwelcome_command,     gf))
    ptb_application.add_handler(CommandHandler("clearwelcome",   clearwelcome_command,   gf))

    ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
    ptb_application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_input))
    ptb_application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    ptb_application.add_handler(ChatMemberHandler(user_chat_member,  ChatMemberHandler.CHAT_MEMBER))
    ptb_application.add_handler(ChatJoinRequestHandler(handle_join_request))
    ptb_application.add_handler(MessageHandler(filters.PHOTO       & gf, check_message))
    ptb_application.add_handler(MessageHandler(filters.Sticker.ALL & gf, check_message))
    ptb_application.add_handler(MessageHandler(filters.TEXT        & gf, check_message))

    await ptb_application.initialize()
    await ptb_application.start()

    if WEBHOOK_URL:
        try:
            await ptb_application.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=["message","edited_message","callback_query",
                                  "my_chat_member","chat_member","chat_join_request"])
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
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)


@app.api_route("/", methods=["GET", "POST"])
async def health_check():
    return {"status": "ok", "bot": "GroupPilot"}


@app.post("/api/approve-join")
async def approve_join_api(request: Request):
    try:
        d = await request.json()
        await ptb_application.bot.approve_chat_join_request(int(d["chat_id"]), int(d["user_id"]))
        supabase.table("join_requests").update({"status": "approved"}).eq("chat_id", d["chat_id"]).eq("user_id", d["user_id"]).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/reject-join")
async def reject_join_api(request: Request):
    try:
        d = await request.json()
        await ptb_application.bot.decline_chat_join_request(int(d["chat_id"]), int(d["user_id"]))
        supabase.table("join_requests").update({"status": "rejected"}).eq("chat_id", d["chat_id"]).eq("user_id", d["user_id"]).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# CRON ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/run-cleanup")
async def run_cleanup_job():
    if ptb_application is None: await startup_event()
    deleted_count = 0; unmuted_count = 0; posts_sent = 0

    try: unmuted_count = await cleanup_expired_mutes(ptb_application.bot)
    except Exception as e: logger.error(f"Mute cleanup: {e}")

    try:
        for item in (await get_due_deletions()):
            try:
                await ptb_application.bot.delete_message(chat_id=item['chat_id'], message_id=item['message_id'])
                deleted_count += 1
            except Exception as e:
                logger.error(f"Delete msg {item['message_id']}: {e}")
            await remove_pending_deletion(item['id'])
    except Exception as e: logger.error(f"Deletion cleanup: {e}")

    try:
        for post in (await get_due_scheduled_posts()):
            try:
                rm = None
                if post.get('buttons_json'):
                    bdata = json.loads(post['buttons_json'])
                    kb    = [[InlineKeyboardButton(b['text'], url=b['url']) for b in row] for row in bdata]
                    rm    = InlineKeyboardMarkup(kb)
                await ptb_application.bot.send_message(
                    chat_id=post['channel_id'], text=post['content'],
                    parse_mode=post.get('parse_mode', 'HTML'), reply_markup=rm)
                await mark_scheduled_post_sent(post['id'])
                posts_sent += 1
            except Exception as e: logger.error(f"Scheduled post {post['id']}: {e}")
    except Exception as e: logger.error(f"Scheduled posts: {e}")

    return {"status": "ok", "deleted_count": deleted_count, "unmuted_count": unmuted_count, "posts_sent": posts_sent}


async def delete_group_and_words(chat_id: int):
    try:
        supabase.table('banned_words').delete().eq('chat_id', chat_id).execute()
        supabase.table('groups').delete().eq('chat_id', chat_id).execute()
    except Exception as e: logger.error(f"delete_group_and_words {chat_id}: {e}")


@app.get("/run-group-cleanup")
async def run_group_cleanup():
    if ptb_application is None: await startup_event()
    try: groups = [g['chat_id'] for g in supabase.table('groups').select('chat_id').execute().data]
    except Exception as e: logger.error(f"run_group_cleanup: {e}"); return {"status": "error"}
    removed = []
    for chat_id in groups:
        try: await ptb_application.bot.get_chat(chat_id)
        except Forbidden as e:
            await delete_group_and_words(chat_id); removed.append(chat_id)
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                await delete_group_and_words(chat_id); removed.append(chat_id)
        except RetryAfter as e: await asyncio.sleep(e.retry_after)
        except Exception as e: logger.error(f"Group cleanup {chat_id}: {e}")
    return {"status": "ok", "removed": removed}
