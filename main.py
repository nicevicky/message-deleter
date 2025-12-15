import os
import logging
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import ChatType, ChatMemberStatus
from telegram.error import BadRequest, RetryAfter
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # REQUIRED: Add to .env → https://your-domain.vercel.app/webhook/webhook

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
        if existing_group:
            delete_promotions = existing_group.get('delete_promotions', False)
            delete_links = existing_group.get('delete_links', False)
            warning_timer = existing_group.get('warning_timer', 30)
            max_word_count = existing_group.get('max_word_count', 0)
            welcome_message = existing_group.get('welcome_message', None)
            welcome_timer = existing_group.get('welcome_timer', 0)
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
            "welcome_timer": welcome_timer
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

async def get_all_groups():
    """Get all groups from the database"""
    try:
        result = supabase.table('groups').select("*").execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting all groups: {e}")
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
        result = supabase.table('pending_deletions').select("*").lte('delete_at', now).execute()
        return result.data
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

# --- BOT COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("➕ Add Bot to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("📋 My Groups", callback_data="my_groups")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = f"""
👋 Welcome {user.mention_html()}!
I'm a powerful group moderation bot that helps you:
✅ Delete messages with banned words
✅ Delete links and URLs
✅ Delete promotional/forwarded messages
✅ Limit maximum words per message
✅ Auto-delete warning messages after set time
✅ Custom welcome messages with HTML support
🚀 Get started by adding me to your group!
    """
    await update.message.reply_html(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
📚 <b>Bot Commands & Features</b>
<b>Commands (Use in private chat):</b>
/start - Start the bot and see main menu
/mygroups - View your groups
/help - Show this help message
<b>Features:</b>
• <b>Banned Words:</b> Auto-delete specific words.
• <b>Links:</b> Auto-delete messages containing http/https/t.me links.
• <b>Word Limit:</b> Delete messages that are too long (e.g., >100 words).
• <b>Anti-Promo:</b> Delete forwarded messages, spam bots, and promotional text.
• <b>Timer:</b> Set how long warning messages stay visible.
• <b>Welcome Messages:</b> Custom HTML welcome messages for new members.
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
            await query.message.edit_text("❌ Group not found!")
        except BadRequest:
            pass
        return
    banned_words = await get_banned_words(chat_id)
    banned_words_text = ", ".join(banned_words) if banned_words else "None"
    promo_status = "✅ Enabled" if settings.get('delete_promotions', False) else "❌ Disabled"
    link_status = "✅ Enabled" if settings.get('delete_links', False) else "❌ Disabled"
    word_limit = settings.get('max_word_count', 0)
    word_limit_status = f"{word_limit} words" if word_limit > 0 else "❌ Disabled (Unlimited)"
    timer_val = settings.get('warning_timer', 30)
    if timer_val >= 60:
        timer_display = f"{timer_val // 60}m"
    else:
        timer_display = f"{timer_val}s"
    welcome_msg = settings.get('welcome_message', None)
    welcome_status = "✅ Enabled" if welcome_msg else "❌ Not Set"
    welcome_timer_val = settings.get('welcome_timer', 0)
    welcome_timer_display = f"{welcome_timer_val}s" if welcome_timer_val > 0 else "Never"
    text = f"""
