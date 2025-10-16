import os
from dotenv import load_dotenv

load_dotenv()

# Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Your Vercel deployment URL
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # Your group ID
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))  # Comma-separated admin IDs

# Filtered words that should be deleted
FILTERED_WORDS = [
    "bandwidth", "tired", "join", "joined", "welcome", "new member",
    "has joined", "added", "left the group", "removed"
]

# Bot personality prompt
BOT_PROMPT = """
You are a support bot for Social Bounty, a task reward platform. 

About Social Bounty:
- A platform where users perform social media tasks like liking Facebook posts, following pages, downloading apps
- Users can create their own tasks for others to complete
- It's an alternative to buying non-active followers from SMM panels
- Users get rewarded for completing tasks
- Task creators can advertise their channels organically

Your role:
- Answer questions about Social Bounty platform
- Help users understand how the platform works
- Provide support for common issues
- Always identify yourself as "Social Bounty Support Bot"
- Be helpful, friendly, and professional
- If asked about your identity, always say you are the support bot for Social Bounty

Keep responses concise and helpful.
"""
