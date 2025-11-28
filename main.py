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
        existing_group = await get_group_settings(chat_id)
        
        # Default settings
        delete_promotions = False
        delete_links = False
        warning_timer = 30

        if existing_group:
            delete_promotions = existing_group.get('delete_promotions', False)
            delete_links = existing_group.get('delete_links', False)
            warning_timer = existing_group.get('warning_timer', 30)

        data = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "added_by": added_by,
            "added_by_username": username,
            "bot_is_admin": bot_is_admin,
            "delete_promotions": delete_promotions,
            "delete_links": delete_links,
            "warning_timer": warning_timer 
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

async def get_banned_words(chat_id: int):
    try:
        result = supabase.table('banned_words').select("word").eq('chat_id', chat_id).execute()
        return [item['word'] for item in result.data]
    except Exception as e:
        logger.error(f"Error getting banned words: {e}")
        return []

async def add_banned_word(chat_id: int, word: str, added_by: int):
    try:
        data = {"chat_id": chat_id, "word": word.lower(), "added_by": added_by}
        return supabase.table('banned_words').insert(data).execute()
    except Exception as e:
        logger.error(f"Error adding banned word: {e}")
        return None

async def remove_banned_word(chat_id: int, word: str):
    try:
        return supabase.table('banned_words').delete().eq('chat_id', chat_id).eq('word', word.lower()).execute()
    except Exception as e:
        logger.error(f"Error removing banned word: {e}")
        return None

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

async def schedule_message_deletion(chat_id: int, message_id: int, delay_seconds: int):
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
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table('pending_deletions').select("*").lte('delete_at', now).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting due deletions: {e}")
        return []

async def remove_pending_deletion(row_id: int):
    try:
        supabase.table('pending_deletions').delete().eq('id', row_id).execute()
    except Exception as e:
        logger.error(f"Error removing pending deletion row: {e}")

# --- SPAM DETECTION LOGIC ---

def is_spam_text(text: str) -> bool:
    """
    Analyzes text for spammy promotional content characteristics.
    Returns True if it looks like the spam example provided.
    """
    if not text:
        return False
        
    text_lower = text.lower()
    
    # 1. Check for specific selling keywords found in the example
    selling_signals = [
        r"dm\s+(to|for)\s+buy",   # DM to buy
        r"price\s*[-:]",          # Price - 
        r"premium\s+collection",  # Premium collection
        r"all\s+in\s+one\s+pack", # All in one pack
        r"buy\s+for\s+resell",    # Buy for resell
        r"\d{2,}\s?rs",           # Pricing in RS (e.g. 99rs)
        r"cheap\s+price",
        r"full\s+video",
        r"only\s?fan",
        r"mega\s+link",
    ]
    
    # Count how many selling signals are present
    signal_count = 0
    for pattern in selling_signals:
        if re.search(pattern, text_lower):
            signal_count += 1
            
    # If 2 or more selling signals are found, it's spam
    if signal_count >= 2:
        return True

    # 2. Check for high emoji density combined with list format (1️⃣, 2️⃣, ✅)
    # The example provided has many checkmarks and numbers
    emoji_list_pattern = r"(✅|🎁|1️⃣|2️⃣|3️⃣|👉|🔥)"
    emojis_found = len(re.findall(emoji_list_pattern, text))
    
    # If user uses more than 3 list-style emojis AND mentions "Price" or "DM", it's spam
    if emojis_found > 3 and ("dm" in text_lower or "price" in text_lower or "buy" in text_lower):
        return True
        
    return False

