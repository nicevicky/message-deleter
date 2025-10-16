from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
import logging
from bot import bot_instance, setup_webhook, process_update
from config import TELEGRAM_BOT_TOKEN

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Social Bounty Telegram Bot", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    """Initialize bot on startup"""
    try:
        # Initialize the bot application
        await bot_instance.application.initialize()
        await bot_instance.application.start()
        
        # Setup webhook
        await setup_webhook()
        
        logger.info("Bot started successfully!")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    try:
        await bot_instance.application.stop()
        await bot_instance.application.shutdown()
        logger.info("Bot stopped successfully!")
    except Exception as e:
        logger.error(f"Error stopping bot: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "active",
        "message": "Social Bounty Telegram Bot is running!",
        "version": "1.0.0"
    }

@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming webhook updates from Telegram"""
    try:
        # Get the update data
        update_data = await request.json()
        
        # Process the update
        await process_update(update_data)
        
        return JSONResponse({"status": "ok"})
    
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/health")
async def health_check():
    """Detailed health check"""
    try:
        # Check if bot token is configured
        if not TELEGRAM_BOT_TOKEN:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "Bot token not configured"}
            )
        
        return {
            "status": "healthy",
            "bot": "active",
            "webhook": "configured"
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@app.get("/bot-info")
async def bot_info():
    """Get bot information"""
    try:
        bot = bot_instance.application.bot
        bot_data = await bot.get_me()
        
        return {
            "bot_id": bot_data.id,
            "bot_username": bot_data.username,
            "bot_name": bot_data.first_name,
            "status": "active"
        }
    except Exception as e:
        logger.error(f"Error getting bot info: {e}")
        raise HTTPException(status_code=500, detail="Could not get bot info")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
