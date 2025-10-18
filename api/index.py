# api/index.py
import os
import re
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ChatMemberStatus
from supabase import create_client, Client
from typing import Optional, List, Dict

app = FastAPI()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Conversation states
WAITING_FOR_GROUP = 1

# Global application instance
application = None

# Initialize bot application
async def get_application():
    global application
    if application is None:
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Conversation handler for adding groups
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_add_group, pattern="^add_group$")],
            states={
                WAITING_FOR_GROUP: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_group_message)]
            },
            fallbacks=[CommandHandler("cancel", cancel_add_group)]
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
        
        await application.initialize()
        await application.start()
    
    return application

# Database helper functions
async def get_group(group_id: str) -> Optional[Dict]:
    """Get group from database"""
    try:
        response = supabase.table("groups").select("*").eq("group_id", group_id).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error getting group: {e}")
        return None

async def create_group(group_id: str, group_name: str, admin_user_id: str) -> bool:
    """Create a new group in database"""
    try:
        # Insert group
        supabase.table("groups").insert({
            "group_id": group_id,
            "group_name": group_name,
            "admin_user_id": admin_user_id,
            "delete_join_leave": True,
            "delete_links": False,
            "delete_promotions": False
        }).execute()
        
        # Link user to group
        supabase.table("user_groups").insert({
            "user_id": admin_user_id,
            "group_id": group_id
        }).execute()
        
        # Add default filtered words
        default_words = ["scam", "fuck"]
        for word in default_words:
            supabase.table("filtered_words").insert({
                "group_id": group_id,
                "word": word
            }).execute()
        
        return True
    except Exception as e:
        print(f"Error creating group: {e}")
        return False

async def get_user_groups(user_id: str) -> List[Dict]:
    """Get all groups for a user"""
    try:
        response = supabase.table("user_groups").select("group_id").eq("user_id", user_id).execute()
        group_ids = [item["group_id"] for item in response.data]
        
        if not group_ids:
            return []
        
        groups_response = supabase.table("groups").select("*").in_("group_id", group_ids).execute()
        return groups_response.data
    except Exception as e:
        print(f"Error getting user groups: {e}")
        return []

async def update_group_setting(group_id: str, setting: str, value: bool) -> bool:
    """Update group setting"""
    try:
        supabase.table("groups").update({setting: value}).eq("group_id", group_id).execute()
        return True
    except Exception as e:
        print(f"Error updating setting: {e}")
        return False

async def get_filtered_words(group_id: str) -> List[str]:
    """Get filtered words for a group"""
    try:
        response = supabase.table("filtered_words").select("word").eq("group_id", group_id).execute()
        return [item["word"] for item in response.data]
    except Exception as e:
        print(f"Error getting filtered words: {e}")
        return []

async def add_filtered_word(group_id: str, word: str) -> bool:
    """Add a filtered word"""
    try:
        supabase.table("filtered_words").insert({
            "group_id": group_id,
            "word": word
        }).execute()
        return True
    except Exception as e:
        print(f"Error adding filtered word: {e}")
        return False

async def remove_filtered_word(group_id: str, word: str) -> bool:
    """Remove a filtered word"""
    try:
        supabase.table("filtered_words").delete().eq("group_id", group_id).eq("word", word).execute()
        return True
    except Exception as e:
        print(f"Error removing filtered word: {e}")
        return False

async def add_banned_user(group_id: str, user_id: int) -> bool:
    """Add a banned user"""
    try:
        supabase.table("banned_users").insert({
            "group_id": group_id,
            "user_id": user_id
        }).execute()
        return True
    except Exception as e:
        print(f"Error adding banned user: {e}")
        return False

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Group Manually", callback_data="add_group")]
    ]
    
    await update.message.reply_text(
        "👋 Welcome to Group Manager Bot!\n\n"
        "Features:\n"
        "✅ Delete join/leave messages\n"
        "✅ Filter banned words\n"
        "✅ Control links & promotions\n"
        "✅ Ban users\n\n"
        "Setup:\n"
        "1. Add me to your group\n"
        "2. Make me admin (delete messages & ban users)\n"
        "3. Use /mygroups to see your groups\n"
        "4. Use /settings to configure\n\n"
        "Or click the button below to add a group manually:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Start add group process
async def start_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📝 To add a group manually:\n\n"
        "1. Go to the group you want to add\n"
        "2. Send any message in that group\n"
        "3. Forward that message to me\n\n"
        "⚠️ Note: I must be a member of that group and have admin rights!\n\n"
        "Send /cancel to cancel this operation."
    )
    
    return WAITING_FOR_GROUP

