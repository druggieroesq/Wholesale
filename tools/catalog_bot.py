#!/usr/bin/env python3
"""
tools/catalog_bot.py
Autonomous Wholesale Catalog & Ingestion Telegram Bot

Features:
- File Upload Handling: Drop .xlsx or .csv files directly in Telegram chat to stage into /incoming
- Manual & Scheduled Triggers: /scan command to launch processing across queued catalogs
- Multi-Partner Anti-Collision: Interactive callback decision handling (Buy / Decline / Snooze)
- Keepa Starter Plan Awareness: Manages background execution without blocking polling
- Status Tracking: /status and /queue inspection commands
"""

import os
import sys
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

import telebot
from telebot import types
from telebot.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv("/app/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("CatalogBot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8501395612").strip()

BASE_DIR = Path("/app")
INCOMING_DIR = BASE_DIR / "data" / "catalogs" / "incoming"
PROCESSED_DIR = BASE_DIR / "data" / "catalogs" / "processed"
REPORTS_DIR = BASE_DIR / "data" / "reports"
LOGS_DIR = BASE_DIR / "data" / "logs"
AUDIT_LOG_FILE = BASE_DIR / "data" / "telegram_actions.json"

for d in [INCOMING_DIR, PROCESSED_DIR, REPORTS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML") if TELEGRAM_BOT_TOKEN else None


# ==============================================================================
# 1. Audit Ledger
# ==============================================================================
def record_decision(item_id: str, action: str, user: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    existing = []
    if AUDIT_LOG_FILE.exists():
        try:
            existing = json.loads(AUDIT_LOG_FILE.read_text())
        except Exception:
            existing = []

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "item_id": item_id,
        "action": action,
        "user": user,
        "details": details or {}
    }
    existing.append(record)
    AUDIT_LOG_FILE.write_text(json.dumps(existing, indent=2))
    return record


# ==============================================================================
# 2. Outbound Card Dispatcher
# ==============================================================================
def send_deal_card(deal: Dict[str, Any], chat_id: Optional[str] = None) -> bool:
    """Sends a qualified catalog multipack deal card with action buttons."""
    if not bot:
        logger.error("TELEGRAM_BOT_TOKEN not configured.")
        return False

    target_chat = chat_id or TELEGRAM_CHAT_ID
    asin = deal.get("asin") or deal.get("ASIN", "N/A")
    title = (deal.get("title") or deal.get("Title", "Wholesale Target"))[:50]
    vendor = deal.get("vendor") or deal.get("Distributor", "Distributor")
    sku = deal.get("sku") or deal.get("Distributor_SKU", "N/A")
    pack_size = deal.get("pack_size") or deal.get("Pack_Size", 1)

    wholesale_cost = float(deal.get("wholesale_cost") or deal.get("Total_COGS", 0.0))
    amz_price = float(deal.get("amazon_price") or deal.get("BuyBox_Price", 0.0))
    roi = float(deal.get("roi_pct") or deal.get("ROI_Pct", 0.0))
    net_profit = float(deal.get("net_profit") or deal.get("Net_Profit", 0.0))
    sales_rank = deal.get("sales_rank") or deal.get("Sales_Rank", "N/A")

    amz_url = f"https://www.amazon.com/dp/{asin}"
    pack_badge = f" ({pack_size}-Pack)" if pack_size > 1 else ""

    text = (
        f"🎯 <b>QUALIFIED CATALOG DEAL{pack_badge}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{title}</b>\n"
        f"• ASIN: <code>{asin}</code> | SKU: <code>{sku}</code>\n"
        f"• Distributor: <b>{vendor}</b>\n"
        f"─────────────────────────────\n"
        f"💵 <b>Wholesale Cost:</b> ${wholesale_cost:.2f}\n"
        f"🛒 <b>Amazon Buy Box:</b> ${amz_price:.2f}\n"
        f"📈 <b>Net Margin:</b> <b>${net_profit:.2f}</b> (<b>{roi:.1f}% ROI</b>)\n"
        f"📊 <b>Sales Rank:</b> {sales_rank}\n"
        f"📍 <b>Fulfillment:</b> Parkland Self-Prep ($0.35/unit)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📦 Open Amazon Listing", url=amz_url)
    )
    markup.add(
        InlineKeyboardButton("✅ Mark Bought", callback_data=f"buy_{asin}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"dec_{asin}"),
        InlineKeyboardButton("⏸️ Snooze", callback_data=f"snz_{asin}")
    )

    try:
        bot.send_message(target_chat, text, reply_markup=markup, disable_web_page_preview=False)
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch deal card: {e}")
        return False


# ==============================================================================
# 3. File Upload & Ingestion Handlers
# ==============================================================================
if bot:
    @bot.message_handler(content_types=["document"])
    def handle_incoming_catalog_file(message: Message):
        doc = message.document
        filename = doc.file_name or "uploaded_catalog.xlsx"

        if not (filename.endswith(".xlsx") or filename.endswith(".csv")):
            bot.reply_to(message, "⚠️ Unsupported file format. Please upload an `.xlsx` or `.csv` catalog.")
            return

        bot.reply_to(message, f"📥 Receiving <code>{filename}</code>... staging into incoming queue.")

        try:
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)

            destination = INCOMING_DIR / filename
            destination.write_bytes(downloaded)

            bot.reply_to(
                message,
                f"✅ <b>File staged successfully!</b>\n"
                f"• Path: <code>{destination}</code>\n"
                f"• Size: {round(len(downloaded) / 1024, 1)} KB\n\n"
                f"Use /scan to start processing all queued catalogs."
            )
        except Exception as e:
            logger.error(f"Failed saving uploaded file: {e}")
            bot.reply_to(message, f"❌ Failed saving file: {e}")

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(message: Message):
        bot.reply_to(
            message,
            "🤖 <b>Wholesale Catalog Automation Bot</b>\n\n"
            "<b>Commands:</b>\n"
            "• /queue - View catalogs waiting in `/incoming/`\n"
            "• /scan - Launch batch processing pipeline\n"
            "• /status - Check system logs and active tasks\n\n"
            "💡 <i>Tip: You can drag and drop any .xlsx or .csv catalog file directly into this chat.</i>"
        )

    @bot.message_handler(commands=["queue"])
    def cmd_queue(message: Message):
        files = list(INCOMING_DIR.glob("*.xlsx")) + list(INCOMING_DIR.glob("*.csv"))
        if not files:
            bot.reply_to(message, "📭 <b>Incoming queue is empty.</b> Drop a file here or move it into `/incoming/`.")
            return

        resp = f"📂 <b>Queued Catalogs ({len(files)}):</b>\n"
        for idx, f in enumerate(files, 1):
            resp += f"{idx}. <code>{f.name}</code> ({round(f.stat().st_size / 1024, 1)} KB)\n"
        resp += "\nSend /scan to execute the batch scanner."
        bot.reply_to(message, resp)

    @bot.message_handler(commands=["scan"])
    def cmd_scan(message: Message):
        files = list(INCOMING_DIR.glob("*.xlsx")) + list(INCOMING_DIR.glob("*.csv"))
        if not files:
            bot.reply_to(message, "⚠️ No files in queue. Upload a catalog spreadsheet first.")
            return

        # Check if scanner is already executing
        ps_check = subprocess.run(["pgrep", "-f", "run_all_catalogs.sh"], capture_output=True, text=True)
        if ps_check.stdout.strip():
            bot.reply_to(message, "⏳ A catalog scan is already running in the background. Use /status to check logs.")
            return

        bot.reply_to(message, f"🚀 <b>Launching scan across {len(files)} catalog(s)...</b>\nRunning Keepa token-pacing loop in background.")
        log_file = LOGS_DIR / "master_batch_run.log"
        subprocess.Popen(
            f"nohup /app/run_all_catalogs.sh > {log_file} 2>&1 &",
            shell=True,
            executable="/bin/bash"
        )

    @bot.message_handler(commands=["status"])
    def cmd_status(message: Message):
        log_file = LOGS_DIR / "master_batch_run.log"
        last_log = "No log output available yet."
        if log_file.exists():
            lines = log_file.read_text().splitlines()
            last_log = "\n".join(lines[-6:]) if lines else "Log file empty."

        reports = list(REPORTS_DIR.glob("*.json"))
        report_status = "\n".join([f"• <code>{r.name}</code>: {r.stat().st_size} bytes" for r in reports]) or "No reports generated yet."

        bot.reply_to(
            message,
            f"📊 <b>System Status</b>\n\n"
            f"<b>Reports:</b>\n{report_status}\n\n"
            f"<b>Recent Log Tail:</b>\n<pre>{last_log}</pre>"
        )

    @bot.callback_query_handler(func=lambda call: True)
    def handle_inline_decision(call: CallbackQuery):
        user_name = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name
        data = call.data
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        original_text = call.message.text or ""

        if data.startswith("buy_"):
            asin = data.replace("buy_", "")
            record_decision(asin, "BOUGHT", user_name)
            new_text = (
                f"{original_text}\n\n"
                f"✅ <b>BOUGHT</b> by {user_name} on {time.strftime('%Y-%m-%d')}\n"
                f"• Target Hub: <b>Parkland, FL Station</b>\n"
                f"• Status: <i>Queued for purchase order allocation</i>"
            )
            bot.edit_message_text(new_text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=None)
            bot.answer_callback_query(call.id, f"Marked as BOUGHT by {user_name}!")

        elif data.startswith("dec_"):
            asin = data.replace("dec_", "")
            record_decision(asin, "DECLINED", user_name)
            new_text = f"{original_text}\n\n❌ <b>DECLINED</b> by {user_name}"
            bot.edit_message_text(new_text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=None)
            bot.answer_callback_query(call.id, "Logged as DECLINED.")

        elif data.startswith("snz_"):
            bot.answer_callback_query(call.id, "Retained in queue for next review.")


# ==============================================================================
# 4. Polling Loop
# ==============================================================================
def run_polling():
    if not bot:
        logger.error("Cannot launch bot: TELEGRAM_BOT_TOKEN is missing.")
        return

    logger.info("Catalog Bot listening for files, commands, and callbacks...")
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"Polling loop encountered an error: {e}. Retrying in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    run_polling()
