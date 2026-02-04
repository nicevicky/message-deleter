
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
)
from supabase import create_client, Client
from dotenv import load_dotenv
import asyncio
import requests
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # REQUIRED: Add to .env \u2192 https://your-domain.vercel.app/webhook/webhook
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize FastAPI
app = FastAPI()

# Global variable to store the Telegram Application
ptb_application = None

def is_forwarded_or_channel_message(message) -> bool:
    """
    Detects forwarded messages (including hidden channel forwards) and direct channel posts.
    """
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

# --- DATABASE HELPER FUNCTIONS ---
async def get_group_settings(chat_id: int):
    """Get group settings"""
    try:
        result = supabase.table('groups').select("*").eq('chat_id', chat_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting group settings: {e}")
        return None

async def is_user_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is admin in the group (supports anonymous admins)"""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False

async def is_sender_admin(chat_id: int, message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the message sender is an admin (supports anonymous admins)"""
    try:
        # For anonymous admins, message.from_user might be None or the group itself
        # We need to check if the sender is actually an admin
        if message.sender_chat and message.sender_chat.id == chat_id:
            # Message sent by anonymous admin (as the group)
            # We need to check if the user who triggered the command is an admin
            # For anonymous admins, we'll allow the command if it's from an admin
            # We can check by getting all admins and seeing if any match
            return True
        
        if message.from_user:
            return await is_user_admin(chat_id, message.from_user.id, context)
        
        return False
    except Exception:
        return False

async def is_callback_user_admin(chat_id: int, callback_user, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if callback user is admin (supports anonymous admins)"""
    try:
        # If callback_user is None, it might be an anonymous admin action
        if callback_user is None:
            # For inline buttons, if from_user is None, we need to verify differently
            # We'll return False and handle it in the specific callback handlers
            return False
        
        return await is_user_admin(chat_id, callback_user.id, context)
    except Exception:
        return False

async def verify_callback_admin(chat_id: int, query, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    """
    Verify if the callback is from an admin.
    Returns (is_admin, user_id, admin_message)
    """
    try:
        # Check if the user who clicked the button is an admin
        if query.from_user:
            is_admin = await is_user_admin(chat_id, query.from_user.id, context)
            if is_admin:
                return (True, query.from_user.id, None)
            else:
                return (False, query.from_user.id, "\u26a0\ufe0f This button is only for admins!")
        else:
            # Anonymous admin scenario - we can't verify directly
            # We'll try to verify by checking if any admin clicked recently
            # For safety, we'll return False
            return (False, None, "\u26a0\ufe0f This button is only for admins!")
    except Exception as e:
        logger.error(f"Error verifying callback admin: {e}")
        return (False, None, f"\u26a0\ufe0f Error verifying admin: {e}")

async def get_chat_admins(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Get list of all admins in the chat"""
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return [admin.user.id for admin in admins]
    except Exception as e:
        logger.error(f"Error getting admins: {e}")
        return []

async def add_warning(chat_id: int, user_id: int, warned_by: int, reason: str, username: str = None):
    """Add a warning for a user"""
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
    """Get all warnings for a user"""
    try:
        result = supabase.table('warnings').select("*").eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting warnings: {e}")
        return []

async def add_ban(chat_id: int, user_id: int, banned_by: int, reason: str, username: str = None):
    """Add a ban record for a user"""
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
    """Check if user has an active ban"""
    try:
        result = supabase.table('bans').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error checking ban: {e}")
        return None

async def unban_user_in_db(chat_id: int, user_id: int):
    """Mark ban as inactive in database"""
    try:
        result = supabase.table('bans').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error unbanning user: {e}")
        return None

async def add_mute(chat_id: int, user_id: int, muted_by: int, reason: str, duration_minutes: int, username: str = None):
    """Add a mute record for a user"""
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
    """Check if user has an active mute"""
    try:
        result = supabase.table('mutes').select("*").eq('chat_id', chat_id).eq('user_id', user_id).eq('is_active', True).execute()
        if result.data:
            # Check if mute has expired
            mute_data = result.data[0]
            mute_until = datetime.fromisoformat(mute_data['mute_until'])
            if datetime.now(timezone.utc) > mute_until:
                # Mute expired, mark as inactive
                await unmute_user_in_db(chat_id, user_id)
                return None
            return mute_data
        return None
    except Exception as e:
        logger.error(f"Error checking mute: {e}")
        return None

async def unmute_user_in_db(chat_id: int, user_id: int):
    """Mark mute as inactive in database"""
    try:
        result = supabase.table('mutes').update({"is_active": False}).eq('chat_id', chat_id).eq('user_id', user_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error unmuting user: {e}")
        return None

async def cleanup_expired_mutes(context: ContextTypes.DEFAULT_TYPE):
    """Check all active mutes and unmute those whose time has expired"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table('mutes').select("*").eq('is_active', True).lte('mute_until', now).execute()
        
        unmuted_count = 0
        for mute_data in result.data:
            chat_id = mute_data['chat_id']
            user_id = mute_data['user_id']
            
            try:
                # Unmute in Telegram
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
                await context.bot.restrict_member(chat_id, user_id, permissions, until_date=0)
                
                # Mark as inactive in database
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
    """Add a report for a user"""
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
    """Get pending reports for a group"""
    try:
        result = supabase.table('reports').select("*").eq('chat_id', chat_id).eq('status', 'pending').execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting reports: {e}")
        return []

async def add_group_to_db(chat_id: int, chat_title: str, added_by: int, username: str, bot_is_admin: bool):
    """Add a group to the database, preserving settings if it already exists"""
    try:
        existing_group = await get_group_settings(chat_id)
        delete_promotions = False
        delete_links = False
        warning_timer = 30
        max_word_count = 0
        welcome_message = None
        welcome_timer = 0
        delete_join_messages = False
        
        if existing_group:
            delete_promotions = existing_group.get('delete_promotions', False)
            delete_links = existing_group.get('delete_links', False)
            warning_timer = existing_group.get('warning_timer', 30)
            max_word_count = existing_group.get('max_word_count', 0)
            welcome_message = existing_group.get('welcome_message', None)
            welcome_timer = existing_group.get('welcome_timer', 0)
            delete_join_messages = existing_group.get('delete_join_messages', False)
        
        data = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "added_by": added_by,
            "added_by_username": username,
            "bot_is_admin": bot_is_admin,
            "delete_promotions": delete_promotions,
            "delete_links": delete_links,
            "warning_timer": warning_timer,
            "max_word_count": max_word_count,
            "welcome_message": welcome_message,
            "welcome_timer": welcome_timer,
            "delete_join_messages": delete_join_messages
        }
        result = supabase.table('groups').upsert(data, on_conflict='chat_id').execute()
        return result
    except Exception as e:
        logger.error(f"Error adding group to DB: {e}")
        return None

async def get_user_groups(user_id: int):
    """Get all groups added by a specific user"""
    try:
        result = supabase.table('groups').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []

async def add_banned_word(chat_id: int, word: str, added_by: int):
    """Add a banned word for a group"""
    try:
        data = {
            "chat_id": chat_id,
            "word": word.lower(),
            "added_by": added_by
        }
        result = supabase.table('banned_words').insert(data).execute()
        return result
    except Exception as e:
        logger.error(f"Error adding banned word: {e}")
        return None

async def remove_banned_word(chat_id: int, word: str):
    """Remove a banned word for a group"""
    try:
        result = supabase.table('banned_words').delete().eq('chat_id', chat_id).eq('word', word.lower()).execute()
        return result
    except Exception as e:
        logger.error(f"Error removing banned word: {e}")
        return None

async def get_banned_words(chat_id: int):
    """Get all banned words for a group"""
    try:
        result = supabase.table('banned_words').select("word").eq('chat_id', chat_id).execute()
        return [item['word'] for item in result.data]
    except Exception as e:
        logger.error(f"Error getting banned words: {e}")
        return []

async def update_promotion_setting(chat_id: int, delete_promotions: bool):
    """Update promotion deletion setting"""
    try:
        result = supabase.table('groups').update({"delete_promotions": delete_promotions}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating promotion setting: {e}")
        return None

async def update_link_setting(chat_id: int, delete_links: bool):
    """Update link deletion setting"""
    try:
        result = supabase.table('groups').update({"delete_links": delete_links}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating link setting: {e}")
        return None

async def update_warning_timer(chat_id: int, seconds: int):
    """Update the warning deletion timer"""
    try:
        result = supabase.table('groups').update({"warning_timer": seconds}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating warning timer: {e}")
        return None

async def update_word_limit(chat_id: int, limit: int):
    """Update the max word count limit (0 = disabled)"""
    try:
        result = supabase.table('groups').update({"max_word_count": limit}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating word limit: {e}")
        return None

async def update_welcome_message(chat_id: int, welcome_html: str, timer: int):
    """Update welcome message and timer"""
    try:
        result = supabase.table('groups').update({
            "welcome_message": welcome_html,
            "welcome_timer": timer
        }).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating welcome message: {e}")
        return None

async def update_delete_join_messages(chat_id: int, delete_join: bool):
    """Update delete join messages setting"""
    try:
        result = supabase.table('groups').update({"delete_join_messages": delete_join}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating delete join messages setting: {e}")
        return None

async def schedule_message_deletion(chat_id: int, message_id: int, delay_seconds: int):
    """Schedule a message for deletion via DB (for Cron)"""
    try:
        delete_time = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "delete_at": delete_time.isoformat()
        }
        supabase.table('pending_deletions').insert(data).execute()
    except Exception as e:
        logger.error(f"Error scheduling deletion: {e}")

async def get_due_deletions():
    """Get messages that are ready to be deleted"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        # Use proper Supabase query with explicit filtering
        query = supabase.table('pending_deletions').select("*").lte('delete_at', now)
        result = query.execute()
        
        # Verify we got data back
        if hasattr(result, 'data') and result.data is not None:
            return result.data
        else:
            logger.warning("get_due_deletions returned None or empty data")
            return []
    except Exception as e:
        logger.error(f"Error getting due deletions: {e}")
        return []

async def remove_pending_deletion(row_id: int):
    """Remove entry from pending_deletions table"""
    try:
        supabase.table('pending_deletions').delete().eq('id', row_id).execute()
    except Exception as e:
        logger.error(f"Error removing pending deletion row: {e}")

async def is_channel_linked_to_group(context: ContextTypes.DEFAULT_TYPE, channel_id: int, chat_id: int) -> bool:
    """Check if a channel is linked to a group"""
    try:
        channel_chat = await context.bot.get_chat(channel_id)
        return True
    except Exception:
        return False

# --- HELPER FUNCTION: PARSE DURATION ---
def parse_duration(duration_str: str) -> int:
    """
    Parse duration string and return seconds
    Supported formats: 10m, 1h, 2d, 1w
    Returns: duration in seconds
    """
    duration_str_lower = duration_str.lower().strip()
    
    # Check for 'm' (minutes)
    if duration_str_lower.endswith('m'):
        try:
            return int(duration_str_lower[:-1]) * 60
        except ValueError:
            return 0
    
    # Check for 'h' (hours)
    elif duration_str_lower.endswith('h'):
        try:
            return int(duration_str_lower[:-1]) * 60 * 60
        except ValueError:
            return 0
    
    # Check for 'd' (days)
    elif duration_str_lower.endswith('d'):
        try:
            return int(duration_str_lower[:-1]) * 60 * 60 * 24
        except ValueError:
            return 0
    
    # Check for 'w' (weeks)
    elif duration_str_lower.endswith('w'):
        try:
            return int(duration_str_lower[:-1]) * 60 * 60 * 24 * 7
        except ValueError:
            return 0
    
    # Try plain number (assume minutes)
    try:
        return int(duration_str_lower) * 60
    except ValueError:
        return 0

# --- MODERATION COMMAND HANDLERS ---
async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warn command - Admin only (supports anonymous admins)"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This command is only for admins!")
        return
    
    # Parse arguments: /warn @username reason
    if not context.args or len(context.args) < 2:
        await message.reply_text(
            "\u274c Usage: /warn <username/ID> <reason>\
\
"
            "Example: /warn @user123 Spamming in group"
        )
        return
    
    # Extract user and reason
    target = context.args[0]
    reason = " ".join(context.args[1:])
    
    # Get target user
    target_user = None
    target_username = None
    
    if target.startswith("@"):
        target_username = target[1:]
        # Try to get user by username (reply to message is better)
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
            await message.reply_text("\u274c Could not find user. Please reply to their message or use @username")
            return
    
    if not target_user:
        await message.reply_text("\u274c User not found. Please reply to the user's message or mention them with @username")
        return
    
    # For anonymous admin, use bot ID or 0 as warned_by
    warned_by = message.from_user.id if message.from_user else 0
    
    # Add warning to database
    await add_warning(chat.id, target_user.id, warned_by, reason, target_username)
    
    # Get warning count
    warnings = await get_user_warnings(chat.id, target_user.id)
    warning_count = len(warnings)
    
    # Send warning message
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    
    warn_msg = f"""
\u26a0\ufe0f <b>WARNING #{warning_count}</b>
\ud83d\udc64 User: {user_mention}
\ud83d\udee1\ufe0f Admin: {admin_mention}
\ud83d\udcdd Reason: {reason}

This user has been warned by admin.
    """
    
    # Create admin keyboard (only visible to admins)
    keyboard = [
        [InlineKeyboardButton("\ud83d\udeab Ban User", callback_data=f"ban_from_warn_{target_user.id}_{chat.id}")],
        [InlineKeyboardButton("\ud83d\udd07 Mute User", callback_data=f"mute_from_warn_{target_user.id}_{chat.id}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    sent_message = await message.reply_html(warn_msg, reply_markup=reply_markup)
    
    # Schedule deletion
    settings = await get_group_settings(chat.id)
    if settings and settings.get('warning_timer'):
        await schedule_message_deletion(chat.id, sent_message.message_id, settings.get('warning_timer'))

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ban command - Admin only (supports anonymous admins)"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This command is only for admins!")
        return
    
    # Parse arguments: /ban @username reason
    if not context.args or len(context.args) < 1:
        await message.reply_text(
            "\u274c Usage: /ban <username/ID> <reason>\
\
"
            "Example: /ban @user123 Repeated spam"
        )
        return
    
    # Extract user and reason
    target = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
    
    # Get target user
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
            await message.reply_text("\u274c Could not find user. Please reply to their message or use @username")
            return
    
    if not target_user:
        await message.reply_text("\u274c User not found. Please reply to the user's message or mention them with @username")
        return
    
    # Check if already banned
    existing_ban = await get_active_ban(chat.id, target_user.id)
    if existing_ban:
        await message.reply_text("\u274c This user is already banned!")
        return
    
    # Ban user in Telegram
    try:
        await chat.ban_member(target_user.id)
    except Exception as e:
        await message.reply_text(f"\u274c Error banning user: {e}")
        return
    
    # For anonymous admin, use bot ID or 0 as banned_by
    banned_by = message.from_user.id if message.from_user else 0
    
    # Add ban to database
    await add_ban(chat.id, target_user.id, banned_by, reason, target_username)
    
    # Send ban message
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    
    ban_msg = f"""
\ud83d\udeab <b>USER BANNED</b>
\ud83d\udc64 User: {user_mention}
\ud83d\udee1\ufe0f Banned by: {admin_mention}
\ud83d\udcdd Reason: {reason}

This user has been banned from the group.
    """
    
    # Create admin keyboard with unban button
    keyboard = [
        [InlineKeyboardButton("\u2705 Unban User", callback_data=f"unban_user_{target_user.id}_{chat.id}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await message.reply_html(ban_msg, reply_markup=reply_markup)

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unban command - Admin only (supports anonymous admins)"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This command is only for admins!")
        return
    
    # Parse arguments: /unban @username or /unban user_id
    if not context.args:
        await message.reply_text(
            "\u274c Usage: /unban <username/ID>\
\
"
            "Example: /unban @user123"
        )
        return
    
    target = context.args[0]
    target_user_id = None
    
    if target.startswith("@"):
        # Find user by username from database
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
        await message.reply_text("\u274c User not found or not banned!")
        return
    
    # Check if user is actually banned
    active_ban = await get_active_ban(chat.id, target_user_id)
    if not active_ban:
        await message.reply_text("\u274c This user is not currently banned!")
        return
    
    # Unban user in Telegram
    try:
        await chat.unban_member(target_user_id)
    except Exception as e:
        await message.reply_text(f"\u274c Error unbanning user: {e}")
        return
    
    # Mark ban as inactive in database
    await unban_user_in_db(chat.id, target_user_id)
    
    await message.reply_text("\u2705 User has been unbanned successfully!")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mute command - Admin only (supports anonymous admins) with custom duration support"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This command is only for admins!")
        return
    
    # Parse arguments: /mute @username duration reason
    # Duration format: 10m, 1h, 1d, 1w, etc.
    if not context.args or len(context.args) < 2:
        await message.reply_text(
            "\u274c Usage: /mute <username/ID> <duration> <reason>\
\
"
            "Duration examples: 10m, 1h, 1d, 1w\
"
            "Example: /mute @user123 1h Spamming"
        )
        return
    
    # Extract user, duration, and reason
    target = context.args[0]
    duration_str = context.args[1]
    reason = " ".join(context.args[2:]) if len(context.args) > 2 else "No reason provided"
    
    # Parse duration using helper function
    duration_seconds = parse_duration(duration_str)
    
    if duration_seconds == 0:
        await message.reply_text(
            "\u274c Invalid duration format!\
\
"
            "Supported formats:\
"
            "\u2022 10m (10 minutes)\
"
            "\u2022 1h (1 hour)\
"
            "\u2022 1d (1 day)\
"
            "\u2022 1w (1 week)\
"
            "\u2022 30 (30 minutes, default)\
\
"
            "Maximum duration: 366 days"
        )
        return
    
    # Validate maximum duration (366 days in seconds)
    max_duration_seconds = 366 * 24 * 60 * 60
    if duration_seconds > max_duration_seconds:
        await message.reply_text(
            "\u274c Duration exceeds maximum limit!\
\
"
            f"Maximum allowed: 366 days\
"
            f"Your duration: {duration_str}"
        )
        return
    
    # Get target user
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
            await message.reply_text("\u274c Could not find user. Please reply to their message or use @username")
            return
    
    if not target_user:
        await message.reply_text("\u274c User not found. Please reply to the user's message or mention them with @username")
        return
    
    # Check if already muted
    existing_mute = await get_active_mute(chat.id, target_user.id)
    if existing_mute:
        await message.reply_text("\u274c This user is already muted!")
        return
    
    # Calculate until_date (Unix timestamp)
    from datetime import datetime, timezone
    until_date = int((datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).timestamp())
    
    # Mute user in Telegram using restrictChatMember with until_date
    try:
        permissions = ChatPermissions(
            can_send_messages=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_documents=False,
            can_send_audios=False,
            can_send_voice_notes=False,
            can_send_video_notes=False,
            can_send_polls=False
        )
        await chat.restrict_member(target_user.id, permissions, until_date=until_date)
    except Exception as e:
        await message.reply_text(f"\u274c Error muting user: {e}")
        return
    
    # Convert duration to minutes for database
    duration_minutes = duration_seconds // 60
    
    # For anonymous admin, use bot ID or 0 as muted_by
    muted_by = message.from_user.id if message.from_user else 0
    
    # Add mute to database
    await add_mute(chat.id, target_user.id, muted_by, reason, duration_minutes, target_username)
    
    # Format duration display
    if duration_seconds >= 604800:  # 1 week
        weeks = duration_seconds // 604800
        duration_display = f"{weeks} week{'s' if weeks > 1 else ''}"
    elif duration_seconds >= 86400:  # 1 day
        days = duration_seconds // 86400
        duration_display = f"{days} day{'s' if days > 1 else ''}"
    elif duration_seconds >= 3600:  # 1 hour
        hours = duration_seconds // 3600
        mins = (duration_seconds % 3600) // 60
        duration_display = f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
    else:
        duration_display = f"{duration_minutes}m"
    
    # Send mute message
    user_mention = target_user.mention_html()
    admin_mention = "Anonymous Admin" if not message.from_user else message.from_user.mention_html()
    
    mute_msg = f"""
\ud83d\udd07 <b>USER MUTED</b>
\ud83d\udc64 User: {user_mention}
\ud83d\udee1\ufe0f Muted by: {admin_mention}
\u23f1 Duration: {duration_display}
\ud83d\udcdd Reason: {reason}

This user has been muted and cannot send messages.
    """
    
    # Create admin keyboard with unmute button
    keyboard = [
        [InlineKeyboardButton("\ud83d\udd0a Unmute User", callback_data=f"unmute_user_{target_user.id}_{chat.id}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await message.reply_html(mute_msg, reply_markup=reply_markup)

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unmute command - Admin only (supports anonymous admins)"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This command is only for admins!")
        return
    
    # Parse arguments: /unmute @username or /unmute user_id
    if not context.args:
        await message.reply_text(
            "\u274c Usage: /unmute <username/ID>\
\
"
            "Example: /unmute @user123"
        )
        return
    
    target = context.args[0]
    target_user_id = None
    
    if target.startswith("@"):
        # Find user by username from database
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
        await message.reply_text("\u274c User not found or not muted!")
        return
    
    # Check if user is actually muted
    active_mute = await get_active_mute(chat.id, target_user_id)
    if not active_mute:
        await message.reply_text("\u274c This user is not currently muted!")
        return
    
    # Unmute user in Telegram using restrictChatMember with until_date=0 (unlimited)
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
        await chat.restrict_member(target_user_id, permissions, until_date=0)
    except Exception as e:
        await message.reply_text(f"\u274c Error unmuting user: {e}")
        return
    
    # Mark mute as inactive in database
    await unmute_user_in_db(chat.id, target_user_id)
    
    # Get user info for professional message
    try:
        target_user = await context.bot.get_chat_member(chat.id, target_user_id)
        if target_user.user:
            username = target_user.user.username or target_user.user.first_name or f"User {target_user_id}"
            if target_user.user.username:
                user_mention = f"@{target_user.user.username}"
            else:
                user_mention = target_user.user.mention_html()
        else:
            user_mention = f"User {target_user_id}"
    except Exception:
        user_mention = f"User {target_user_id}"
    
    await message.reply_html(
        f"\u2705 <b>User Unmuted</b>\
\
"
        f"{user_mention} has been unmuted and can now send messages in the group."
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /report command - Available to all members, but cannot report admins"""
    message = update.message
    chat = message.chat
    
    # Parse arguments: /report @username reason
    if not context.args or len(context.args) < 2:
        await message.reply_text(
            "\u274c Usage: /report <username> <reason>\
\
"
            "Example: /report @user123 Sending spam messages\
\
"
            "Your report will be sent to the group admins privately."
        )
        return
    
    # Extract user and reason
    target = context.args[0]
    reason = " ".join(context.args[1:])
    
    # Get target user
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
            await message.reply_text("\u274c Could not find user. Please reply to their message or use @username")
            return
    
    if not target_user:
        await message.reply_text("\u274c User not found. Please reply to the user's message or mention them with @username")
        return
    
    # Check if the target user is an admin
    try:
        target_member = await context.bot.get_chat_member(chat.id, target_user.id)
        if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text("\u274c You cannot report an admin!")
            return
    except Exception as e:
        logger.error(f"Error checking if target is admin: {e}")
        # If we can't check, allow the report to proceed
        pass
    
    # Add report to database
    reporter_username = message.from_user.username or message.from_user.first_name
    await add_report(chat.id, message.from_user.id, target_user.id, reason, reporter_username, target_username)
    
    # Send confirmation to reporter
    await message.reply_text(
        "\u2705 Your report has been sent to the group admins privately. Thank you for helping keep the group safe!"
    )
    
    # Send report to all admins privately
    admins = await get_chat_admins(chat.id, context)
    user_mention = target_user.mention_html() if target_user else f"@{target_username}"
    reporter_mention = message.from_user.mention_html()
    
    report_msg = f"""
\ud83d\udea8 <b>NEW REPORT</b>
\ud83d\udcf1 Group: {chat.title}
\ud83d\udc64 Reported User: {user_mention}
\ud83d\udcdd Reporter: {reporter_mention}
\ud83d\udccb Reason: {reason}
\u23f0 Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
    """
    
    for admin_id in admins:
        try:
            await context.bot.send_message(
                admin_id,
                report_msg,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error sending report to admin {admin_id}: {e}")

# --- CALLBACK QUERY HANDLERS FOR MODERATION ---
async def unban_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unban button callback - works with anonymous admins"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data: unban_user_{user_id}_{chat_id}
    parts = query.data.split("_")
    if len(parts) < 4:
        return
    
    user_id = int(parts[2])
    chat_id = int(parts[3])
    
    # Verify the callback user is an admin
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    
    # Unban user
    try:
        await context.bot.unban_member(chat_id, user_id)
        await unban_user_in_db(chat_id, user_id)
        
        # Get user info for professional message
        try:
            target_user = await context.bot.get_chat_member(chat_id, user_id)
            if target_user.user:
                username = target_user.user.username or target_user.user.first_name or f"User {user_id}"
                if target_user.user.username:
                    user_mention = f"@{target_user.user.username}"
                else:
                    user_mention = target_user.user.mention_html()
            else:
                user_mention = f"User {user_id}"
        except Exception:
            user_mention = f"User {user_id}"
        
        # Update message
        try:
            await query.message.edit_text(
                f"\u2705 <b>User Unbanned</b>\
\
{user_mention} has been unbanned and can join the group again.",
                reply_markup=None,
                parse_mode='HTML'
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"\u274c Error: {e}", show_alert=True)

async def unmute_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unmute button callback - works with anonymous admins"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data: unmute_user_{user_id}_{chat_id}
    parts = query.data.split("_")
    if len(parts) < 4:
        return
    
    user_id = int(parts[2])
    chat_id = int(parts[3])
    
    # Verify the callback user is an admin
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    
    # Unmute user using restrictChatMember with until_date=0 (unlimited)
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
        await context.bot.restrict_member(chat_id, user_id, permissions, until_date=0)
        await unmute_user_in_db(chat_id, user_id)
        
        # Get user info for professional message
        try:
            target_user = await context.bot.get_chat_member(chat_id, user_id)
            if target_user.user:
                username = target_user.user.username or target_user.user.first_name or f"User {user_id}"
                if target_user.user.username:
                    user_mention = f"@{target_user.user.username}"
                else:
                    user_mention = target_user.user.mention_html()
            else:
                user_mention = f"User {user_id}"
        except Exception:
            user_mention = f"User {user_id}"
        
        # Update message
        try:
            await query.message.edit_text(
                f"\u2705 <b>User Unmuted</b>\
\
{user_mention} has been unmuted and can now send messages in the group.",
                reply_markup=None,
                parse_mode='HTML'
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"\u274c Error: {e}", show_alert=True)

async def ban_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban from warning button callback - works with anonymous admins"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data: ban_from_warn_{user_id}_{chat_id}
    parts = query.data.split("_")
    if len(parts) < 5:
        return
    
    user_id = int(parts[3])
    chat_id = int(parts[4])
    
    # Verify the callback user is an admin
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    
    # Ban user
    try:
        await context.bot.ban_member(chat_id, user_id)
        banned_by = user_id_clicker if user_id_clicker else 0
        await add_ban(chat_id, user_id, banned_by, "Banned from warning")
        
        # Update message
        keyboard = [
            [InlineKeyboardButton("\u2705 Unban User", callback_data=f"unban_user_{user_id}_{chat_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.message.edit_text(
                f"\ud83d\udeab User has been banned!\
\
User ID: {user_id}",
                reply_markup=reply_markup
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"\u274c Error: {e}", show_alert=True)

async def mute_from_warn_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mute from warning button callback - works with anonymous admins"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data: mute_from_warn_{user_id}_{chat_id}
    parts = query.data.split("_")
    if len(parts) < 5:
        return
    
    user_id = int(parts[3])
    chat_id = int(parts[4])
    
    # Verify the callback user is an admin
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    
    # Mute user for 1 hour using restrictChatMember with until_date
    try:
        from datetime import datetime, timezone
        until_date = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        
        permissions = ChatPermissions(
            can_send_messages=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_documents=False,
            can_send_audios=False,
            can_send_voice_notes=False,
            can_send_video_notes=False,
            can_send_polls=False
        )
        await context.bot.restrict_member(chat_id, user_id, permissions, until_date=until_date)
        muted_by = user_id_clicker if user_id_clicker else 0
        await add_mute(chat_id, user_id, muted_by, "Muted from warning", 60)
        
        # Update message
        keyboard = [
            [InlineKeyboardButton("\ud83d\udd0a Unmute User", callback_data=f"unmute_user_{user_id}_{chat_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.message.edit_text(
                f"\ud83d\udd07 User has been muted for 1 hour!\
\
User ID: {user_id}",
                reply_markup=reply_markup
            )
        except BadRequest:
            pass
    except Exception as e:
        await query.answer(f"\u274c Error: {e}", show_alert=True)

# --- ADMIN KEYBOARD HANDLER ---
async def show_admin_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin-only keyboard in group chat"""
    message = update.message
    chat = message.chat
    
    # Support anonymous admins - check if sender is admin
    if not await is_sender_admin(chat.id, message, context):
        await message.reply_text("\u26a0\ufe0f This keyboard is only visible to admins!")
        return
    
    # Create admin keyboard
    keyboard = [
        [
            InlineKeyboardButton("\u26a0\ufe0f Warn", callback_data=f"cmd_warn_{chat.id}"),
            InlineKeyboardButton("\ud83d\udd07 Mute", callback_data=f"cmd_mute_{chat.id}"),
            InlineKeyboardButton("\ud83d\udeab Ban", callback_data=f"cmd_ban_{chat.id}")
        ],
        [
            InlineKeyboardButton("\ud83d\udd0a Unmute", callback_data=f"cmd_unmute_{chat.id}"),
            InlineKeyboardButton("\u2705 Unban", callback_data=f"cmd_unban_{chat.id}")
        ],
        [
            InlineKeyboardButton("\ud83d\udccb Reports", callback_data=f"cmd_reports_{chat.id}")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    admin_msg = """
\ud83d\udee1\ufe0f <b>Admin Commands</b>
Click a button to use moderation commands:

\u26a0\ufe0f <b>Warn</b> - Warn a user
\ud83d\udd07 <b>Mute</b> - Mute a user temporarily
\ud83d\udeab <b>Ban</b> - Ban a user permanently
\ud83d\udd0a <b>Unmute</b> - Unmute a muted user
\u2705 <b>Unban</b> - Unban a banned user
\ud83d\udccb <b>Reports</b> - View pending reports

This keyboard is only visible to admins.
    """
    
    await message.reply_html(admin_msg, reply_markup=reply_markup)

async def admin_keyboard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin keyboard button clicks - works with anonymous admins"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data
    parts = query.data.split("_")
    if len(parts) < 3:
        return
    
    command = parts[1]
    chat_id = int(parts[2])
    
    # Verify the callback user is an admin
    is_admin, user_id_clicker, admin_message = await verify_callback_admin(chat_id, query, context)
    
    if not is_admin:
        await query.answer(admin_message, show_alert=True)
        return
    
    # Show usage instructions
    instructions = {
        "warn": "\u26a0\ufe0f <b>Warn Usage</b>\
\
Reply to a message and use: /warn @username reason\
Example: /warn @user123 Spamming",
        "mute": "\ud83d\udd07 <b>Mute Usage</b>\
\
Reply to a message and use: /mute @username duration reason\
Example: /mute @user123 1h Spamming\
Duration: 10m, 1h, 1d, 1w",
        "ban": "\ud83d\udeab <b>Ban Usage</b>\
\
Reply to a message and use: /ban @username reason\
Example: /ban @user123 Repeated spam",
        "unmute": "\ud83d\udd0a <b>Unmute Usage</b>\
\
Use: /unmute @username\
Example: /unmute @user123",
        "unban": "\u2705 <b>Unban Usage</b>\
\
Use: /unban @username\
Example: /unban @user123",
        "reports": "\ud83d\udccb <b>View Reports</b>\
\
Reports are sent privately to admins. Check your private messages for new reports."
    }
    
    instruction_text = instructions.get(command, "Unknown command")
    
    try:
        await query.message.reply_html(instruction_text)
    except Exception as e:
        logger.error(f"Error sending instructions: {e}")

# --- ORIGINAL BOT COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("\u2795 Add Bot to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("\ud83d\udccb My Groups", callback_data="my_groups")],
        [InlineKeyboardButton("\u2753 Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = f"""
\ud83d\udc4b Welcome {user.mention_html()}!
I'm a powerful group moderation bot that helps you:
\u2705 Delete messages with banned words
\u2705 Delete links and URLs
\u2705 Delete promotional/forwarded messages
\u2705 Limit maximum words per message
\u2705 Auto-delete warning messages after set time
\u2705 Custom welcome messages with HTML support
\u2705 Admin commands: /warn, /mute, /ban, /unmute, /unban
\u2705 User reports: /report
\ud83d\ude80 Get started by adding me to your group!
    """
    await update.message.reply_html(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
\ud83d\udcda <b>Bot Commands & Features</b>
<b>Admin Commands (Group only):</b>
/warn <username> <reason> - Warn a user
/mute <username> <duration> <reason> - Mute a user (supports 10m, 1h, 1d, 1w)
/ban <username> <reason> - Ban a user
/unmute <username> - Unmute a user
/unban <username> - Unban a user
/admin - Show admin command keyboard

<b>Member Commands (Group only):</b>
/report <username> <reason> - Report a user to admins (cannot report admins)

<b>Commands (Use in private chat):</b>
/start - Start the bot and see main menu
/mygroups - View your groups
/help - Show this help message

<b>Features:</b>
\u2022 <b>Banned Words:</b> Auto-delete specific words.
\u2022 <b>Links:</b> Auto-delete messages containing http/https/t.me links.
\u2022 <b>Word Limit:</b> Delete messages that are too long (e.g., >100 words).
\u2022 <b>Anti-Promo:</b> Delete forwarded messages, spam bots, and promotional text.
\u2022 <b>Timer:</b> Set how long warning messages stay visible.
\u2022 <b>Welcome Messages:</b> Custom HTML welcome messages for new members.
<b>Note:</b> Admins and Anonymous Admins are exempt from deletion.
    """
    if update.message:
        await update.message.reply_html(help_text)
    else:
        try:
            await update.callback_query.message.edit_text(help_text, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                logger.error(f"Error in help command: {e}")

async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's groups"""
    user_id = update.effective_user.id
    groups = await get_user_groups(user_id)
    
    if not groups:
        text = "\u274c You haven't added me to any groups yet!\
\
Click the button below to add me to a group."
        keyboard = [[InlineKeyboardButton("\u2795 Add to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        text = "\ud83d\udccb <b>Your Groups:</b>\
\
Select a group to manage settings:"
        keyboard = []
        for group in groups:
            keyboard.append([InlineKeyboardButton(
                f"\ud83d\udd27 {group['chat_title']}",
                callback_data=f"group_settings_{group['chat_id']}"
            )])
        keyboard.append([InlineKeyboardButton("\ud83d\udd19 Back", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                logger.error(f"Error in my_groups: {e}")
    else:
        await update.message.reply_html(text, reply_markup=reply_markup)

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group settings"""
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    settings = await get_group_settings(chat_id)
    if not settings:
        try:
            await query.message.edit_text("\u274c Group not found!")
        except BadRequest:
            pass
        return
    
    banned_words = await get_banned_words(chat_id)
    banned_words_text = ", ".join(banned_words) if banned_words else "None"
    
    promo_status = "\u2705 Enabled" if settings.get('delete_promotions', False) else "\u274c Disabled"
    link_status = "\u2705 Enabled" if settings.get('delete_links', False) else "\u274c Disabled"
    word_limit = settings.get('max_word_count', 0)
    word_limit_status = f"{word_limit} words" if word_limit > 0 else "\u274c Disabled (Unlimited)"
    
    timer_val = settings.get('warning_timer', 30)
    if timer_val >= 60:
        timer_display = f"{timer_val // 60}m"
    else:
        timer_display = f"{timer_val}s"
    
    welcome_msg = settings.get('welcome_message', None)
    welcome_status = "\u2705 Enabled" if welcome_msg else "\u274c Not Set"
    welcome_timer_val = settings.get('welcome_timer', 0)
    welcome_timer_display = f"{welcome_timer_val}s" if welcome_timer_val > 0 else "Never"
    
    delete_join = settings.get('delete_join_messages', False)
    delete_join_status = "\u2705 Enabled" if delete_join else "\u274c Disabled"
    
    text = f"""
\u2699\ufe0f <b>Group Settings</b>
\ud83d\udcf1 Group: {settings['chat_title']}
\ud83d\udc64 Added by: @{settings['added_by_username']}
\ud83c\udf89 <b>Welcome Message:</b> {welcome_status} (Delete in: {welcome_timer_display})
\ud83d\udeab <b>Banned Words:</b>
{banned_words_text}
\ud83d\udcdd <b>Max Word Limit:</b> {word_limit_status}
\ud83d\udd17 <b>Delete Promotions (Forwards/Bots/Spam):</b> {promo_status}
\ud83c\udf10 <b>Delete Links (URLs):</b> {link_status}
\u23f1 <b>Warning Delete Timer:</b> {timer_display}
\ud83d\udc4b <b>Delete Join Messages:</b> {delete_join_status}
    """
    
    keyboard = [
        [InlineKeyboardButton("\ud83c\udf89 Set Welcome Message", callback_data=f"set_welcome_{chat_id}")],
        [InlineKeyboardButton("\u2795 Add Banned Word", callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("\u2796 Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("\ud83d\udcdd Word Count Limit", callback_data=f"set_word_limit_{chat_id}"),
         InlineKeyboardButton("\u23f1 Warning Timer", callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton("\ud83d\udce2 Toggle Promotions", callback_data=f"toggle_promo_{chat_id}"),
         InlineKeyboardButton("\ud83c\udf10 Toggle Links", callback_data=f"toggle_links_{chat_id}")],
        [InlineKeyboardButton("\ud83d\udc4b Toggle Join Delete", callback_data=f"toggle_join_delete_{chat_id}")],
        [InlineKeyboardButton("\ud83d\udd19 Back to Groups", callback_data="my_groups")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error editing message in settings: {e}")

async def set_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome message setup"""
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_welcome'
    
    text = """
\ud83c\udf89 <b>Set Welcome Message</b>
You can use HTML formatting and variables:
\u2022 <code>{BOT_NAME}</code> - Bot's username
\u2022 <code>{USER_NAME}</code> - Member's name
\u2022 <code>{USER_ID}</code> - Member's user ID
\u2022 <code>{CHAT_TITLE}</code> - Group's title

<b>HTML Example:</b>
<code>\ud83d\udc4b Welcome {USER_NAME} to {CHAT_TITLE}!
I'm {BOT_NAME}, your group's guardian.</code>

<b>With Inline Buttons Example:</b>
<code>Welcome to {CHAT_TITLE}! {USER_NAME}
\ud83d\udccc Read rules: [Rules](http://t.me/yourgroup/rules)
\ud83d\udcac Chat: [Join](http://t.me/yourgroup)</code>

Button Format: <code>[Button Text](https://link)</code>

\u23f1 After setting message, I'll ask for auto-delete timer (0 = never delete).
\u270d\ufe0f Send your welcome message HTML now:
    """
    await query.message.edit_text(text, parse_mode='HTML')

async def set_welcome_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome timer setup"""
    chat_id = context.user_data['awaiting_input']
    context.user_data['action'] = 'set_welcome_timer'
    
    text = """
\u23f1 <b>Set Welcome Message Auto-Delete Timer</b>
How long should welcome messages stay before deleting?
Examples:
\u2022 <code>0</code> (Never delete)
\u2022 <code>30</code> (30 seconds)
\u2022 <code>1m</code> (1 minute)
\u2022 <code>5m</code> (5 minutes)

\u270d\ufe0f Send the time now:
    """
    await update.message.reply_html(text)

async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'add_word'
    
    text = "\u270d\ufe0f Please send the word you want to ban.\
\
\ud83d\udca1 Send /cancel to cancel."
    await query.message.edit_text(text)

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
    
    text = f"\u270d\ufe0f Current banned words:\
{', '.join(banned_words)}\
\
Send the word you want to remove.\
\
\ud83d\udca1 Send /cancel to cancel."
    await query.message.edit_text(text)

async def set_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_timer'
    
    text = """
\u23f1 <b>Set Warning Deletion Time</b>
How long should warning messages stay before deleting?
Examples:
\u2022 <code>5s</code> (5 seconds)
\u2022 <code>1m</code> (1 minute)
\u2022 <code>30</code> (30 seconds)

\u270d\ufe0f Send the time duration now.
    """
    await query.message.edit_text(text, parse_mode='HTML')

async def set_word_limit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_word_limit'
    
    text = """
\ud83d\udcdd <b>Set Max Word Count</b>
Any message with more words than this number will be deleted.
Examples:
\u2022 <code>100</code> (Max 100 words)
\u2022 <code>35</code> (Max 35 words)
\u2022 <code>2</code> (Max 2 words)
\u2022 <code>0</code> (Disable limit / Unlimited)

\u270d\ufe0f Send the maximum number of words allowed now.
    """
    await query.message.edit_text(text, parse_mode='HTML')

async def toggle_promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_promotions', False)
    await update_promotion_setting(chat_id, new_value)
    
    status = "enabled" if new_value else "disabled"
    await query.answer(f"Promotion deletion {status}!", show_alert=True)
    await group_settings_handler(update, context)

async def toggle_links_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggling link deletion"""
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_links', False)
    await update_link_setting(chat_id, new_value)
    
    status = "enabled" if new_value else "disabled"
    await query.answer(f"Link deletion {status}!", show_alert=True)
    await group_settings_handler(update, context)

async def toggle_join_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggling join message deletion"""
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_join_messages', False)
    await update_delete_join_messages(chat_id, new_value)
    
    status = "enabled" if new_value else "disabled"
    await query.answer(f"Join message deletion {status}!", show_alert=True)
    await group_settings_handler(update, context)

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' not in context.user_data:
        return
    
    chat_id = context.user_data['awaiting_input']
    action = context.user_data['action']
    user_text = update.message.text.strip()
    
    if action == 'set_welcome':
        context.user_data['welcome_message_html'] = user_text
        context.user_data['action'] = 'set_welcome_timer'
        
        text = """
\u23f1 <b>Set Welcome Message Auto-Delete Timer</b>
How long should welcome messages stay before deleting?
Examples:
\u2022 <code>0</code> (Never delete)
\u2022 <code>30</code> (30 seconds)
\u2022 <code>1m</code> (1 minute)
\u2022 <code>5m</code> (5 minutes)

\u270d\ufe0f Send the time now:
        """
        await update.message.reply_html(text)
        return
    
    elif action == 'set_welcome_timer':
        welcome_html = context.user_data.get('welcome_message_html', '')
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text.strip())
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit == 'm':
                timer_seconds = value * 60
                display_unit = "minutes"
            else:
                timer_seconds = value
                display_unit = "seconds"
            await update_welcome_message(chat_id, welcome_html, timer_seconds)
            text = f"\u2705 Welcome message set! Auto-delete in <b>{value} {display_unit}</b>"
        else:
            text = "\u274c Invalid format! Please use '0', '30s', or '1m'"
            await update.message.reply_html(text)
            return
    
    elif action == 'add_word':
        user_text_lower = user_text.lower()
        await add_banned_word(chat_id, user_text_lower, update.effective_user.id)
        text = f"\u2705 Word '<b>{user_text_lower}</b>' added to banned words!"
    
    elif action == 'remove_word':
        user_text_lower = user_text.lower()
        await remove_banned_word(chat_id, user_text_lower)
        text = f"\u2705 Word '<b>{user_text_lower}</b>' removed from banned words!"
    
    elif action == 'set_timer':
        match = re.match(r'^(\d+)\s*(s|m)?$', user_text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit == 'm':
                seconds = value * 60
                display_unit = "minutes"
            else:
                seconds = value
                display_unit = "seconds"
            await update_warning_timer(chat_id, seconds)
            text = f"\u2705 Warning deletion timer set to <b>{value} {display_unit}</b>!"
        else:
            text = "\u274c Invalid format! Please use '10s' for seconds or '1m' for minutes."
    
    elif action == 'set_word_limit':
        if user_text.isdigit():
            limit = int(user_text)
            await update_word_limit(chat_id, limit)
            if limit == 0:
                text = "\u2705 Word limit disabled. Messages can be any length."
            else:
                text = f"\u2705 Max word count set to <b>{limit} words</b>!"
        else:
            text = "\u274c Invalid number! Please send a number like 100, 35, or 2."
    
    # Clear state
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
    if 'action' in context.user_data:
        del context.user_data['action']
    if 'welcome_message_html' in context.user_data:
        del context.user_data['welcome_message_html']
    
    keyboard = [[InlineKeyboardButton("\ud83d\udd19 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_html(text, reply_markup=reply_markup)

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
        del context.user_data['action']
    if 'welcome_message_html' in context.user_data:
        del context.user_data['welcome_message_html']
    await update.message.reply_text("\u2705 Operation cancelled.")

def parse_welcome_message(html_template: str, bot_name: str, user_name: str, user_id: int, chat_title: str) -> tuple:
    """Parse welcome message template and extract buttons"""
    message = html_template.replace('{BOT_NAME}', bot_name)
    message = message.replace('{USER_NAME}', user_name)
    message = message.replace('{USER_ID}', str(user_id))
    message = message.replace('{CHAT_TITLE}', chat_title)
    
    button_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    buttons = re.findall(button_pattern, message)
    message = re.sub(button_pattern, '', message).strip()
    
    return message, buttons

async def send_welcome_message(chat: any, new_member: any, context: ContextTypes.DEFAULT_TYPE, settings: dict):
    """Send custom welcome message to new member"""
    try:
        if settings and settings.get('welcome_message'):
            welcome_html = settings['welcome_message']
            bot_name = context.bot.username or "Bot"
            user_name = new_member.first_name or new_member.username or "Member"
            user_id = new_member.id
            message_text, buttons = parse_welcome_message(welcome_html, bot_name, user_name, user_id, chat.title)
            
            # Translation logic
            user_lang = new_member.language_code or 'en'
            if user_lang != 'en':
                # Prepare texts to translate: message_text and button texts
                texts_to_translate = [message_text]
                for btn_text, _ in buttons:
                    texts_to_translate.append(btn_text)
                
                text_to_translate = "\
---\
".join(texts_to_translate)
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
                prompt = f"Translate the following texts to {user_lang}, preserving all HTML tags, emojis, and formatting intact. Each section separated by --- should be translated separately and output in the same order separated by ---:\
{text_to_translate}"
                
                payload = {
                    "contents": [{
                        "parts": [{
                            "text": prompt
                        }]
                    }]
                }
                headers = {"Content-Type": "application/json"}
                
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(payload))
                    if response.status_code == 200:
                        result = response.json()
                        translated_text = result['candidates'][0]['content']['parts'][0]['text']
                        translated_parts = translated_text.split("\
---\
")
                        if len(translated_parts) == len(texts_to_translate):
                            message_text = translated_parts[0]
                            for i in range(len(buttons)):
                                buttons[i] = (translated_parts[i+1], buttons[i][1])
                except Exception as e:
                    logger.error(f"Error translating welcome message: {e}")
                    # Fallback to English
            
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
                default_welcome = f"\ud83d\udc4b Welcome {new_member.mention_html()} to {chat.title}!"
                try:
                    welcome_msg = await chat.send_message(default_welcome, parse_mode='HTML')
                except Exception as ex:
                    logger.error(f"Error sending fallback welcome: {ex}")
        else:
            default_welcome = f"\ud83d\udc4b Welcome {new_member.mention_html()} to {chat.title}!"
            try:
                welcome_msg = await chat.send_message(default_welcome, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Error sending default welcome: {e}")
    except Exception as e:
        logger.error(f"Error in send_welcome_message: {e}")

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when bot is added/removed from group"""
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
                await chat.send_message("\u26a0\ufe0f Only group admins can add me!")
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
                "\u26a0\ufe0f Please make me an admin with 'Delete Messages' permission!\
\
"
                "I'll leave now, add me again after making me admin."
            )
            await chat.leave()
            return
        
        username = added_by.username or f"user_{added_by.id}"
        await add_group_to_db(chat.id, chat.title, added_by.id, username, bot_is_admin)
        
        welcome_text = f"""
\ud83c\udf89 Thank you for adding me!
\u2705 I'm now protecting this group!
\ud83d\udc64 Added by: @{username}
\u2699\ufe0f To configure settings, open a private chat with me and click "My Groups".
\ud83d\udcdd You can also set a custom welcome message for new members!

<b>Admin Commands:</b>
/warn <username> <reason> - Warn a user
/mute <username> <duration> <reason> - Mute a user
/ban <username> <reason> - Ban a user
/admin - Show admin command keyboard

<b>Member Commands:</b>
/report <username> <reason> - Report a user to admins
        """
        await chat.send_message(welcome_text, parse_mode='HTML')

async def user_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle when any user joins or leaves the group"""
    chat_member_update = update.chat_member
    if not chat_member_update:
        return
    
    chat = chat_member_update.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    
    new_member = chat_member_update.new_chat_member
    old_member = chat_member_update.old_chat_member
    
    # FIXED: Use ChatMemberStatus.BANNED instead of non-existent .KICKED
    if old_member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED] and new_member.status == ChatMemberStatus.MEMBER:
        logger.info(f"New member {new_member.user.id} ({new_member.user.first_name}) joined group {chat.id}")
        settings = await get_group_settings(chat.id)
        
        # Track join message for deletion if enabled
        if settings and settings.get('delete_join_messages', False):
            context.user_data['last_join_message_id'] = None
        
        await send_welcome_message(chat, new_member.user, context, settings)

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    
    chat = message.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    
    settings = await get_group_settings(chat.id)
    if not settings:
        return
    
    warning_timer = settings.get('warning_timer', 30)
    
    is_admin_or_exempt = False
    if message.from_user.id == 1087968824 or (message.sender_chat and message.sender_chat.id == chat.id):
        is_admin_or_exempt = True
    else:
        try:
            member = await chat.get_member(message.from_user.id)
            if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                is_admin_or_exempt = True
        except Exception:
            pass
    
    if message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        is_admin_or_exempt = True
    
    if is_admin_or_exempt:
        return
    
    max_word_count = settings.get('max_word_count', 0)
    if max_word_count > 0:
        word_count = len(message.text.split())
        if word_count > max_word_count:
            try:
                await message.delete()
                username = message.from_user.username or message.from_user.first_name
                warning = f"\u26a0\ufe0f @{username}, your message was too long ({word_count} words). Max allowed is {max_word_count} words."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
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
        elif message.text:
            text_lower = message.text.lower()
            spam_patterns = [
                r'(?i)(dm to buy|dm for|price\s*[\-:)]|accounts?\s*(available|for sale)|cheap\s*accounts?)',
                r'(?i)(c\s*x\s*p|p\s*[0o@$]\s*r\s*n|n\s*[u#]\s*d\s*e|r\s*[@a]\s*x\s*p\s*e|h\s*@\s*r\s*d|f\s*o\s*r\s*c\s*e\s*d|t\s*@\s*r\s*c\s*h\s*[u\u20b9]\s*r)',
                r'(?i)(daily\s*offer|limited\s*stock|buy\s*for\s*resell|all\s*in\s*one\s*pack|full\s*(pack|cp|cxp|nude))',
                r'(\d+[\ufe0f\u20e3\u0030-\u0039]\s*[\w\s/]+\u2705)',
            ]
            if any(re.search(p, text_lower) for p in spam_patterns):
                is_promotion = True
                reason = "spam keywords"
        
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
                username = message.from_user.username or message.from_user.first_name if message.from_user else "Anonymous"
                warning = f"\u26a0\ufe0f @{username}, {reason} is not allowed."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
                return
            except Exception as e:
                logger.error(f"Error deleting promotional message: {e}")
    
    if settings.get('delete_links', False):
        link_pattern = r'(https?://\S+|www\.\S+|t\.me/\S+)'
        has_link = False
        
        if re.search(link_pattern, message.text):
            has_link = True
        
        if message.entities:
            for entity in message.entities:
                if entity.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                    has_link = True
        
        if has_link:
            try:
                await message.delete()
                username = message.from_user.username or message.from_user.first_name
                warning = f"\u26a0\ufe0f @{username}, links are not allowed in this group."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
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
                username = message.from_user.username or message.from_user.first_name
                warning = f"\u26a0\ufe0f @{username}, your message was hidden because it contained a banned word."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
                return
            except Exception as e:
                logger.error(f"Error deleting message with banned word: {e}")
                return

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # FIXED: Handle join/leave delete toggles separately to avoid callback parsing error
    if data == "toggle_join_delete":
        await query.answer("\u26a0\ufe0f Invalid callback data!", show_alert=True)
        return
    
    if data == "my_groups":
        await my_groups_handler(update, context)
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

# --- FASTAPI / WEBHOOK SETUP ---
@app.on_event("startup")
async def startup_event():
    """Initialize the bot and set webhook with required allowed_updates"""
    global ptb_application
    
    if ptb_application is None:
        ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add all handlers
        ptb_application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("help", help_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("cancel", cancel_handler, filters.ChatType.PRIVATE))
        
        # Moderation commands - Group only
        ptb_application.add_handler(CommandHandler("warn", warn_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("mute", mute_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("unmute", unmute_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("ban", ban_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("unban", unban_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("report", report_command, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        ptb_application.add_handler(CommandHandler("admin", show_admin_keyboard, filters.ChatType.GROUP | filters.ChatType.SUPERGROUP))
        
        ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_input
        ))
        ptb_application.add_handler(ChatMemberHandler(
            track_chat_member,
            ChatMemberHandler.MY_CHAT_MEMBER
        ))
        ptb_application.add_handler(ChatMemberHandler(
            user_chat_member,
            ChatMemberHandler.CHAT_MEMBER
        ))
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            check_message
        ))
        
        await ptb_application.initialize()
        await ptb_application.start()
        
        # Set webhook safely with retry for flood control
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
                logger.info(f"Webhook successfully set to {WEBHOOK_URL} with chat_member updates")
            except RetryAfter as e:
                logger.warning(f"Rate limited when setting webhook. Will retry in {e.retry_after} seconds...")
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")
        else:
            logger.error("WEBHOOK_URL is not set! Please add it to your .env file.")

@app.post("/webhook/webhook")
async def telegram_webhook(request: Request):
    """Handle incoming Telegram updates"""
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

# --- CRON JOB ENDPOINT ---
@app.get("/run-cleanup")
async def run_cleanup_job():
    """Check database for messages that need to be deleted and cleanup expired mutes"""
    if ptb_application is None:
        await startup_event()
    
    deleted_count = 0
    unmuted_count = 0
    
    # Cleanup expired mutes first
    try:
        unmuted_count = await cleanup_expired_mutes(ptb_application)
    except Exception as e:
        logger.error(f"Error in mute cleanup: {e}")
    
    # Get due deletions
    try:
        due_items = await get_due_deletions()
        if not due_items:
            return {"status": "ok", "deleted_count": 0, "unmuted_count": unmuted_count}
        
        for item in due_items:
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
        return {"status": "error", "error": str(e)}
    
    return {"status": "ok", "deleted_count": deleted_count, "unmuted_count": unmuted_count}

async def delete_group_and_words(chat_id: int):
    """Delete group and associated banned words from database"""
    try:
        supabase.table('banned_words').delete().eq('chat_id', chat_id).execute()
        supabase.table('groups').delete().eq('chat_id', chat_id).execute()
    except Exception as e:
        logger.error(f"Error deleting group {chat_id} and banned words: {e}")

@app.get("/run-group-cleanup")
async def run_group_cleanup():
    """Cleanup dead groups from database"""
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
