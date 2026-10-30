"""
main.py
Master Production Runner for Wholesale MRO Engine (Webhook + Scheduler).
"""

import os
import sys
import time
import logging
import threading
from pathlib import Path
import schedule
import uvicorn

# Add project root to sys.path
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

def run_morning_dispatch_job():
    """Triggers at 7:00 AM ET every morning."""
    logger.info("Executing 7:00 AM Morning Dispatch Routine...")
    try:
        confirmed_buys = []
        reverse_rfqs = []
        worklist = generate_daily_worklist(confirmed_buys, reverse_rfqs, feed_a_limit=25, feed_b_limit=25)
        logger.info(
            f"Daily worklist compiled: "
            f"{len(worklist.get('feed_a_buys', []))} Buys, "
            f"{len(worklist.get('feed_b_rfqs', []))} RFQs."
        )
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

def run_scheduler_daemon():
    """Background scheduler loop."""
    schedule.every().day.at("07:00").do(run_morning_dispatch_job)
    schedule.every().day.at("20:00").do(run_evening_digest_job)
    logger.info("Scheduler initialized: 07:00 AM Morning Dispatch, 20:00 PM Evening Digest.")
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    logger.info("Booting Wholesale MRO Engine v2.0 (Webhook Mode)...")

    # 1. Start morning/evening cron scheduler in a background thread
    scheduler_thread = threading.Thread(target=run_scheduler_daemon, daemon=True)
    scheduler_thread.start()

    # 2. Bind and run FastAPI webhook listener on Railway's assigned port
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Binding HTTP Webhook listener on 0.0.0.0:{port}...")
    uvicorn.run("tools.webhook_server:app", host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    main()
