import os
import asyncio
from telegram import Update, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ChatMemberHandler
)
from telegram.constants import ChatMemberStatus
from bot.gemini_ai import GeminiAI
from bot.database import Database
from bot.filters import WordFilter
from utils.image_generator import TopUsersImageGenerator
from utils.helpers import is_admin, get_user_stats

# Initialize components
gemini_ai = GeminiAI()
db = Database()
word_filter = WordFilter()
image_gen = TopUsersImageGenerator()

# Admin IDs from environment
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    chat = update.effective_chat
    
    if chat.type == "private":
        welcome_text = """
🤖 **Social Bounty Support Bot**

Hello! I'm the official support bot for Social Bounty - your task reward platform.

**What I can help with:**
• Answer questions about Social Bounty
• Provide platform support
• Help with task-related queries

**About Social Bounty:**
Social Bounty is a task reward platform where users can:
- Perform social media tasks (likes, follows, downloads)
- Create custom tasks for others
- Earn rewards for completed tasks
- Advertise authentically without fake followers

For group management, add me to your group and make me an admin!
        """
        await update.message.reply_text(welcome_text, parse_mode="Markdown")
    else:
        # In group, just acknowledge
        await update.message.reply_text("👋 Social Bounty Support Bot is active!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        return
    
    help_text = """
🔧 **Admin Commands:**

/start - Bot introduction
/help - Show this help message
/topusers - Generate top users image
/stats - Show group statistics
/filter add <word> - Add word to filter
/filter remove <word> - Remove word from filter
/filter list - Show filtered words

**Auto Features:**
• Delete join/leave messages
• Welcome new members
• Filter spam words
• AI responses to questions
• Admin-only private messaging
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new member joins"""
    message = update.message
    
    if message.new_chat_members:
        # Delete the join message
        try:
            await message.delete()
        except Exception as e:
            print(f"Failed to delete join message: {e}")
        
        # Welcome new members
        for new_member in message.new_chat_members:
            if not new_member.is_bot:
                welcome_text = f"""
🎉 Welcome to Social Bounty, {new_member.first_name}!

**About Social Bounty:**
We're a task reward platform where you can:
• Complete social media tasks and earn rewards
• Create your own tasks for promotion
• Build authentic engagement
• Grow your social presence organically

Get started at: [Social Bounty Platform]
Questions? Just ask in the group!
                """
                
                try:
                    welcome_msg = await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=welcome_text,
                        parse_mode="Markdown"
                    )
                    
                    # Delete welcome message after 60 seconds
                    asyncio.create_task(delete_message_later(context.bot, welcome_msg, 60))
                    
                    # Store user in database
                    await db.add_user(new_member.id, new_member.first_name, new_member.username)
                    
                except Exception as e:
                    print(f"Failed to send welcome message: {e}")

async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle member leaving"""
    message = update.message
    
    if message.left_chat_member:
        # Delete the leave message
        try:
            await message.delete()
        except Exception as e:
            print(f"Failed to delete leave message: {e}")

async def handle_ai_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle AI responses to questions"""
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    
    # Only respond in group chat or to admins in private
    if chat.type == "private" and not is_admin(user.id, ADMIN_IDS):
        await message.reply_text("🚫 Sorry, only admins can message me privately.")
        return
    
    # Check if message contains filtered words
    if word_filter.contains_filtered_words(message.text):
        try:
            await message.delete()
            return
        except Exception as e:
            print(f"Failed to delete filtered message: {e}")
    
    # Check if it's a question or mention
    text = message.text.lower()
    bot_username = context.bot.username.lower()
    
    is_question = any(word in text for word in [
        '?', 'what', 'how', 'why', 'when', 'where', 'who',
        'help', 'support', 'problem', 'issue', 'question'
    ])
    
    is_mention = f"@{bot_username}" in text or message.reply_to_message
    
    if is_question or is_mention or chat.type == "private":
        # Get AI response
        try:
            response = await gemini_ai.get_response(message.text, user.first_name)
            await message.reply_text(response, parse_mode="Markdown")
            
            # Store interaction in database
            await db.log_interaction(user.id, message.text, response)
            
        except Exception as e:
            print(f"AI response error: {e}")
            await message.reply_text("🤖 Sorry, I'm having trouble processing your request right now.")

async def top_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send top users image"""
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        await update.message.reply_text("🚫 Only admins can use this command.")
        return
    
    try:
        # Get top users from database
        top_users = await db.get_top_users(limit=10)
        
        if not top_users:
            await update.message.reply_text("📊 No user data available yet.")
            return
        
        # Generate image
        image_path = await image_gen.create_top_users_image(top_users)
        
        # Send image
        with open(image_path, 'rb') as photo:
            await update.message.reply_photo(
                photo=photo,
                caption="🏆 **Top Active Users in Social Bounty Group**",
                parse_mode="Markdown"
            )
        
        # Clean up
        os.remove(image_path)
        
    except Exception as e:
        print(f"Top users command error: {e}")
        await update.message.reply_text("❌ Failed to generate top users image.")

async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle word filter commands"""
    if not is_admin(update.effective_user.id, ADMIN_IDS):
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /filter <add/remove/list> [word]")
        return
    
    action = context.args[0].lower()
    
    if action == "add" and len(context.args) > 1:
        word = " ".join(context.args[1:]).lower()
        word_filter.add_word(word)
        await update.message.reply_text(f"✅ Added '{word}' to filter list.")
        
    elif action == "remove" and len(context.args) > 1:
        word = " ".join(context.args[1:]).lower()
        word_filter.remove_word(word)
        await update.message.reply_text(f"✅ Removed '{word}' from filter list.")
        
    elif action == "list":
        words = word_filter.get_filtered_words()
        if words:
            word_list = "\n".join([f"• {word}" for word in words])
            await update.message.reply_text(f"🚫 **Filtered Words:**\n{word_list}")
        else:
            await update.message.reply_text("📝 No words in filter list.")
    else:
        await update.message.reply_text("Usage: /filter <add/remove/list> [word]")

async def delete_message_later(bot, message, delay):
    """Delete a message after specified delay"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Failed to delete message: {e}")

def setup_handlers(application: Application):
    """Setup all bot handlers"""
    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("topusers", top_users_command))
    application.add_handler(CommandHandler("filter", filter_command))
    
    # Message handlers
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, 
        handle_new_member
    ))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER, 
        handle_member_left
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, 
        handle_ai_questions
    ))
