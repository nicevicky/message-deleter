import os
import logging
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
import re

# === CRITICAL: Do NOT use load_dotenv() on Vercel ===
# load_dotenv()  # ← REMOVE THIS LINE ENTIRELY

# Configure logging first
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === GET ENV VARS SAFELY ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# === VALIDATE REQUIRED VARIABLES ===
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is missing! Bot cannot start.")
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Supabase credentials missing!")
    raise ValueError("SUPABASE_URL and SUPABASE_KEY are required")

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FastAPI app
app = FastAPI()
ptb_application = None

# --- DATABASE HELPER FUNCTIONS ---
async def add_group_to_db(chat_id: int, chat_title: str, added_by: int, username: str, bot_is_admin: bool):
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
    try:
        result = supabase.table('groups').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []

async def get_group_settings(chat_id: int):
    try:
        result = supabase.table('groups').select("*").eq('chat_id', chat_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting group settings: {e}")
        return None

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

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not message:
        await context.bot.send_message(user.id, "Welcome! Use /start to begin.")
        return

    keyboard = [
        [InlineKeyboardButton("Add Bot to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("My Groups", callback_data="my_groups")],
        [InlineKeyboardButton("Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = f"""
Welcome {user.mention_html()}!

I'm a powerful group moderation bot that helps you:
Delete messages with banned words
Send welcome messages
Delete promotional/forwarded messages
Keep your group clean

Get started by adding me to your group!
Make sure I'm admin with "Delete Messages" permission.
    """
    await message.reply_html(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
Bot Commands & Features

/start - Show main menu
/mygroups - Manage your groups
/help - This message

Add me to a group → Open private chat → Click "My Groups"
    """
    await update.effective_message.reply_html(text)

async def my_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    groups = await get_user_groups(user_id)
    msg = update.effective_message

    if not groups:
        text = "You haven't added me to any groups yet!\n\nAdd me using the button below:"
        keyboard = [[InlineKeyboardButton("Add to Group", url=f"https://t.me/{context.bot.username}?startgroup=true")]]
    else:
        text = "<b>Your Groups:</b>\n\nSelect a group to manage:"
        keyboard = [
            [InlineKeyboardButton(g['chat_title'], callback_data=f"group_settings_{g['chat_id']}")]
            for g in groups
        ]
        keyboard.append([InlineKeyboardButton("Back", callback_data="back_to_main")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    if not settings:
        await query.edit_message_text("Group not found!")
        return

    words = await get_banned_words(chat_id)
    words_text = ", ".join(words) if words else "None"
    promo = "Enabled" if settings.get("delete_promotions") else "Disabled"

    text = f"""
<b>Group Settings</b>
Group: {settings['chat_title']}
Added by: @{settings['added_by_username']}

<b>Banned Words:</b> {words_text}
<b>Delete Promotions:</b> {promo}
    """
    keyboard = [
        [InlineKeyboardButton("Add Word", callback_data=f"add_word_{chat_id}")],
        [InlineKeyboardButton("Remove Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("View Words", callback_data=f"view_words_{chat_id}")],
        [InlineKeyboardButton("Toggle Promo Delete", callback_data=f"toggle_promo_{chat_id}")],
        [InlineKeyboardButton("Back", callback_data="my_groups")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ... [keep all other handlers exactly as before: add_word_handler, remove_word_handler, etc.]
# I'm skipping repeating them for brevity — they are unchanged and correct

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "my_groups":
        await my_groups_handler(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "back_to_main":
        await start(update, context)
    elif data.startswith("group_settings_"):
        await group_settings_handler(update, context)
    # ... add other handlers
    # (keep all your existing ones)

# --- STARTUP ---
@app.on_event("startup")
async def startup_event():
    global ptb_application
    if ptb_application is not None:
        return

    try:
        builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
        ptb_application = builder.build()

        # Register handlers
        ptb_application.add_handler(CommandHandler("start", start))
        ptb_application.add_handler(CommandHandler("help", help_command))
        ptb_application.add_handler(CommandHandler("mygroups", my_groups_handler))
        ptb_application.add_handler(CommandHandler("cancel", lambda u, c: c.bot.send_message(u.effective_chat.id, "Cancelled.")))
        ptb_application.add_handler(CallbackQueryHandler(callback_query_router))
        ptb_application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_word_input))
        ptb_application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_member_handler))
        ptb_application.add_handler(MessageHandler(filters.TEXT & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUPS), check_message))

        await ptb_application.initialize()
        await ptb_application.start()
        await ptb_application.updater.start_polling()  # Needed for job queue & proper shutdown

        logger.info("Bot started successfully!")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_application.bot)
        await ptb_application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)

@app.api_route("/", methods=["GET", "POST"])
async def root():
    return {"status": "ok", "message": "Bot is running"}