# Receive forwarded message from group
async def receive_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # Check if message is forwarded from a group
    if not message.forward_from_chat:
        await message.reply_text(
            "❌ Please forward a message from the group you want to add.\n"
            "Send /cancel to cancel."
        )
        return WAITING_FOR_GROUP
    
    if message.forward_from_chat.type not in ["group", "supergroup"]:
        await message.reply_text(
            "❌ The forwarded message must be from a group!\n"
            "Send /cancel to cancel."
        )
        return WAITING_FOR_GROUP
    
    group_id = str(message.forward_from_chat.id)
    group_name = message.forward_from_chat.title
    user_id = str(message.from_user.id)
    
    # Check if bot is member of the group
    try:
        bot_member = await context.bot.get_chat_member(message.forward_from_chat.id, context.bot.id)
        
        if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
            await message.reply_text(
                f"❌ I'm not a member of '{group_name}'!\n"
                "Please add me to the group first."
            )
            return ConversationHandler.END
        
        # Check if bot is admin
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.reply_text(
                f"⚠️ I'm a member of '{group_name}' but not an admin!\n"
                "Please make me an admin with these permissions:\n"
                "• Delete messages\n"
                "• Ban users\n\n"
                "Group added, but features may not work properly."
            )
        
        # Check if user is admin of the group
        user_member = await context.bot.get_chat_member(message.forward_from_chat.id, message.from_user.id)
        
        if user_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text(
                f"❌ You must be an admin of '{group_name}' to add it!"
            )
            return ConversationHandler.END
        
    except Exception as e:
        await message.reply_text(
            f"❌ Error checking group status: {str(e)}\n"
            "Make sure I'm a member of the group and you're an admin."
        )
        return ConversationHandler.END
    
    # Check if group already exists
    existing_group = await get_group(group_id)
    if existing_group:
        await message.reply_text(
            f"ℹ️ '{group_name}' is already registered!\n"
            "Use /settings to configure it."
        )
        return ConversationHandler.END
    
    # Add group to database
    success = await create_group(group_id, group_name, user_id)
    
    if success:
        await message.reply_text(
            f"✅ Successfully added '{group_name}'!\n\n"
            "You can now:\n"
            "• Use /settings to configure the bot\n"
            "• Use /mygroups to see all your groups\n"
            "• Use /filter to add filtered words\n\n"
            "Make sure I have admin rights in the group!"
        )
    else:
        await message.reply_text(
            "❌ Failed to add group. Please try again later."
        )
    
    return ConversationHandler.END

# Cancel add group
async def cancel_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Operation cancelled.\n"
        "Use /start to see available options."
    )
    return ConversationHandler.END

# Bot added to group
async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.id == context.bot.id:
                group_id = str(update.effective_chat.id)
                group_name = update.effective_chat.title
                admin_id = str(update.message.from_user.id)
                
                # Check if group already exists
                existing_group = await get_group(group_id)
                if not existing_group:
                    # Create new group
                    await create_group(group_id, group_name, admin_id)
                
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"✅ I've been added to '{group_name}'!\nUse /settings to configure me."
                    )
                except:
                    pass

# My groups command
async def mygroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    groups = await get_user_groups(user_id)
    
    if not groups:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group Manually", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "You don't have any groups with this bot yet.\n"
            "Click the button below to add a group manually:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    text = "📋 Your Groups:\n\n"
    for group in groups:
        text += f"• {group['group_name']}\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Another Group", callback_data="add_group")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Settings command
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
        )]
    ]
    
    filtered = ", ".join(filtered_words) if filtered_words else "None"
    text = f"⚙️ Settings for '{group['group_name']}'\n\n"
    text += f"Filtered words: {filtered}\n\n"
    text += "Use /filter <word> to add\nUse /unfilter <word> to remove\nUse /listfilters to view all"
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Toggle settings callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data.startswith("toggle_"):
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
                )]
            ]
            
            filtered = ", ".join(filtered_words) if filtered_words else "None"
            text = f"⚙️ Settings for '{group['group_name']}'\n\n"
            text += f"Filtered words: {filtered}\n\n"
            text += "Use /filter <word> to add\nUse /unfilter <word> to remove\nUse /listfilters to view all"
            
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Filter command
async def filter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /filter <word>")
        return
    
    word = context.args[0].lower()
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
            await update.message.reply_text(f"✅ Added '{word}' to filtered words")
        else:
            await update.message.reply_text(f"❌ Failed to add '{word}'")
    else:
        await update.message.reply_text(f"'{word}' is already filtered")

# Unfilter command
async def unfilter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unfilter <word>")
        return
    
    word = context.args[0].lower()
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
            await update.message.reply_text(f"✅ Removed '{word}' from filtered words")
        else:
            await update.message.reply_text(f"❌ Failed to remove '{word}'")
    else:
        await update.message.reply_text(f"'{word}' is not in filtered words")

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
    filtered_words = await get_filtered_words(group_id)
    
    if filtered_words:
        text = "📝 Filtered words:\n" + "\n".join([f"• {word}" for word in filtered_words])
    else:
        text = "No filtered words yet"
    
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
    member = await context.bot.get_chat_member(update.effective_chat.id, update.message.from_user.id)
    if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        await update.message.reply_text("Only admins can use this command!")
        await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
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
        except:
            await update.message.reply_text("User not found!")
            await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
            return
    else:
        await update.message.reply_text("Usage: /ban @username or reply to a message with /ban")
        await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
        return
    
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_ban)
        await add_banned_user(group_id, user_to_ban)
        await update.message.reply_text("✅ User has been banned")
        await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception as e:
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
                    return
        
        # Delete links
        if group["delete_links"]:
            if message.text:
                url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
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
        print(f"Error handling message: {e}")

# FastAPI webhook endpoint
@app.post("/webhook")
async def webhook(request: Request):
    try:
        app_instance = await get_application()
        data = await request.json()
        update = Update.de_json(data, app_instance.bot)
        await app_instance.process_update(update)
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {"status": "Bot is running", "bot": "Group Manager Bot"}

@app.get("/setwebhook")
async def set_webhook():
    try:
        app_instance = await get_application()
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await app_instance.bot.set_webhook(webhook_url)
        return {"status": "Webhook set", "url": webhook_url}
    except Exception as e:
        return {"status": "Failed", "error": str(e)}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Test Supabase connection
        supabase.table("groups").select("count").limit(1).execute()
        return {
            "status": "healthy",
            "bot": "running",
            "database": "connected"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }

# Initialize application on startup
@app.on_event("startup")
async def startup():
    await get_application()
    # Set webhook
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await application.bot.set_webhook(webhook_url)
        print(f"Webhook set to: {webhook_url}")

@app.on_event("shutdown")
async def shutdown():
    if application:
        await application.stop()
        await application.shutdown()


