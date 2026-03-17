import os
from dotenv import load_dotenv
from loguru import logger
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables from .env
load_dotenv()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    # Echo for now, but this is where we can add more complex interactions later
    await update.message.reply_text("Hello! I am David. I am currently running in echo mode.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echoes the user message back to them."""
    text = update.message.text
    logger.info(f"Received message: {text}")
    await update.message.reply_text(f"Echo: {text}")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    authorized_user_id = os.getenv("AUTHORIZED_USER_ID")
    
    if not token or token == "your_telegram_bot_token_here" or not authorized_user_id:
        logger.error("Please set valid TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_ID in your .env file.")
        return
        
    try:
        auth_user_id = int(authorized_user_id)
    except ValueError:
        logger.error("AUTHORIZED_USER_ID must be an integer.")
        return

    logger.info("Initializing David's Telegram interface...")
    app = ApplicationBuilder().token(token).build()

    # Restrict the bot to only respond to a specific user for security reasons
    user_filter = filters.User(user_id=auth_user_id)
    app.add_handler(CommandHandler("start", start, filters=user_filter))
    # MessageHandler is a catch-all for any text messsages that aren't commands
    app.add_handler(MessageHandler(user_filter & filters.TEXT & ~filters.COMMAND, echo))

    logger.info("Bot is now polling for messages...")
    app.run_polling()

if __name__ == "__main__":
    main()