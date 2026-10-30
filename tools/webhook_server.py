"""
tools/webhook_server.py
FastAPI server handling Telegram webhook updates and button callbacks.
"""

import os
import sys
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
import uvicorn
import telebot

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TelegramWebhook")

from tools.telegram_polling_daemon import bot

# Resolve domain from Railway public domain or generic host variable
DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()

@asynccontextmanager
async def lifespan(app: FastAPI):
    if DOMAIN and bot:
        # Strip scheme if user pasted full URL in Railway variable
        clean_domain = DOMAIN.replace("https://", "").replace("http://", "").rstrip("/")
        webhook_url = f"https://{clean_domain}/telegram_webhook"
        try:
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            logger.info(f"Registered Telegram webhook: {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")
    else:
        logger.warning("RAILWAY_PUBLIC_DOMAIN not configured or bot missing; skipping webhook registration.")
    yield
    # Teardown logic on shutdown
    if bot:
        try:
            bot.remove_webhook()
            logger.info("Webhook cleanly removed on service shutdown.")
        except Exception as e:
            logger.warning(f"Error removing webhook on shutdown: {e}")

app = FastAPI(lifespan=lifespan)

@app.post("/telegram_webhook")
async def telegram_webhook_handler(request: Request):
    try:
        data = await request.json()
        update = telebot.types.Update.de_json(data)
        if update:
            bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"Error processing Telegram webhook update: {e}")
    return {"status": "ok"}

@app.get("/health")
def healthcheck():
    return {"status": "healthy", "service": "wholesale_mro_engine"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("tools.webhook_server:app", host="0.0.0.0", port=port, log_level="info")
