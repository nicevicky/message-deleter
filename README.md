# Social Bounty Telegram Bot

A comprehensive Telegram bot for Social Bounty group management with AI support.

## Features

- 🤖 AI-powered responses using Gemini 2.0 Flash
- 🛡️ Auto-delete join/leave messages
- 👋 Welcome new members
- 🚫 Word filtering and spam protection
- 📊 Top users statistics with image generation
- 👨‍💼 Admin-only private messaging
- 💾 User activity tracking

## Setup

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set up environment variables (copy `.env.example` to `.env`)
4. Deploy to Vercel
5. Set webhook: `GET /set-webhook`

## Environment Variables

- `TELEGRAM_BOT_TOKEN`: Your bot token from @BotFather
- `GEMINI_API_KEY`: Your Google Gemini API key
- `WEBHOOK_URL`: Your Vercel app URL + /webhook
- `ADMIN_IDS`: Comma-separated admin user IDs
- `GROUP_CHAT_ID`: Your group chat ID

## Commands

- `/start` - Bot introduction
- `/help` - Show help (admin only)
- `/topusers` - Generate top users image (admin only)
- `/filter add/remove/list` - Manage word filters (admin only)

## Deployment

1. Push to GitHub
2. Connect to Vercel
3. Set environment variables in Vercel dashboard
4. Deploy
5. Set webhook using the `/set-webhook` endpoint
