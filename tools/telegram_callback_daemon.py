#!/usr/bin/env python3
"""
Telegram Feedback Decision Daemon
---------------------------------
Listens for inline button callbacks from Telegram:
- BOUGHT: Permanently silences the ASIN.
- DENIED: Silences the ASIN for 180 days.
- PASS: Clears alert state without suppression.
"""

import os
import sys
import time
import json
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/app/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FEEDBACK] %(message)s")
logger = logging.getLogger("FeedbackDaemon")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DECISIONS_FILE = Path("/app/data/lead_decisions.json")

def load_decisions():
    if DECISIONS_FILE.exists():
        try:
            return json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_decisions(data):
    DECISIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def answer_callback(cb_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", json={
            "callback_query_id": cb_id,
            "text": text,
            "show_alert": False
        }, timeout=5)
    except Exception as e:
        logger.error(f"Error answering callback: {e}")

def edit_message_reply_markup(chat_id, message_id, status_text):
    try:
        # Edit message reply markup to remove buttons and show confirmation tag
        requests.post(f"https://api.telegram.org/bot{TOKEN}/editMessageReplyMarkup", json={
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": f"STATUS: {status_text}", "callback_data": "noop"}
                ]]
            }
        }, timeout=5)
    except Exception as e:
        logger.error(f"Error updating message markup: {e}")

def main():
    if not TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN. Exiting.")
        sys.exit(1)

    logger.info("Feedback Decision Daemon active. Polling Telegram for button clicks...")
    last_update_id = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            r = requests.get(url, params=params, timeout=25).json()
            
            for update in r.get("result", []):
                last_update_id = update["update_id"]
                cb = update.get("callback_query")
                if not cb:
                    continue

                cb_id = cb["id"]
                data = cb.get("data", "")
                msg = cb.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                msg_id = msg.get("message_id")

                if data == "noop":
                    answer_callback(cb_id, "Decision already logged.")
                    continue

                if ":" not in data:
                    continue

                action, asin = data.split(":", 1)
                asin = asin.strip().upper()
                now_ts = int(time.time())

                decisions = load_decisions()

                if action == "BUY":
                    decisions[asin] = {
                        "status": "BOUGHT",
                        "timestamp": now_ts,
                        "expires_ts": None  # Permanent
                    }
                    save_decisions(decisions)
                    answer_callback(cb_id, f"✅ Logged as BOUGHT! Permanently muted.")
                    edit_message_reply_markup(chat_id, msg_id, "✅ PURCHASED")
                    logger.info(f"ASIN {asin} marked as BOUGHT (Permanent Suppression)")

                elif action == "DENY":
                    expires_ts = now_ts + (180 * 86400)  # 180 days in seconds
                    decisions[asin] = {
                        "status": "DENIED",
                        "timestamp": now_ts,
                        "expires_ts": expires_ts
                    }
                    save_decisions(decisions)
                    answer_callback(cb_id, f"⛔ DENIED! Muted for 180 days.")
                    edit_message_reply_markup(chat_id, msg_id, "⛔ DENIED (180d Mute)")
                    logger.info(f"ASIN {asin} marked as DENIED (Suppressed until day 180)")

                elif action == "PASS":
                    answer_callback(cb_id, f"⏩ Ignored. No cooldown applied.")
                    edit_message_reply_markup(chat_id, msg_id, "⏩ SKIPPED")
                    logger.info(f"ASIN {asin} marked as PASS")

        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
