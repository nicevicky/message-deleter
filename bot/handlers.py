import os
import asyncio
from telegram import Update, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ChatMemberHandler
)
from telegram.constants import ChatMemberStatus
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global application instance
_application = None

# Simple in-memory storage
class SimpleStorage:
    def __init__(self):
        self.users = {}
        self.interactions = []
        self.filtered_words = {
            "bandwidth", "bandwith", "band width",
            "spam", "scam", "fake", 
            "join my channel", "free money",
            "click here", "bit.ly", "tinyurl"
        }
    
    def add_user(self, user_id, first_name, username=None):
        if user_id not in self.users:
            self.users[user_id] = {
                "id": user_id,
                "first_name": first_name,
                "username": username,
                "message_count": 0,
                "ai_interactions": 0
            }
        else:
            self.users[user_id].update({
                "first_name": first_name,
                "username": username
            })
    
    def increment_message_count(self, user_id):
        if user_id in self.users:
            self.users[user_id]["message_count"] += 1
    
    def get_top_users(self, limit=10):
        users = list(self.users.values())
        users.sort(key=lambda x: x.get("message_count", 0), reverse=True)
        return users[:limit]
    
    def contains_filtered_words(self, text):
        if not text:
            return False
        text_lower = text.lower()
        return any(word in text_lower for word in self.filtered_words)

# Initialize storage
storage = SimpleStorage()

# Parse environment variables safely
def parse_admin_ids():
    try:
        admin_str = os.getenv("ADMIN_IDS", "")
        if not admin_str:
            return []
        return [int(id.strip()) for id in admin_str.split(",") if id.strip().isdigit()]
    except Exception as e:
        logger.error(f"Error parsing ADMIN_IDS: {e}")
        return []

def parse_group_chat_id():
    try:
        chat_id_str = os.getenv("GROUP_CHAT_ID", "0")
        # Remove extra dashes if present
        chat_id_str = chat_id_str.replace("--", "-")
        return int(chat_id_str) if chat_id_str != "0" else None
    except Exception as e:
        logger.error(f"Error parsing GROUP_CHAT_ID: {e}")
        return None

ADMIN_IDS = parse_admin_ids()
GROUP_CHAT_ID = parse_group_chat_id()

