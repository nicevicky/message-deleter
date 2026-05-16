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

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize FastAPI
app = FastAPI()

# Global variable to store the Telegram Application
ptb_application = None

# ============================================================
# UTILITY HELPERS
# ============================================================

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


# ============================================================
# DATABASE HELPER FUNCTIONS — GROUPS / WARNINGS / BANS / MUTES
# ============================================================

async def get_group_settings(chat_id: int):
    try:
        result = supabase.table('groups').select("*").eq('chat_id', chat_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting group settings: {e}")
        return None


async def is_user_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
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


async def is_callback_user_admin(chat_id: int, callback_user, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        if callback_user is None:
            return False
        return await is_user_admin(chat_id, callback_user.id, context)
    except Exception:
        return False


async def verify_callback_admin(chat_id: int, query, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    try:
        if query.from_user:
            is_admin = await is_user_admin(chat_id, query.from_user.id, context)
            if is_admin:
                return (True, query.from_user.id, None)
            else:
                return (False, query.from_user.id, "❌ This button is for admins only.")
        else:
            return (False, None, "❌ This button is for admins only.")
    except Exception as e:
        logger.error(f"Error verifying callback admin: {e}")
        return (False, None, "❌ This button is for admins only.")


async def get_chat_admins(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return [admin.user.id for admin in admins]
    except Exception as e:
        logger.error(f"Error getting admins: {e}")
        return []


async def add_warning(chat_id: int, user_id: int, warned_by: int, reason: str, username: str = None):
    try:
        data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "warned_by": warned_by,
            "reason": reason,
            "warned_at": datetime.now(timezone.utc).isoformat()
        }
        result = supabase.table('warnings').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding warning: {e}")
        return None


async def get_user_warnings(chat_id: int, user_id: int):
    try:
        result = supabase.table('warnings').select("*").eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting warnings: {e}")
        return []


async def clear_user_warnings(chat_id: int, user_id: int):
    try:
        supabase.table('warnings').delete().eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"Error clearing warnings: {e}")


async def add_ban(chat_id: int, user_id: int, banned_by: int, reason: str, username: str = None):
    try:
        data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "banned_by": banned_by,
            "reason": reason,
            "banned_at": datetime.now(timezone.utc).isoformat(),
            "is_active": True
        }
        result = supabase.table('bans').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding ban: {e}")
        return None


async def get_active_ban(chat_id: int, user_id: int):
    try:
        result = supabase.table('bans').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error checking ban: {e}")
        return None


async def unban_user_in_db(chat_id: int, user_id: int):
    try:
        result = supabase.table('bans').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error unbanning user: {e}")
        return None


async def add_mute(chat_id: int, user_id: int, muted_by: int, reason: str, duration_minutes: int, username: str = None):
    try:
        mute_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "muted_by": muted_by,
            "reason": reason,
            "muted_at": datetime.now(timezone.utc).isoformat(),
            "mute_until": mute_until.isoformat(),
            "is_active": True
        }
        result = supabase.table('mutes').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding mute: {e}")
        return None


async def get_active_mute(chat_id: int, user_id: int):
    try:
        result = supabase.table('mutes').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        if result.data:
            mute_data = result.data[0]
            mute_until = datetime.fromisoformat(mute_data['mute_until'])
            if datetime.now(timezone.utc) > mute_until:
                await unmute_user_in_db(chat_id, user_id)
                return None
            return mute_data
        return None
    except Exception as e:
        logger.error(f"Error checking mute: {e}")
        return None


async def unmute_user_in_db(chat_id: int, user_id: int):
    try:
        result = supabase.table('mutes').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error unmuting user: {e}")
        return None


async def cleanup_expired_mutes(bot):
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table('mutes').select("*").eq('is_active', True).lte('mute_until', now).execute()
        unmuted_count = 0
        for mute_data in (result.data or []):
            chat_id = mute_data['chat_id']
            user_id = mute_data['user_id']
            try:
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_documents=True,
                    can_send_audios=True,
                    can_send_voice_notes=True,
                    can_send_video_notes=True,
                    can_send_polls=True
                )
                await bot.restrict_chat_member(chat_id, user_id, permissions, until_date=0)
                await unmute_user_in_db(chat_id, user_id)
                unmuted_count += 1
                logger.info(f"Auto-unmuted user {user_id} in chat {chat_id}")
            except Exception as e:
                logger.error(f"Error auto-unmuting user {user_id} in chat {chat_id}: {e}")
        return unmuted_count
    except Exception as e:
        logger.error(f"Error in cleanup_expired_mutes: {e}")
        return 0


async def add_report(chat_id: int, reporter_id: int, reported_user_id: int, reason: str, reporter_username: str = None, reported_username: str = None):
    try:
        data = {
            "chat_id": chat_id,
            "reporter_id": reporter_id,
            "reporter_username": reporter_username,
            "reported_user_id": reported_user_id,
            "reported_username": reported_username,
            "reason": reason,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending"
        }
        result = supabase.table('reports').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding report: {e}")
        return None


async def get_pending_reports(chat_id: int):
    try:
        result = supabase.table('reports').select("*").eq('chat_id', chat_id).eq('status', 'pending').execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting reports: {e}")
        return []


async def add_group_to_db(chat_id: int, chat_title: str, added_by: int, username: str, bot_is_admin: bool, chat_username: str = None):
    try:
        existing_group = await get_group_settings(chat_id)
        delete_promotions = False
        delete_links = False
        warning_timer = 30
        max_word_count = 0
        welcome_message = None
        welcome_timer = 0
        delete_join_messages = False
        max_warnings = 3
        require_approval = False
        auto_approve = False
        sticker_protect = False
        force_sub_channel = None
        force_sub_message_timer = 60
        member_count = 0

        if existing_group:
            delete_promotions = existing_group.get('delete_promotions', False)
            delete_links = existing_group.get('delete_links', False)
            warning_timer = existing_group.get('warning_timer', 30)
            max_word_count = existing_group.get('max_word_count', 0)
            welcome_message = existing_group.get('welcome_message', None)
            welcome_timer = existing_group.get('welcome_timer', 0)
            delete_join_messages = existing_group.get('delete_join_messages', False)
            max_warnings = existing_group.get('max_warnings', 3)
            require_approval = existing_group.get('require_approval', False)
            auto_approve = existing_group.get('auto_approve', False)
            sticker_protect = existing_group.get('sticker_protect', False)
            force_sub_channel = existing_group.get('force_sub_channel', None)
            force_sub_message_timer = existing_group.get('force_sub_message_timer', 60)
            member_count = existing_group.get('member_count', 0)

        data = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "chat_username": chat_username,
            "added_by": added_by,
            "added_by_username": username,
            "bot_is_admin": bot_is_admin,
            "delete_promotions": delete_promotions,
            "delete_links": delete_links,
            "warning_timer": warning_timer,
            "max_word_count": max_word_count,
            "welcome_message": welcome_message,
            "welcome_timer": welcome_timer,
            "delete_join_messages": delete_join_messages,
            "max_warnings": max_warnings,
            "require_approval": require_approval,
            "auto_approve": auto_approve,
            "sticker_protect": sticker_protect,
            "force_sub_channel": force_sub_channel,
            "force_sub_message_timer": force_sub_message_timer,
            "member_count": member_count,
        }
        result = supabase.table('groups').upsert(data, on_conflict='chat_id').execute()
        return result
    except Exception as e:
        logger.error(f"Error adding group to DB: {e}")
        return None


async def get_user_groups(user_id: int):
    try:
        result = supabase.table('groups').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []


async def add_banned_word(chat_id: int, word: str, added_by: int):
    try:
        data = {"chat_id": chat_id, "word": word.lower(), "added_by": added_by}
        result = supabase.table('banned_words').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding banned word: {e}")
        return None


async def remove_banned_word(chat_id: int, word: str):
    try:
        result = supabase.table('banned_words').delete().eq('chat_id', chat_id).eq('word', word.lower()).execute()
        return result
    except Exception as e:
        logger.error(f"Error removing banned word: {e}")
        return None


# ============================================================
# USERS / GROUP MEMBERS / NOTES / JOIN REQUESTS / FORCE SUB
# ============================================================

