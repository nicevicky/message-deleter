# api/index.py
import os
import re
import logging
import asyncio
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
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

# Conversation states
WAITING_FOR_GROUP_VERIFICATION = 1

# Global application instance
application = None

# Store pending group additions (user_id -> group_id)
pending_groups = {}

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
                "An error occurred while processing your request. Please try again later."
            )
    except Exception as e:
        logger.error(f"Failed to send error message to user: {e}")

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
        
        # === FIXED: ConversationHandler with per_message=True ===
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_add_group, pattern="^add_group$")],
            states={
                WAITING_FOR_GROUP_VERIFICATION: [
                    MessageHandler(filters.ALL & ~filters.COMMAND, verify_group_message)
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel_add_group)],
            per_chat=False,      # Only private chats
            per_user=True,       # One conversation per user
            per_message=True     # REQUIRED for CallbackQueryHandler
        )
        
        # Register handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("mygroups", mygroups))
        application.add_handler(CommandHandler("settings", settings))
        application.add_handler(CommandHandler("filter", filter_word))
        application.add_handler(CommandHandler("unfilter", unfilter_word))
        application.add_handler(CommandHandler("listfilters", list_filters))
        application.add_handler(CommandHandler("ban", ban_user))
        application.add_handler(conv_handler)
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bot_added_to_group))
        application.add_handler(MessageHandler(filters.ALL, handle_group_message))
        
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
        
        default_words = ["scam", "fuck"]
        for word in default_words:
            supabase.table("filtered_words").insert({
                "group_id": group_id,
                "word": word
            }).execute()
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
                f"To complete the setup, please:\n"
                f"1. Go to '{chat.title}'\n"
                f"2. Send any message in that group\n"
                f"3. Forward that message back to me\n\n"
                f"This helps me verify that you're an admin of the group.\n\n"
                f"Send /cancel to cancel this operation."
            )
            return
        except Exception as e:
            logger.error(f"Error getting pending group chat: {e}")
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
        "Click 'Add Group' to get started!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    add_to_group_link = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    keyboard = [
        [InlineKeyboardButton("Add Bot to Group", url=add_to_group_link)],
        [InlineKeyboardButton("Cancel", callback_data="cancel_add")]
    ]
    
    await query.edit_message_text(
        "To add me to your group:\n\n"
        "1. Click the 'Add Bot to Group' button below\n"
        "2. Select the group you want to add me to\n"
        "3. Make me an admin with these permissions:\n"
        "   • Delete messages\n"
        "   • Ban users\n\n"
        "4. After adding me, come back here and click /start\n"
        "5. Forward any message from that group to verify\n\n"
        "Important: You must be an admin of the group!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.id == context.bot.id:
                group_id = str(update.effective_chat.id)
                group_name = update.effective_chat.title
                admin_id = str(update.message.from_user.id)
                
                if await get_group(group_id):
                    return
                
                pending_groups[admin_id] = group_id
                
                keyboard = [InlineKeyboardButton("Verify Group Now", url=f"https://t.me/{BOT_USERNAME}")]
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"Great! I've been added to '{group_name}'!\n\n"
                             f"Next step: Verify the group\n\n"
                             f"1. Go to '{group_name}'\n"
                             f"2. Send any message there\n"
                             f"3. Forward that message to me\n\n"
                             f"Click the button below to continue:",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except Exception as e:
                    logger.error(f"Error sending message to user: {e}")
                
                try:
                    await context.bot.send_message(
                        chat_id=group_id,
                        text=f"Hello! I'm Group Manager Bot.\n\n"
                             f"Setup not complete yet!\n\n"
                             f"The admin who added me needs to verify this group.\n\n"
                             f"Admin: Please go to @{BOT_USERNAME} and follow the verification steps."
                    )
                except Exception as e:
                    logger.error(f"Error sending message to group: {e}")

async def verify_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = str(message.from_user.id)
    
    if not message.forward_from_chat:
        await message.reply_text(
            "Please forward a message from the group you want to verify.\n\n"
            "Steps:\n"
            "1. Go to the group where you added me\n"
            "2. Send any message there\n"
            "3. Forward that message to me\n\n"
            "Send /cancel to cancel."
        )
        return WAITING_FOR_GROUP_VERIFICATION
    
    if message.forward_from_chat.type not in ["group", "supergroup"]:
        await message.reply_text("The forwarded message must be from a group!\nSend /cancel to cancel.")
        return WAITING_FOR_GROUP_VERIFICATION
    
    group_id = str(message.forward_from_chat.id)
    group_name = message.forward_from_chat.title
    
    if user_id not in pending_groups:
        await message.reply_text("No pending group verification found.\nPlease add me to a group first.")
        return ConversationHandler.END
    
    pending_group_id = pending_groups[user_id]
    if group_id != pending_group_id:
        try:
            pending_chat = await context.bot.get_chat(pending_group_id)
            await message.reply_text(
                f"Wrong group!\n\n"
                f"You added me to '{pending_chat.title}', but you forwarded a message from '{group_name}'.\n\n"
                f"Please forward a message from '{pending_chat.title}' instead."
            )
        except Exception:
            await message.reply_text("Wrong group!\nPlease forward a message from the group where you added me.")
        return WAITING_FOR_GROUP_VERIFICATION
    
    try:
        bot_member = await context.bot.get_chat_member(group_id, context.bot.id)
        if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await message.reply_text(f"I'm not a member of '{group_name}' anymore!\nPlease add me again.")
            del pending_groups[user_id]
            return ConversationHandler.END
        
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.reply_text(
                f"I'm a member of '{group_name}' but not an admin!\n\n"
                f"Please make me an admin with:\n"
                "• Delete messages\n"
                "• Ban users\n\n"
                "Then forward another message."
            )
            return WAITING_FOR_GROUP_VERIFICATION
        
        user_member = await context.bot.get_chat_member(group_id, message.from_user.id)
        if user_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text(f"You must be an admin of '{group_name}' to add it!")
            del pending_groups[user_id]
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error checking group status: {e}")
        await message.reply_text("Error checking group status. Ensure I'm still in the group and have admin rights.")
        return WAITING_FOR_GROUP_VERIFICATION
    
    if await get_group(group_id):
        await message.reply_text(f"'{group_name}' is already registered!\nUse /settings to configure it.")
        del pending_groups[user_id]
        return ConversationHandler.END
    
    if await create_group(group_id, group_name, user_id):
        del pending_groups[user_id]
        keyboard = [
            [InlineKeyboardButton("Configure Settings", callback_data="settings")],
            [InlineKeyboardButton("View My Groups", callback_data="my_groups")]
        ]
        await message.reply_text(
            f"Successfully verified and added '{group_name}'!\n\n"
            f"Setup complete! Your group is now protected.\n\n"
            f"Use /settings, /filter <word>, /mygroups, etc.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=f"Setup complete!\n\n"
                     f"I'm now protecting this group with:\n"
                     f"• Auto-delete join/leave messages\n"
                     f"• Filter banned words (scam, fuck)\n"
                     f"• Delete links\n"
                     f"• Delete promotions\n\n"
                     f"Admins can configure via @{BOT_USERNAME}"
            )
        except Exception as e:
            logger.error(f"Error sending confirmation to group: {e}")
    else:
        await message.reply_text("Failed to add group to database. Please try again later.")
    
    return ConversationHandler.END

async def cancel_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id in pending_groups:
        del pending_groups[user_id]
    
    keyboard = [
        [InlineKeyboardButton("Add Group", callback_data="add_group")],
        [InlineKeyboardButton("My Groups", callback_data="my_groups")]
    ]
    await update.message.reply_text("Operation cancelled.", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def mygroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "📋 You don't have any groups yet.\n\n"
            "Click the button below to add your first group:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    text = "📋 Your Groups:\n\n"
    keyboard = []
    
    for idx, group in enumerate(groups, 1):
        text += f"{idx}. {group['group_name']}\n"
        text += f"   ID: `{group['group_id']}`\n"
        text += f"   Join/Leave: {'✅' if group['delete_join_leave'] else '❌'}\n"
        text += f"   Links: {'✅' if group['delete_links'] else '❌'}\n"
        text += f"   Promotions: {'✅' if group['delete_promotions'] else '❌'}\n\n"
    
    keyboard.append([InlineKeyboardButton("➕ Add Another Group", callback_data="add_group")])
    keyboard.append([InlineKeyboardButton("⚙️ Settings", callback_data="settings")])
    
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found. Add me to a group first!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # If multiple groups, use first group
    group = groups[0]
    group_id = group['group_id']
    
    # Get filtered words
    filtered_words = await get_filtered_words(group_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"🗑️ Join/Leave: {'✅' if group['delete_join_leave'] else '❌'}",
            callback_data=f"toggle_join_leave_{group_id}"
        )],
        [InlineKeyboardButton(
            f"🔗 Delete Links: {'✅' if group['delete_links'] else '❌'}",
            callback_data=f"toggle_links_{group_id}"
        )],
        [InlineKeyboardButton(
            f"📢 Delete Promotions: {'✅' if group['delete_promotions'] else '❌'}",
            callback_data=f"toggle_promotions_{group_id}"
        )],
        [InlineKeyboardButton("📋 Back to My Groups", callback_data="my_groups")]
    ]
    
    filtered = ", ".join(filtered_words[:5]) if filtered_words else "None"
    if len(filtered_words) > 5:
        filtered += f" (+{len(filtered_words) - 5} more)"
    
    text = f"⚙️ Settings for '{group['group_name']}'\n\n"
    text += f"📝 Filtered words ({len(filtered_words)}): {filtered}\n\n"
    text += "Commands:\n"
    text += "• /filter <word> - Add filtered word\n"
    text += "• /unfilter <word> - Remove filtered word\n"
    text += "• /listfilters - View all filtered words\n\n"
    text += "Toggle settings below:"
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    user_id = str(query.from_user.id)
    
    if callback_data == "add_group":
        # Create the add to group link
        add_to_group_link = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Bot to Group", url=add_to_group_link)],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_add")]
        ]
        
        await query.edit_message_text(
            "📝 To add me to your group:\n\n"
            "1️⃣ Click the 'Add Bot to Group' button below\n"
            "2️⃣ Select the group you want to add me to\n"
            "3️⃣ Make me an admin with these permissions:\n"
            "   • Delete messages\n"
            "   • Ban users\n\n"
            "4️⃣ After adding me, come back here and click /start\n"
            "5️⃣ Forward any message from that group to verify\n\n"
            "⚠️ Important: You must be an admin of the group!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif callback_data == "my_groups":
        groups = await get_user_groups(user_id)
        
        if not groups:
            keyboard = [
                [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
            ]
            await query.edit_message_text(
                "📋 You don't have any groups yet.\n\n"
                "Click the button below to add your first group:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        text = "📋 Your Groups:\n\n"
        keyboard = []
        
        for idx, group in enumerate(groups, 1):
            text += f"{idx}. {group['group_name']}\n"
            text += f"   ID: `{group['group_id']}`\n"
            text += f"   Join/Leave: {'✅' if group['delete_join_leave'] else '❌'}\n"
            text += f"   Links: {'✅' if group['delete_links'] else '❌'}\n"
            text += f"   Promotions: {'✅' if group['delete_promotions'] else '❌'}\n\n"
        
        keyboard.append([InlineKeyboardButton("➕ Add Another Group", callback_data="add_group")])
        keyboard.append([InlineKeyboardButton("⚙️ Settings", callback_data="settings")])
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    
    elif callback_data == "settings":
        groups = await get_user_groups(user_id)
        
        if not groups:
            keyboard = [
                [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
            ]
            await query.edit_message_text(
                "No groups found. Add me to a group first!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        group = groups[0]
        group_id = group['group_id']
        
        # Get filtered words
        filtered_words = await get_filtered_words(group_id)
        
        keyboard = [
            [InlineKeyboardButton(
                f"🗑️ Join/Leave: {'✅' if group['delete_join_leave'] else '❌'}",
                callback_data=f"toggle_join_leave_{group_id}"
            )],
            [InlineKeyboardButton(
                f"🔗 Delete Links: {'✅' if group['delete_links'] else '❌'}",
                callback_data=f"toggle_links_{group_id}"
            )],
            [InlineKeyboardButton(
                f"📢 Delete Promotions: {'✅' if group['delete_promotions'] else '❌'}",
                callback_data=f"toggle_promotions_{group_id}"
            )],
            [InlineKeyboardButton("📋 Back to My Groups", callback_data="my_groups")]
        ]
        
        filtered = ", ".join(filtered_words[:5]) if filtered_words else "None"
        if len(filtered_words) > 5:
            filtered += f" (+{len(filtered_words) - 5} more)"
        
        text = f"⚙️ Settings for '{group['group_name']}'\n\n"
        text += f"📝 Filtered words ({len(filtered_words)}): {filtered}\n\n"
        text += "Commands:\n"
        text += "• /filter <word> - Add filtered word\n"
        text += "• /unfilter <word> - Remove filtered word\n"
        text += "• /listfilters - View all filtered words\n\n"
        text += "Toggle settings below:"
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif callback_data == "cancel_add":
        # Remove from pending groups if exists
        if user_id in pending_groups:
            del pending_groups[user_id]
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")],
            [InlineKeyboardButton("📋 My Groups", callback_data="my_groups")]
        ]
        
        await query.edit_message_text(
            "❌ Operation cancelled.\n\n"
            "Use the buttons below to continue:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif callback_data.startswith("toggle_"):
        parts = callback_data.split("_")
        setting = "_".join(parts[1:-1])
        group_id = parts[-1]
        
        # Get current group settings
        group = await get_group(group_id)
        
        if group:
            # Toggle the setting
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
            
            # Get updated group data
            group = await get_group(group_id)
            filtered_words = await get_filtered_words(group_id)
            
            keyboard = [
                [InlineKeyboardButton(
                    f"🗑️ Join/Leave: {'✅' if group['delete_join_leave'] else '❌'}",
                    callback_data=f"toggle_join_leave_{group_id}"
                )],
                [InlineKeyboardButton(
                    f"🔗 Delete Links: {'✅' if group['delete_links'] else '❌'}",
                    callback_data=f"toggle_links_{group_id}"
                )],
                [InlineKeyboardButton(
                    f"📢 Delete Promotions: {'✅' if group['delete_promotions'] else '❌'}",
                    callback_data=f"toggle_promotions_{group_id}"
                )],
                [InlineKeyboardButton("📋 Back to My Groups", callback_data="my_groups")]
            ]
            
            filtered = ", ".join(filtered_words[:5]) if filtered_words else "None"
            if len(filtered_words) > 5:
                filtered += f" (+{len(filtered_words) - 5} more)"
            
            text = f"⚙️ Settings for '{group['group_name']}'\n\n"
            text += f"📝 Filtered words ({len(filtered_words)}): {filtered}\n\n"
            text += "Commands:\n"
            text += "• /filter <word> - Add filtered word\n"
            text += "• /unfilter <word> - Remove filtered word\n"
            text += "• /listfilters - View all filtered words\n\n"
            text += "Toggle settings below:"
            
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Filter command
async def filter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /filter <word>")
        return
    
    word = " ".join(context.args).lower()
    user_id = str(update.message.from_user.id)
    
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = groups[0]['group_id']
    filtered_words = await get_filtered_words(group_id)
    
    if word not in filtered_words:
        success = await add_filtered_word(group_id, word)
        if success:
            await update.message.reply_text(f"✅ Added '{word}' to filtered words for {groups[0]['group_name']}")
        else:
            await update.message.reply_text(f"❌ Failed to add '{word}'")
    else:
        await update.message.reply_text(f"'{word}' is already filtered in {groups[0]['group_name']}")

# Unfilter command
async def unfilter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unfilter <word>")
        return
    
    word = " ".join(context.args).lower()
    user_id = str(update.message.from_user.id)
    
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = groups[0]['group_id']
    filtered_words = await get_filtered_words(group_id)
    
    if word in filtered_words:
        success = await remove_filtered_word(group_id, word)
        if success:
            await update.message.reply_text(f"✅ Removed '{word}' from filtered words for {groups[0]['group_name']}")
        else:
            await update.message.reply_text(f"❌ Failed to remove '{word}'")
    else:
        await update.message.reply_text(f"'{word}' is not in filtered words for {groups[0]['group_name']}")

# List filters command
async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
            
    group_id = groups[0]['group_id']
    group_name = groups[0]['group_name']
    filtered_words = await get_filtered_words(group_id)
    
    if filtered_words:
        text = f"📝 Filtered words for '{group_name}':\n\n"
        text += "\n".join([f"• {word}" for word in filtered_words])
        text += f"\n\nTotal: {len(filtered_words)} words"
    else:
        text = f"No filtered words yet for '{group_name}'\n\n"
        text += "Use /filter <word> to add filtered words"
    
    await update.message.reply_text(text)

# Ban command
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups!")
        return
    
    group_id = str(update.effective_chat.id)
    
    # Check if group exists in database
    group = await get_group(group_id)
    if not group:
        return
    
    # Check if user is admin
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await update.message.reply_text("Only admins can use this command!")
            try:
                await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
            except Exception as e:
                logger.error(f"Error deleting command message: {e}")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return
    
    user_to_ban = None
    
    # Check if replying to a message
    if update.message.reply_to_message:
        user_to_ban = update.message.reply_to_message.from_user.id
    # Check if username provided
    elif context.args:
        username = context.args[0].replace("@", "")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            user_to_ban = chat.id
        except Exception as e:
            logger.error(f"Error getting user by username: {e}")
            await update.message.reply_text("User not found!")
            try:
                await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
            except Exception as e:
                logger.error(f"Error deleting command message: {e}")
            return
    else:
        await update.message.reply_text("Usage: /ban @username or reply to a message with /ban")
        try:
            await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
        except Exception as e:
            logger.error(f"Error deleting command message: {e}")
        return
    
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_ban)
        await add_banned_user(group_id, user_to_ban)
        success_msg = await update.message.reply_text("✅ User has been banned")
        
        # Delete the ban command message
        try:
            await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
        except Exception as e:
            logger.error(f"Error deleting command message: {e}")
        
        # Delete success message after 5 seconds
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(update.effective_chat.id, success_msg.message_id)
        except Exception as e:
            logger.error(f"Error deleting success message: {e}")
            
    except Exception as e:
        logger.error(f"Error banning user: {e}")
        await update.message.reply_text(f"Failed to ban user: {str(e)}")

# Handle all group messages
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return
    
    group_id = str(update.effective_chat.id)
    
    # Get group from database
    group = await get_group(group_id)
    if not group:
        return
    
    message = update.message
    
    try:
        # Delete join/leave messages
        if group["delete_join_leave"]:
            if message.new_chat_members or message.left_chat_member:
                await context.bot.delete_message(update.effective_chat.id, message.message_id)
                return
        
        # Check filtered words
        if message.text:
            text_lower = message.text.lower()
            filtered_words = await get_filtered_words(group_id)
            
            for word in filtered_words:
                if word in text_lower:
                    await context.bot.delete_message(update.effective_chat.id, message.message_id)
                    # Optionally warn the user
                    try:
                        warning = await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"⚠️ Message deleted: Contains filtered word '{word}'"
                        )
                        # Delete warning after 5 seconds
                        await asyncio.sleep(5)
                        await context.bot.delete_message(update.effective_chat.id, warning.message_id)
                    except Exception as e:
                        logger.error(f"Error sending/deleting warning: {e}")
                    return
        
        # Delete links
        if group["delete_links"]:
            if message.text:
                url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*$$$$,]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
                if re.search(url_pattern, message.text):
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
                promo_keywords = ["join", "channel", "group", "subscribe", "follow", "t.me"]
                text_lower = message.text.lower()
                for keyword in promo_keywords:
                    if keyword in text_lower:
                        await context.bot.delete_message(update.effective_chat.id, message.message_id)
                        return
    
    except Exception as e:
        logger.error(f"Error handling message: {e}")

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
        logger.error(f"Error processing update: {e}")

@app.get("/")
async def root():
    return {
        "status": "Bot is running",
        "bot": "Group Manager Bot",
        "bot_username": BOT_USERNAME,
        "add_to_group": f"https://t.me/{BOT_USERNAME}?startgroup=true"
    }

@app.get("/setwebhook")
async def set_webhook():
    try:
        app_instance = await get_application()
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await app_instance.bot.set_webhook(webhook_url)
        return {"status": "Webhook set", "url": webhook_url}
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return {"status": "Failed", "error": str(e)}

@app.get("/health")
async def health_check():
    try:
        supabase.table("groups").select("count").limit(1).execute()
        return {"status": "healthy", "database": "connected", "pending": len(pending_groups)}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

@app.get("/pending")
async def get_pending():
    return {"pending_groups": pending_groups, "count": len(pending_groups)}

@app.get("/deletewebhook")
async def delete_webhook():
    try:
        app_instance = await get_application()
        await app_instance.bot.delete_webhook()
        return {"status": "Webhook deleted"}
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}")
        return {"status": "Failed", "error": str(e)}
