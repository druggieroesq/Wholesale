"""
main.py
Master Production Runner for Wholesale MRO Engine (Native Standard-Library Webhook).
Requires ZERO external web dependencies (uses standard library http.server).
"""

import os
import sys
import json
import time
import logging
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import schedule

# Force sys.path resolution
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Safe .env loader
from dotenv import load_dotenv
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    try:
        load_dotenv(dotenv_path=env_path)
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MasterFleet")

from tools.queue_manager import send_daily_decision_email_digest
from tools.dual_feed_dispatcher import generate_daily_worklist
from tools.telegram_polling_daemon import bot

# 1. Background Schedulers
def run_morning_dispatch_job():
    logger.info("Executing 7:00 AM Morning Dispatch Routine...")
    try:
        worklist = generate_daily_worklist([], [], feed_a_limit=25, feed_b_limit=25)
        logger.info(
            f"Daily worklist compiled: "
            f"{len(worklist.get('feed_a_buys', []))} Buys, "
            f"{len(worklist.get('feed_b_rfqs', []))} RFQs."
        )
    except Exception as e:
        logger.error(f"Error in morning dispatch job: {e}")

def run_evening_digest_job():
    logger.info("Executing 8:00 PM Partner Email Digest Routine...")
    try:
        send_daily_decision_email_digest()
        logger.info("Daily decision email digest successfully sent.")
    except Exception as e:
        logger.error(f"Error sending evening digest: {e}")

def run_scheduler_daemon():
    schedule.every().day.at("07:00").do(run_morning_dispatch_job)
    schedule.every().day.at("20:00").do(run_evening_digest_job)
    logger.info("Scheduler initialized: 07:00 AM Morning Dispatch, 20:00 PM Evening Digest.")
    while True:
        schedule.run_pending()
        time.sleep(30)

# 2. Native Telegram Webhook Request Handler
class TelegramWebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/telegram_webhook":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                import telebot
                update = telebot.types.Update.de_json(data)
                if update and bot:
                    bot.process_new_updates([update])
            except Exception as e:
                logger.error(f"Error processing webhook data: {e}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress routine health check console noise
        return

def setup_telegram_webhook():
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain and bot:
        clean_domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
        webhook_url = f"https://{clean_domain}/telegram_webhook"
        try:
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            logger.info(f"Registered Telegram webhook: {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to register Telegram webhook: {e}")

def main():
    logger.info("Booting Wholesale MRO Engine v2.0 (Native Webhook Mode)...")

    # Start cron scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler_daemon, daemon=True)
    scheduler_thread.start()

    # Register webhook with Telegram
    setup_telegram_webhook()

    # Bind HTTP listener to Railway's assigned port
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), TelegramWebhookHandler)
    logger.info(f"HTTP Webhook listener active on port {port}.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
