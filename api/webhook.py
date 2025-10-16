from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os
import json
from dotenv import load_dotenv
import asyncio
from telegram import Update
from telegram.ext import Application
from bot.handlers import setup_handlers
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        
        logger.info(f"Received update: {update_data}")
        
        # Create Update object
        update = Update.de_json(update_data, application.bot)
        
        if update:
            # Process the update
            await application.process_update(update)
        
        return JSONResponse(content={"status": "ok"})
    
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Always return 200 to Telegram to avoid retries
        return JSONResponse(
            status_code=200,
            content={"status": "error", "message": str(e)}
        )

@app.get("/set-webhook")
async def set_webhook():
    try:
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="WEBHOOK_URL not set")
        
        # Ensure webhook URL ends with /webhook
        if not webhook_url.endswith('/webhook'):
            webhook_url = webhook_url.rstrip('/') + '/webhook'
        
        result = await application.bot.set_webhook(url=webhook_url)
        
        if result:
            return {"message": f"Webhook set successfully to {webhook_url}"}
        else:
            return {"message": "Failed to set webhook"}
            
    except Exception as e:
        logger.error(f"Set webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "bot_token_set": bool(bot_token),
        "webhook_url": os.getenv("WEBHOOK_URL")
    }

# For local development
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
