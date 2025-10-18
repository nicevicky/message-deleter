# api/index.py
import os
import json
import re
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ChatMemberStatus

app = FastAPI()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

DATA_FILE = "/tmp/settings.json"

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

# Load/Save JSON data
def load_data():
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"groups": {}, "user_groups": {}}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

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
    
    # Add group to database
    data = load_data()
    
    if group_id in data["groups"]:
        await message.reply_text(
            f"ℹ️ '{group_name}' is already registered!\n"
            "Use /settings to configure it."
        )
        return ConversationHandler.END
    
    data["groups"][group_id] = {
        "group_name": group_name,
        "admin_user_id": user_id,
        "settings": {
            "delete_join_leave": True,
            "delete_links": False,
            "delete_promotions": False
        },
        "filtered_words": ["scam", "fuck"],
        "banned_users": []
    }
    
    if user_id not in data["user_groups"]:
        data["user_groups"][user_id] = []
    
    if group_id not in data["user_groups"][user_id]:
        data["user_groups"][user_id].append(group_id)
    
    save_data(data)
    
    await message.reply_text(
        f"✅ Successfully added '{group_name}'!\n\n"
        "You can now:\n"
        "• Use /settings to configure the bot\n"
        "• Use /mygroups to see all your groups\n"
        "• Use /filter to add filtered words\n\n"
        "Make sure I have admin rights in the group!"
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
                data = load_data()
                group_id = str(update.effective_chat.id)
                group_name = update.effective_chat.title
                admin_id = str(update.message.from_user.id)
                
                data["groups"][group_id] = {
                    "group_name": group_name,
                    "admin_user_id": admin_id,
                    "settings": {
                        "delete_join_leave": True,
                        "delete_links": False,
                        "delete_promotions": False
                    },
                    "filtered_words": ["scam", "fuck"],
                    "banned_users": []
                }
                
                if admin_id not in data["user_groups"]:
                    data["user_groups"][admin_id] = []
                
                if group_id not in data["user_groups"][admin_id]:
                    data["user_groups"][admin_id].append(group_id)
                
                save_data(data)
                
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"✅ I've been added to '{group_name}'!\nUse /settings to configure me."
                    )
                except:
                    pass

# My groups command
async def mygroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.message.from_user.id)
    
    if user_id not in data["user_groups"] or not data["user_groups"][user_id]:
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
    for group_id in data["user_groups"][user_id]:
        if group_id in data["groups"]:
            text += f"• {data['groups'][group_id]['group_name']}\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Another Group", callback_data="add_group")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Settings command
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.message.from_user.id)
    
    if user_id not in data["user_groups"] or not data["user_groups"][user_id]:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
                        "No groups found. Add me to a group first!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = data["user_groups"][user_id][0]
    group = data["groups"][group_id]
    
    keyboard = [
        [InlineKeyboardButton(
            f"🗑️ Join/Leave: {'✅' if group['settings']['delete_join_leave'] else '❌'}",
            callback_data=f"toggle_join_leave_{group_id}"
        )],
        [InlineKeyboardButton(
            f"🔗 Delete Links: {'✅' if group['settings']['delete_links'] else '❌'}",
            callback_data=f"toggle_links_{group_id}"
        )],
        [InlineKeyboardButton(
            f"📢 Delete Promotions: {'✅' if group['settings']['delete_promotions'] else '❌'}",
            callback_data=f"toggle_promotions_{group_id}"
        )]
    ]
    
    filtered = ", ".join(group['filtered_words']) if group['filtered_words'] else "None"
    text = f"⚙️ Settings for '{group['group_name']}'\n\n"
    text += f"Filtered words: {filtered}\n\n"
    text += "Use /filter <word> to add\nUse /unfilter <word> to remove\nUse /listfilters to view all"
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Toggle settings callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = load_data()
    callback_data = query.data
    
    if callback_data.startswith("toggle_"):
        parts = callback_data.split("_")
        setting = "_".join(parts[1:-1])
        group_id = parts[-1]
        
        if group_id in data["groups"]:
            if setting == "join_leave":
                data["groups"][group_id]["settings"]["delete_join_leave"] = not data["groups"][group_id]["settings"]["delete_join_leave"]
            elif setting == "links":
                data["groups"][group_id]["settings"]["delete_links"] = not data["groups"][group_id]["settings"]["delete_links"]
            elif setting == "promotions":
                data["groups"][group_id]["settings"]["delete_promotions"] = not data["groups"][group_id]["settings"]["delete_promotions"]
            
            save_data(data)
            
            group = data["groups"][group_id]
            keyboard = [
                [InlineKeyboardButton(
                    f"🗑️ Join/Leave: {'✅' if group['settings']['delete_join_leave'] else '❌'}",
                    callback_data=f"toggle_join_leave_{group_id}"
                )],
                [InlineKeyboardButton(
                    f"🔗 Delete Links: {'✅' if group['settings']['delete_links'] else '❌'}",
                    callback_data=f"toggle_links_{group_id}"
                )],
                [InlineKeyboardButton(
                    f"📢 Delete Promotions: {'✅' if group['settings']['delete_promotions'] else '❌'}",
                    callback_data=f"toggle_promotions_{group_id}"
                )]
            ]
            
            filtered = ", ".join(group['filtered_words']) if group['filtered_words'] else "None"
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
    data = load_data()
    user_id = str(update.message.from_user.id)
    
    if user_id not in data["user_groups"] or not data["user_groups"][user_id]:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = data["user_groups"][user_id][0]
    
    if word not in data["groups"][group_id]["filtered_words"]:
        data["groups"][group_id]["filtered_words"].append(word)
        save_data(data)
        await update.message.reply_text(f"✅ Added '{word}' to filtered words")
    else:
        await update.message.reply_text(f"'{word}' is already filtered")

