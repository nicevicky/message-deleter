import os
import logging
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ChatMemberStatus
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase credentials missing! Database features will fail.")

# Initialize Supabase Client safely
try:
    supabase: Client = create_client(SUPABASE_URL or "", SUPABASE_KEY or "")
except Exception as e:
    logger.error(f"Failed to init Supabase: {e}")
    supabase = None

# Global variable for the bot app
ptb_application = None

# --- DATABASE HELPER FUNCTIONS ---

async def add_group_to_db(chat_id: int, chat_title: str, added_by: int, username: str, bot_is_admin: bool):
    if not supabase: return None
    try:
        data = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "added_by": added_by,
            "added_by_username": username,
            "bot_is_admin": bot_is_admin,
            "delete_promotions": False
        }
        result = supabase.table('groups').upsert(data, on_conflict='chat_id').execute()
        return result
    except Exception as e:
        logger.error(f"Error adding group to DB: {e}")
        return None

async def get_user_groups(user_id: int):
    if not supabase: return []
    try:
        result = supabase.table('groups').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []

async def get_group_settings(chat_id: int):
    if not supabase: return None
    try:
        result = supabase.table('groups').select("*").eq('chat_id', chat_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting group settings: {e}")
        return None

async def add_banned_word(chat_id: int, word: str, added_by: int):
    if not supabase: return None
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
    if not supabase: return None
    try:
        result = supabase.table('banned_words').delete().eq('chat_id', chat_id).eq('word', word.lower()).execute()
        return result
    except Exception as e:
        logger.error(f"Error removing banned word: {e}")
        return None

async def get_banned_words(chat_id: int):
    if not supabase: return []
    try:
        result = supabase.table('banned_words').select("word").eq('chat_id', chat_id).execute()
        return [item['word'] for item in result.data]
    except Exception as e:
        logger.error(f"Error getting banned words: {e}")
        return []

async def update_promotion_setting(chat_id: int, delete_promotions: bool):
    if not supabase: return None
    try:
        result = supabase.table('groups').update({"delete_promotions": delete_promotions}).eq('chat_id', chat_id).execute()
        return result
    except Exception as e:
        logger.error(f"Error updating promotion setting: {e}")
        return None

# --- BOT COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
✅ Send welcome messages
✅ Delete promotional messages

🚀 <b>Add me to your group to get started!</b>
    """
    await update.message.reply_html(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 <b>Bot Commands</b>
/start - Main menu
/mygroups - Manage your groups
/help - Show this message

<b>Setup:</b>
1. Add me to a group as Admin
2. Open this chat and click "My Groups"
3. Configure banned words
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
        keyboard = []
        for group in groups:
            keyboard.append([InlineKeyboardButton(f"🔧 {group['chat_title']}", callback_data=f"group_settings_{group['chat_id']}")])
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
        await query.message.edit_text("❌ Group not found or you are not the owner.")
        return
    
    banned_words = await get_banned_words(chat_id)
    banned_words_text = ", ".join(banned_words) if banned_words else "None"
    promo_status = "✅ Enabled" if settings.get('delete_promotions', False) else "❌ Disabled"
    
    text = f"""
⚙️ <b>Settings for {settings['chat_title']}</b>

🚫 <b>Banned Words:</b> {banned_words_text}
🔗 <b>Delete Promotions:</b> {promo_status}
    """
    keyboard = [
        [InlineKeyboardButton("➕ Add Word", callback_data=f"add_word_{chat_id}"),
         InlineKeyboardButton("➖ Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("🔗 Toggle Promotions", callback_data=f"toggle_promo_{chat_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="my_groups")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')

async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_word'] = chat_id
    context.user_data['action'] = 'add'
    await query.message.edit_text("✍️ Send the word you want to <b>BAN</b>.\nSend /cancel to cancel.", parse_mode='HTML')

async def remove_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_word'] = chat_id
    context.user_data['action'] = 'remove'
    await query.message.edit_text("✍️ Send the word you want to <b>REMOVE</b>.\nSend /cancel to cancel.", parse_mode='HTML')

async def toggle_promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    new_value = not settings.get('delete_promotions', False)
    await update_promotion_setting(chat_id, new_value)
    await group_settings_handler(update, context)

async def handle_word_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_word' not in context.user_data:
        return
    
    chat_id = context.user_data['awaiting_word']
    action = context.user_data['action']
    word = update.message.text.strip().lower()
    
    if action == 'add':
        await add_banned_word(chat_id, word, update.effective_user.id)
        text = f"✅ Word '<b>{word}</b>' added!"
    else: 
        await remove_banned_word(chat_id, word)
        text = f"✅ Word '<b>{word}</b>' removed!"
    
    del context.user_data['awaiting_word']
    del context.user_data['action']
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_word' in context.user_data:
        del context.user_data['awaiting_word']
        del context.user_data['action']
    await update.message.reply_text("✅ Operation cancelled.")

async def new_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = message.chat
    for new_member in message.new_chat_members:
        if new_member.id == context.bot.id:
            # Bot was added
            added_by = message.from_user
            bot_member = await chat.get_member(context.bot.id)
            bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
            
            username = added_by.username or f"user_{added_by.id}"
            await add_group_to_db(chat.id, chat.title, added_by.id, username, bot_is_admin)
            
            await message.reply_text("🎉 Thanks for adding me! Please make sure I am an Admin with 'Delete Messages' permission.")
        else:
            # User joined
            await message.reply_html(f"👋 Welcome {new_member.mention_html()} to {chat.title}!")

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    
    chat = message.chat
    # Only check groups
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    
    settings = await get_group_settings(chat.id)
    if not settings:
        return

    # 1. Check Promotions
    if settings.get('delete_promotions', False):
        if message.forward_from or message.forward_from_chat or message.forward_sender_name or "t.me/" in message.text:
            try:
                await message.delete()
                # NOTE: On Vercel, we cannot safely use JobQueue to delete a warning later.
                # We simply delete the bad message to keep the group clean.
                return
            except Exception as e:
                logger.error(f"Error deleting promo: {e}")

    # 2. Check Banned Words
    banned_words = await get_banned_words(chat.id)
    if not banned_words:
        return
    
    message_text = message.text.lower()
    for word in banned_words:
        # Simple word boundary check
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, message_text):
            try:
                await message.delete()
                # On Vercel: Do not use context.job_queue for delayed warnings
                # Just delete the message.
                return
            except Exception as e:
                logger.error(f"Error deleting banned word: {e}")
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
    elif data.startswith("toggle_promo_"):
        await toggle_promo_handler(update, context)

# --- FASTAPI LIFESPAN & SETUP ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Bot
    global ptb_application
    if not TELEGRAM_BOT_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        yield
        return

    # Build Application without JobQueue (Serverless Mode)
    ptb_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add Handlers
    ptb_application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler, filters=filters.ChatType.PRIVATE))
    ptb_application.add_handler(CommandHandler("cancel", cancel_handler, filters=filters.ChatType.PRIVATE))
    
    ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
    
    # Private chat text handler for inputs
    ptb_application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_word_input))
    
    # Group handlers
    ptb_application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_member_handler))
    ptb_application.add_handler(MessageHandler(filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), check_message))

    # Initialize bot
    await ptb_application.initialize()
    await ptb_application.start()
    
    # Set Webhook if URL is provided
    if WEBHOOK_URL:
        logger.info(f"Setting webhook to: {WEBHOOK_URL}")
        await ptb_application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", allowed_updates=Update.ALL_TYPES)
    
    yield # Application runs here
    
    # Shutdown
    logger.info("Stopping bot...")
    await ptb_application.stop()
    await ptb_application.shutdown()

# Initialize FastAPI with Lifespan
app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle incoming Telegram updates"""
    try:
        if not ptb_application:
            return Response(status_code=500, content="Bot not initialized")
            
        data = await request.json()
        update = Update.de_json(data, ptb_application.bot)
        
        # Process update - await it to ensure it finishes before Vercel freezes the function
        await ptb_application.process_update(update)
        
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return Response(status_code=500)

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Bot is active"}
