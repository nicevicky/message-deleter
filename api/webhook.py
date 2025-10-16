from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os
import json
from dotenv import load_dotenv
import asyncio
from telegram import Update
from telegram.ext import Application
from bot.handlers import setup_handlers

load_dotenv()

app = FastAPI()

# Initialize bot application
bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
if not bot_token:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")

application = Application.builder().token(bot_token).build()
setup_handlers(application)

@app.get("/")
async def root():
    return {"message": "Social Bounty Telegram Bot is running!"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        update_data = json.loads(body.decode('utf-8'))
        
        # Create Update object
        update = Update.de_json(update_data, application.bot)
        
        # Process the update
        await application.process_update(update)
        
        return JSONResponse(content={"status": "ok"})
    
    except Exception as e:
        print(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/set-webhook")
async def set_webhook():
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_URL not set")
    
    try:
        await application.bot.set_webhook(url=webhook_url)
        return {"message": f"Webhook set to {webhook_url}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# For local development
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
