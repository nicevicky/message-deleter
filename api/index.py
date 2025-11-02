# api/index.py
import os
import re
import logging
import asyncio
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.constants import ChatMemberStatus
from supabase import create_client, Client
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Messagersdeleterbot")

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Global application instance
application = None

# Store pending group verifications (user_id -> group_id)
pending_groups: Dict[str, str] = {}

# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global application
    try:
        application = await get_application()
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/webhook"
            await application.bot.set_webhook(webhook_url)
            logger.info(f"Webhook set to: {webhook_url}")
            logger.info(f"Add to group link: https://t.me/{BOT_USERNAME}?startgroup=true")
        else:
            logger.warning("WEBHOOK_URL not set!")
    except Exception as e:
        logger.error(f"Startup error: {e}")
    
    yield
    logger.info("Lifespan context ending")

app = FastAPI(lifespan=lifespan)

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Please try again later."
            )
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

# Initialize bot application
async def get_application():
    global application
    if application is None:
        builder = Application.builder()
        builder.token(BOT_TOKEN)
        builder.concurrent_updates(True)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.connect_timeout(30)
        builder.pool_timeout(30)
        
        application = builder.build()
        
        # Register handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("mygroups", mygroups))
        application.add_handler(CommandHandler("settings", settings))
        application.add_handler(CommandHandler("filter", filter_word))
        application.add_handler(CommandHandler("unfilter", unfilter_word))
        application.add_handler(CommandHandler("listfilters", list_filters))
        application.add_handler(CommandHandler("ban", ban_user))
        application.add_handler(CommandHandler("cancel", cancel_verification))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bot_added_to_group))
        application.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, verify_forwarded_message))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE, handle_private_message))
        # Fixed: SUPERGROUPS → SUPERGROUP
        application.add_handler(MessageHandler(
            filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP,
            handle_group_message
        ))
        
        application.add_error_handler(error_handler)
        
        await application.initialize()
        await application.start()
    
    return application

# === DATABASE HELPERS ===
async def get_group(group_id: str) -> Optional[Dict]:
    try:
        response = supabase.table("groups").select("*").eq("group_id", group_id).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error getting group: {e}")
        return None

async def create_group(group_id: str, group_name: str, admin_user_id: str) -> bool:
    try:
        supabase.table("groups").insert({
            "group_id": group_id,
            "group_name": group_name,
            "admin_user_id": admin_user_id,
            "delete_join_leave": True,
            "delete_links": False,
            "delete_promotions": False
        }).execute()
        supabase.table("user_groups").insert({
            "user_id": admin_user_id,
            "group_id": group_id
        }).execute()
        for word in ["scam", "fuck"]:
            supabase.table("filtered_words").insert({"group_id": group_id, "word": word}).execute()
        return True
    except Exception as e:
        logger.error(f"Error creating group: {e}")
        return False

async def get_user_groups(user_id: str) -> List[Dict]:
    try:
        response = supabase.table("user_groups").select("group_id").eq("user_id", user_id).execute()
        group_ids = [item["group_id"] for item in response.data]
        if not group_ids:
            return []
        groups_response = supabase.table("groups").select("*").in_("group_id", group_ids).execute()
        return groups_response.data
    except Exception as e:
        logger.error(f"Error getting user groups: {e}")
        return []

