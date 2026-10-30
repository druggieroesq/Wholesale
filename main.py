"""
main.py - Robust Outbound Scheduler for Wholesale MRO Engine
"""
import os
import sys
import time
import logging
import schedule

# Force immediate stdout flushing
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MasterFleet")

print(">>> [BOOT] Python process initialized. Loading dependencies...", flush=True)

# Safe imports with explicit logging
try:
    from tools.queue_manager import send_daily_decision_email_digest
    from tools.dual_feed_dispatcher import generate_daily_worklist
    print(">>> [BOOT] Fleet modules imported successfully.", flush=True)
except Exception as e:
    logger.error(f"Critical error importing modules: {e}")

def run_morning_dispatch_job():
    logger.info("Executing 07:00 AM Morning Dispatch Routine...")
    try:
        worklist = generate_daily_worklist([], [], feed_a_limit=25, feed_b_limit=25)
        logger.info(f"Daily worklist compiled: {len(worklist.get('feed_a_buys', []))} Buys, {len(worklist.get('feed_b_rfqs', []))} RFQs.")
    except Exception as e:
        logger.error(f"Error in morning dispatch job: {e}")

def run_evening_digest_job():
    logger.info("Executing 20:00 PM Partner Email Digest Routine...")
    try:
        send_daily_decision_email_digest()
        logger.info("Daily decision email digest sent.")
    except Exception as e:
        logger.error(f"Error sending evening digest: {e}")

def main():
    logger.info("MasterFleet: Booting Wholesale MRO Engine v2.0...")
    
    # Schedule cron routines (Eastern Time)
    schedule.every().day.at("07:00").do(run_morning_dispatch_job)
    schedule.every().day.at("20:00").do(run_evening_digest_job)

    logger.info("MasterFleet: Schedules initialized: 07:00 AM Morning Dispatch, 20:00 PM Evening Digest.")
    logger.info("MasterFleet: Background scheduler active. Entering keep-alive loop.")

    # Main scheduler keep-alive loop
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