# Unfilter command
async def unfilter_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unfilter <word>")
        return
    
    word = context.args[0].lower()
    data = load_data()
    user_id = str(update.message.from_user.id)
    
    if user_id not in data["user_groups"] or not data["user_groups"][user_id]:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = data["user_groups"][user_id][0]
    
    if word in data["groups"][group_id]["filtered_words"]:
        data["groups"][group_id]["filtered_words"].remove(word)
        save_data(data)
        await update.message.reply_text(f"✅ Removed '{word}' from filtered words")
    else:
        await update.message.reply_text(f"'{word}' is not in filtered words")

# List filters command
async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = str(update.message.from_user.id)
    
    if user_id not in data["user_groups"] or not data["user_groups"][user_id]:
        keyboard = [
            [InlineKeyboardButton("➕ Add Group", callback_data="add_group")]
        ]
        await update.message.reply_text(
            "No groups found!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    group_id = data["user_groups"][user_id][0]
    filtered = data["groups"][group_id]["filtered_words"]
    
    if filtered:
        text = "📝 Filtered words:\n" + "\n".join([f"• {word}" for word in filtered])
    else:
        text = "No filtered words yet"
    
    await update.message.reply_text(text)

# Ban command
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups!")
        return
    
    data = load_data()
    group_id = str(update.effective_chat.id)
    
    if group_id not in data["groups"]:
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
        data["groups"][group_id]["banned_users"].append(user_to_ban)
        save_data(data)
        await update.message.reply_text("✅ User has been banned")
        await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception as e:
        await update.message.reply_text(f"Failed to ban user: {str(e)}")

# Handle all group messages
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return
    
    data = load_data()
    group_id = str(update.effective_chat.id)
    
    if group_id not in data["groups"]:
        return
    
    group = data["groups"][group_id]
    message = update.message
    
    try:
        # Delete join/leave messages
        if group["settings"]["delete_join_leave"]:
            if message.new_chat_members or message.left_chat_member:
                await context.bot.delete_message(update.effective_chat.id, message.message_id)
                return
        
        # Check filtered words
        if message.text:
            text_lower = message.text.lower()
            for word in group["filtered_words"]:
                if word in text_lower:
                    await context.bot.delete_message(update.effective_chat.id, message.message_id)
                    return
        
        # Delete links
        if group["settings"]["delete_links"]:
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
        if group["settings"]["delete_promotions"]:
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

# Initialize application on startup
@app.on_event("startup")
async def startup():
    await get_application()
    # Set webhook
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await application.bot.set_webhook(webhook_url)

@app.on_event("shutdown")
async def shutdown():
    if application:
        await application.stop()
        await application.shutdown()