async def update_group_setting(group_id: str, setting: str, value: bool) -> bool:
    try:
        supabase.table("groups").update({setting: value}).eq("group_id", group_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating setting: {e}")
        return False

async def get_filtered_words(group_id: str) -> List[str]:
    try:
        response = supabase.table("filtered_words").select("word").eq("group_id", group_id).execute()
        return [item["word"] for item in response.data]
    except Exception as e:
        logger.error(f"Error getting filtered words: {e}")
        return []

async def add_filtered_word(group_id: str, word: str) -> bool:
    try:
        supabase.table("filtered_words").insert({"group_id": group_id, "word": word}).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding filtered word: {e}")
        return False

async def remove_filtered_word(group_id: str, word: str) -> bool:
    try:
        supabase.table("filtered_words").delete().eq("group_id", group_id).eq("word", word).execute()
        return True
    except Exception as e:
        logger.error(f"Error removing filtered word: {e}")
        return False

async def add_banned_user(group_id: str, user_id: int) -> bool:
    try:
        supabase.table("banned_users").insert({"group_id": group_id, "user_id": user_id}).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding banned user: {e}")
        return False

# === COMMAND HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    if user_id in pending_groups:
        group_id = pending_groups[user_id]
        try:
            chat = await context.bot.get_chat(group_id)
            await update.message.reply_text(
                f"Welcome back!\n\n"
                f"I detected that you added me to '{chat.title}'.\n\n"
                f"To complete setup, forward any message from that group here.\n\n"
                f"Send /cancel to stop."
            )
            return
        except Exception as e:
            logger.error(f"Error: {e}")
            del pending_groups[user_id]
    
    keyboard = [
        [InlineKeyboardButton("Add Group", callback_data="add_group")],
        [InlineKeyboardButton("My Groups", callback_data="my_groups")]
    ]
    await update.message.reply_text(
        "Welcome to Group Manager Bot!\n\n"
        "Features:\n"
        "Delete join/leave messages\n"
        "Filter banned words\n"
        "Control links & promotions\n"
        "Ban users\n\n"
        "Click 'Add Group' to start!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)

    if data == "add_group":
        link = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        keyboard = [
            [InlineKeyboardButton("Add Bot to Group", url=link)],
            [InlineKeyboardButton("Cancel", callback_data="cancel_add")]
        ]
        await query.edit_message_text(
            "To add me:\n\n"
            "1. Click button below\n"
            "2. Choose group\n"
            "3. Make me admin (Delete + Ban)\n"
            "4. Return here & /start\n"
            "5. Forward a group message\n\n"
            "You must be admin!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "my_groups":
        groups = await get_user_groups(user_id)
        if not groups:
            keyboard = [[InlineKeyboardButton("Add Group", callback_data="add_group")]]
            await query.edit_message_text(
                "You don't have any groups yet.\n\n"
                "Click below to add one:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        text = "Your Groups:\n\n"
        keyboard = []
        for idx, group in enumerate(groups, 1):
            text += f"{idx}. {group['group_name']}\n"
            text += f"   ID: `{group['group_id']}`\n"
            text += f"   Join/Leave: {'ON' if group['delete_join_leave'] else 'OFF'}\n"
            text += f"   Links: {'ON' if group['delete_links'] else 'OFF'}\n"
            text += f"   Promotions: {'ON' if group['delete_promotions'] else 'OFF'}\n\n"
        keyboard.append([InlineKeyboardButton("Add Another Group", callback_data="add_group")])
        keyboard.append([InlineKeyboardButton("Settings", callback_data="settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "settings":
        groups = await get_user_groups(user_id)
        if not groups:
            keyboard = [[InlineKeyboardButton("Add Group", callback_data="add_group")]]
            await query.edit_message_text("No groups found. Add one first!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        group = groups[0]
        group_id = group['group_id']
        filtered_words = await get_filtered_words(group_id)
        keyboard = [
            [InlineKeyboardButton(
                f"Join/Leave: {'ON' if group['delete_join_leave'] else 'OFF'}",
                callback_data=f"toggle_join_leave_{group_id}"
            )],
            [InlineKeyboardButton(
                f"Delete Links: {'ON' if group['delete_links'] else 'OFF'}",
                callback_data=f"toggle_links_{group_id}"
            )],
            [InlineKeyboardButton(
                f"Delete Promotions: {'ON' if group['delete_promotions'] else 'OFF'}",
                callback_data=f"toggle_promotions_{group_id}"
            )],
            [InlineKeyboardButton("Back to My Groups", callback_data="my_groups")]
        ]
        filtered = ", ".join(filtered_words[:5]) if filtered_words else "None"
        if len(filtered_words) > 5:
            filtered += f" (+{len(filtered_words) - 5} more)"
        text = f"Settings for '{group['group_name']}'\n\n"
        text += f"Filtered words ({len(filtered_words)}): {filtered}\n\n"
        text += "Commands:\n"
        text += "• /filter <word>\n"
        text += "• /unfilter <word>\n"
        text += "• /listfilters\n\n"
        text += "Toggle below:"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "cancel_add":
        if user_id in pending_groups:
            del pending_groups[user_id]
        keyboard = [
            [InlineKeyboardButton("Add Group", callback_data="add_group")],
            [InlineKeyboardButton("My Groups", callback_data="my_groups")]
        ]
        await query.edit_message_text("Operation cancelled.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("toggle_"):
        parts = data.split("_")
        setting = "_".join(parts[1:-1])
        group_id = parts[-1]
        group = await get_group(group_id)
        if not group:
            await query.edit_message_text("Group not found.")
            return
        new_value = None
        if setting == "join_leave":
            new_value = not group['delete_join_leave']
            await update_group_setting(group_id, "delete_join_leave", new_value)
        elif setting == "links":
            new_value = not group['delete_links']
            await update_group_setting(group_id, "delete_links", new_value)
        elif setting == "promotions":
            new_value = not group['delete_promotions']
            await update_group_setting(group_id, "delete_promotions", new_value)
        # Refresh settings
        await settings(update, context)

# === VERIFICATION HANDLERS ===
async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.new_chat_members:
        return
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            group_id = str(update.effective_chat.id)
            group_name = update.effective_chat.title
            admin_id = str(update.message.from_user.id)
            
            if await get_group(group_id):
                return
            
            pending_groups[admin_id] = group_id
            keyboard = [InlineKeyboardButton("Verify Now", url=f"https://t.me/{BOT_USERNAME}")]
            try:
                await context.bot.send_message(
                    admin_id,
                    f"Added to '{group_name}'!\n\n"
                    f"Next: Forward any message from that group to me.\n"
                    f"Click below to open:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                logger.error(f"DM failed: {e}")

async def verify_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = str(message.from_user.id)
    
    if user_id not in pending_groups:
        return
    
    if not message.forward_from_chat or message.forward_from_chat.type not in ["group", "supergroup"]:
        await message.reply_text("Please forward a message from the group.")
        return
    
    group_id = str(message.forward_from_chat.id)
    pending_id = pending_groups[user_id]
    
    if group_id != pending_id:
        await message.reply_text("Wrong group! Forward from the correct one.")
        return
    
    group_name = message.forward_from_chat.title
    
    # Check bot status
    try:
        bot_member = await context.bot.get_chat_member(group_id, context.bot.id)
        if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await message.reply_text(f"I'm not in '{group_name}' anymore. Add me again.")
            del pending_groups[user_id]
            return
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.reply_text(
                f"I'm in '{group_name}' but not admin!\n\n"
                "Make me admin with:\n"
                "• Delete messages\n"
                "• Ban users\n\n"
                "Then forward again."
            )
            return
    except Exception as e:
        logger.error(f"Bot status error: {e}")
        await message.reply_text("Error checking my status.")
        return
    
    # Check user is admin
    try:
        user_member = await context.bot.get_chat_member(group_id, message.from_user.id)
        if user_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text(f"You must be admin of '{group_name}'.")
            del pending_groups[user_id]
            return
    except Exception as e:
        logger.error(f"User status error: {e}")
        await message.reply_text("Error checking your status.")
        return
    
    # Finalize
    if await get_group(group_id):
        await message.reply_text(f"'{group_name}' is already registered!")
        del pending_groups[user_id]
        return
    
    if await create_group(group_id, group_name, user_id):
        del pending_groups[user_id]
        keyboard = [
            [InlineKeyboardButton("Configure Settings", callback_data="settings")],
            [InlineKeyboardButton("View My Groups", callback_data="my_groups")]
        ]
        await message.reply_text(
            f"Successfully added '{group_name}'!\n\n"
            f"Setup complete. Bot is active.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        try:
            await context.bot.send_message(
                group_id,
                f"Setup complete!\n\n"
                f"I'm now protecting this group.\n"
                f"Admins: Use @{BOT_USERNAME} to configure."
            )
        except Exception as e:
            logger.error(f"Group confirm failed: {e}")
    else:
        await message.reply_text("Failed to save group. Try again.")

async def cancel_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id in pending_groups:
        del pending_groups[user_id]
        await update.message.reply_text("Verification cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id in pending_groups:
        await update.message.reply_text(
            "Please forward a message from the group to verify.\n"
            "Send /cancel to stop."
        )

# === GROUP MESSAGE HANDLER ===
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Skip private chats
    if update.effective_chat.type == "private":
        return
    group_id = str(update.effective_chat.id)
    group = await get_group(group_id)
    if not group:
        return
    message = update.message

    try:
        # Delete join/leave
        if group["delete_join_leave"] and (message.new_chat_members or message.left_chat_member):
            await context.bot.delete_message(update.effective_chat.id, message.message_id)
            return

        # Filter words
        if message.text:
            text_lower = message.text.lower()
            filtered_words = await get_filtered_words(group_id)
            for word in filtered_words:
                if word in text_lower:
                    await context.bot.delete_message(update.effective_chat.id, message.message_id)
                    warning = await context.bot.send_message(
                        update.effective_chat.id,
                        f"Message deleted: Contains filtered word '{word}'"
                    )
                    await asyncio.sleep(5)
                    await context.bot.delete_message(update.effective_chat.id, warning.message_id)
                    return

        # Delete links
        if group["delete_links"]:
            if message.text and re.search(r'http[s]?://', message.text):
                await context.bot.delete_message(update.effective_chat.id, message.message_id)
                return
            if message.entities:
                for entity in message.entities:
                    if entity.type in ["url", "text_link"]:
                        await context.bot.delete_message(update.effective_chat.id, message.message_id)
                        return

        # Delete promotions
        if group["delete_promotions"]:
            if message.forward_from or message.forward_from_chat:
                await context.bot.delete_message(update.effective_chat.id, message.message_id)
                return
            if message.text:
                promo = ["join", "channel", "group", "subscribe", "follow", "t.me"]
                if any(k in text_lower for k in promo):
                    await context.bot.delete_message(update.effective_chat.id, message.message_id)
                    return
    except Exception as e:
        logger.error(f"Message handling error: {e}")

# === FILTER COMMANDS ===
async def filter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /filter <word>")
        return
    word = " ".join(context.args).lower()
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    if not groups:
        await update.message.reply_text("No groups. Add one first!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Add Group", callback_data="add_group")]]))
        return
    group_id = groups[0]['group_id']
    if word not in await get_filtered_words(group_id):
        if await add_filtered_word(group_id, word):
            await update.message.reply_text(f"Added '{word}' to filters.")
        else:
            await update.message.reply_text("Failed to add word.")
    else:
        await update.message.reply_text(f"'{word}' already filtered.")

async def unfilter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unfilter <word>")
        return
    word = " ".join(context.args).lower()
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    if not groups:
        await update.message.reply_text("No groups.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Add Group", callback_data="add_group")]]))
        return
    group_id = groups[0]['group_id']
    if word in await get_filtered_words(group_id):
        if await remove_filtered_word(group_id, word):
            await update.message.reply_text(f"Removed '{word}'.")
        else:
            await update.message.reply_text("Failed to remove.")
    else:
        await update.message.reply_text(f"'{word}' not filtered.")

async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    if not groups:
        await update.message.reply_text("No groups.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Add Group", callback_data="add_group")]]))
        return
    words = await get_filtered_words(groups[0]['group_id'])
    if words:
        text = f"Filtered words:\n\n" + "\n".join([f"• {w}" for w in words]) + f"\n\nTotal: {len(words)}"
    else:
        text = "No filtered words.\nUse /filter <word>"
    await update.message.reply_text(text)

