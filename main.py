import os
import sys
import time
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("WholesaleMain")

# Upgraded Core & Tools Imports
from tools.master_vacuum_engine import MasterVacuumEngine
from tools.automated_deal_sniper import AutomatedDealSniper
from tools.telegram_polling_daemon import TelegramPollingDaemon
from tools.git_state_sync import GitStateSync

def run_scheduled_sniper():
    """
    Periodically executes the Deal Sniper pipeline to harvest 
    0-seller vacuums and dispatch actionable cards to Telegram.
    """
    sniper = AutomatedDealSniper()
    interval_seconds = int(os.getenv("SCAN_INTERVAL_SECONDS", "3600"))  # Default: hourly
    
    logger.info(f"Starting Sniper background loop (interval: {interval_seconds}s)...")
    while True:
        try:
            logger.info("Executing scheduled vacuum sniper scan...")
            sniper.run_sniper_mission(max_alerts=5)
        except Exception as e:
            logger.error(f"Error during sniper execution cycle: {e}")
        time.sleep(interval_seconds)

def main():
    logger.info("==================================================")
    logger.info("  WHOLESALE MRO AUTOMATION: SYSTEM INITIALIZING   ")
    logger.info("  DOCK LOCATION: PARKLAND, FL STATION (LOCAL_HQ)  ")
    logger.info("==================================================")

    # Validate essential environment variables
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token:
        logger.warning("[SECURITY] TELEGRAM_BOT_TOKEN is not set in environment.")
    if not chat_id:
        logger.warning("[SECURITY] TELEGRAM_CHAT_ID is not set in environment.")

    # 1. Start Sniper in a background thread
    sniper_thread = threading.Thread(target=run_scheduled_sniper, daemon=True)
    sniper_thread.start()
    logger.info("Deal Sniper background scanner thread active.")

    # 2. Run Telegram Polling Daemon in the foreground
    # This keeps Railway's container running 24/7 and listens for button clicks
    logger.info("Starting Telegram Polling Daemon on primary process...")
    daemon = TelegramPollingDaemon()
    daemon.run()

if __name__ == "__main__":
    main()