async def upsert_user(telegram_id: int, username: str = None, first_name: str = None, last_name: str = None):
    try:
        data = {
            "telegram_id": telegram_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table('users').upsert(data, on_conflict='telegram_id').execute()
    except Exception as e:
        logger.error(f"Error upserting user {telegram_id}: {e}")


async def upsert_group_member(chat_id: int, user_id: int, username: str = None, first_name: str = None):
    try:
        existing = supabase.table('group_members').select("*").eq('chat_id', chat_id).eq('user_id', user_id).execute()
        if existing.data:
            supabase.table('group_members').update({
                "username": username,
                "first_name": first_name,
                "last_active": datetime.now(timezone.utc).isoformat(),
                "message_count": existing.data[0].get('message_count', 0) + 1,
            }).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        else:
            supabase.table('group_members').insert({
                "chat_id": chat_id,
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_active": datetime.now(timezone.utc).isoformat(),
                "message_count": 1,
                "joined_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception as e:
        logger.error(f"Error upserting group member {user_id} in {chat_id}: {e}")


async def get_group_members(chat_id: int):
    try:
        result = supabase.table('group_members').select("*").eq('chat_id', chat_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting group members: {e}")
        return []


async def remove_group_member(chat_id: int, user_id: int):
    try:
        supabase.table('group_members').delete().eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"Error removing group member {user_id} from {chat_id}: {e}")


async def add_note(chat_id: int, name: str, content: str, added_by: int):
    try:
        data = {"chat_id": chat_id, "name": name.lower(), "content": content, "added_by": added_by}
        supabase.table('notes').upsert(data, on_conflict='chat_id,name').execute()
    except Exception as e:
        logger.error(f"Error adding note '{name}' in {chat_id}: {e}")


async def get_note(chat_id: int, name: str):
    try:
        result = supabase.table('notes').select("*").eq('chat_id', chat_id).eq('name', name.lower()).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting note '{name}': {e}")
        return None


async def get_all_notes(chat_id: int):
    try:
        result = supabase.table('notes').select("*").eq('chat_id', chat_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting notes for {chat_id}: {e}")
        return []


async def delete_note(chat_id: int, name: str):
    try:
        supabase.table('notes').delete().eq('chat_id', chat_id).eq('name', name.lower()).execute()
    except Exception as e:
        logger.error(f"Error deleting note '{name}' in {chat_id}: {e}")


async def add_join_request(chat_id: int, user_id: int, username: str = None, first_name: str = None):
    try:
        data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        supabase.table('join_requests').upsert(data, on_conflict='chat_id,user_id').execute()
    except Exception as e:
        logger.error(f"Error adding join request for {user_id} in {chat_id}: {e}")


async def get_pending_join_requests(chat_id: int):
    try:
        result = supabase.table('join_requests').select("*").eq('chat_id', chat_id).eq('status', 'pending').execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting join requests: {e}")
        return []


async def update_join_request_status(chat_id: int, user_id: int, status: str, reviewed_by: int):
    try:
        supabase.table('join_requests').update({
            "status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq('chat_id', chat_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"Error updating join request status: {e}")


async def add_force_sub(chat_id: int, channel_id: int, channel_title: str = None, channel_username: str = None, added_by: int = 0):
    try:
        data = {
            "chat_id": chat_id,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "channel_username": channel_username,
            "added_by": added_by,
            "is_active": True,
        }
        supabase.table('force_sub').upsert(data, on_conflict='chat_id,channel_id').execute()
    except Exception as e:
        logger.error(f"Error adding force_sub for {chat_id}: {e}")


async def get_active_force_subs(chat_id: int):
    try:
        result = supabase.table('force_sub').select("*").eq('chat_id', chat_id).eq('is_active', True).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting force_sub channels: {e}")
        return []


async def remove_force_sub(chat_id: int, channel_id: int):
    try:
        supabase.table('force_sub').update({"is_active": False}).eq('chat_id', chat_id).eq('channel_id', channel_id).execute()
    except Exception as e:
        logger.error(f"Error removing force_sub: {e}")


async def check_user_force_sub(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    force_subs = await get_active_force_subs(chat_id)
    if not force_subs:
        return True, []
    missing = []
    for fs in force_subs:
        try:
            member = await context.bot.get_chat_member(fs['channel_id'], user_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                missing.append(fs)
        except Exception:
            missing.append(fs)
    return len(missing) == 0, missing


async def increment_member_count(chat_id: int):
    try:
        settings = await get_group_settings(chat_id)
        if settings:
            current = settings.get('member_count', 0)
            supabase.table('groups').update({"member_count": current + 1}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error incrementing member count: {e}")


async def decrement_member_count(chat_id: int):
    try:
        settings = await get_group_settings(chat_id)
        if settings:
            current = settings.get('member_count', 0)
            supabase.table('groups').update({"member_count": max(0, current - 1)}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error decrementing member count: {e}")


async def get_banned_words(chat_id: int):
    try:
        result = supabase.table('banned_words').select("word").eq('chat_id', chat_id).execute()
        return [item['word'] for item in result.data]
    except Exception as e:
        logger.error(f"Error getting banned words: {e}")
        return []


async def update_promotion_setting(chat_id: int, delete_promotions: bool):
    try:
        return supabase.table('groups').update({"delete_promotions": delete_promotions}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating promotion setting: {e}")
        return None


async def update_link_setting(chat_id: int, delete_links: bool):
    try:
        return supabase.table('groups').update({"delete_links": delete_links}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating link setting: {e}")
        return None


async def update_warning_timer(chat_id: int, seconds: int):
    try:
        return supabase.table('groups').update({"warning_timer": seconds}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating warning timer: {e}")
        return None


async def update_word_limit(chat_id: int, limit: int):
    try:
        return supabase.table('groups').update({"max_word_count": limit}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating word limit: {e}")
        return None


async def update_welcome_message(chat_id: int, welcome_html: str, timer: int):
    try:
        return supabase.table('groups').update({
            "welcome_message": welcome_html,
            "welcome_timer": timer
        }).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating welcome message: {e}")
        return None


async def update_delete_join_messages(chat_id: int, delete_join: bool):
    try:
        return supabase.table('groups').update({"delete_join_messages": delete_join}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating delete join messages setting: {e}")
        return None


async def update_max_warnings(chat_id: int, max_warnings: int):
    try:
        if max_warnings < 3 or max_warnings > 31:
            raise ValueError("Max warnings must be between 3 and 31")
        return supabase.table('groups').update({"max_warnings": max_warnings}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating max warnings: {e}")
        return None


async def update_require_approval(chat_id: int, require_approval: bool):
    try:
        supabase.table('groups').update({"require_approval": require_approval}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating require_approval: {e}")


async def update_auto_approve(chat_id: int, auto_approve: bool):
    try:
        supabase.table('groups').update({"auto_approve": auto_approve}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating auto_approve: {e}")


async def update_sticker_protect(chat_id: int, sticker_protect: bool):
    try:
        supabase.table('groups').update({"sticker_protect": sticker_protect}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating sticker_protect: {e}")


async def update_force_sub_channel(chat_id: int, channel: str):
    try:
        supabase.table('groups').update({"force_sub_channel": channel}).eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error updating force_sub_channel: {e}")


async def schedule_message_deletion(chat_id: int, message_id: int, delay_seconds: int):
    try:
        delete_time = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        data = {"chat_id": chat_id, "message_id": message_id, "delete_at": delete_time.isoformat()}
        supabase.table('pending_deletions').insert(data).execute()
    except Exception as e:
        logger.error(f"Error scheduling deletion: {e}")


async def get_due_deletions():
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table('pending_deletions').select("*").lte('delete_at', now).execute()
        if hasattr(result, 'data') and result.data is not None:
            return result.data
        return []
    except Exception as e:
        logger.error(f"Error getting due deletions: {e}")
        return []


async def remove_pending_deletion(row_id: int):
    try:
        supabase.table('pending_deletions').delete().eq('id', row_id).execute()
    except Exception as e:
        logger.error(f"Error removing pending deletion row: {e}")


# ============================================================
# NEW: CHANNEL SETTINGS DATABASE HELPERS
# ============================================================

async def get_channel_settings(channel_id: int):
    """Get settings for a channel managed by the bot."""
    try:
        result = supabase.table('channel_settings').select("*").eq('channel_id', channel_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting channel settings: {e}")
        return None


async def upsert_channel_settings(channel_id: int, data: dict):
    """Create or update channel settings."""
    try:
        data['channel_id'] = channel_id
        supabase.table('channel_settings').upsert(data, on_conflict='channel_id').execute()
    except Exception as e:
        logger.error(f"Error upserting channel settings: {e}")


async def get_user_channels(user_id: int):
    """Get all channels owned/managed by a user."""
    try:
        result = supabase.table('channel_settings').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user channels: {e}")
        return []


async def record_channel_join(channel_id: int, user_id: int, username: str = None, first_name: str = None, invite_source: str = None):
    """Record a new channel member join for analytics."""
    try:
        data = {
            "channel_id": channel_id,
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "invite_source": invite_source,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table('channel_members').upsert(data, on_conflict='channel_id,user_id').execute()
    except Exception as e:
        logger.error(f"Error recording channel join: {e}")


async def get_channel_analytics(channel_id: int) -> dict:
    """Get analytics summary for a channel."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        total_res = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).execute()
        today_res = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).gte('joined_at', today).execute()
        week_res = supabase.table('channel_members').select("user_id", count='exact').eq('channel_id', channel_id).gte('joined_at', week_ago).execute()

        return {
            "total_members": total_res.count or 0,
            "joined_today": today_res.count or 0,
            "joined_this_week": week_res.count or 0,
        }
    except Exception as e:
        logger.error(f"Error getting channel analytics: {e}")
        return {"total_members": 0, "joined_today": 0, "joined_this_week": 0}


async def save_scheduled_post(channel_id: int, content: str, scheduled_at: str, added_by: int, parse_mode: str = "HTML", buttons_json: str = None):
    """Save a scheduled post to the database."""
    try:
        data = {
            "channel_id": channel_id,
            "content": content,
            "scheduled_at": scheduled_at,
            "added_by": added_by,
            "parse_mode": parse_mode,
            "buttons_json": buttons_json,
            "status": "pending",
        }
        supabase.table('scheduled_posts').insert(data).execute()
    except Exception as e:
        logger.error(f"Error saving scheduled post: {e}")


async def get_due_scheduled_posts():
    """Get scheduled posts that are ready to be sent."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table('scheduled_posts').select("*").eq('status', 'pending').lte('scheduled_at', now).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting due scheduled posts: {e}")
        return []


async def mark_scheduled_post_sent(post_id: int):
    """Mark a scheduled post as sent."""
    try:
        supabase.table('scheduled_posts').update({"status": "sent"}).eq('id', post_id).execute()
    except Exception as e:
        logger.error(f"Error marking post sent: {e}")


async def record_user_onboarded(channel_id: int, user_id: int):
    """Mark user as having received the onboarding DM."""
    try:
        supabase.table('channel_members').update({
            "onboarded": True,
            "onboarded_at": datetime.now(timezone.utc).isoformat()
        }).eq('channel_id', channel_id).eq('user_id', user_id).execute()
    except Exception as e:
        logger.error(f"Error recording onboard: {e}")


async def is_user_onboarded(channel_id: int, user_id: int) -> bool:
    """Check if user already received their onboarding DM."""
    try:
        result = supabase.table('channel_members').select("onboarded").eq('channel_id', channel_id).eq('user_id', user_id).execute()
        if result.data:
            return bool(result.data[0].get('onboarded', False))
        return False
    except Exception as e:
        logger.error(f"Error checking onboard status: {e}")
        return False


# ============================================================
# GEMINI AI HELPER
# ============================================================

async def generate_mute_reason_with_gemini(warning_count: int, recent_warnings: list, offense_type: str) -> str:
    try:
        warnings_summary = ""
        if recent_warnings:
            warnings_summary = "\n".join([f"- {w['reason']} ({w['warned_at']})" for w in recent_warnings[-5:]])
        prompt = f"""Generate a concise, professional mute reason (2-3 sentences) for a Telegram group moderation.
Context:
- Warning count: {warning_count}
- Offense type: {offense_type}
- Recent warnings: {warnings_summary if warnings_summary else "None"}
The reason should explain WHY the user is being muted so other group members understand.
Focus on the pattern of behavior (spamming, repeated violations, etc.).
Keep it under 150 characters if possible.
Format as plain text, no markdown."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 100}
        }
        headers = {"Content-Type": "application/json"}
        response = http_requests.post(url, headers=headers, data=json.dumps(payload), timeout=5)
        if response.status_code == 200:
            result = response.json()
            return result['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            logger.error(f"Gemini API error: {response.status_code}")
            return f"Multiple violations ({offense_type})"
    except Exception as e:
        logger.error(f"Error generating mute reason with Gemini: {e}")
        return f"Repeated violations ({offense_type})"


# ============================================================
# PARSE / RESOLVE HELPERS
# ============================================================

def parse_duration(duration_str: str) -> int:
    duration_str_lower = duration_str.lower().strip()
    if duration_str_lower.endswith('m'):
        try:
            return int(duration_str_lower[:-1]) * 60
        except ValueError:
            return 0
    elif duration_str_lower.endswith('h'):
        try:
            return int(duration_str_lower[:-1]) * 3600
        except ValueError:
            return 0
    elif duration_str_lower.endswith('d'):
        try:
            return int(duration_str_lower[:-1]) * 86400
        except ValueError:
            return 0
    elif duration_str_lower.endswith('w'):
        try:
            return int(duration_str_lower[:-1]) * 604800
        except ValueError:
            return 0
    try:
        return int(duration_str_lower) * 60
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
        kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat.id}")]]
        await chat.send_message(
            f"🔇 <b>AUTO-MUTED</b>\nUser: {mention}\nDuration: 1 hour\n"
            f"Reason: {reason}\nReached {max_w} warnings.",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"auto_mute_user: {e}")


# ============================================================
# NEW: CHANNEL WELCOME DM SENDER
# ============================================================

def parse_channel_welcome(template: str, bot_name: str, user_name: str, user_id: int, channel_title: str) -> tuple:
    """Parse channel welcome template, return (text, buttons_list)."""
    message = template.replace('{BOT_NAME}', bot_name)
    message = message.replace('{USER_NAME}', user_name)
    message = message.replace('{USER_ID}', str(user_id))
    message = message.replace('{CHANNEL_TITLE}', channel_title)
    button_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    buttons = re.findall(button_pattern, message)
    message = re.sub(button_pattern, '', message).strip()
    return message, buttons


async def send_channel_welcome_dm(bot, user, channel_id: int, channel_title: str, settings: dict):
    """
    Attempt to send a private welcome DM to a new channel member.
    Telegram requires the user to have started the bot first.
    If we cannot DM them, we silently skip.
    """
    try:
        welcome_template = settings.get('welcome_message') or (
            f"👋 Welcome to <b>{channel_title}</b>, {{USER_NAME}}!\n\n"
            f"We're glad to have you here. Enjoy the content!"
        )
        bot_name = bot.username or "Bot"
        user_name = user.first_name or user.username or "Member"
        text, buttons = parse_channel_welcome(welcome_template, bot_name, user_name, user.id, channel_title)

        keyboard = []
        if buttons:
            for i in range(0, len(buttons), 2):
                row = []
                row.append(InlineKeyboardButton(buttons[i][0], url=buttons[i][1]))
                if i + 1 < len(buttons):
                    row.append(InlineKeyboardButton(buttons[i + 1][0], url=buttons[i + 1][1]))
                keyboard.append(row)

        # Add default channel button if no buttons set
        if not keyboard:
            ch_username = settings.get('channel_username')
            if ch_username:
                keyboard.append([InlineKeyboardButton(f"📢 Open {channel_title}", url=f"https://t.me/{ch_username}")])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await bot.send_message(
            chat_id=user.id,
            text=text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        logger.info(f"Sent channel welcome DM to user {user.id} for channel {channel_id}")
        return True
    except Forbidden:
        logger.info(f"User {user.id} has not started the bot — cannot DM.")
        return False
    except Exception as e:
        logger.error(f"Error sending channel welcome DM: {e}")
        return False


# ============================================================
# NEW: CHANNEL JOIN REQUEST HANDLER (CORE NEW FEATURE)
# ============================================================

async def handle_channel_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle join requests for CHANNELS managed by this bot.
    Auto-approves if enabled, records analytics, sends private welcome DM.
    """
    jr = update.chat_join_request
    if not jr:
        return

    chat = jr.chat
    user = jr.from_user

    # Only handle channels here
    if chat.type != ChatType.CHANNEL:
        # Delegate to group join request handler
        await handle_join_request(update, context)
        return

    logger.info(f"Channel join request: user {user.id} wants to join channel {chat.id} ({chat.title})")

    # Record the join request
    await add_join_request(chat.id, user.id, user.username, user.first_name)
    await upsert_user(user.id, user.username, user.first_name, getattr(user, 'last_name', None))

    # Get channel settings
    settings = await get_channel_settings(chat.id)

    if not settings:
        # No settings stored — auto-approve by default and send basic welcome
        try:
            await context.bot.approve_chat_join_request(chat.id, user.id)
            await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
            await record_channel_join(chat.id, user.id, user.username, user.first_name, "direct")
            # Try sending DM
            basic_welcome = f"👋 Welcome to <b>{chat.title}</b>, {user.first_name or 'Member'}!\n\nEnjoy the content!"
            try:
                await context.bot.send_message(user.id, basic_welcome, parse_mode='HTML')
            except Forbidden:
                pass
        except Exception as e:
            logger.error(f"Error auto-approving (no settings) channel join request: {e}")
        return

    auto_approve = settings.get('auto_approve', True)
    approval_delay = settings.get('approval_delay', 0)  # seconds

    if auto_approve:
        if approval_delay > 0:
            await asyncio.sleep(approval_delay)
        try:
            await context.bot.approve_chat_join_request(chat.id, user.id)
            await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
            await record_channel_join(chat.id, user.id, user.username, user.first_name, "join_request")
            logger.info(f"Auto-approved channel join for user {user.id} in {chat.id}")

            # Send private welcome DM
            already_onboarded = await is_user_onboarded(chat.id, user.id)
            if not already_onboarded:
                dm_sent = await send_channel_welcome_dm(context.bot, user, chat.id, chat.title, settings)
                if dm_sent:
                    await record_user_onboarded(chat.id, user.id)
        except Exception as e:
            logger.error(f"Error processing channel join request: {e}")
    else:
        # Manual approval mode — notify admins
        logger.info(f"Manual approval required for user {user.id} in channel {chat.id}")
        admin_id = settings.get('added_by')
        if admin_id:
            try:
                kb = [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"ch_approve_{chat.id}_{user.id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"ch_reject_{chat.id}_{user.id}")
                    ]
                ]
                await context.bot.send_message(
                    admin_id,
                    f"🔔 <b>New Join Request</b>\n\n"
                    f"Channel: <b>{chat.title}</b>\n"
                    f"User: {get_user_mention_html(user)}\n"
                    f"ID: <code>{user.id}</code>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            except Exception as e:
                logger.error(f"Error notifying admin of join request: {e}")


# ============================================================
# NEW: CHANNEL APPROVE / REJECT CALLBACK HANDLERS
# ============================================================

async def channel_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin manually approves a channel join request."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    # ch_approve_{channel_id}_{user_id}
    channel_id = int(parts[2])
    user_id = int(parts[3])

    try:
        await context.bot.approve_chat_join_request(channel_id, user_id)
        await update_join_request_status(channel_id, user_id, "approved", query.from_user.id)
        await record_channel_join(channel_id, user_id, None, None, "manual_approve")

        # Try to get user info and send welcome
        settings = await get_channel_settings(channel_id)
        channel_info = await context.bot.get_chat(channel_id)
        try:
            user_info = await context.bot.get_chat(user_id)
            if settings:
                await send_channel_welcome_dm(context.bot, user_info, channel_id, channel_info.title, settings)
        except Exception:
            pass

        try:
            await query.message.edit_text(f"✅ User {user_id} approved for channel {channel_id}.", reply_markup=None)
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


async def channel_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin manually rejects a channel join request."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    channel_id = int(parts[2])
    user_id = int(parts[3])

    try:
        await context.bot.decline_chat_join_request(channel_id, user_id)
        await update_join_request_status(channel_id, user_id, "rejected", query.from_user.id)
        try:
            await query.message.edit_text(f"❌ User {user_id} rejected from channel {channel_id}.", reply_markup=None)
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


# ============================================================
# NEW: /addchannel COMMAND — Register a channel with the bot
# ============================================================

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addchannel @channelUsername
    Registers a channel so the bot can manage its join requests and send welcome DMs.
    Must be used in private chat. Bot must already be admin in the channel.
    """
    message = update.message
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Please use /addchannel in private chat with me.")
        return

    if not context.args:
        await message.reply_text(
            "❌ Usage: /addchannel @channelUsername\n\n"
            "Make sure I'm already an admin in the channel with 'Invite Users' permission."
        )
        return

    channel_ref = context.args[0]
    try:
        channel_chat = await context.bot.get_chat(channel_ref)
    except Exception as e:
        await message.reply_text(f"❌ Could not find channel: {e}\n\nMake sure you spelled it correctly and I'm a member.")
        return

    if channel_chat.type != ChatType.CHANNEL:
        await message.reply_text("❌ That is not a channel! Please provide a channel username.")
        return

    # Check bot is admin in the channel
    try:
        bot_member = await context.bot.get_chat_member(channel_chat.id, context.bot.id)
        if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text(
                "❌ I'm not an admin in that channel!\n\n"
                "Please make me admin with at least:\n"
                "• Invite Users\n"
                "• Manage Channel"
            )
            return
    except Exception as e:
        await message.reply_text(f"❌ Error checking my permissions: {e}")
        return

    # Save channel settings
    await upsert_channel_settings(channel_chat.id, {
        "channel_title": channel_chat.title,
        "channel_username": channel_chat.username,
        "added_by": message.from_user.id,
        "auto_approve": True,
        "approval_delay": 0,
        "welcome_message": None,
        "welcome_timer": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    kb = [
        [InlineKeyboardButton("⚙️ Channel Settings", callback_data=f"ch_settings_{channel_chat.id}")],
        [InlineKeyboardButton("📊 Analytics", callback_data=f"ch_analytics_{channel_chat.id}")],
    ]
    await message.reply_html(
        f"✅ <b>Channel Registered!</b>\n\n"
        f"📢 Channel: <b>{channel_chat.title}</b>\n"
        f"🆔 ID: <code>{channel_chat.id}</code>\n\n"
        f"I will now:\n"
        f"• Auto-approve join requests\n"
        f"• Send private welcome DMs\n"
        f"• Track analytics\n\n"
        f"Use the buttons below to configure.",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ============================================================
# NEW: CHANNEL SETTINGS MENU
# ============================================================

async def channel_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show channel settings panel."""
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.split("_")[2])

    settings = await get_channel_settings(channel_id)
    if not settings:
        await query.message.edit_text("❌ Channel not found. Use /addchannel to register it.")
        return

    auto_approve = "✅ ON" if settings.get('auto_approve', True) else "❌ OFF"
    welcome_set = "✅ Set" if settings.get('welcome_message') else "❌ Not Set"
    approval_delay = settings.get('approval_delay', 0)
    delay_display = f"{approval_delay}s" if approval_delay else "Instant"

    text = (
        f"⚙️ <b>Channel Settings</b>\n\n"
        f"📢 Channel: <b>{settings.get('channel_title', 'Unknown')}</b>\n\n"
        f"✅ Auto Approve: {auto_approve}\n"
        f"⏱ Approval Delay: {delay_display}\n"
        f"💌 Welcome DM: {welcome_set}\n"
    )

    kb = [
        [InlineKeyboardButton(f"Auto Approve: {auto_approve}", callback_data=f"ch_toggle_approve_{channel_id}")],
        [InlineKeyboardButton("💌 Set Welcome DM", callback_data=f"ch_set_welcome_{channel_id}")],
        [InlineKeyboardButton("⏱ Set Approval Delay", callback_data=f"ch_set_delay_{channel_id}")],
        [InlineKeyboardButton("📊 Analytics", callback_data=f"ch_analytics_{channel_id}")],
        [InlineKeyboardButton("🔙 My Channels", callback_data="my_channels")],
    ]
    try:
        await query.message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest:
        pass


async def channel_analytics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show channel analytics."""
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.split("_")[2])

    settings = await get_channel_settings(channel_id)
    analytics = await get_channel_analytics(channel_id)

    text = (
        f"📊 <b>Channel Analytics</b>\n\n"
        f"📢 {settings.get('channel_title', 'Unknown') if settings else 'Unknown'}\n\n"
        f"👥 Total Members (tracked): {analytics['total_members']}\n"
        f"📈 Joined Today: {analytics['joined_today']}\n"
        f"📆 Joined This Week: {analytics['joined_this_week']}\n"
    )

    kb = [[InlineKeyboardButton("🔙 Back", callback_data=f"ch_settings_{channel_id}")]]
    try:
        await query.message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest:
        pass


async def channel_toggle_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-approve for a channel."""
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.split("_")[3])

    settings = await get_channel_settings(channel_id)
    if not settings:
        return
    new_val = not settings.get('auto_approve', True)
    await upsert_channel_settings(channel_id, {"auto_approve": new_val})
    await query.answer(f"Auto Approve: {'ON' if new_val else 'OFF'}", show_alert=True)

    # Refresh settings panel
    query.data = f"ch_settings_{channel_id}"
    await channel_settings_handler(update, context)


async def channel_set_welcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set channel welcome message."""
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.split("_")[3])

    context.user_data['awaiting_input'] = channel_id
    context.user_data['action'] = 'ch_set_welcome'

    text = (
        "💌 <b>Set Channel Welcome DM</b>\n\n"
        "This message is sent privately to new members who join your channel.\n\n"
        "<b>Variables you can use:</b>\n"
        "• <code>{USER_NAME}</code> — Member's first name\n"
        "• <code>{USER_ID}</code> — Member's Telegram ID\n"
        "• <code>{CHANNEL_TITLE}</code> — Your channel name\n"
        "• <code>{BOT_NAME}</code> — Bot username\n\n"
        "<b>Inline Button Format:</b>\n"
        "<code>[Button Text](https://link)</code>\n\n"
        "<b>Example:</b>\n"
        "<code>👋 Welcome {USER_NAME} to {CHANNEL_TITLE}!\n"
        "Enjoy exclusive content.\n"
        "[Open Channel](https://t.me/yourchannel) [Rules](https://t.me/yourchannel/2)</code>\n\n"
        "✏️ Send your welcome message now:"
    )
    try:
        await query.message.edit_text(text, parse_mode='HTML')
    except BadRequest:
        pass


async def channel_set_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set approval delay."""
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.split("_")[3])

    context.user_data['awaiting_input'] = channel_id
    context.user_data['action'] = 'ch_set_delay'

    text = (
        "⏱ <b>Set Approval Delay</b>\n\n"
        "How many seconds to wait before approving a join request?\n"
        "• <code>0</code> — Instant (recommended)\n"
        "• <code>5</code> — 5 seconds\n"
        "• <code>30</code> — 30 seconds\n\n"
        "✏️ Send the number of seconds:"
    )
    try:
        await query.message.edit_text(text, parse_mode='HTML')
    except BadRequest:
        pass


# ============================================================
# NEW: MY CHANNELS COMMAND / HANDLER
# ============================================================

async def my_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's registered channels."""
    user_id = update.effective_user.id
    channels = await get_user_channels(user_id)

    if not channels:
        text = (
            "📢 You have no registered channels yet.\n\n"
            "Use /addchannel @channelUsername to register a channel.\n"
            "Make sure I'm already an admin in the channel!"
        )
        kb = [[InlineKeyboardButton("➕ Add Channel", callback_data="how_to_add_channel")]]
        rm = InlineKeyboardMarkup(kb)
    else:
        text = "📢 <b>Your Channels:</b>\n\nSelect a channel to manage:"
        kb = []
        for ch in channels:
            kb.append([InlineKeyboardButton(
                f"📢 {ch.get('channel_title', 'Unknown')}",
                callback_data=f"ch_settings_{ch['channel_id']}"
            )])
        kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")])
        rm = InlineKeyboardMarkup(kb)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode='HTML', reply_markup=rm)
        except BadRequest:
            pass
    else:
        await update.message.reply_html(text, reply_markup=rm)


# ============================================================
# NEW: SCHEDULE POST COMMAND
# ============================================================

async def schedule_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /schedulepost @channel YYYY-MM-DD HH:MM Your message here
    Schedule a post to be sent to a channel at a specific time (UTC).
    """
    message = update.message
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Please use /schedulepost in private chat.")
        return

    if not context.args or len(context.args) < 4:
        await message.reply_text(
            "❌ Usage: /schedulepost @channel YYYY-MM-DD HH:MM Your message\n\n"
            "Example:\n/schedulepost @mychannel 2025-12-25 09:00 Merry Christmas! 🎄"
        )
        return

    channel_ref = context.args[0]
    date_str = context.args[1]
    time_str = context.args[2]
    content = " ".join(context.args[3:])

    try:
        scheduled_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.reply_text("❌ Invalid date/time format! Use YYYY-MM-DD HH:MM (UTC)")
        return

    if scheduled_dt <= datetime.now(timezone.utc):
        await message.reply_text("❌ Scheduled time must be in the future!")
        return

    try:
        channel_chat = await context.bot.get_chat(channel_ref)
    except Exception as e:
        await message.reply_text(f"❌ Could not find channel: {e}")
        return

    settings = await get_channel_settings(channel_chat.id)
    if not settings or settings.get('added_by') != message.from_user.id:
        await message.reply_text("❌ You don't manage this channel or it's not registered. Use /addchannel first.")
        return

    await save_scheduled_post(
        channel_id=channel_chat.id,
        content=content,
        scheduled_at=scheduled_dt.isoformat(),
        added_by=message.from_user.id,
        parse_mode="HTML"
    )

    await message.reply_html(
        f"✅ <b>Post Scheduled!</b>\n\n"
        f"📢 Channel: <b>{channel_chat.title}</b>\n"
        f"⏰ Time: <b>{date_str} {time_str} UTC</b>\n\n"
        f"📝 Content preview:\n<i>{content[:200]}</i>"
    )


# ============================================================
# MODERATION COMMAND HANDLERS (UNCHANGED FROM ORIGINAL)
# ============================================================

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /warn <username/ID> <reason>\nExample: /warn @user123 Spamming in group")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:])
    target_user = None
    target_username = None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            target_id = int(target)
            target_user = await context.bot.get_chat_member(chat.id, target_id)
            if target_user.user:
                target_user = target_user.user
                target_username = target_user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    warned_by = message.from_user.id if message.from_user else 0
    await add_warning(chat.id, target_user.id, warned_by, reason, target_username)
    warnings = await get_user_warnings(chat.id, target_user.id)
    warning_count = len(warnings)
    settings = await get_group_settings(chat.id)
    max_warnings = settings.get('max_warnings', 3) if settings else 3
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    warn_msg = (
        f"⚠️ <b>WARNING #{warning_count}/{max_warnings}</b>\n"
        f"👤 User: {user_mention}\n"
        f"🛡️ Admin: {admin_mention}\n"
        f"📝 Reason: {reason}\n\nThis user has been warned by admin."
    )
    if warning_count >= max_warnings:
        mute_reason = await generate_mute_reason_with_gemini(warning_count, warnings, "Maximum warnings reached")
        until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        try:
            permissions = ChatPermissions(
                can_send_messages=False, can_send_photos=False, can_send_videos=False,
                can_send_documents=False, can_send_audios=False, can_send_voice_notes=False,
                can_send_video_notes=False, can_send_polls=False
            )
            await context.bot.restrict_chat_member(chat.id, target_user.id, permissions, until_date=until_date)
            await add_mute(chat.id, target_user.id, warned_by, mute_reason, 60, target_username)
            mute_msg = (
                f"🔇 <b>USER MUTED (AUTO)</b>\n"
                f"👤 User: {user_mention}\n"
                f"🛡️ Muted by: Admin\n"
                f"⏱ Duration: 1 hour\n"
                f"📝 Reason: {mute_reason}\n\n"
                f"User has reached {max_warnings} warnings and has been automatically muted."
            )
            kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{target_user.id}_{chat.id}")]]
            await message.reply_html(mute_msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logger.error(f"Error auto-muting user: {e}")
    kb = [
        [InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_from_warn_{target_user.id}_{chat.id}")],
        [InlineKeyboardButton("🔇 Mute User", callback_data=f"mute_from_warn_{target_user.id}_{chat.id}")]
    ]
    await message.reply_html(warn_msg, reply_markup=InlineKeyboardMarkup(kb))


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args or len(context.args) < 1:
        await message.reply_text("❌ Usage: /ban <username/ID> <reason>")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
    target_user = None
    target_username = None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            target_id = int(target)
            target_user = await context.bot.get_chat_member(chat.id, target_id)
            if target_user.user:
                target_user = target_user.user
                target_username = target_user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    existing_ban = await get_active_ban(chat.id, target_user.id)
    if existing_ban:
        await message.reply_text("❌ This user is already banned!")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target_user.id)
    except Exception as e:
        await message.reply_text(f"❌ Error banning user: {e}")
        return
    banned_by = message.from_user.id if message.from_user else 0
    await add_ban(chat.id, target_user.id, banned_by, reason, target_username)
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    ban_msg = (
        f"🚫 <b>USER BANNED</b>\n"
        f"👤 User: {user_mention}\n"
        f"🛡️ Banned by: {admin_mention}\n"
        f"📝 Reason: {reason}\n\nThis user has been banned from the group."
    )
    kb = [[InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{target_user.id}_{chat.id}")]]
    await message.reply_html(ban_msg, reply_markup=InlineKeyboardMarkup(kb))


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args:
        await message.reply_text("❌ Usage: /unban <username/ID>")
        return
    target = context.args[0]
    target_user_id = None
    if target.startswith("@"):
        target_username = target[1:]
        result = supabase.table('bans').select("*").eq('chat_id', chat.id).eq('username', target_username).eq('is_active', True).execute()
        if result.data:
            target_user_id = result.data[0]['user_id']
    else:
        try:
            target_user_id = int(target)
        except ValueError:
            pass
    if not target_user_id:
        await message.reply_text("❌ User not found or not banned!")
        return
    active_ban = await get_active_ban(chat.id, target_user_id)
    if not active_ban:
        await message.reply_text("❌ This user is not currently banned!")
        return
    try:
        await context.bot.unban_chat_member(chat.id, target_user_id)
    except Exception as e:
        await message.reply_text(f"❌ Error unbanning user: {e}")
        return
    await unban_user_in_db(chat.id, target_user_id)
    await message.reply_text("✅ User has been unbanned successfully!")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /mute <username/ID> <duration> <reason>\nDuration: 10m, 1h, 1d, 1w")
        return
    target = context.args[0]
    duration_str = context.args[1]
    reason = " ".join(context.args[2:]) if len(context.args) > 2 else "No reason provided"
    duration_seconds = parse_duration(duration_str)
    if duration_seconds == 0:
        await message.reply_text("❌ Invalid duration! Use 10m, 1h, 1d, 1w")
        return
    max_duration_seconds = 366 * 24 * 60 * 60
    if duration_seconds > max_duration_seconds:
        await message.reply_text("❌ Duration exceeds maximum (366 days)!")
        return
    target_user = None
    target_username = None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            target_id = int(target)
            target_user = await context.bot.get_chat_member(chat.id, target_id)
            if target_user.user:
                target_user = target_user.user
                target_username = target_user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    existing_mute = await get_active_mute(chat.id, target_user.id)
    if existing_mute:
        await message.reply_text("❌ This user is already muted!")
        return
    until_date = int((datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).timestamp())
    try:
        permissions = ChatPermissions(
            can_send_messages=False, can_send_photos=False, can_send_videos=False,
            can_send_documents=False, can_send_audios=False, can_send_voice_notes=False,
            can_send_video_notes=False, can_send_polls=False
        )
        await context.bot.restrict_chat_member(chat.id, target_user.id, permissions, until_date=until_date)
    except Exception as e:
        await message.reply_text(f"❌ Error muting user: {e}")
        return
    duration_minutes = duration_seconds // 60
    muted_by = message.from_user.id if message.from_user else 0
    user_warnings = await get_user_warnings(chat.id, target_user.id)
    warning_count = len(user_warnings)
    mute_reason = await generate_mute_reason_with_gemini(warning_count, user_warnings, f"Manual mute: {reason}")
    await add_mute(chat.id, target_user.id, muted_by, mute_reason, duration_minutes, target_username)
    if duration_seconds >= 604800:
        weeks = duration_seconds // 604800
        duration_display = f"{weeks} week{'s' if weeks > 1 else ''}"
    elif duration_seconds >= 86400:
        days = duration_seconds // 86400
        duration_display = f"{days} day{'s' if days > 1 else ''}"
    elif duration_seconds >= 3600:
        hours = duration_seconds // 3600
        mins = (duration_seconds % 3600) // 60
        duration_display = f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
    else:
        duration_display = f"{duration_minutes}m"
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    mute_msg = (
        f"🔇 <b>USER MUTED</b>\n"
        f"👤 User: {user_mention}\n"
        f"🛡️ Muted by: {admin_mention}\n"
        f"⏱ Duration: {duration_display}\n"
        f"📝 Reason: {mute_reason}\n\nThis user has been muted and cannot send messages."
    )
    kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{target_user.id}_{chat.id}")]]
    await message.reply_html(mute_msg, reply_markup=InlineKeyboardMarkup(kb))


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This command is only for admins!")
        return
    if not context.args:
        await message.reply_text("❌ Usage: /unmute <username/ID>")
        return
    target = context.args[0]
    target_user_id = None
    if target.startswith("@"):
        target_username = target[1:]
        result = supabase.table('mutes').select("*").eq('chat_id', chat.id).eq('username', target_username).eq('is_active', True).execute()
        if result.data:
            target_user_id = result.data[0]['user_id']
    else:
        try:
            target_user_id = int(target)
        except ValueError:
            pass
    if not target_user_id:
        await message.reply_text("❌ User not found or not muted!")
        return
    active_mute = await get_active_mute(chat.id, target_user_id)
    if not active_mute:
        await message.reply_text("❌ This user is not currently muted!")
        return
    try:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_photos=True, can_send_videos=True,
            can_send_documents=True, can_send_audios=True, can_send_voice_notes=True,
            can_send_video_notes=True, can_send_polls=True
        )
        await context.bot.restrict_chat_member(chat.id, target_user_id, permissions, until_date=0)
    except Exception as e:
        await message.reply_text(f"❌ Error unmuting user: {e}")
        return
    await unmute_user_in_db(chat.id, target_user_id)
    try:
        target_user = await context.bot.get_chat_member(chat.id, target_user_id)
        if target_user.user:
            if target_user.user.username:
                user_mention = f"@{target_user.user.username}"
            else:
                user_mention = target_user.user.mention_html()
        else:
            user_mention = f"User {target_user_id}"
    except Exception:
        user_mention = f"User {target_user_id}"
    await message.reply_html(
        f"✅ <b>User Unmuted</b>\n\n{user_mention} has been unmuted and can now send messages in the group."
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not context.args or len(context.args) < 2:
        await message.reply_text("❌ Usage: /report <username> <reason>")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:])
    target_user = None
    target_username = None
    if target.startswith("@"):
        target_username = target[1:]
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            target_username = target_user.username
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_username = target_user.username
    else:
        try:
            target_id = int(target)
            target_user = await context.bot.get_chat_member(chat.id, target_id)
            if target_user.user:
                target_user = target_user.user
                target_username = target_user.username
        except Exception:
            await message.reply_text("❌ Could not find user.")
            return
    if not target_user:
        await message.reply_text("❌ User not found.")
        return
    try:
        target_member = await context.bot.get_chat_member(chat.id, target_user.id)
        if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text("❌ You cannot report an admin!")
            return
    except Exception as e:
        logger.error(f"Error checking if target is admin: {e}")
    reporter_username = message.from_user.username or message.from_user.first_name
    await add_report(chat.id, message.from_user.id, target_user.id, reason, reporter_username, target_username)
    await message.reply_text("✅ Your report has been sent to the group admins. Thank you!")
    admins = await get_chat_admins(chat.id, context)
    user_mention = target_user.mention_html() if target_user else f"@{target_username}"
    reporter_mention = message.from_user.mention_html()
    report_msg = (
        f"🚨 <b>NEW REPORT</b>\n"
        f"📱 Group: {chat.title}\n"
        f"👤 Reported: {user_mention}\n"
        f"📝 Reporter: {reporter_mention}\n"
        f"📋 Reason: {reason}\n"
        f"⏰ Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    for admin_id in admins:
        try:
            await context.bot.send_message(admin_id, report_msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Error sending report to admin {admin_id}: {e}")


# ============================================================
# CALLBACK QUERY HANDLERS FOR MODERATION
# ============================================================

async def unban_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 4:
        return
    user_id = int(parts[2])
    chat_id = int(parts[3])
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        await unban_user_in_db(chat_id, user_id)
        try:
            target_user = await context.bot.get_chat_member(chat_id, user_id)
            user_mention = f"@{target_user.user.username}" if target_user.user and target_user.user.username else f"User {user_id}"
        except Exception:
            user_mention = f"User {user_id}"
        try:
            await query.message.edit_text(
                f"✅ <b>User Unbanned</b>\n\n{user_mention} has been unbanned.", reply_markup=None, parse_mode='HTML'
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


async def unmute_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 4:
        return
    user_id = int(parts[2])
    chat_id = int(parts[3])
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    try:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_photos=True, can_send_videos=True,
            can_send_documents=True, can_send_audios=True, can_send_voice_notes=True,
            can_send_video_notes=True, can_send_polls=True
        )
        await context.bot.restrict_chat_member(chat_id, user_id, permissions, until_date=0)
        await unmute_user_in_db(chat_id, user_id)
        try:
            target_user = await context.bot.get_chat_member(chat_id, user_id)
            user_mention = f"@{target_user.user.username}" if target_user.user and target_user.user.username else f"User {user_id}"
        except Exception:
            user_mention = f"User {user_id}"
        try:
            await query.message.edit_text(
                f"✅ <b>User Unmuted</b>\n\n{user_mention} has been unmuted.", reply_markup=None, parse_mode='HTML'
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


async def ban_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 5:
        return
    user_id = int(parts[3])
    chat_id = int(parts[4])
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        banned_by = user_id_clicker if user_id_clicker else 0
        await add_ban(chat_id, user_id, banned_by, "Banned from warning")
        kb = [[InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user_id}_{chat_id}")]]
        try:
            await query.message.edit_text(
                f"🚫 User has been banned!\n\nUser ID: {user_id}", reply_markup=InlineKeyboardMarkup(kb)
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


async def mute_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 5:
        return
    user_id = int(parts[3])
    chat_id = int(parts[4])
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    try:
        until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        permissions = ChatPermissions(
            can_send_messages=False, can_send_photos=False, can_send_videos=False,
            can_send_documents=False, can_send_audios=False, can_send_voice_notes=False,
            can_send_video_notes=False, can_send_polls=False
        )
        await context.bot.restrict_chat_member(chat_id, user_id, permissions, until_date=until_date)
        user_warnings = await get_user_warnings(chat_id, user_id)
        warning_count = len(user_warnings)
        mute_reason = await generate_mute_reason_with_gemini(warning_count, user_warnings, "Muted from warning")
        muted_by = user_id_clicker if user_id_clicker else 0
        await add_mute(chat_id, user_id, muted_by, mute_reason, 60)
        kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat_id}")]]
        try:
            await query.message.edit_text(
                f"🔇 User muted for 1 hour!\n\nUser ID: {user_id}\nReason: {mute_reason}",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"❌ Error: {e}", show_alert=True)


# ============================================================
# ADMIN KEYBOARD
# ============================================================

async def show_admin_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("⚠️ This keyboard is only visible to admins!")
        return
    keyboard = [
        [
            InlineKeyboardButton("⚠️ Warn", callback_data=f"cmd_warn_{chat.id}"),
            InlineKeyboardButton("🔇 Mute", callback_data=f"cmd_mute_{chat.id}"),
            InlineKeyboardButton("🚫 Ban", callback_data=f"cmd_ban_{chat.id}")
        ],
        [
            InlineKeyboardButton("🔊 Unmute", callback_data=f"cmd_unmute_{chat.id}"),
            InlineKeyboardButton("✅ Unban", callback_data=f"cmd_unban_{chat.id}")
        ],
        [
            InlineKeyboardButton("📋 Reports", callback_data=f"cmd_reports_{chat.id}"),
            InlineKeyboardButton("📢 Tag All", callback_data=f"cmd_tagall_{chat.id}")
        ]
    ]
    admin_msg = (
        "🛡️ <b>Admin Commands</b>\n\n"
        "⚠️ <b>Warn</b> — Warn a user\n"
        "🔇 <b>Mute</b> — Mute a user temporarily\n"
        "🚫 <b>Ban</b> — Ban a user permanently\n"
        "🔊 <b>Unmute</b> — Unmute a muted user\n"
        "✅ <b>Unban</b> — Unban a banned user\n"
        "📋 <b>Reports</b> — View pending reports\n"
        "📢 <b>Tag All</b> — Tag all tracked members"
    )
    await message.reply_html(admin_msg, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_keyboard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 3:
        return
    command = parts[1]
    chat_id = int(parts[2])
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    instructions = {
        "warn":    "/warn @user reason",
        "mute":    "/mute @user 1h reason",
        "ban":     "/ban @user reason",
        "unmute":  "/unmute @user",
        "unban":   "/unban <user_id>",
        "reports": "Reports are sent privately to admins. Check your DMs.",
        "tagall":  "/tagall Your announcement",
    }
    instruction_text = instructions.get(command, "Unknown command")
    try:
        await query.message.reply_html(f"<b>{command.title()} usage:</b>\n{instruction_text}")
    except Exception as e:
        logger.error(f"Error sending instructions: {e}")


# ============================================================
# TAG ALL / NOTES / FORCE SUB / FILTER DELETED
# ============================================================

async def tag_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    note_text = " ".join(context.args) if context.args else "Attention everyone!"
    members = await get_group_members(chat.id)
    if not members:
        await msg.reply_text("No members tracked yet.")
        return
    mentions = []
    for m in members:
        uid = m["user_id"]
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
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    if not context.args or len(context.args) < 2:
        await msg.reply_text("Usage: /note <name> <content>")
        return
    name = context.args[0].lower()
    content = " ".join(context.args[1:])
    added_by = msg.from_user.id if msg.from_user else 0
    await add_note(chat.id, name, content, added_by)
    await msg.reply_html(f"📝 Note <b>{name}</b> saved.")


async def get_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not context.args:
        await msg.reply_text("Usage: /get <name>")
        return
    note = await get_note(chat.id, context.args[0].lower())
    if note:
        await msg.reply_html(f"📝 <b>{context.args[0]}</b>\n\n{note['content']}")
    else:
        await msg.reply_text(f"❌ No note named '{context.args[0]}'.")


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    notes = await get_all_notes(chat.id)
    if notes:
        names = [n['name'] for n in notes]
        lines = "\n".join(f"- <code>{n}</code>" for n in names)
        await msg.reply_html(f"<b>Notes ({len(notes)}):</b>\n{lines}\n\nUse /get &lt;name&gt; to retrieve.")
    else:
        await msg.reply_text("No notes yet. Use /note <name> <content>.")


async def delnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /delnote <name>")
        return
    await delete_note(chat.id, context.args[0].lower())
    await msg.reply_html(f"🗑 Note <b>{context.args[0]}</b> deleted.")


async def forcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    if not context.args:
        channels = await get_active_force_subs(chat.id)
        if not channels:
            await msg.reply_text("No force-subscribe channels.\nUsage: /forcesub @channel")
        else:
            lines = "\n".join(
                f"- {c['channel_title']} (@{c.get('channel_username') or c['channel_id']})"
                for c in channels
            )
            await msg.reply_html(f"<b>Force Subscribe Channels:</b>\n{lines}")
        return
    try:
        co = await context.bot.get_chat(context.args[0])
        bm = await context.bot.get_chat_member(co.id, context.bot.id)
        if bm.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await msg.reply_text("I must be a member of that channel first.")
            return
        await add_force_sub(chat.id, co.id, co.title or context.args[0], co.username, msg.from_user.id if msg.from_user else 0)
        await msg.reply_html(
            f"✅ Force subscribe enabled for <b>{co.title}</b>.\nMembers must join before chatting."
        )
    except Exception as e:
        logger.error(f"forcesub: {e}")
        await msg.reply_text("Could not access that channel. Make sure I'm a member.")


async def removeforcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /removeforcesub @channel")
        return
    try:
        co = await context.bot.get_chat(context.args[0])
        await remove_force_sub(chat.id, co.id)
        await msg.reply_html(f"✅ Force subscribe removed for <b>{co.title}</b>.")
    except Exception as e:
        await msg.reply_text(f"❌ Failed: {e}")


async def filter_deleted_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = msg.chat
    if not await is_sender_admin(chat.id, msg, context):
        await msg.reply_text("⚠️ Admins only.")
        return
    members = await get_group_members(chat.id)
    removed = 0
    for m in members:
        try:
            cm = await context.bot.get_chat_member(chat.id, m["user_id"])
            user = cm.user
            if is_deleted_account(user):
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await context.bot.unban_chat_member(chat.id, user.id)
                except Exception:
                    pass
                await remove_group_member(chat.id, user.id)
                removed += 1
        except (Forbidden, BadRequest):
            await remove_group_member(chat.id, m["user_id"])
            removed += 1
        except Exception:
            pass
    await msg.reply_html(f"✅ Done. Removed <b>{removed}</b> deleted/ghost accounts.")


# ============================================================
# JOIN REQUEST HANDLER (GROUPS — original)
# ============================================================

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    if not jr:
        return
    chat = jr.chat
    user = jr.from_user
    s = await get_group_settings(chat.id)
    if not s:
        return
    await add_join_request(chat.id, user.id, user.username, user.first_name)
    if s.get("auto_approve", False):
        try:
            await context.bot.approve_chat_join_request(chat.id, user.id)
            await update_join_request_status(chat.id, user.id, "approved", context.bot.id)
        except Exception as e:
            logger.error(f"auto-approve join: {e}")


# ============================================================
# FORCE SUB CHECK + MESSAGE
# ============================================================

async def check_force_sub(chat_id: int, user_id: int, context) -> list:
    channels = await get_active_force_subs(chat_id)
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
        ch_names = ", ".join(f"<b>{fc['channel_title']}</b>" for fc in not_joined)
        text = (
            f"{user_link}, you must join the required channel(s) before chatting here.\n\n"
            f"Please join: {ch_names}\n\nThen send your message again."
        )
        kb = []
        for fc in not_joined:
            link = (f"https://t.me/{fc['channel_username']}"
                    if fc.get("channel_username")
                    else f"https://t.me/c/{str(fc['channel_id']).replace('-100', '')}")
            kb.append([InlineKeyboardButton(f"Join {fc['channel_title']}", url=link)])
        msg = await chat.send_message(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None,
        )
        if warning_timer > 0:
            await schedule_message_deletion(chat.id, msg.message_id, warning_timer)
    except Exception as e:
        logger.error(f"send_force_sub_message: {e}")


# ============================================================
# ORIGINAL BOT COMMAND HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Handle deep-link payloads (e.g. ?start=channel_{channel_id})
    if context.args:
        payload = context.args[0]
        if payload.startswith("channel_"):
            try:
                channel_id = int(payload.split("_")[1])
                settings = await get_channel_settings(channel_id)
                if settings:
                    already = await is_user_onboarded(channel_id, user.id)
                    if not already:
                        channel_chat = await context.bot.get_chat(channel_id)
                        dm_sent = await send_channel_welcome_dm(context.bot, user, channel_id, channel_chat.title, settings)
                        if dm_sent:
                            await record_user_onboarded(channel_id, user.id)
                    return
            except Exception as e:
                logger.error(f"Deep-link start error: {e}")

    keyboard = [
        [InlineKeyboardButton("➕ Add Bot to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("📋 My Groups", callback_data="my_groups"),
         InlineKeyboardButton("📢 My Channels", callback_data="my_channels")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    welcome_text = (
        f"👋 Welcome {user.mention_html()}!\n\n"
        f"I'm a powerful group & channel management bot.\n\n"
        f"<b>Group Features:</b>\n"
        f"✅ Auto-delete banned words, links, promotions\n"
        f"✅ Warn, mute, ban members\n"
        f"✅ Custom welcome messages\n"
        f"✅ Word limit, sticker protection\n\n"
        f"<b>Channel Features (NEW):</b>\n"
        f"✅ Auto-approve join requests\n"
        f"✅ Send private welcome DM to new members\n"
        f"✅ Join analytics & tracking\n"
        f"✅ Schedule posts\n\n"
        f"🚀 Get started by adding me to your group or channel!"
    )
    await update.message.reply_html(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>Bot Commands & Features</b>\n\n"
        "<b>Group Admin Commands:</b>\n"
        "/warn &lt;username&gt; &lt;reason&gt; — Warn a user\n"
        "/mute &lt;username&gt; &lt;duration&gt; &lt;reason&gt; — Mute a user\n"
        "/ban &lt;username&gt; &lt;reason&gt; — Ban a user\n"
        "/unmute &lt;username&gt; — Unmute a user\n"
        "/unban &lt;username&gt; — Unban a user\n"
        "/admin — Show admin keyboard\n"
        "/tagall &lt;message&gt; — Tag all members\n"
        "/note &lt;name&gt; &lt;content&gt; — Save a note\n"
        "/get &lt;name&gt; — Get a note\n"
        "/notes — List all notes\n"
        "/delnote &lt;name&gt; — Delete a note\n"
        "/forcesub @channel — Add force subscribe\n"
        "/removeforcesub @channel — Remove force subscribe\n"
        "/filterdeleted — Remove ghost accounts\n\n"
        "<b>Member Commands:</b>\n"
        "/report &lt;username&gt; &lt;reason&gt; — Report a user\n\n"
        "<b>Channel Commands (Private chat):</b>\n"
        "/addchannel @channel — Register a channel\n"
        "/mychannels — View your channels\n"
        "/schedulepost @channel DATE TIME message — Schedule a post\n\n"
        "<b>Private Chat:</b>\n"
        "/start — Main menu\n"
        "/mygroups — View your groups\n"
        "/help — This help message\n"
    )
    if update.message:
        await update.message.reply_html(help_text)
    else:
        try:
            await update.callback_query.message.edit_text(help_text, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in help command: {e}")


async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    groups = await get_user_groups(user_id)
    if not groups:
        text = "❌ You haven't added me to any groups yet!\n\nClick the button below to add me to a group."
        keyboard = [[InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        text = "📋 <b>Your Groups:</b>\n\nSelect a group to manage settings:"
        keyboard = []
        for group in groups:
            keyboard.append([InlineKeyboardButton(
                f"🔧 {group['chat_title']}",
                callback_data=f"group_settings_{group['chat_id']}"
            )])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in my_groups: {e}")
    else:
        await update.message.reply_html(text, reply_markup=reply_markup)


async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    if not settings:
        try:
            await query.message.edit_text("❌ Group not found!")
        except BadRequest:
            pass
        return
    banned_words = await get_banned_words(chat_id)
    banned_words_text = ", ".join(banned_words) if banned_words else "None"
    promo_status = "✅ Enabled" if settings.get('delete_promotions', False) else "❌ Disabled"
    link_status = "✅ Enabled" if settings.get('delete_links', False) else "❌ Disabled"
    word_limit = settings.get('max_word_count', 0)
    word_limit_status = f"{word_limit} words" if word_limit > 0 else "❌ Disabled"
    timer_val = settings.get('warning_timer', 30)
    timer_display = f"{timer_val // 60}m" if timer_val >= 60 else f"{timer_val}s"
    welcome_msg = settings.get('welcome_message', None)
    welcome_status = "✅ Enabled" if welcome_msg else "❌ Not Set"
    welcome_timer_val = settings.get('welcome_timer', 0)
    welcome_timer_display = f"{welcome_timer_val}s" if welcome_timer_val > 0 else "Never"
    delete_join = settings.get('delete_join_messages', False)
    delete_join_status = "✅ Enabled" if delete_join else "❌ Disabled"
    max_warnings = settings.get('max_warnings', 3)
    require_approval = settings.get('require_approval', False)
    require_approval_status = "✅ Enabled" if require_approval else "❌ Disabled"
    auto_approve = settings.get('auto_approve', False)
    auto_approve_status = "✅ Enabled" if auto_approve else "❌ Disabled"
    sticker_protect = settings.get('sticker_protect', False)
    sticker_protect_status = "✅ Enabled" if sticker_protect else "❌ Disabled"
    force_sub_channel = settings.get('force_sub_channel', None)
    force_sub_status = f"@{force_sub_channel}" if force_sub_channel else "❌ Not Set"
    member_count = settings.get('member_count', 0)
    text = (
        f"⚙️ <b>Group Settings</b>\n"
        f"📱 Group: {settings['chat_title']}\n"
        f"👤 Added by: @{settings['added_by_username']}\n"
        f"👥 Member Count: {member_count}\n"
        f"🎉 Welcome Message: {welcome_status} (Delete: {welcome_timer_display})\n"
        f"🚫 Banned Words: {banned_words_text}\n"
        f"📝 Max Word Limit: {word_limit_status}\n"
        f"📨 Delete Promotions: {promo_status}\n"
        f"🌐 Delete Links: {link_status}\n"
        f"⏱ Warning Timer: {timer_display}\n"
        f"⚠️ Max Warnings: {max_warnings}\n"
        f"👋 Delete Join Messages: {delete_join_status}\n"
        f"🔐 Require Approval: {require_approval_status}\n"
        f"✅ Auto Approve: {auto_approve_status}\n"
        f"🎭 Sticker Protect: {sticker_protect_status}\n"
        f"📢 Force Subscribe: {force_sub_status}\n"
    )
    keyboard = [
        [InlineKeyboardButton("🎉 Set Welcome Message", callback_data=f"set_welcome_{chat_id}")],
        [InlineKeyboardButton("➕ Add Banned Word", callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("➖ Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("📝 Word Count Limit", callback_data=f"set_word_limit_{chat_id}"),
         InlineKeyboardButton("⏱ Warning Timer", callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton("📨 Toggle Promotions", callback_data=f"toggle_promo_{chat_id}"),
         InlineKeyboardButton("🌐 Toggle Links", callback_data=f"toggle_links_{chat_id}")],
        [InlineKeyboardButton("⚠️ Max Warnings", callback_data=f"set_max_warnings_{chat_id}"),
         InlineKeyboardButton("👋 Toggle Join Delete", callback_data=f"toggle_join_delete_{chat_id}")],
        [InlineKeyboardButton("🔙 Back to Groups", callback_data="my_groups")]
    ]
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message in settings: {e}")


async def set_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_welcome'
    text = (
        "🎉 <b>Set Welcome Message</b>\n\n"
        "Variables: <code>{BOT_NAME}</code> <code>{USER_NAME}</code> <code>{USER_ID}</code> <code>{CHAT_TITLE}</code>\n"
        "Button Format: <code>[Button Text](https://link)</code>\n\n"
        "⏱ After setting, I'll ask for an auto-delete timer.\n"
        "✏️ Send your welcome message now:"
    )
    await query.message.edit_text(text, parse_mode='HTML')


async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'add_word'
    await query.message.edit_text("✏️ Send the word you want to ban.\n\n💡 Send /cancel to cancel.")


async def remove_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    banned_words = await get_banned_words(chat_id)
    if not banned_words:
        await query.answer("No banned words to remove!", show_alert=True)
        return
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'remove_word'
    await query.message.edit_text(f"✏️ Current banned words:\n{', '.join(banned_words)}\n\nSend the word to remove.\n\n💡 /cancel to cancel.")


async def set_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_timer'
    await query.message.edit_text(
        "⏱ <b>Set Warning Deletion Time</b>\nExamples: <code>5s</code> <code>1m</code> <code>30</code>\n\n✏️ Send duration now.",
        parse_mode='HTML'
    )


async def set_word_limit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_word_limit'
    await query.message.edit_text(
        "📝 <b>Set Max Word Count</b>\nExamples: <code>100</code> <code>35</code> <code>0</code> (unlimited)\n\n✏️ Send the max number of words:",
        parse_mode='HTML'
    )


async def set_max_warnings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_max_warnings'
    await query.message.edit_text(
        "⚠️ <b>Set Maximum Warnings (3–31)</b>\n\nAuto-mute triggers when user hits this limit.\n\n✏️ Send a number between 3 and 31:",
        parse_mode='HTML'
    )


async def toggle_promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_promotions', False)
    await update_promotion_setting(chat_id, new_value)
    await query.answer(f"Promotion deletion {'enabled' if new_value else 'disabled'}!", show_alert=True)
    await group_settings_handler(update, context)


async def toggle_links_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_links', False)
    await update_link_setting(chat_id, new_value)
    await query.answer(f"Link deletion {'enabled' if new_value else 'disabled'}!", show_alert=True)
    await group_settings_handler(update, context)


async def toggle_join_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_join_messages', False)
    await update_delete_join_messages(chat_id, new_value)
    await query.answer(f"Join message deletion {'enabled' if new_value else 'disabled'}!", show_alert=True)
    await group_settings_handler(update, context)


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' not in context.user_data:
        return
    chat_id = context.user_data['awaiting_input']
    action = context.user_data['action']
    user_text = update.message.text.strip()

    text = None

    if action == 'set_welcome':
        context.user_data['welcome_message_html'] = user_text
        context.user_data['action'] = 'set_welcome_timer'
        await update.message.reply_html(
            "⏱ <b>Set Welcome Auto-Delete Timer</b>\nExamples: <code>0</code> <code>30</code> <code>1m</code>\n\n✏️ Send the time:"
        )
        return

    elif action == 'set_welcome_timer':
        welcome_html = context.user_data.get('welcome_message_html', '')
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text.strip())
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            timer_seconds = value * 60 if unit == 'm' else value
            display_unit = "minutes" if unit == 'm' else "seconds"
            await update_welcome_message(chat_id, welcome_html, timer_seconds)
            text = f"✅ Welcome message set! Auto-delete in <b>{value} {display_unit}</b>"
        else:
            await update.message.reply_html("❌ Invalid format! Use '0', '30', or '1m'")
            return

    elif action == 'add_word':
        user_text_lower = user_text.lower()
        await add_banned_word(chat_id, user_text_lower, update.effective_user.id)
        text = f"✅ Word '<b>{user_text_lower}</b>' added to banned words!"

    elif action == 'remove_word':
        user_text_lower = user_text.lower()
        await remove_banned_word(chat_id, user_text_lower)
        text = f"✅ Word '<b>{user_text_lower}</b>' removed from banned words!"

    elif action == 'set_timer':
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            seconds = value * 60 if unit == 'm' else value
            display_unit = "minutes" if unit == 'm' else "seconds"
            await update_warning_timer(chat_id, seconds)
            text = f"✅ Warning deletion timer set to <b>{value} {display_unit}</b>!"
        else:
            await update.message.reply_html("❌ Invalid format! Use '10s' or '1m'")
            return

    elif action == 'set_word_limit':
        if user_text.isdigit():
            limit = int(user_text)
            await update_word_limit(chat_id, limit)
            text = "✅ Word limit disabled." if limit == 0 else f"✅ Max word count set to <b>{limit} words</b>!"
        else:
            await update.message.reply_html("❌ Invalid number!")
            return

    elif action == 'set_max_warnings':
        if user_text.isdigit():
            max_warnings = int(user_text)
            if 3 <= max_warnings <= 31:
                await update_max_warnings(chat_id, max_warnings)
                text = f"✅ Max warnings set to <b>{max_warnings}</b>!"
            else:
                await update.message.reply_html("❌ Must be between 3 and 31!")
                return
        else:
            await update.message.reply_html("❌ Invalid number!")
            return

    # NEW: Channel welcome DM input
    elif action == 'ch_set_welcome':
        await upsert_channel_settings(chat_id, {"welcome_message": user_text})
        text = "✅ Channel welcome DM saved! New members will receive this message after joining."
        kb = [[InlineKeyboardButton("🔙 Back to Channel Settings", callback_data=f"ch_settings_{chat_id}")]]
        for key in ['awaiting_input', 'action', 'welcome_message_html']:
            context.user_data.pop(key, None)
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
        return

    # NEW: Channel approval delay input
    elif action == 'ch_set_delay':
        if user_text.isdigit():
            delay = int(user_text)
            await upsert_channel_settings(chat_id, {"approval_delay": delay})
            text = f"✅ Approval delay set to <b>{delay} seconds</b>!"
        else:
            await update.message.reply_html("❌ Invalid number! Send a number like 0, 5, or 30.")
            return

    # Clear state
    for key in ['awaiting_input', 'action', 'welcome_message_html']:
        context.user_data.pop(key, None)

    if text:
        # Decide back button destination
        if action.startswith('ch_'):
            kb = [[InlineKeyboardButton("🔙 Back to Channel Settings", callback_data=f"ch_settings_{chat_id}")]]
        else:
            kb = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ['awaiting_input', 'action', 'welcome_message_html']:
        context.user_data.pop(key, None)
    await update.message.reply_text("✅ Operation cancelled.")


def parse_welcome_message(html_template: str, bot_name: str, user_name: str, user_id: int, chat_title: str) -> tuple:
    message = html_template.replace('{BOT_NAME}', bot_name)
    message = message.replace('{USER_NAME}', user_name)
    message = message.replace('{USER_ID}', str(user_id))
    message = message.replace('{CHAT_TITLE}', chat_title)
    button_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    buttons = re.findall(button_pattern, message)
    message = re.sub(button_pattern, '', message).strip()
    return message, buttons


async def send_welcome_message(chat: any, new_member: any, context: ContextTypes.DEFAULT_TYPE, settings: dict):
    try:
        if settings and settings.get('welcome_message'):
            welcome_html = settings['welcome_message']
            bot_name = context.bot.username or "Bot"
            user_name = new_member.first_name or new_member.username or "Member"
            user_id = new_member.id
            message_text, buttons = parse_welcome_message(welcome_html, bot_name, user_name, user_id, chat.title)

            user_lang = new_member.language_code or 'en'
            if user_lang != 'en':
                texts_to_translate = [message_text]
                for btn_text, _ in buttons:
                    texts_to_translate.append(btn_text)
                text_to_translate = "\n---\n".join(texts_to_translate)
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
                prompt = f"Translate the following texts to {user_lang}, preserving all HTML tags, emojis, and formatting. Each section separated by --- should be translated separately and output in the same order separated by ---:\n{text_to_translate}"
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 500}}
                headers = {"Content-Type": "application/json"}
                try:
                    response = http_requests.post(url, headers=headers, data=json.dumps(payload))
                    if response.status_code == 200:
                        result = response.json()
                        translated_text = result['candidates'][0]['content']['parts'][0]['text']
                        translated_parts = translated_text.split("\n---\n")
                        if len(translated_parts) == len(texts_to_translate):
                            message_text = translated_parts[0]
                            for i in range(len(buttons)):
                                buttons[i] = (translated_parts[i + 1], buttons[i][1])
                except Exception as e:
                    logger.error(f"Error translating welcome message: {e}")

            keyboard = []
            if buttons:
                for i in range(0, len(buttons), 2):
                    row = []
                    btn_text, btn_url = buttons[i]
                    row.append(InlineKeyboardButton(btn_text, url=btn_url))
                    if i + 1 < len(buttons):
                        btn_text2, btn_url2 = buttons[i + 1]
                        row.append(InlineKeyboardButton(btn_text2, url=btn_url2))
                    keyboard.append(row)
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            try:
                welcome_msg = await chat.send_message(message_text, reply_markup=reply_markup, parse_mode='HTML')
                welcome_timer = settings.get('welcome_timer', 0)
                if welcome_timer > 0:
                    await schedule_message_deletion(chat.id, welcome_msg.message_id, welcome_timer)
            except BadRequest as e:
                logger.error(f"Error sending welcome message: {e}")
                default_welcome = f"👋 Welcome {new_member.mention_html()} to {chat.title}!"
                try:
                    await chat.send_message(default_welcome, parse_mode='HTML')
                except Exception as ex:
                    logger.error(f"Error sending fallback welcome: {ex}")
        else:
            default_welcome = f"👋 Welcome {new_member.mention_html()} to {chat.title}!"
            try:
                await chat.send_message(default_welcome, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Error sending default welcome: {e}")
    except Exception as e:
        logger.error(f"Error in send_welcome_message: {e}")


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_chat_member = update.my_chat_member
    chat = my_chat_member.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    new_member = my_chat_member.new_chat_member
    old_member = my_chat_member.old_chat_member
    if old_member.status == ChatMemberStatus.LEFT and new_member.status != ChatMemberStatus.LEFT:
        added_by = my_chat_member.from_user
        try:
            member = await chat.get_member(added_by.id)
            if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                await chat.send_message("⚠️ Only group admins can add me!")
                await chat.leave()
                return
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            await chat.leave()
            return
        try:
            bot_member = await chat.get_member(context.bot.id)
            bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            bot_is_admin = False
        if not bot_is_admin:
            await chat.send_message(
                "⚠️ Please make me an admin with 'Delete Messages' permission!\n\nI'll leave now."
            )
            await chat.leave()
            return
        username = added_by.username or f"user_{added_by.id}"
        chat_username = chat.username if hasattr(chat, 'username') else None
        await add_group_to_db(chat.id, chat.title, added_by.id, username, bot_is_admin, chat_username)
        welcome_text = (
            f"🎉 Thank you for adding me!\n"
            f"✅ I'm now protecting this group!\n"
            f"👤 Added by: @{username}\n\n"
            f"<b>Admin Commands:</b>\n"
            f"/warn /mute /ban /unmute /unban /admin\n\n"
            f"<b>Member Commands:</b>\n"
            f"/report — Report a user to admins\n\n"
            f"⚙️ Configure settings in private chat → My Groups"
        )
        await chat.send_message(welcome_text, parse_mode='HTML')


async def user_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member_update = update.chat_member
    if not chat_member_update:
        return
    chat = chat_member_update.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    new_member = chat_member_update.new_chat_member
    old_member = chat_member_update.old_chat_member
    user = new_member.user

    if old_member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED] and new_member.status == ChatMemberStatus.MEMBER:
        logger.info(f"New member {user.id} joined group {chat.id}")
        settings = await get_group_settings(chat.id)
        await upsert_user(user.id, user.username, user.first_name, user.last_name)
        await upsert_group_member(chat.id, user.id, user.username, user.first_name)
        await increment_member_count(chat.id)

        if settings and settings.get('auto_approve', False):
            try:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                await update_join_request_status(chat.id, user.id, 'approved', context.bot.id)
            except Exception:
                pass

        if settings and settings.get('delete_join_messages', False):
            context.user_data['last_join_message_id'] = None

        is_subscribed, missing_channels = await check_user_force_sub(chat.id, user.id, context)
        if not is_subscribed and missing_channels:
            timer = settings.get('force_sub_message_timer', 60) if settings else 60
            channel_links = "\n".join([
                f"• <a href='https://t.me/{fs['channel_username']}'>{fs['channel_title'] or fs['channel_username']}</a>"
                if fs.get("channel_username") else f"• {fs.get('channel_title', 'Channel')}"
                for fs in missing_channels
            ])
            force_msg = await chat.send_message(
                f"👋 Welcome {user.mention_html()}!\n\n"
                f"⚠️ Please subscribe to the following channel(s) to stay in this group:\n{channel_links}",
                parse_mode='HTML'
            )
            if timer > 0:
                await schedule_message_deletion(chat.id, force_msg.message_id, timer)
            return

        await send_welcome_message(chat, user, context, settings)

    elif new_member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        logger.info(f"Member {user.id} left/banned from group {chat.id}")
        await remove_group_member(chat.id, user.id)
        await decrement_member_count(chat.id)


async def send_warning_with_count(chat, user_id, username, reason, context, offense_type="general"):
    warnings = await get_user_warnings(chat.id, user_id)
    warning_count = len(warnings)
    settings = await get_group_settings(chat.id)
    max_warnings = settings.get('max_warnings', 3) if settings else 3
    user_mention = f"@{username}" if username else f"User {user_id}"

    if offense_type == "banned_word":
        warning_msg = f"⚠️ {user_mention}, your message was hidden because it contained a banned word."
        await chat.send_message(warning_msg)
        return

    await add_warning(chat.id, user_id, 0, reason, username)
    warning_count += 1
    warning_msg = (
        f"⚠️ <b>WARNING #{warning_count}/{max_warnings}</b>\n"
        f"👤 User: {user_mention}\n"
        f"📝 Reason: {reason}\n\nThis user has been warned by the bot."
    )
    if warning_count >= max_warnings:
        updated_warnings = await get_user_warnings(chat.id, user_id)
        mute_reason = await generate_mute_reason_with_gemini(warning_count, updated_warnings, f"Maximum warnings: {offense_type}")
        until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        try:
            permissions = ChatPermissions(
                can_send_messages=False, can_send_photos=False, can_send_videos=False,
                can_send_documents=False, can_send_audios=False, can_send_voice_notes=False,
                can_send_video_notes=False, can_send_polls=False
            )
            await context.bot.restrict_chat_member(chat.id, user_id, permissions, until_date=until_date)
            await add_mute(chat.id, user_id, 0, mute_reason, 60, username)
            mute_msg = (
                f"🔇 <b>USER MUTED (AUTO)</b>\n"
                f"👤 User: {user_mention}\n"
                f"🛡️ Muted by: Bot\n"
                f"⏱ Duration: 1 hour\n"
                f"📝 Reason: {mute_reason}\n\n"
                f"User has reached {max_warnings} warnings and has been automatically muted."
            )
            kb = [[InlineKeyboardButton("🔊 Unmute User", callback_data=f"unmute_user_{user_id}_{chat.id}")]]
            await chat.send_message(mute_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        except Exception as e:
            logger.error(f"Error auto-muting user: {e}")
    await chat.send_message(warning_msg, parse_mode='HTML')


def contains_link_in_caption(caption: str, caption_entities: list) -> bool:
    if not caption:
        return False
    if caption_entities:
        for entity in caption_entities:
            if entity.type in [MessageEntity.TEXT_LINK, MessageEntity.URL]:
                return True
    link_patterns = [r'https?://\S+', r'www\.\S+', r't\.me/\S+']
    for pattern in link_patterns:
        if re.search(pattern, caption, re.IGNORECASE):
            return True
    return False


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    chat = message.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    settings = await get_group_settings(chat.id)
    if not settings:
        return
    warning_timer = settings.get('warning_timer', 30)
    is_admin_or_exempt = False
    if message.from_user and message.from_user.id == 1087968824 or (message.sender_chat and message.sender_chat.id == chat.id):
        is_admin_or_exempt = True
    else:
        try:
            if message.from_user:
                member = await chat.get_member(message.from_user.id)
                if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                    is_admin_or_exempt = True
        except Exception:
            pass
    if message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        is_admin_or_exempt = True
    if is_admin_or_exempt:
        return

    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username or message.from_user.first_name if message.from_user else "Anonymous"

    if message.from_user and user_id:
        await upsert_user(user_id, message.from_user.username, message.from_user.first_name,
                          getattr(message.from_user, 'last_name', None))
        await upsert_group_member(chat.id, user_id, message.from_user.username, message.from_user.first_name)

    if settings.get('sticker_protect', False) and message.sticker:
        try:
            await message.delete()
            sticker_warn = await chat.send_message(
                f"⚠️ @{username}, stickers are not allowed in this group.", parse_mode='HTML'
            )
            if warning_timer > 0:
                await schedule_message_deletion(chat.id, sticker_warn.message_id, warning_timer)
        except Exception as e:
            logger.error(f"Error deleting sticker: {e}")
        return

    if settings.get('delete_links', False) and message.photo and message.caption:
        if contains_link_in_caption(message.caption, message.caption_entities):
            try:
                await message.delete()
                await send_warning_with_count(chat, user_id, username,
                                              "Links in photo captions are not allowed", context, "photo_caption_link")
                return
            except Exception as e:
                logger.error(f"Error deleting photo with caption link: {e}")

    if message.text:
        max_word_count = settings.get('max_word_count', 0)
        if max_word_count > 0:
            word_count = len(message.text.split())
            if word_count > max_word_count:
                try:
                    await message.delete()
                    await send_warning_with_count(chat, user_id, username,
                                                  f"Message too long ({word_count} words. Max: {max_word_count})",
                                                  context, "word_limit")
                    return
                except Exception as e:
                    logger.error(f"Error deleting long message: {e}")

        if settings.get('delete_promotions', False):
            is_promotion = False
            reason = "promotional content"
            if is_forwarded_or_channel_message(message):
                is_promotion = True
                reason = "forwarded or channel message"
            elif message.via_bot:
                is_promotion = True
                reason = "sent via bot"
            elif message.from_user and message.from_user.is_bot:
                is_promotion = True
                reason = "bot message"
            if message.text:
                emoji_pattern = r'[\U0001F000-\U0001FFFF]|[\U00002600-\U000027BF]|[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|[\U0001F680-\U0001F6FF]|[\u200d\u2600-\u26FF\u2700-\u27BF]'
                emojis = re.findall(emoji_pattern, message.text)
                emoji_count = len(emojis)
                text_len = len(message.text)
                if emoji_count > 15 or (text_len > 10 and (emoji_count / text_len) > 0.4):
                    is_promotion = True
                    reason = "too many emojis"
            if is_promotion:
                try:
                    await message.delete()
                    await send_warning_with_count(chat, user_id, username, f"{reason} is not allowed",
                                                  context, reason.replace(" ", "_"))
                    return
                except Exception as e:
                    logger.error(f"Error deleting promotional message: {e}")

        if settings.get('delete_links', False):
            link_pattern = r'(https?://\S+|www\.\S+|t\.me/\S+)'
            has_link = bool(re.search(link_pattern, message.text))
            if message.entities:
                for entity in message.entities:
                    if entity.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                        has_link = True
            if has_link:
                try:
                    await message.delete()
                    await send_warning_with_count(chat, user_id, username,
                                                  "Links are not allowed in this group", context, "link")
                    return
                except Exception as e:
                    logger.error(f"Error deleting link message: {e}")

        banned_words = await get_banned_words(chat.id)
        if not banned_words:
            return
        message_text = message.text.lower()
        for word in banned_words:
            pattern = r'\b' + re.escape(word) + r'\b'
            if re.search(pattern, message_text):
                try:
                    await message.delete()
                    await send_warning_with_count(chat, user_id, username, "banned word", context, "banned_word")
                    return
                except Exception as e:
                    logger.error(f"Error deleting message with banned word: {e}")
                    return


# ============================================================
# CALLBACK QUERY ROUTER
# ============================================================

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "my_groups":
        await my_groups_handler(update, context)
    elif data == "my_channels":
        await my_channels_command(update, context)
    elif data == "how_to_add_channel":
        await query.answer()
        try:
            await query.message.edit_text(
                "📢 <b>How to Add a Channel</b>\n\n"
                "1. Add me as admin to your channel\n"
                "2. Give me <b>Invite Users</b> + <b>Manage Channel</b> permissions\n"
                "3. Enable <b>Join Requests</b> in channel settings\n"
                "4. Come back here and use /addchannel @yourchannel\n\n"
                "That's it! I'll auto-approve new members and send them welcome DMs.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_channels")]])
            )
        except BadRequest:
            pass
    elif data == "help":
        await help_command(update, context)
    elif data == "back_to_main":
        await start(update, context)
    elif data.startswith("group_settings_"):
        await group_settings_handler(update, context)
    elif data.startswith("set_welcome_"):
        await set_welcome_handler(update, context)
    elif data.startswith("add_word_"):
        await add_word_handler(update, context)
    elif data.startswith("remove_word_"):
        await remove_word_handler(update, context)
    elif data.startswith("set_timer_"):
        await set_timer_handler(update, context)
    elif data.startswith("set_word_limit_"):
        await set_word_limit_handler(update, context)
    elif data.startswith("toggle_promo_"):
        await toggle_promo_handler(update, context)
    elif data.startswith("toggle_links_"):
        await toggle_links_handler(update, context)
    elif data.startswith("toggle_join_delete_"):
        await toggle_join_delete_handler(update, context)
    elif data.startswith("set_max_warnings_"):
        await set_max_warnings_handler(update, context)
    # Moderation callbacks
    elif data.startswith("unban_user_"):
        await unban_callback_handler(update, context)
    elif data.startswith("unmute_user_"):
        await unmute_callback_handler(update, context)
    elif data.startswith("ban_from_warn_"):
        await ban_from_warn_callback_handler(update, context)
    elif data.startswith("mute_from_warn_"):
        await mute_from_warn_callback_handler(update, context)
    elif data.startswith("cmd_"):
        await admin_keyboard_callback_handler(update, context)
    # NEW: Channel callbacks
    elif data.startswith("ch_settings_"):
        await channel_settings_handler(update, context)
    elif data.startswith("ch_analytics_"):
        await channel_analytics_handler(update, context)
    elif data.startswith("ch_toggle_approve_"):
        await channel_toggle_approve_callback(update, context)
    elif data.startswith("ch_set_welcome_"):
        await channel_set_welcome_callback(update, context)
    elif data.startswith("ch_set_delay_"):
        await channel_set_delay_callback(update, context)
    elif data.startswith("ch_approve_"):
        await channel_approve_callback(update, context)
    elif data.startswith("ch_reject_"):
        await channel_reject_callback(update, context)


# ============================================================
# FASTAPI / WEBHOOK SETUP
# ============================================================

@app.on_event("startup")
async def startup_event():
    global ptb_application

    if ptb_application is None:
        ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Private chat commands
        ptb_application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("help", help_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("mychannels", my_channels_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("addchannel", add_channel_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("schedulepost", schedule_post_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("cancel", cancel_handler, filters.ChatType.PRIVATE))

        # Group moderation commands
        ptb_application.add_handler(CommandHandler("warn", warn_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("mute", mute_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("unmute", unmute_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("ban", ban_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("unban", unban_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("report", report_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("admin", show_admin_keyboard, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("tagall", tag_all_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("note", note_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("get", get_note_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("notes", notes_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("delnote", delnote_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("forcesub", forcesub_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("removeforcesub", removeforcesub_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("filterdeleted", filter_deleted_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))

        ptb_application.add_handler(CallbackQueryHandler(callback_query_router))

        # Private chat text input handler
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_input
        ))

        # Bot added/removed from group
        ptb_application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

        # User joins/leaves group
        ptb_application.add_handler(ChatMemberHandler(user_chat_member, ChatMemberHandler.CHAT_MEMBER))

        # NEW: Channel join request handler (handles both groups and channels)
        ptb_application.add_handler(ChatJoinRequestHandler(handle_channel_join_request))

        # Photo handler
        ptb_application.add_handler(MessageHandler(
            filters.PHOTO & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            check_message
        ))

        # Sticker handler
        ptb_application.add_handler(MessageHandler(
            filters.Sticker.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            check_message
        ))

        # Text message handler
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            check_message
        ))

        await ptb_application.initialize()
        await ptb_application.start()

        if WEBHOOK_URL:
            try:
                await ptb_application.bot.set_webhook(
                    url=WEBHOOK_URL,
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "callback_query",
                        "my_chat_member",
                        "chat_member",
                        "chat_join_request",
                    ]
                )
                logger.info(f"Webhook set to {WEBHOOK_URL}")
            except RetryAfter as e:
                logger.warning(f"Rate limited when setting webhook. Retry in {e.retry_after}s...")
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")
        else:
            logger.error("WEBHOOK_URL is not set!")


@app.post("/webhook/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_application.bot)
        await ptb_application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return Response(status_code=500)


@app.api_route("/", methods=["GET", "POST"])
async def health_check():
    return {"status": "ok", "message": "Bot is running"}


# ============================================================
# CRON JOB ENDPOINTS
# ============================================================

@app.get("/run-cleanup")
async def run_cleanup_job():
    """Delete scheduled messages, cleanup expired mutes, send scheduled posts."""
    if ptb_application is None:
        await startup_event()

    deleted_count = 0
    unmuted_count = 0
    posts_sent = 0

    # Cleanup expired mutes
    try:
        unmuted_count = await cleanup_expired_mutes(ptb_application.bot)
    except Exception as e:
        logger.error(f"Error in mute cleanup: {e}")

    # Delete scheduled messages
    try:
        due_items = await get_due_deletions()
        for item in (due_items or []):
            chat_id = item['chat_id']
            message_id = item['message_id']
            row_id = item['id']
            try:
                await ptb_application.bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
            await remove_pending_deletion(row_id)
    except Exception as e:
        logger.error(f"Error in cleanup job: {e}")

    # Send scheduled posts
    try:
        due_posts = await get_due_scheduled_posts()
        for post in (due_posts or []):
            try:
                buttons_json = post.get('buttons_json')
                reply_markup = None
                if buttons_json:
                    buttons_data = json.loads(buttons_json)
                    kb = []
                    for row in buttons_data:
                        kb.append([InlineKeyboardButton(btn['text'], url=btn['url']) for btn in row])
                    reply_markup = InlineKeyboardMarkup(kb)
                await ptb_application.bot.send_message(
                    chat_id=post['channel_id'],
                    text=post['content'],
                    parse_mode=post.get('parse_mode', 'HTML'),
                    reply_markup=reply_markup
                )
                await mark_scheduled_post_sent(post['id'])
                posts_sent += 1
                logger.info(f"Sent scheduled post {post['id']} to channel {post['channel_id']}")
            except Exception as e:
                logger.error(f"Error sending scheduled post {post['id']}: {e}")
    except Exception as e:
        logger.error(f"Error processing scheduled posts: {e}")

    return {
        "status": "ok",
        "deleted_count": deleted_count,
        "unmuted_count": unmuted_count,
        "posts_sent": posts_sent
    }


async def delete_group_and_words(chat_id: int):
    try:
        supabase.table('banned_words').delete().eq('chat_id', chat_id).execute()
        supabase.table('groups').delete().eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error deleting group {chat_id} and banned words: {e}")


@app.get("/run-group-cleanup")
async def run_group_cleanup():
    """Remove dead groups from database."""
    if ptb_application is None:
        await startup_event()
    try:
        result = supabase.table('groups').select('chat_id').execute()
        groups = [g['chat_id'] for g in result.data]
    except Exception as e:
        logger.error(f"Error getting groups: {e}")
        return {"status": "error"}

    removed = []
    for chat_id in groups:
        try:
            await ptb_application.bot.get_chat(chat_id)
        except Forbidden as e:
            logger.info(f"Removing forbidden chat {chat_id}: {e}")
            await delete_group_and_words(chat_id)
            removed.append(chat_id)
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                logger.info(f"Removing not found chat {chat_id}: {e}")
                await delete_group_and_words(chat_id)
                removed.append(chat_id)
            else:
                logger.warning(f"Other BadRequest for {chat_id}: {e}")
        except RetryAfter as e:
            logger.warning(f"Rate limit hit: {e}")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error(f"Unexpected error for {chat_id}: {e}")

    return {"status": "ok", "removed": removed}
