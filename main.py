import os
import logging
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import ChatType, ChatMemberStatus
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client
from dotenv import load_dotenv

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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize FastAPI
app = FastAPI()

# Global variable to store the Telegram Application
ptb_application = None

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
        # Check if group exists to preserve custom settings
        existing_group = await get_group_settings(chat_id)
        
        # Default settings
        delete_promotions = False
        delete_links = False  # <--- NEW DEFAULT
        warning_timer = 30

        # If group exists, use its current settings instead of defaults
        if existing_group:
            delete_promotions = existing_group.get('delete_promotions', False)
            delete_links = existing_group.get('delete_links', False) # <--- PRESERVE SETTING
            warning_timer = existing_group.get('warning_timer', 30)

        data = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "added_by": added_by,
            "added_by_username": username,
            "bot_is_admin": bot_is_admin,
            "delete_promotions": delete_promotions,
            "delete_links": delete_links, # <--- ADDED TO DB UPDATE
            "warning_timer": warning_timer 
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

async def schedule_message_deletion(chat_id: int, message_id: int, delay_seconds: int):
    """Schedule a message for deletion via DB (for Cron)"""
    try:
        # Calculate delete time (UTC)
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
        # Select messages where delete_at is older than or equal to now
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
✅ Auto-delete warning messages after set time

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
• <b>Anti-Promo:</b> Delete forwarded messages from channels/bots.
• <b>Timer:</b> Set how long warning messages stay visible.

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
    
    # Format current timer display
    timer_val = settings.get('warning_timer', 30)
    if timer_val >= 60:
        timer_display = f"{timer_val // 60}m"
    else:
        timer_display = f"{timer_val}s"
    
    text = f"""
⚙️ <b>Group Settings</b>

📱 Group: {settings['chat_title']}
👤 Added by: @{settings['added_by_username']}

🚫 <b>Banned Words:</b>
{banned_words_text}

🔗 <b>Delete Promotions (Forwards/Bots):</b> {promo_status}
🌐 <b>Delete Links (URLs):</b> {link_status}
⏱ <b>Warning Delete Timer:</b> {timer_display}
    """
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Banned Word", callback_data=f"add_word_{chat_id}")],
        [InlineKeyboardButton("➖ Remove Banned Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("⏱ Set Warning Timer", callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton(
            "📢 Toggle Promotion Deletion", 
            callback_data=f"toggle_promo_{chat_id}"
        )],
        [InlineKeyboardButton(
            "🌐 Toggle Link Deletion", 
            callback_data=f"toggle_links_{chat_id}"
        )],
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
    user_text = update.message.text.strip().lower()
    
    if action == 'add_word':
        await add_banned_word(chat_id, user_text, update.effective_user.id)
        text = f"✅ Word '<b>{user_text}</b>' added to banned words!"
        
    elif action == 'remove_word':
        await remove_banned_word(chat_id, user_text)
        text = f"✅ Word '<b>{user_text}</b>' removed from banned words!"
        
    elif action == 'set_timer':
        # Parse time input (1s, 1m, 30)
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

    # Clear state
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
    if 'action' in context.user_data:
        del context.user_data['action']
        
    keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(text, reply_markup=reply_markup)

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' in context.user_data:
        del context.user_data['awaiting_input']
        del context.user_data['action']
    await update.message.reply_text("✅ Operation cancelled.")

