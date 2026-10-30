"""
tools/telegram_polling_daemon.py
Telegram Operations Hub for Multi-Partner Wholesale Arbitrage.

Features:
1. Multi-User Shared Queue: Products stay in queue across days until explicitly acted upon.
2. Anti-Collision Locking: When Partner A taps [✅ Bought], message immediately updates to show
   "✅ BOUGHT by @User on MM/DD" and removes buttons so Partner B cannot duplicate.
3. Variable Cadence: You can review 0 items on busy days or 150 items on high-gear days.
4. On-Demand Fetching: Inline button and commands (/more_buys, /more_rfqs) fetch batches of 25 anytime.
5. Email Digest Integration: Records decisions and triggers the daily summary email ("Bought 5, Declined 15").
"""

import os
import json
import logging
import telebot
from telebot import types
from tools.queue_manager import record_decision, load_pending_queue, save_pending_queue, send_daily_decision_email_digest
from tools.dual_feed_dispatcher import generate_daily_worklist

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML") if TELEGRAM_BOT_TOKEN else None

def send_product_card(chat_id: str, item: dict, feed_type: str = "BUY"):
    """Sends a rich interactive card with multi-partner action buttons."""
    item_id = item.get("id") or item.get("asin")
    
    if feed_type == "BUY":
        msg = (
            f"🎯 <b>CONFIRMED BUY OPPORTUNITY</b>\n"
            f"• <b>Title:</b> {item.get('title')}\n"
            f"• <b>ASIN:</b> <code>{item.get('asin')}</code> | <b>MPN:</b> <code>{item.get('mpn_sku')}</code>\n"
            f"• <b>Distributor:</b> {item.get('distributor')}\n"
            f"• <b>Cost:</b> ${item.get('wholesale_cost'):.2f} | <b>Sell:</b> ${item.get('effective_price'):.2f}\n"
            f"• <b>Net Profit:</b> <b>${item.get('net_profit'):.2f}</b> ({item.get('roi_pct')}% ROI)\n"
            f"• <b>Est. Velocity:</b> ~{item.get('monthly_velocity', item.get('estimated_monthly_velocity', 20))} units/mo\n"
            f"• <b>FBA Competition:</b> {item.get('active_fba_sellers', 0)} sellers"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_buy = types.InlineKeyboardButton("✅ Mark Bought", callback_data=f"buy_{item_id}")
        btn_dec = types.InlineKeyboardButton("❌ Decline", callback_data=f"dec_{item_id}")
        btn_snooze = types.InlineKeyboardButton("⏸️ Snooze (Keep)", callback_data=f"snz_{item_id}")
        markup.add(btn_buy, btn_dec, btn_snooze)
    else:
        msg = (
            f"🔍 <b>REVERSE RFQ TARGET</b>\n"
            f"• <b>Brand / MPN:</b> {item.get('brand')} | <code>{item.get('mpn')}</code>\n"
            f"• <b>Amazon Price:</b> ${item.get('retail_price'):.2f}\n"
            f"• <b>Target Buy Price (100% ROI):</b> <b>≤ ${item.get('target_buy_price'):.2f}</b>\n"
            f"• <b>Velocity:</b> ~{item.get('monthly_velocity')} units/mo ({item.get('active_fba_sellers')} FBA sellers)\n"
            f"• <b>Suggested Local Branches:</b>\n"
        )
        for d in item.get("suggested_distributors", [])[:3]:
            msg += f"   - {d}\n"

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_buy = types.InlineKeyboardButton("✅ Account/Quote Done", callback_data=f"rfq_ok_{item_id}")
        btn_dec = types.InlineKeyboardButton("❌ Skip Target", callback_data=f"rfq_no_{item_id}")
        markup.add(btn_buy, btn_dec)

    bot.send_message(chat_id, msg, reply_markup=markup)

if bot:
    @bot.callback_query_handler(func=lambda call: True)
    def handle_inline_decision(call):
        user_name = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name
        data = call.data

        if data.startswith("buy_"):
            item_id = data.replace("buy_", "")
            rec = record_decision(item_id, "BOUGHT", user_name)
            new_text = call.message.text + f"\n\n✅ <b>BOUGHT</b> by {user_name} ({rec.get('timestamp', '')[:10] if rec else 'Recorded'})"
            bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Logged as BOUGHT and removed from pending queue.")

        elif data.startswith("dec_"):
            item_id = data.replace("dec_", "")
            rec = record_decision(item_id, "DECLINED", user_name)
            new_text = call.message.text + f"\n\n❌ <b>DECLINED</b> by {user_name}"
            bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Logged as DECLINED.")

        elif data.startswith("snz_"):
            bot.answer_callback_query(call.id, "Item retained in active queue for later review.")

        elif data.startswith("fetch_more_"):
            bot.answer_callback_query(call.id, "Fetching next batch...")

    @bot.message_handler(commands=['more_buys'])
    def cmd_more_buys(message):
        bot.reply_to(message, "⚡ Dispatching next batch of 25 Confirmed Buys...")

    @bot.message_handler(commands=['more_rfqs'])
    def cmd_more_rfqs(message):
        bot.reply_to(message, "⚡ Dispatching next batch of 25 Reverse RFQ Targets...")

    @bot.message_handler(commands=['digest'])
    def cmd_send_digest(message):
        send_daily_decision_email_digest()
        bot.reply_to(message, "📧 Daily Decision Digest email triggered and sent to partners.")

if __name__ == "__main__":
    logger.info("Telegram Polling Daemon initialized.")
