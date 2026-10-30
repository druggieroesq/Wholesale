"""
tools/queue_manager.py
Centralized multi-user queue and state tracking.
Ensures persistent inventory review queues until an explicit decision is logged.
Prevents duplicate actions between business partners.
"""

import os
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, List, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DECISIONS_LOG_FILE = "data/decisions_history.json"
PENDING_QUEUE_FILE = "data/pending_decision_queue.json"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ALERT_EMAIL_RECIPIENT = os.getenv("ALERT_EMAIL_RECIPIENT", "")

def load_pending_queue() -> List[Dict[str, Any]]:
    if os.path.exists(PENDING_QUEUE_FILE):
        try:
            with open(PENDING_QUEUE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading pending queue: {e}")
    return []

def save_pending_queue(queue: List[Dict[str, Any]]):
    with open(PENDING_QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)

def load_decisions() -> List[Dict[str, Any]]:
    if os.path.exists(DECISIONS_LOG_FILE):
        try:
            with open(DECISIONS_LOG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def record_decision(item_id: str, action: str, user_name: str, notes: str = "") -> Optional[Dict[str, Any]]:
    """
    Records BOUGHT, DECLINED, or SNOOZED.
    Removes item from pending queue so it never repeats.
    """
    queue = load_pending_queue()
    target_item = None
    remaining_queue = []

    for item in queue:
        if str(item.get("id")) == str(item_id) or str(item.get("asin")) == str(item_id):
            target_item = item
        else:
            remaining_queue.append(item)

    if not target_item:
        logger.warning(f"Item {item_id} already resolved or not found.")
        return None

    save_pending_queue(remaining_queue)

    decision_record = {
        "item_id": item_id,
        "title": target_item.get("title", ""),
        "asin": target_item.get("asin", ""),
        "mpn": target_item.get("mpn_sku", ""),
        "distributor": target_item.get("distributor", ""),
        "wholesale_cost": target_item.get("wholesale_cost", 0.0),
        "effective_price": target_item.get("effective_price", 0.0),
        "net_profit": target_item.get("net_profit", 0.0),
        "roi_pct": target_item.get("roi_pct", 0.0),
        "action": action.upper(),  # BOUGHT, DECLINED
        "decided_by": user_name,
        "timestamp": datetime.now().isoformat(),
        "notes": notes
    }

    decisions = load_decisions()
    decisions.append(decision_record)
    with open(DECISIONS_LOG_FILE, "w") as f:
        json.dump(decisions, f, indent=2)

    return decision_record

def send_daily_decision_email_digest(date_str: str = None):
    """Sends executive email summary of decisions made."""
    if not SMTP_USER or not SMTP_PASS or not ALERT_EMAIL_RECIPIENT:
        logger.warning("SMTP credentials not configured. Skipping email digest.")
        return

    today = date_str or datetime.now().strftime("%Y-%m-%d")
    decisions = load_decisions()
    todays_actions = [d for d in decisions if d.get("timestamp", "").startswith(today)]

    bought = [d for d in todays_actions if d.get("action") == "BOUGHT"]
    declined = [d for d in todays_actions if d.get("action") == "DECLINED"]

    html = f"""
    <h2>Wholesale MRO Operations Digest — {today}</h2>
    <p><b>Summary:</b> Bought {len(bought)} SKUs | Declined {len(declined)} SKUs</p>
    
    <h3>✅ Bought ({len(bought)})</h3>
    <ul>
    """
    for b in bought:
        html += f"<li><b>{b.get('title')}</b> (ASIN: {b.get('asin')}, MPN: {b.get('mpn')}) - Net Profit: ${b.get('net_profit')} ({b.get('roi_pct')}% ROI) — Bought by {b.get('decided_by')}</li>"
    html += "</ul>"

    html += f"<h3>❌ Declined ({len(declined)})</h3><ul>"
    for d in declined:
        html += f"<li>{d.get('title')} (ASIN: {d.get('asin')}) — Declined by {d.get('decided_by')}</li>"
    html += "</ul>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"MRO Team Digest: {len(bought)} Bought, {len(declined)} Declined ({today})"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_EMAIL_RECIPIENT, msg.as_string())
        logger.info("Decision digest email dispatched successfully.")
    except Exception as e:
        logger.error(f"Failed to send digest email: {e}")