async def new_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    for new_member in message.new_chat_members:
        if new_member.id == context.bot.id:
            added_by = message.from_user
            try:
                member = await chat.get_member(added_by.id)
                if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                    await message.reply_text("⚠️ Only group admins can add me!")
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
                await message.reply_text(
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
            """
            await message.reply_text(welcome_text)
        else:
            username = new_member.username or new_member.first_name
            welcome_text = f"👋 Welcome {new_member.mention_html()} to {chat.title}!"
            # Welcome messages usually stay, so we don't schedule delete here unless requested
            await message.reply_html(welcome_text)

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
    
    # Get warning timer setting (default 30s if not set)
    warning_timer = settings.get('warning_timer', 30)

    # --- EXEMPTION LOGIC FOR ADMINS AND ANONYMOUS ADMINS ---
    is_admin_or_exempt = False
    
    # Check if sender is anonymous group admin (Telegram ID 1087968824 is GroupAnonymousBot)
    if message.from_user.id == 1087968824 or message.sender_chat and message.sender_chat.id == chat.id:
        is_admin_or_exempt = True
    else:
        # Check actual admin status
        try:
            member = await chat.get_member(message.from_user.id)
            if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                is_admin_or_exempt = True
        except Exception:
            pass

    # If user is admin/exempt, we do NOT delete links or promotions
    # However, we MIGHT still delete banned words depending on preference, 
    # but usually admins are allowed to say anything. 
    # Assuming admins are exempt from everything for now.
    if is_admin_or_exempt:
        return

    # 1. Check Promotions (Forwards / Bots / Channels)
    if settings.get('delete_promotions', False):
        is_promotion = False
        
        # Check if forwarded from anywhere
        if message.forward_from or message.forward_from_chat or message.forward_sender_name:
            is_promotion = True
            
        # Check if sent via an inline bot
        if message.via_bot:
            is_promotion = True
            
        # Check if user is a bot (spam bots)
        if message.from_user.is_bot:
            is_promotion = True

        if is_promotion:
            try:
                await message.delete()
                username = message.from_user.username or message.from_user.first_name
                warning = f"⚠️ @{username}, promotional content/forwards are not allowed."
                warning_msg = await chat.send_message(warning)
                await schedule_message_deletion(chat.id, warning_msg.message_id, warning_timer)
                return
            except Exception as e:
                logger.error(f"Error deleting promotional message: {e}")

    # 2. Check Links (New Feature)
    if settings.get('delete_links', False):
        # Regex for common links (http, https, www, t.me)
        link_pattern = r'(https?://\S+|www\.\S+|t\.me/\S+)'
        has_link = False
        
        if re.search(link_pattern, message.text):
            has_link = True
        
        # Also check entities for hidden links
        if message.entities:
            for entity in message.entities:
                if entity.type in [MessageEntity.URL, MessageEntity.TEXT_LINK, MessageEntity.MENTION]:
                    # Strict mode: treat all URL entities as links
                    if entity.type == MessageEntity.URL or entity.type == MessageEntity.TEXT_LINK:
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

    # 3. Check Banned Words
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
    elif data.startswith("add_word_"):
        await add_word_handler(update, context)
    elif data.startswith("remove_word_"):
        await remove_word_handler(update, context)
    elif data.startswith("set_timer_"):
        await set_timer_handler(update, context)
    elif data.startswith("toggle_promo_"):
        await toggle_promo_handler(update, context)
    elif data.startswith("toggle_links_"):
        await toggle_links_handler(update, context)

# --- VERCEL / FASTAPI SETUP ---

@app.on_event("startup")
async def startup_event():
    """Initialize the bot when the server starts"""
    global ptb_application
    if ptb_application is None:
        ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Add handlers
        ptb_application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("help", help_command, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CommandHandler("cancel", cancel_handler, filters.ChatType.PRIVATE))
        ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_input
        ))
        ptb_application.add_handler(MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_chat_member_handler
        ))
        ptb_application.add_handler(MessageHandler(
            filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            check_message
        ))

        await ptb_application.initialize()
        await ptb_application.start()

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
# Provide this URL to cron-job.org
@app.get("/run-cleanup")
async def run_cleanup_job():
    """Check database for warnings that need to be deleted"""
    # Ensure bot is initialized
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
            # Delete from Telegram
            await ptb_application.bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted_count += 1
        except Exception as e:
            # Message might already be deleted or bot kicked
            logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
        
        # Remove from DB regardless of success (to stop trying)
        await remove_pending_deletion(row_id)
        
    return {"status": "ok", "deleted_count": deleted_count}
