import logging
import google.generativeai as genai
from telegram import Update, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction, ParseMode
import asyncio
from config import *
from utils import UserStats, create_top_users_image, should_delete_message, is_question_about_social_bounty

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Initialize user stats
user_stats = UserStats()

class SocialBountyBot:
    def __init__(self):
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.setup_handlers()
    
    def setup_handlers(self):
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("topusers", self.top_users_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        
        # Message handlers
        self.application.add_handler(MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_new_member
        ))
        self.application.add_handler(MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER, self.goodbye_member
        ))
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_message
        ))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        chat = update.effective_chat
        
        if chat.type == "private":
            welcome_text = f"""
🤖 **Social Bounty Support Bot**

Hello {user.first_name}! I'm here to help you with Social Bounty platform.

**What is Social Bounty?**
• A task reward platform for social media tasks
• Earn rewards by liking posts, following pages, downloading apps
• Create your own tasks for others to complete
• Better alternative to buying fake followers

**Commands:**
/help - Show this help message
/topusers - Show top active users (Admin only)
/stats - Show your activity stats

**Note:** Only admins can use me in the group. You can message me privately for support!
            """
        else:
            welcome_text = "👋 Social Bounty Support Bot is active! Only admins can interact with me in groups."
        
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await self.start_command(update, context)
    
    async def top_users_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show top users (Admin only)"""
        user_id = update.effective_user.id
        chat = update.effective_chat
        
        # Check if user is admin
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("❌ Only admins can use this command.")
            return
        
        try:
            top_users = user_stats.get_top_users(5)
            
            if not top_users:
                await update.message.reply_text("📊 No user activity data available yet.")
                return
            
            # Create and send image
            img_bytes = create_top_users_image(top_users)
            
            await context.bot.send_photo(
                chat_id=chat.id,
                photo=img_bytes,
                caption="🏆 **Top Active Users in Social Bounty Group**\n\nThese users are most active and helpful in our community!",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error in top_users_command: {e}")
            await update.message.reply_text("❌ Error generating top users report.")
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's personal stats"""
        user = update.effective_user
        user_id = str(user.id)
        
        if user_id in user_stats.stats:
            stats = user_stats.stats[user_id]
            stats_text = f"""
📊 **Your Activity Stats**

👤 **User:** @{user.username or user.first_name}
💬 **Messages:** {stats['messages']}
❓ **Questions:** {stats['questions']}
🎯 **Helpful Responses:** {stats['helpful_responses']}
⭐ **Total Score:** {stats['messages'] + stats['questions']*2 + stats['helpful_responses']*3}

Keep being active in our Social Bounty community! 🚀
            """
        else:
            stats_text = "📊 No activity data found. Start participating in the group to build your stats!"
        
        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
    
    async def welcome_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Welcome new members and delete join message"""
        try:
            # Delete the join message
            await update.message.delete()
            
            new_members = update.message.new_chat_members
            for member in new_members:
                if not member.is_bot:
                    welcome_text = f"""
🎉 **Welcome to Social Bounty Group, {member.first_name}!**

We're excited to have you join our community! 

**What is Social Bounty?**
🎯 Complete social media tasks and earn rewards
📱 Like posts, follow pages, download apps
💰 Create your own tasks for others
🚀 Grow your social media organically

**Getting Started:**
1. Visit our platform and create an account
2. Browse available tasks
3. Complete tasks and earn rewards
4. Create your own tasks to promote your content

Feel free to ask any questions. Our support bot is here to help!

**Rules:** Be respectful, no spam, and enjoy earning! 💪
                    """
                    
                    # Send welcome message
                    welcome_msg = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=welcome_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    
                    # Delete welcome message after 5 minutes
                    asyncio.create_task(self.delete_message_later(context, update.effective_chat.id, welcome_msg.message_id, 300))
        
        except Exception as e:
            logger.error(f"Error in welcome_new_member: {e}")
    
    async def goodbye_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle member leaving and delete leave message"""
        try:
            await update.message.delete()
        except Exception as e:
            logger.error(f"Error deleting goodbye message: {e}")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all text messages"""
        user = update.effective_user
        chat = update.effective_chat
        message = update.message
        text = message.text
        
        # Update user stats
        user_stats.update_user_activity(str(user.id), user.username or user.first_name, "message")
        
        # Check if message should be deleted
        if should_delete_message(text):
            try:
                await message.delete()
                return
            except Exception as e:
                logger.error(f"Error deleting filtered message: {e}")
        
        # Handle private chat
        if chat.type == "private":
            await self.handle_private_message(update, context)
            return
        
        # Handle group chat
        if chat.type in ["group", "supergroup"]:
            await self.handle_group_message(update, context)
    
    async def handle_private_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle private messages"""
        user = update.effective_user
        text = update.message.text
        
        # Check if user is admin for special commands
        if user.id in ADMIN_IDS and text.startswith("/filter"):
            # Admin can add words to filter
            await update.message.reply_text("🔧 Admin commands in development...")
            return
        
        # Regular AI response for private messages
        await self.generate_ai_response(update, context, text)
    
    async def handle_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle group messages"""
        user = update.effective_user
        text = update.message.text
        
        # Only respond to admins in group or if bot is mentioned
        bot_username = context.bot.username
        is_mentioned = f"@{bot_username}" in text if bot_username else False
        is_reply_to_bot = (update.message.reply_to_message and 
                          update.message.reply_to_message.from_user.id == context.bot.id)
        
        should_respond = (user.id in ADMIN_IDS or is_mentioned or is_reply_to_bot)
        
        if should_respond and is_question_about_social_bounty(text):
            # Update question stats
            user_stats.update_user_activity(str(user.id), user.username or user.first_name, "question")
            await self.generate_ai_response(update, context, text)
    
    async def generate_ai_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Generate AI response using Gemini"""
        try:
            # Show typing action
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
            
            # Prepare prompt
            full_prompt = f"{BOT_PROMPT}\n\nUser question: {text}"
            
            # Generate response
            response = model.generate_content(full_prompt)
            ai_response = response.text
            
            # Send response
            await update.message.reply_text(
                ai_response,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id
            )
            
            # Update helpful response stats for the bot user (conceptually)
            user = update.effective_user
            user_stats.update_user_activity(str(user.id), user.username or user.first_name, "helpful")
            
        except Exception as e:
            logger.error(f"Error generating AI response: {e}")
            await update.message.reply_text(
                "❌ Sorry, I'm having trouble processing your request right now. Please try again later."
            )
    
    async def delete_message_later(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
        """Delete a message after specified delay"""
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"Error deleting message later: {e}")

# Global bot instance
bot_instance = SocialBountyBot()

async def setup_webhook():
    """Setup webhook for the bot"""
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await bot_instance.application.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")

async def process_update(update_data: dict):
    """Process incoming update"""
    try:
        update = Update.de_json(update_data, bot_instance.application.bot)
        await bot_instance.application.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")
