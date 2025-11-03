from fastapi import FastAPI, Request, Response
from telegram import Update, Bot
from telegram.constants import ParseMode
import asyncio
import os
from contextlib import asynccontextmanager

# Store for tracking messages to delete
messages_to_delete = {}

# Bot instance
bot = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global bot
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    yield
    # Shutdown
    if bot:
        await bot.close()

app = FastAPI(lifespan=lifespan)


async def delete_message_after_delay(chat_id: int, message_id: int, delay: int = 3):
    """Delete a message after specified delay in seconds"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Error deleting message: {e}")


@app.get("/")
async def root():
    return {
        "status": "Bot is running",
        "description": "Telegram bot for managing join/leave messages"
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming webhook updates from Telegram"""
    try:
        # Parse the update
        json_data = await request.json()
        update = Update.de_json(json_data, bot)
        
        # Handle new chat members (someone joined)
        if update.message and update.message.new_chat_members:
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            
            for new_member in update.message.new_chat_members:
                # Check if it's not a bot joining
                if not new_member.is_bot:
                    username = new_member.username
                    first_name = new_member.first_name
                    
                    # Create welcome message
                    if username:
                        welcome_text = f"Welcome @{username}! 👋"
                    else:
                        welcome_text = f"Welcome {first_name}! 👋"
                    
                    # Send welcome message
                    sent_message = await bot.send_message(
                        chat_id=chat_id,
                        text=welcome_text,
                        parse_mode=ParseMode.HTML
                    )
                    
                    # Delete the join message
                    asyncio.create_task(delete_message_after_delay(chat_id, message_id, 3))
                    
                    # Delete the welcome message after 3 seconds
                    asyncio.create_task(delete_message_after_delay(chat_id, sent_message.message_id, 3))
        
        # Handle left chat member (someone left)
        elif update.message and update.message.left_chat_member:
            chat_id = update.message.chat_id
            message_id = update.message.message_id
            
            # Delete the leave message after 3 seconds
            asyncio.create_task(delete_message_after_delay(chat_id, message_id, 3))
        
        return Response(status_code=200)
    
    except Exception as e:
        print(f"Error processing update: {e}")
        return Response(status_code=200)


@app.get("/set-webhook")
async def set_webhook():
    """Set the webhook URL for your bot"""
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if not webhook_url:
        return {
            "error": "WEBHOOK_URL environment variable not set",
            "instruction": "Set WEBHOOK_URL to https://your-domain.vercel.app/webhook"
        }
    
    try:
        await bot.set_webhook(url=f"{webhook_url}/webhook")
        return {
            "status": "success",
            "message": "Webhook set successfully",
            "webhook_url": f"{webhook_url}/webhook"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


@app.get("/webhook-info")
async def webhook_info():
    """Get current webhook information"""
    try:
        webhook_info = await bot.get_webhook_info()
        return {
            "url": webhook_info.url,
            "pending_update_count": webhook_info.pending_update_count,
            "last_error_date": webhook_info.last_error_date,
            "last_error_message": webhook_info.last_error_message
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


@app.delete("/webhook")
async def delete_webhook():
    """Delete the webhook"""
    try:
        await bot.delete_webhook()
        return {
            "status": "success",
            "message": "Webhook deleted successfully"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
