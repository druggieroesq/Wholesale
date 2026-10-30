"""
tools/webhook_server.py
Webhook server for receiving Telegram button callbacks without polling.
"""
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI, Request
import uvicorn
from tools.telegram_polling_daemon import bot

app = FastAPI()

DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")

@app.on_event("startup")
def setup_webhook():
    if DOMAIN:
        url = f"https://{DOMAIN}/telegram_webhook"
        bot.remove_webhook()
        bot.set_webhook(url=url, drop_pending_updates=True)
        print(f"[WEBHOOK] Telegram webhook registered at: {url}")

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    import telebot
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return {"status": "ok"}

@app.get("/health")
def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