async def mygroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await button_callback(update, context)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await button_callback(update, context)

# === BAN COMMAND ===
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use in groups only.")
        return
    group_id = str(update.effective_chat.id)
    group = await get_group(group_id)
    if not group:
        return
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await update.message.reply_text("Admins only!")
            await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
            return
    except Exception as e:
        logger.error(f"Admin check failed: {e}")
        return

    user_to_ban = None
    if update.message.reply_to_message:
        user_to_ban = update.message.reply_to_message.from_user.id
    elif context.args:
        username = context.args[0].lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            user_to_ban = chat.id
        except Exception:
            await update.message.reply_text("User not found.")
            return
    else:
        await update.message.reply_text("Reply to a message or use /ban @username")
        return

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_ban)
        await add_banned_user(group_id, user_to_ban)
        msg = await update.message.reply_text("User banned.")
        await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
        await asyncio.sleep(5)
        await context.bot.delete_message(update.effective_chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Ban failed: {e}")
        await update.message.reply_text("Failed to ban.")

# === WEBHOOK ===
@app.post("/webhook")
async def webhook(request: Request):
    try:
        app_instance = await get_application()
        data = await request.json()
        update = Update.de_json(data, app_instance.bot)
        asyncio.create_task(process_update_safe(app_instance, update))
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

async def process_update_safe(app_instance, update):
    try:
        await app_instance.process_update(update)
    except Exception as e:
        logger.error(f"Update error: {e}")

@app.get("/")
async def root():
    return {"status": "Bot running", "bot_username": BOT_USERNAME}

@app.get("/health")
async def health_check():
    try:
        supabase.table("groups").select("count").limit(1).execute()
        return {"status": "healthy", "pending": len(pending_groups)}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.get("/pending")
async def get_pending():
    return {"pending": pending_groups, "count": len(pending_groups)}