# --- BOT COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("➕ Add Bot to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("📋 My Groups", callback_data="my_groups")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = f"👋 Welcome {user.mention_html()}!\n\nI am a group protector bot.\nAdd me to your group to filter spam, links, and banned words."
    await update.message.reply_html(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 <b>Bot Features</b>
• <b>Banned Words:</b> Auto-delete specific words.
• <b>Links:</b> Auto-delete URLs (http, t.me, etc).
• <b>Promotions:</b> Deletes forwards and Spammy selling text (Pricing, DM to buy).
• <b>Timer:</b> Auto-deletes the warning message.
    """
    if update.message:
        await update.message.reply_html(help_text)
    else:
        await update.callback_query.message.edit_text(help_text, parse_mode='HTML')

async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    groups = await get_user_groups(user_id)
    
    if not groups:
        text = "❌ You haven't added me to any groups yet!"
        keyboard = [[InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")]]
    else:
        text = "📋 <b>Your Groups:</b>\nSelect a group to manage:"
        keyboard = [[InlineKeyboardButton(f"🔧 {g['chat_title']}", callback_data=f"group_settings_{g['chat_id']}")] for g in groups]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await update.message.reply_html(text, reply_markup=reply_markup)

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    
    if not settings:
        await query.message.edit_text("❌ Group not found.")
        return

    promo_status = "✅ Enabled" if settings.get('delete_promotions') else "❌ Disabled"
    link_status = "✅ Enabled" if settings.get('delete_links') else "❌ Disabled"
    timer = settings.get('warning_timer', 30)

    text = f"""
⚙️ <b>Settings for {settings['chat_title']}</b>

🔗 <b>Block Promos/Spam:</b> {promo_status}
🌐 <b>Block Links:</b> {link_status}
⏱ <b>Warning Time:</b> {timer}s
    """
    keyboard = [
        [InlineKeyboardButton("📝 Banned Words", callback_data=f"view_words_{chat_id}")],
        [InlineKeyboardButton("⏱ Set Timer", callback_data=f"set_timer_{chat_id}")],
        [InlineKeyboardButton("📢 Toggle Anti-Promo", callback_data=f"toggle_promo_{chat_id}")],
        [InlineKeyboardButton("🌐 Toggle Anti-Link", callback_data=f"toggle_links_{chat_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="my_groups")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# --- HANDLERS FOR SETTINGS LOGIC ---

async def view_words_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    words = await get_banned_words(chat_id)
    text = f"🚫 <b>Banned Words:</b>\n{', '.join(words) if words else 'None'}"
    keyboard = [
        [InlineKeyboardButton("➕ Add Word", callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("➖ Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"group_settings_{chat_id}")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def input_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, chat_id = query.data.split("_")[0:2], int(query.data.split("_")[-1])
    
    context.user_data['awaiting_input'] = chat_id
    context.user_data['action'] = query.data
    
    prompt = "✍️ Send the word to add/remove." if "word" in query.data else "✍️ Send time in seconds (e.g. 30)."
    await query.message.edit_text(prompt)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_input' not in context.user_data:
        return
    
    chat_id = context.user_data['awaiting_input']
    action = context.user_data['action']
    text = update.message.text.strip().lower()
    
    if "add_word" in action:
        await add_banned_word(chat_id, text, update.effective_user.id)
        msg = f"✅ Added '{text}'."
    elif "remove_word" in action:
        await remove_banned_word(chat_id, text)
        msg = f"✅ Removed '{text}'."
    elif "set_timer" in action:
        if text.isdigit():
            await update_warning_timer(chat_id, int(text))
            msg = f"✅ Timer set to {text}s."
        else:
            msg = "❌ Invalid number."

    del context.user_data['awaiting_input']
    del context.user_data['action']
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_setting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[-1])
    settings = await get_group_settings(chat_id)
    
    if "toggle_promo" in query.data:
        new_val = not settings.get('delete_promotions', False)
        await update_promotion_setting(chat_id, new_val)
    elif "toggle_links" in query.data:
        new_val = not settings.get('delete_links', False)
        await update_link_setting(chat_id, new_val)
        
    await group_settings_handler(update, context)

# --- MAIN MESSAGE CHECKER ---

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

    # Check for Admins (They are exempt)
    user_id = message.from_user.id
    try:
        member = await chat.get_member(user_id)
        if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return # Ignore admins
    except:
        pass

    warning_timer = settings.get('warning_timer', 30)
    user_name = message.from_user.username or message.from_user.first_name
    should_delete = False
    reason = ""

    # 1. LINK DETECTION (Strict)
    if settings.get('delete_links', False):
        # Regex for URLs including t.me, http, www
        if re.search(r'(https?://|www\.|t\.me/|telegram\.me/)', message.text, re.IGNORECASE):
            should_delete = True
            reason = "Links are not allowed."
        
        # Check text entities (Text links)
        if message.entities:
            for entity in message.entities:
                if entity.type in [MessageEntity.URL, MessageEntity.TEXT_LINK, MessageEntity.MENTION]:
                     # We delete Text Links and standard URLs
                    if entity.type != MessageEntity.MENTION:
                        should_delete = True
                        reason = "Links are not allowed."

    # 2. PROMOTION / SPAM DETECTION
    if not should_delete and settings.get('delete_promotions', False):
        # A. Check if it is a Forward (from channel or bot)
        if message.forward_origin or message.forward_from or message.forward_from_chat:
            should_delete = True
            reason = "Forwarded messages are not allowed."
        
        # B. Check for Bot User
        elif message.from_user.is_bot:
            should_delete = True
            reason = "Bots are not allowed."
            
        # C. HEURISTIC SPAM CHECK (Catches the text you pasted)
        elif is_spam_text(message.text):
            should_delete = True
            reason = "Promotional/Spam content is not allowed."

    # 3. BANNED WORDS DETECTION
    if not should_delete:
        banned_words = await get_banned_words(chat.id)
        text_lower = message.text.lower()
        for word in banned_words:
            if re.search(rf'\b{re.escape(word)}\b', text_lower):
                should_delete = True
                reason = "Message contained banned words."
                break

    # EXECUTE DELETION
    if should_delete:
        try:
            await message.delete()
            warning = await chat.send_message(f"⚠️ @{user_name}, {reason}")
            # Schedule warning deletion
            await schedule_message_deletion(chat.id, warning.message_id, warning_timer)
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")

async def new_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine if bot was added or a user joined
    message = update.message
    for member in message.new_chat_members:
        if member.id == context.bot.id:
            # Bot was added
            await add_group_to_db(message.chat.id, message.chat.title, message.from_user.id, message.from_user.username, False)
            await message.reply_text("✅ I am active! Make me Admin to function correctly.")
        else:
            # Regular user joined - optional welcome logic here
            pass

# --- FASTAPI / SERVER ---

@app.on_event("startup")
async def startup_event():
    global ptb_application
    if ptb_application is None:
        ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Handlers
        ptb_application.add_handler(CommandHandler("start", start))
        ptb_application.add_handler(CommandHandler("help", help_command))
        ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler))
        ptb_application.add_handler(CallbackQueryHandler(group_settings_handler, pattern="^group_settings_"))
        ptb_application.add_handler(CallbackQueryHandler(view_words_handler, pattern="^view_words_"))
        ptb_application.add_handler(CallbackQueryHandler(input_request_handler, pattern="^(add_word|remove_word|set_timer)_"))
        ptb_application.add_handler(CallbackQueryHandler(toggle_setting_handler, pattern="^(toggle_promo|toggle_links)_"))
        
        # Text Handler for Inputs (Private) and Checks (Groups)
        ptb_application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_text_input))
        ptb_application.add_handler(MessageHandler(filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), check_message))
        ptb_application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_member_handler))

        await ptb_application.initialize()
        await ptb_application.start()

@app.post("/webhook/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_application.bot)
        await ptb_application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error: {e}")
        return Response(status_code=500)

@app.get("/run-cleanup")
async def run_cleanup_job():
    if ptb_application is None:
        await startup_event()
    
    due = await get_due_deletions()
    for item in due:
        try:
            await ptb_application.bot.delete_message(chat_id=item['chat_id'], message_id=item['message_id'])
        except Exception:
            pass # Message likely already deleted
        await remove_pending_deletion(item['id'])
    
    return {"status": "cleaned", "count": len(due)}

@app.get("/")
async def health():
    return "Bot is running"
