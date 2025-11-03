from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from mangum import Mangum
import os
import asyncio
import httpx
from typing import Optional

app = FastAPI()

# Get bot token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def delete_message_after_delay(chat_id: int, message_id: int, delay: int = 3):
    """Delete a message after specified delay (in seconds)"""
    await asyncio.sleep(delay)
    
    async with httpx.AsyncClient() as client:
        delete_url = f"{TELEGRAM_API_URL}/deleteMessage"
        params = {
            "chat_id": chat_id,
            "message_id": message_id
        }
        try:
            response = await client.post(delete_url, json=params)
            if response.status_code == 200:
                print(f"Message {message_id} deleted successfully")
            else:
                print(f"Failed to delete message: {response.text}")
        except Exception as e:
            print(f"Error deleting message: {str(e)}")


async def send_welcome_message(chat_id: int, username: str, first_name: str):
    """Send welcome message and schedule deletion"""
    async with httpx.AsyncClient() as client:
        # Create welcome message
        display_name = f"@{username}" if username else first_name
        welcome_text = f"Welcome {display_name}! 👋"
        
        send_url = f"{TELEGRAM_API_URL}/sendMessage"
        params = {
            "chat_id": chat_id,
            "text": welcome_text,
            "parse_mode": "HTML"
        }
        
        try:
            response = await client.post(send_url, json=params)
            if response.status_code == 200:
                result = response.json()
                message_id = result["result"]["message_id"]
                print(f"Welcome message sent: {message_id}")
                
                # Schedule deletion after 3 seconds
                asyncio.create_task(delete_message_after_delay(chat_id, message_id, 3))
                return message_id
            else:
                print(f"Failed to send message: {response.text}")
        except Exception as e:
            print(f"Error sending message: {str(e)}")
    
    return None


async def handle_new_member(chat_id: int, user: dict, join_message_id: int):
    """Handle new member joining the group"""
    username = user.get("username", "")
    first_name = user.get("first_name", "User")
    
    # Delete the system join message
    asyncio.create_task(delete_message_after_delay(chat_id, join_message_id, 3))
    
    # Send and schedule welcome message deletion
    await send_welcome_message(chat_id, username, first_name)


async def handle_left_member(chat_id: int, left_message_id: int):
    """Handle member leaving the group"""
    # Delete the system left message
    asyncio.create_task(delete_message_after_delay(chat_id, left_message_id, 3))


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "status": "Bot is running",
        "bot": "Telegram Join/Leave Message Handler",
        "version": "1.0.0"
    }


@app.get("/api")
async def api_root():
    """API root endpoint"""
    return {
        "status": "Bot is running",
        "bot": "Telegram Join/Leave Message Handler",
        "version": "1.0.0"
    }


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle incoming webhook updates from Telegram"""
    
    if not BOT_TOKEN:
        return JSONResponse(
            content={"error": "BOT_TOKEN not configured"},
            status_code=500
        )
    
    try:
        data = await request.json()
        print(f"Received update: {data}")
        
        if "message" in data:
            message = data["message"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            
            # Handle new chat members
            if "new_chat_members" in message:
                for user in message["new_chat_members"]:
                    # Don't welcome bots
                    if not user.get("is_bot", False):
                        background_tasks.add_task(
                            handle_new_member,
                            chat_id,
                            user,
                            message_id
                        )
            
            # Handle left chat member
            elif "left_chat_member" in message:
                background_tasks.add_task(
                    handle_left_member,
                    chat_id,
                    message_id
                )
        
        return {"status": "ok"}
    
    except Exception as e:
        print(f"Error processing update: {str(e)}")
        return JSONResponse(
            content={"error": str(e)},
            status_code=500
        )


@app.get("/set-webhook")
async def set_webhook(request: Request):
    """Set the webhook URL for the bot"""
    
    if not BOT_TOKEN:
        return {"error": "BOT_TOKEN not configured"}
    
    # Get the base URL from the request
    base_url = str(request.base_url).rstrip('/')
    webhook_url = f"{base_url}/webhook"
    
    async with httpx.AsyncClient() as client:
        set_webhook_url = f"{TELEGRAM_API_URL}/setWebhook"
        params = {
            "url": webhook_url,
            "drop_pending_updates": True
        }
        
        try:
            response = await client.post(set_webhook_url, json=params)
            result = response.json()
            return result
        except Exception as e:
            return {"error": str(e)}


@app.get("/webhook-info")
async def webhook_info():
    """Get current webhook information"""
    
    if not BOT_TOKEN:
        return {"error": "BOT_TOKEN not configured"}
    
    async with httpx.AsyncClient() as client:
        info_url = f"{TELEGRAM_API_URL}/getWebhookInfo"
        
        try:
            response = await client.get(info_url)
            result = response.json()
            return result
        except Exception as e:
            return {"error": str(e)}


# Mangum handler for Vercel
handler = Mangum(app)