⚙️ <b>Group Settings</b>
📱 Group: {settings['chat_title']}
👤 Added by: @{settings['added_by_username']}
🎉 <b>Welcome Message:</b> {welcome_status} (Delete in: {welcome_timer_display})
🚫 <b>Banned Words:</b>
{banned_words_text}
📝 <b>Max Word Limit:</b> {word_limit_status}
🔗 <b>Delete Promotions (Forwards/Bots/Spam):</b> {promo_status}
🌐 <b>Delete Links (URLs):</b> {link_status}
⏱ <b>Warning Delete Timer:</b> {timer_display}
    """
    keyboard = [
        [InlineKeyboardButton("🎉 Set Welcome Message", callback_data=f"set_welcome_{chat_id}")],
        [InlineKeyboardButton("➕ Add Banned Word", callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("➖ Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("📝 Word Count Limit", callback_data=f"set_word_limit_{chat_id}"),
         InlineKeyboardButton("⏱ Warning Timer", callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton("📢 Toggle Promotions", callback_data=f"toggle_promo_{chat_id}"),
         InlineKeyboardButton("🌐 Toggle Links", callback_data=f"toggle_links_{chat_id}")],
        [InlineKeyboardButton("🔙 Back to Groups", callback_data="my_groups")]
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
🎉 <b>Set Welcome Message</b>
You can use HTML formatting and variables:
• <code>{BOT_NAME}</code> - Bot's username
• <code>{USER_NAME}</code> - Member's name
• <code>{USER_ID}</code> - Member's user ID
<b>HTML Example:</b>
<code>👋 Welcome {USER_NAME}!
I'm {BOT_NAME}, your group's guardian.</code>
<b>With Inline Buttons Example:</b>
<code>Welcome to our group! {USER_NAME}
📌 Read rules: [Rules](http://t.me/yourgroup/rules)
💬 Chat: [Join](http://t.me/yourgroup)</code>
Button Format: <code>[Button Text](https://link)</code>
⏱ After setting message, I'll ask for auto-delete timer (0 = never delete).
✍️ Send your welcome message HTML now:
    """
    await query.message.edit_text(text, parse_mode='HTML')

async def set_welcome_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome timer setup"""
    chat_id = context.user_data['awaiting_input']
    context.user_data['action'] = 'set_welcome_timer'
    text = """
⏱ <b>Set Welcome Message Auto-Delete Timer</b>
How long should welcome messages stay before deleting?
Examples:
• <code>0</code> (Never delete)
• <code>30</code> (30 seconds)
• <code>1m</code> (1 minute)
• <code>5m</code> (5 minutes)
✍️ Send the time now:
    """
    await update.message.reply_html(text)

async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'add_word'
    text = "✍️ Please send the word you want to ban.\n\n💡 Send /cancel to cancel."
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
    text = f"✍️ Current banned words:\n{', '.join(banned_words)}\n\nSend the word you want to remove.\n\n💡 Send /cancel to cancel."
    await query.message.edit_text(text)

async def set_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_timer'
    text = """
⏱ <b>Set Warning Deletion Time</b>
How long should warning messages stay before deleting?
Examples:
• <code>5s</code> (5 seconds)
• <code>1m</code> (1 minute)
• <code>30</code> (30 seconds)
✍️ Send the time duration now.
    """
    await query.message.edit_text(text, parse_mode='HTML')

async def set_word_limit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[3])
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = 'set_word_limit'
    text = """
📝 <b>Set Max Word Count</b>
Any message with more words than this number will be deleted.
Examples:
• <code>100</code> (Max 100 words)
• <code>35</code> (Max 35 words)
• <code>2</code> (Max 2 words)
• <code>0</code> (Disable limit / Unlimited)
✍️ Send the maximum number of words allowed now.
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
⏱ <b>Set Welcome Message Auto-Delete Timer</b>
How long should welcome messages stay before deleting?
Examples:
• <code>0</code> (Never delete)
• <code>30</code> (30 seconds)
• <code>1m</code> (1 minute)
• <code>5m</code> (5 minutes)
✍️ Send the time now:
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
            text = f"✅ Welcome message set! Auto-delete in <b>{value} {display_unit}</b>"
        else:
            text = "❌ Invalid format! Please use '0', '30s', or '1m'"
            await update.message.reply_html(text)
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
            if unit == 'm':
                seconds = value * 60
                display_unit = "minutes"
            else:
                seconds = value
                display_unit = "seconds"
            await update_warning_timer(chat_id, seconds)
            text = f"✅ Warning deletion timer set to <b>{value} {display_unit}</b>!"
        else:
            text = "❌ Invalid format! Please use '10s' for seconds or '1m' for minutes."
    elif action == 'set_word_limit':
        if user_text.isdigit():
            limit = int(user_text)
            await update_word_limit(chat_id, limit)
            if limit == 0:
                text = "✅ Word limit disabled. Messages can be any length."
            else:
                text = f"✅ Max word count set to <b>{limit} words</b>!"
        else:
            text = "❌ Invalid number! Please send a number like 100, 35, or 2."
    # Clear state
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
    if 'action' in context.user_data:
        del context.user_data['action']
    if 'welcome_message_html' in context.user_data:
        del context.user_data['welcome_message_html']
    keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(text, reply_markup=reply_markup)

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
        del context.user_data['action']
    if 'welcome_message_html' in context.user_data:
        del context.user_data['welcome_message_html']
    await update.message.reply_text("✅ Operation cancelled.")

