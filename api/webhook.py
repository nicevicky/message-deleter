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

# Global application instance
_application = None
_initialized = False

async def get_application():
    """Get or create the initialized application instance"""
    global _application, _initialized
    
    if _application is None or not _initialized:
        try:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            if not bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")
            
            # Create application
            _application = Application.builder().token(bot_token).build()
            
            # Setup handlers
            setup_handlers(_application)
            
            # Initialize the application
            await _application.initialize()
            _initialized = True
            
            logger.info("Application initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize application: {e}")
            raise
    
    return _application

@app.on_event("startup")
async def startup_event():
    """Initialize the bot application on startup"""
    try:
        await get_application()
        logger.info("Bot application initialized on startup")
    except Exception as e:
        logger.error(f"Startup initialization failed: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    global _application, _initialized
    if _application and _initialized:
        try:
            await _application.shutdown()
            _application = None
            _initialized = False
            logger.info("Application shutdown completed")
        except Exception as e:
            logger.error(f"Shutdown error: {e}")

@app.get("/")
async def root():
    return {"message": "Social Bounty Telegram Bot is running!"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        update_data = json.loads(body.decode('utf-8'))
        
        logger.info(f"Received update: {update_data}")
        
        # Get the initialized application
        application = await get_application()
        
        # Create Update object
        update = Update.de_json(update_data, application.bot)
        
        if update:
            # Process the update
            await application.process_update(update)
            logger.info(f"Successfully processed update: {update.update_id}")
        else:
            logger.warning("Failed to create Update object from data")
        
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
        
        # Get the initialized application
        application = await get_application()
        
        result = await application.bot.set_webhook(url=webhook_url)
        
        if result:
            logger.info(f"Webhook set successfully to {webhook_url}")
            return {"message": f"Webhook set successfully to {webhook_url}"}
        else:
            logger.error("Failed to set webhook")
            return {"message": "Failed to set webhook"}
            
    except Exception as e:
        logger.error(f"Set webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/webhook-info")
async def get_webhook_info():
    """Get current webhook information"""
    try:
        application = await get_application()
        webhook_info = await application.bot.get_webhook_info()
        
        return {
            "url": webhook_info.url,
            "has_custom_certificate": webhook_info.has_custom_certificate,
            "pending_update_count": webhook_info.pending_update_count,
            "last_error_date": webhook_info.last_error_date,
            "last_error_message": webhook_info.last_error_message,
            "max_connections": webhook_info.max_connections,
            "allowed_updates": webhook_info.allowed_updates
        }
    except Exception as e:
        logger.error(f"Get webhook info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/webhook")
async def delete_webhook():
    """Delete the current webhook"""
    try:
        application = await get_application()
        result = await application.bot.delete_webhook()
        
        if result:
            logger.info("Webhook deleted successfully")
            return {"message": "Webhook deleted successfully"}
        else:
            logger.error("Failed to delete webhook")
            return {"message": "Failed to delete webhook"}
            
    except Exception as e:
        logger.error(f"Delete webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    """Health check endpoint"""
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        webhook_url = os.getenv("WEBHOOK_URL")
        
        # Check if application is initialized
        app_status = "initialized" if _initialized else "not_initialized"
        
        # Try to get bot info if initialized
        bot_info = None
        if _initialized and _application:
            try:
                bot_info = await _application.bot.get_me()
                bot_info = {
                    "id": bot_info.id,
                    "username": bot_info.username,
                    "first_name": bot_info.first_name
                }
            except Exception as e:
                logger.error(f"Failed to get bot info: {e}")
        
        return {
            "status": "healthy",
            "application_status": app_status,
            "bot_token_set": bool(bot_token),
            "webhook_url": webhook_url,
            "bot_info": bot_info
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "unhealthy",
            "error": str(e)
        }

@app.get("/test-bot")
async def test_bot():
    """Test bot connectivity"""
    try:
        application = await get_application()
        bot_info = await application.bot.get_me()
        
        return {
            "status": "success",
            "bot_info": {
                "id": bot_info.id,
                "username": bot_info.username,
                "first_name": bot_info.first_name,
                "is_bot": bot_info.is_bot
            }
        }
    except Exception as e:
        logger.error(f"Bot test error: {e}")
        raise HTTPException(status_code=500, detail=f"Bot test failed: {str(e)}")

# For local development with polling
@app.get("/start-polling")
async def start_polling():
    """Start polling for local development (use with caution)"""
    try:
        application = await get_application()
        
        # Delete webhook first
        await application.bot.delete_webhook()
        
        # This is for development only
        if os.getenv("ENVIRONMENT") == "development":
            # Start polling in background
            asyncio.create_task(run_polling_background(application))
            return {"message": "Polling started (development mode only)"}
        else:
            return {"message": "Polling not available in production mode"}
            
    except Exception as e:
        logger.error(f"Start polling error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def run_polling_background(application):
    """Run polling in background"""
    try:
        await application.start()
        await application.updater.start_polling()
        logger.info("Background polling started")
    except Exception as e:
        logger.error(f"Background polling error: {e}")

# For local development
if __name__ == "__main__":
    import uvicorn
    
    # Set development environment
    os.environ["ENVIRONMENT"] = "development"
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
