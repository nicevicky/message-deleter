import os
import asyncio
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
import httpx
import logging

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Bot Token
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in env vars")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
client = httpx.AsyncClient(timeout=10.0)

class Update(BaseModel):
    update_id: int
    message: dict = None
    edited_message: dict = None

async def send_message(chat_id: int, text: str):
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return None

async def delete_message(chat_id: int, message_id: int):
    url = f"{BASE_URL}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        await client.post(url, json=payload)
    except Exception as e:
        logger.error(f"Delete failed: {e}")

async def delayed_delete(chat_id: int, message_id: int, delay: int = 3):
    await asyncio.sleep(delay)
    await delete_message(chat_id, message_id)

async def handle_join(message: dict):
    chat_id = message["chat"]["id"]
    join_msg_id = message["message_id"]
    new_members = message.get("new_chat_members", [])

    for member in new_members:
        first = member.get("first_name", "")
        last = member.get("last_name", "")
        username = member.get("username")
        name = f"@{username}" if username else f"{first} {last}".strip()

        welcome_text = f"Welcome <b>{name}</b>! Enjoy your stay!"

        # Send welcome
        sent = await send_message(chat_id, welcome_text)
        if sent and sent.get("ok"):
            welcome_id = sent["result"]["message_id"]

            # Delete join message
            asyncio.create_task(delete_message(chat_id, join_msg_id))

            # Delete welcome after 3s
            asyncio.create_task(delayed_delete(chat_id, welcome_id, 3))

        break  # Only one welcome per update

@app.post("/")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update(**data)

        if update.message and "new_chat_members" in update.message:
            asyncio.create_task(handle_join(update.message))

        return {"ok": True}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
async def health():
    return {"status": "running", "bot": "join-deleter"}

# Vercel handler
handler = app
