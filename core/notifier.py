import os
import sys
import logging
import requests
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Notifier")

class Notifier:
    """
    Central alerting engine. Posts HTML deal cards directly to Telegram
    using environment variables and verified Chat ID (8501395612).
    """
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "8501395612").strip()
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_deal_alert(self, deal: Dict[str, Any]) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.")
            return False

        asin = deal.get("asin", "N/A")
        title = deal.get("title", "High Margin Wholesale Opportunity")[:45]
        vendor = deal.get("vendor", "Wholesale Partner")
        wholesale_cost = float(deal.get("wholesale_cost", 0.0))
        amz_price = float(deal.get("amazon_price", 0.0))
        roi = float(deal.get("roi_pct", 0.0))
        net_profit = float(deal.get("net_profit", 0.0))
        vendor_url = deal.get("vendor_url", "https://www.google.com")
        amz_url = f"https://www.amazon.com/dp/{asin}"

        text = (
            f"⚡ <b>0-SELLER VACUUM ALERT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>{title}</b>\n"
            f"• ASIN: <code>{asin}</code>\n"
            f"• Vendor: <b>{vendor}</b>\n"
            f"─────────────────────────────\n"
            f"💵 <b>Wholesale Cost:</b> ${wholesale_cost:.2f}\n"
            f"🛒 <b>Amazon Buy Box:</b> ${amz_price:.2f}\n"
            f"📈 <b>Net Margin:</b> ${net_profit:.2f} (<b>{roi:.1f}% ROI</b>)\n"
            f"📍 <b>Dock:</b> Parkland Self-Prep ($0.35/unit)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔗 Vendor Portal", "url": vendor_url},
                    {"text": "🛒 Amazon Listing", "url": amz_url}
                ],
                [
                    {"text": "🛒 Buy & Ship to Parkland", "callback_data": f"buy:{asin}:{wholesale_cost}:{vendor}"},
                    {"text": "❌ Pass", "callback_data": f"decline:{asin}"}
                ]
            ]
        }

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard
        }

        try:
            res = requests.post(f"{self.api_url}/sendMessage", json=payload, timeout=12)
            data = res.json()
            if data.get("ok"):
                logger.info(f"Dispatched alert for {asin} to Telegram chat {self.chat_id}")
                return True
            else:
                logger.error(f"Telegram API rejection: {data.get('description')}")
                return False
        except Exception as e:
            logger.error(f"Failed to post to Telegram: {e}")
            return False

    def send_system_message(self, message: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": f"ℹ️ <b>SYSTEM UPDATE:</b>\n{message}",
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(f"{self.api_url}/sendMessage", json=payload, timeout=10)
            return res.json().get("ok", False)
        except Exception:
            return False


if __name__ == "__main__":
    notifier = Notifier()
    test_deal = {
        "asin": "B08N5XYZ12",
        "title": "Industrial Brass Shut-Off Ball Valve 1/2\"",
        "vendor": "Global Industrial",
        "wholesale_cost": 11.50,
        "amazon_price": 36.99,
        "roi_pct": 78.4,
        "net_profit": 9.02,
        "vendor_url": "https://www.globalindustrial.com/search?q=VALVE"
    }
    notifier.send_deal_alert(test_deal)
