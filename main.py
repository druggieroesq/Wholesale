"""
main.py
Master Production Runner for Wholesale MRO Arbitrage Fleet.

Orchestrates:
1. Telegram Polling Daemon (Background Thread) with 409-conflict mitigation & anti-collision state.
2. Scheduled Morning Scout Run (7:00 AM ET): Generates Feed A (Buys) & Feed B (Reverse RFQs).
3. Scheduled Evening Digest (8:00 PM ET): Dispatches partner email digest.
4. Healthcheck / Ping loop for hosting runtime (Railway / Docker).
"""

import os
import sys
import time
import logging
import threading
import schedule
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MasterFleet")

from tools.queue_manager import send_daily_decision_email_digest
from tools.dual_feed_dispatcher import generate_daily_worklist
from tools.walmart_morning_scout import evaluate_walmart_wfs_gap, send_morning_scout_telegram

def run_morning_dispatch_job():
    """Triggers at 7:00 AM ET every morning."""
    logger.info("Executing 7:00 AM Morning Dispatch Routine...")
    try:
        confirmed_buys = []
        reverse_rfqs = []
        
        worklist = generate_daily_worklist(confirmed_buys, reverse_rfqs, feed_a_limit=25, feed_b_limit=25)
        logger.info(f"Daily worklist compiled: {len(worklist.get('feed_a_buys', []))} Buys, {len(worklist.get('feed_b_rfqs', []))} RFQs.")
    except Exception as e:
        logger.error(f"Error in morning dispatch job: {e}")

def run_evening_digest_job():
    """Triggers at 8:00 PM ET every evening."""
    logger.info("Executing 8:00 PM Partner Email Digest Routine...")
    try:
        send_daily_decision_email_digest()
        logger.info("Daily decision email digest successfully sent.")
    except Exception as e:
        logger.error(f"Error sending evening digest: {e}")

def start_telegram_daemon():
    """
    Runs Telegram listener thread safely after clearing pending connections
    and handles automatic retry if an ephemeral 409 conflict occurs during deploy overlap.
    """
    try:
        from tools.telegram_polling_daemon import bot
        if not bot:
            logger.warning("TELEGRAM_BOT_TOKEN not configured. Telegram bot daemon skipped.")
            return

        logger.info("Purging old Telegram sessions / webhooks before starting listener...")
        try:
            # Correct telebot syntax: delete_webhook accepts drop_pending_updates
            if hasattr(bot, "delete_webhook"):
                bot.delete_webhook(drop_pending_updates=True)
            else:
                bot.remove_webhook()
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Webhook reset notice: {e}")

        logger.info("Starting Telegram Bot Polling thread...")
        while True:
            try:
                bot.infinity_polling(
                    timeout=20,
                    long_polling_timeout=10,
                    skip_pending=True,
                    logger_level=logging.WARNING
                )
            except Exception as poll_err:
                logger.error(f"Polling connection interrupted: {poll_err}. Retrying in 5 seconds...")
                time.sleep(5)
    except Exception as e:
        logger.error(f"Telegram polling thread terminated: {e}")

def main():
    logger.info("Booting Wholesale MRO Engine v2.0...")

    # 1. Start Telegram Bot in background daemon thread
    tg_thread = threading.Thread(target=start_telegram_daemon, daemon=True)
    tg_thread.start()

    # 2. Configure Cron Schedule (Eastern Time baseline)
    schedule.every().day.at("07:00").do(run_morning_dispatch_job)
    schedule.every().day.at("20:00").do(run_evening_digest_job)

    logger.info("Schedules initialized: 07:00 AM Morning Dispatch, 20:00 PM Evening Digest.")

    # 3. Main scheduler keep-alive loop
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