def parse_welcome_message(html_template: str, bot_name: str, user_name: str, user_id: int) -> tuple:
    """Parse welcome message template and extract buttons"""
    message = html_template.replace('{BOT_NAME}', bot_name)
    message = message.replace('{USER_NAME}', user_name)
    message = message.replace('{USER_ID}', str(user_id))
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
            message_text, buttons = parse_welcome_message(welcome_html, bot_name, user_name, user_id)
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
                    welcome_msg = await chat.send_message(default_welcome, parse_mode='HTML')
                except Exception as ex:
                    logger.error(f"Error sending fallback welcome: {ex}")
        else:
            default_welcome = f"👋 Welcome {new_member.mention_html()} to {chat.title}!"
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
                "⚠️ Please make me an admin with 'Delete Messages' permission!\n\n"
                "I'll leave now, add me again after making me admin."
            )
            await chat.leave()
            return
        username = added_by.username or f"user_{added_by.id}"
        await add_group_to_db(chat.id, chat.title, added_by.id, username, bot_is_admin)
        welcome_text = f"""
🎉 Thank you for adding me!
✅ I'm now protecting this group!
👤 Added by: @{username}
⚙️ To configure settings, open a private chat with me and click "My Groups".
📝 You can also set a custom welcome message for new members!
        """
        await chat.send_message(welcome_text)

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
                warning = f"⚠️ @{username}, your message was too long ({word_count} words). Max allowed is {max_word_count} words."
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
                r'(?i)(c\s*x\s*p|p\s*[0o@$]\s*r\s*n|n\s*[u#]\s*d\s*e|r\s*[@a]\s*x\s*p\s*e|h\s*@\s*r\s*d|f\s*o\s*r\s*c\s*e\s*d|t\s*@\s*r\s*c\s*h\s*[u₹]\s*r)',
                r'(?i)(daily\s*offer|limited\s*stock|buy\s*for\s*resell|all\s*in\s*one\s*pack|full\s*(pack|cp|cxp|nude))',
                r'(\d+[\ufe0f\u20e3\u0030-\u0039]\s*[\w\s/]+✅)',
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
                warning = f"⚠️ @{username}, {reason} is not allowed."
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
                warning = f"⚠️ @{username}, links are not allowed in this group."
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
                warning = f"⚠️ @{username}, your message was hidden because it contained a banned word."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
                return
            except Exception as e:
                logger.error(f"Error deleting message with banned word: {e}")
                return

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
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
    """Check database for warnings and welcome messages that need to be deleted"""
    if ptb_application is None:
        await startup_event()
    due_items = await get_due_deletions()
    if not due_items:
        return {"status": "ok", "deleted_count": 0}
    deleted_count = 0
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
    return {"status": "ok", "deleted_count": deleted_count}

@app.get("/run-group-cleanup")
async def run_group_cleanup():
    """Cleanup dead groups from the database"""
    if ptb_application is None:
        await startup_event()
    groups = await get_all_groups()
    removed = []
    for group in groups:
        chat_id = group['chat_id']
        try:
            await ptb_application.bot.get_chat(chat_id)
        except RetryAfter as ra:
            logger.warning(f"Rate limit hit for group {chat_id}, sleeping {ra.retry_after} seconds")
            await asyncio.sleep(ra.retry_after)
            await ptb_application.bot.get_chat(chat_id)  # Retry once
        except telegram.error.TelegramError as e:
            error_msg = str(e).lower()
            if "forbidden" in error_msg or "chat not found" in error_msg:
                # Delete group and related data
                supabase.table('groups').delete().eq('chat_id', chat_id).execute()
                supabase.table('banned_words').delete().eq('chat_id', chat_id).execute()
                supabase.table('pending_deletions').delete().eq('chat_id', chat_id).execute()
                removed.append(chat_id)
                logger.info(f"Removed dead group {chat_id}: {error_msg}")
            else:
                logger.error(f"Unexpected error for group {chat_id}: {e}")
        await asyncio.sleep(0.5)  # Delay to avoid spamming Telegram
    return {"status": "ok", "removed_count": len(removed), "removed": removed}