logger.info(f"Loaded ADMIN_IDS: {ADMIN_IDS}")
logger.info(f"Loaded GROUP_CHAT_ID: {GROUP_CHAT_ID}")

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
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
            await update.message.reply_text(welcome_text.strip())
        else:
            await update.message.reply_text("👋 Social Bounty Support Bot is active!")
    except Exception as e:
        logger.error(f"Start command error: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    try:
        if not is_admin(update.effective_user.id):
            return
        
        help_text = """
🔧 **Admin Commands:**

/start - Bot introduction
/help - Show this help message
/topusers - Show top users
/stats - Show group statistics

**Auto Features:**
• Delete join/leave messages
• Welcome new members
• Filter spam words
• AI responses to questions
• Admin-only private messaging
        """
        await update.message.reply_text(help_text.strip())
    except Exception as e:
        logger.error(f"Help command error: {e}")

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new member joins"""
    try:
        message = update.message
        
        if message.new_chat_members:
            # Delete the join message
            try:
                await message.delete()
            except Exception as e:
                logger.error(f"Failed to delete join message: {e}")
            
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

Questions? Just ask in the group!
                    """
                    
                    try:
                        welcome_msg = await context.bot.send_message(
                            chat_id=message.chat_id,
                            text=welcome_text.strip()
                        )
                        
                        # Delete welcome message after 60 seconds
                        asyncio.create_task(delete_message_later(context.bot, welcome_msg, 60))
                        
                        # Store user in storage
                        storage.add_user(new_member.id, new_member.first_name, new_member.username)
                        
                    except Exception as e:
                        logger.error(f"Failed to send welcome message: {e}")
    except Exception as e:
        logger.error(f"Handle new member error: {e}")

async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle member leaving"""
    try:
        message = update.message
        
        if message.left_chat_member:
            # Delete the leave message
            try:
                await message.delete()
            except Exception as e:
                logger.error(f"Failed to delete leave message: {e}")
    except Exception as e:
        logger.error(f"Handle member left error: {e}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all text messages"""
    try:
        message = update.message
        chat = update.effective_chat
        user = update.effective_user
        text = message.text or ""
        
        # Store user data
        storage.add_user(user.id, user.first_name, user.username)
        storage.increment_message_count(user.id)
        
        # Only respond in private chat to admins
        if chat.type == "private" and not is_admin(user.id):
            await message.reply_text("🚫 Sorry, only admins can message me privately.")
            return
        
        # Check if message contains filtered words
        if storage.contains_filtered_words(text):
            try:
                await message.delete()
                return
            except Exception as e:
                logger.error(f"Failed to delete filtered message: {e}")
        
        # Check if it's a question or mention
        is_question = any(word in text.lower() for word in [
            '?', 'what', 'how', 'why', 'when', 'where', 'who',
            'help', 'support', 'problem', 'issue', 'question'
        ])
        
        bot_username = context.bot.username.lower() if context.bot.username else "bot"
        is_mention = f"@{bot_username}" in text.lower() or (
            message.reply_to_message and 
            message.reply_to_message.from_user.id == context.bot.id
        )
        
        if is_question or is_mention or chat.type == "private":
            # Get simple response
            try:
                response = get_simple_response(text, user.first_name)
                await message.reply_text(response)
                
            except Exception as e:
                logger.error(f"Response error: {e}")
                await message.reply_text("🤖 Sorry, I'm having trouble processing your request right now.")
                
    except Exception as e:
        logger.error(f"Handle messages error: {e}")

def get_simple_response(text: str, user_name: str) -> str:
    """Get simple response based on keywords"""
    text_lower = text.lower()
    
    if any(word in text_lower for word in ['hello', 'hi', 'hey']):
        return f"Hello {user_name}! 👋 How can I help you with Social Bounty?"
    
    elif any(word in text_lower for word in ['social bounty', 'platform', 'what is']):
        return """
🚀 **About Social Bounty:**

Social Bounty is a task reward platform where you can:
• Complete social media tasks (likes, follows, downloads)
• Create custom tasks for others
• Earn rewards for completed tasks
• Advertise authentically without fake followers

Join us and start earning today!
        """.strip()
    
    elif any(word in text_lower for word in ['how', 'start', 'begin']):
        return """
📝 **Getting Started:**

1. Sign up on Social Bounty platform
2. Browse available tasks
3. Complete tasks to earn rewards
4. Create your own tasks for promotion
5. Withdraw your earnings

Need more help? Ask in the group!
        """.strip()
    
    elif any(word in text_lower for word in ['task', 'earn', 'money', 'reward']):
        return """
💰 **About Tasks & Rewards:**

Available task types:
• Social media engagement (likes, follows)
• App downloads and reviews
• Website visits and signups
• Content sharing and promotion

Earn rewards for each completed task and withdraw when you reach minimum threshold!
        """.strip()
    
    else:
        return f"Thanks for your message, {user_name}! 🤖 I'm here to help with Social Bounty questions. Feel free to ask about our platform, tasks, or rewards!"

async def top_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top users"""
    try:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("🚫 Only admins can use this command.")
            return
        
        top_users = storage.get_top_users(limit=10)
        
        if not top_users:
            await update.message.reply_text("📊 No user data available yet.")
            return
        
        response = "🏆 **Top Active Users:**\n\n"
        for i, user in enumerate(top_users, 1):
            name = user.get("first_name", "Unknown")
            count = user.get("message_count", 0)
            response += f"{i}. {name}: {count} messages\n"
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Top users command error: {e}")
        await update.message.reply_text("❌ Failed to get top users.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    try:
        if not is_admin(update.effective_user.id):
            return
        
        total_users = len(storage.users)
        total_messages = sum(user.get("message_count", 0) for user in storage.users.values())
        filtered_words_count = len(storage.filtered_words)
        
        stats_text = f"""
📊 **Bot Statistics**

👥 Total Users: {total_users}
💬 Total Messages: {total_messages}
🚫 Filtered Words: {filtered_words_count}
🤖 Bot Status: Active
        """
        
        await update.message.reply_text(stats_text.strip())
        
    except Exception as e:
        logger.error(f"Stats command error: {e}")
        await update.message.reply_text("❌ Failed to get statistics.")

async def delete_message_later(bot, message, delay):
    """Delete a message after specified delay"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except Exception as e:
        logger.error(f"Failed to delete message: {e}")

def setup_handlers(application: Application):
    """Setup all bot handlers"""
    try:
        # Command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("topusers", top_users_command))
        application.add_handler(CommandHandler("stats", stats_command))
        
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
            handle_messages
        ))
        
        logger.info("Handlers setup completed successfully")
        
    except Exception as e:
        logger.error(f"Setup handlers error: {e}")
        raise

async def get_application():
    """Get or create the application instance"""
    global _application
    
    if _application is None:
        try:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            if not bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
            
            # Create application
            _application = Application.builder().token(bot_token).build()
            
            # Setup handlers
            setup_handlers(_application)
            
            # Initialize the application
            await _application.initialize()
            
            logger.info("Application initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize application: {e}")
            raise
    
    return _application

async def process_update(update_data):
    """Process a single update"""
    try:
        # Get the application instance
        application = await get_application()
        
        # Create Update object from the data
        update = Update.de_json(update_data, application.bot)
        
        if update:
            # Process the update
            await application.process_update(update)
            logger.info(f"Processed update: {update.update_id}")
        else:
            logger.warning("Failed to create Update object from data")
            
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        raise

# For webhook handler (if using webhooks)
async def webhook_handler(request_data):
    """Handle webhook requests"""
    try:
        await process_update(request_data)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook handler error: {e}")
        return {"status": "error", "message": str(e)}

# For polling (if running locally)
async def run_polling():
    """Run the bot with polling"""
    try:
        application = await get_application()
        
        # Start polling
        await application.start()
        await application.updater.start_polling()
        
        logger.info("Bot started with polling")
        
        # Keep running
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopping bot...")
        finally:
            await application.stop()
            
    except Exception as e:
        logger.error(f"Polling error: {e}")
        raise

# Cleanup function for serverless
async def cleanup():
    """Cleanup resources"""
    global _application
    if _application:
        try:
            await _application.shutdown()
            _application = None
            logger.info("Application cleaned up")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# Main entry point
if __name__ == "__main__":
    asyncio.run(run_polling())
