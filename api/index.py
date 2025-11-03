import os
import asyncio
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
import httpx
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI()

# Telegram Bot Token from environment variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment variables")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Async HTTP client
client = httpx.AsyncClient(timeout=10.0)

class Update(BaseModel):
    update_id: int
    message: dict = None
    edited_message: dict = None
    my_chat_member: dict = None
    chat_member: dict = None
    chat_join_request: dict = None

async def send_message(chat_id: int, text: str):
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return None

async def delete_message(chat_id: int, message_id: int):
    url = f"{BASE_URL}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    try:
        await client.post(url, json=payload)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")

async def handle_new_member(message: dict):
    chat_id = message["chat"]["id"]
    new_members = message.get("new_chat_members", [])

    for member in new_members:
        user = member
        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        username = user.get("username", "")
        full_name = f"{first_name} {last_name}".strip()
        display_name = f"@{username}" if username else full_name

        # Welcome text
        welcome_text = f"Welcome <b>{display_name}</b>! 👋\nEnjoy your stay!"

        # Send welcome message
        sent = await send_message(chat_id, welcome_text)
        if sent and sent.get("ok"):
            welcome_message_id = sent["result"]["message_id"]

            # Delete the original "joined" system message (if bot has permission)
            original_message_id = message["message_id"]
            asyncio.create_task(delete_message(chat_id, original_message_id))

            # Schedule deletion of welcome message after 3 seconds
            asyncio.create_task(
                asyncio.sleep(3).then(lambda: delete_message(chat_id, welcome_message_id))
            )
        break  # Handle one at a time

@app.post("/")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        update = Update(**body)

        # Handle message with new chat members
        if update.message and "new_chat_members" in update.message:
            asyncio.create_task(handle_new_member(update.message))

        return Response(content='{"ok": true}', media_type="application/json")

    except Exception as e:
        logger.error(f"Error processing update: {e}")
        return Response(status_code=500)

# Health check
@app.get("/")
async def root():
    return {"message": "Telegram Join Deleter Bot is running!"}

# Vercel entry point
handler = app
