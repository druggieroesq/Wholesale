"""
main.py - Pure Outbound Dispatcher (No Polling, Zero 409 Conflicts)
"""
import os
import sys
import time
import logging
import schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MasterFleet")

from tools.queue_manager import send_daily_decision_email_digest
from tools.dual_feed_dispatcher import generate_daily_worklist

def run_morning_dispatch_job():
    logger.info("Executing 7:00 AM Morning Dispatch Routine...")
    try:
        worklist = generate_daily_worklist([], [], feed_a_limit=25, feed_b_limit=25)
        logger.info(f"Daily worklist compiled: {len(worklist.get('feed_a_buys', []))} Buys, {len(worklist.get('feed_b_rfqs', []))} RFQs.")
    except Exception as e:
        logger.error(f"Error in morning dispatch job: {e}")

def run_evening_digest_job():
    logger.info("Executing 8:00 PM Partner Email Digest Routine...")
    try:
        send_daily_decision_email_digest()
        logger.info("Daily decision email digest sent.")
    except Exception as e:
        logger.error(f"Error sending evening digest: {e}")

def main():
    logger.info("Booting Wholesale MRO Engine v2.0 (Outbound Mode)...")

    # Schedule daily dispatch jobs
    schedule.every().day.at("07:00").do(run_morning_dispatch_job)
    schedule.every().day.at("20:00").do(run_evening_digest_job)

    logger.info("Schedules initialized: 07:00 AM Morning Dispatch, 20:00 PM Evening Digest.")

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
