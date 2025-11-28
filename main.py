import os
import logging
import re
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

# Ensure Supabase credentials exist to avoid immediate crash
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase credentials missing! Database features will fail.")

supabase: Client = create_client(SUPABASE_URL or "", SUPABASE_KEY or "")

# Initialize FastAPI
app = FastAPI()

# Global variable to store the Telegram Application
ptb_application = None

# --- DATABASE HELPER FUNCTIONS ---

async def add_group_to_db(chat_id: int, chat_title: str, added_by: int, username: str, bot_is_admin: bool):
    """Add a group to the database"""
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
    """Get all groups added by a specific user"""
    try:
        result = supabase.table('groups').select("*").eq('added_by', user_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []

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
✅ Send welcome messages to new members
✅ Delete promotional/forwarded messages
✅ Keep your group clean and organized

🚀 Get started by adding me to your group!

⚠️ Make sure:
• You are an admin in the group
• I am made an admin with "Delete Messages" permission
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

<b>How to use:</b>
1️⃣ Add me to your group as admin
2️⃣ Click "My Groups" to manage settings
3️⃣ Add banned words for each group
4️⃣ Enable/disable promotional message deletion

<b>Features:</b>
• Automatic message deletion for banned words
• Welcome new members with personalized messages
• Delete promotional/forwarded messages
• Works with anonymous admins

<b>Support:</b>
If you need help, contact the bot developer.
    """
    
    if update.message:
        await update.message.reply_html(help_text)
    else:
        await update.callback_query.message.edit_text(help_text, parse_mode='HTML')

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
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await update.message.reply_html(text, reply_markup=reply_markup)

async def group_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group settings"""
    query = update.callback_query
    await query.answer()
    
    chat_id = int(query.data.split("_")[2])
    settings = await get_group_settings(chat_id)
    
    if not settings:
        await query.message.edit_text("❌ Group not found!")
        return
    
    banned_words = await get_banned_words(chat_id)
    banned_words_text = ", ".join(banned_words) if banned_words else "None"
    
    promo_status = "✅ Enabled" if settings.get('delete_promotions', False) else "❌ Disabled"
    
    text = f"""
⚙️ <b>Group Settings</b>

📱 Group: {settings['chat_title']}
👤 Added by: @{settings['added_by_username']}

🚫 <b>Banned Words:</b>
{banned_words_text}

🔗 <b>Delete Promotions:</b> {promo_status}
    """
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Banned Word", callback_data=f"add_word_{chat_id}")],
        [InlineKeyboardButton("➖ Remove Banned Word", callback_data=f"remove_word_{chat_id}")],
        [InlineKeyboardButton("📋 View All Banned Words", callback_data=f"view_words_{chat_id}")],
        [InlineKeyboardButton(
            "🔗 Toggle Promotion Deletion", 
            callback_data=f"toggle_promo_{chat_id}"
        )],
        [InlineKeyboardButton("🔙 Back to Groups", callback_data="my_groups")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')

async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    context.user_data['awaiting_word'] = chat_id
    context.user_data['action'] = 'add'
    text = "✍️ Please send the word you want to ban in this group.\n\n💡 Send /cancel to cancel."
    await query.message.edit_text(text)

async def remove_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    banned_words = await get_banned_words(chat_id)
    if not banned_words:
        await query.answer("No banned words to remove!", show_alert=True)
        return
    context.user_data['awaiting_word'] = chat_id
    context.user_data['action'] = 'remove'
    text = f"✍️ Current banned words:\n{', '.join(banned_words)}\n\nSend the word you want to remove.\n\n💡 Send /cancel to cancel."
    await query.message.edit_text(text)

async def view_words_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[2])
    banned_words = await get_banned_words(chat_id)
    if not banned_words:
        text = "📝 No banned words set for this group."
    else:
        words_list = "\n".join([f"• {word}" for word in banned_words])
        text = f"🚫 <b>Banned Words:</b>\n\n{words_list}"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data=f"group_settings_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')

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

async def handle_word_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_word' not in context.user_data:
        return
    chat_id = context.user_data['awaiting_word']
    action = context.user_data['action']
    word = update.message.text.strip().lower()
    if action == 'add':
        await add_banned_word(chat_id, word, update.effective_user.id)
        text = f"✅ Word '<b>{word}</b>' added to banned words!"
    else: 
        await remove_banned_word(chat_id, word)
        text = f"✅ Word '<b>{word}</b>' removed from banned words!"
    del context.user_data['awaiting_word']
    del context.user_data['action']
    keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data=f"group_settings_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(text, reply_markup=reply_markup)

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
            added_by = message.from_user
            try:
                member = await chat.get_member(added_by.id)
                # Correctly using imported ChatMemberStatus
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
                # Correctly using imported ChatMemberStatus
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
    
    if settings.get('delete_promotions', False):
        if message.forward_from or message.forward_from_chat or message.forward_sender_name:
            try:
                await message.delete()
                username = message.from_user.username or message.from_user.first_name
                warning = f"⚠️ @{username}, your forwarded message was deleted."
                warning_msg = await chat.send_message(warning)
                context.job_queue.run_once(
                    lambda ctx: warning_msg.delete(),
                    5,
                    name=f"delete_warning_{warning_msg.message_id}"
                )
                return
            except Exception as e:
                logger.error(f"Error deleting promotional message: {e}")

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
                context.job_queue.run_once(
                    lambda ctx: warning_msg.delete(),
                    5,
                    name=f"delete_warning_{warning_msg.message_id}"
                )
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
    elif data.startswith("view_words_"):
        await view_words_handler(update, context)
    elif data.startswith("toggle_promo_"):
        await toggle_promo_handler(update, context)

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
            handle_word_input
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

@app.post("/webhook")
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
