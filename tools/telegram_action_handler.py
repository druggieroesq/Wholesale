import os
import sys
import json
import requests
from typing import Dict, Any, List

class TelegramActionHandler:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "8501395612").strip()
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_deal_card(self, deal: Dict[str, Any]) -> bool:
        if not self.bot_token or not self.chat_id:
            print("[error] Telegram credentials missing. Ensure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.")
            return False

        asin = deal.get("asin", "N/A")
        title = deal.get("title", "Industrial Sourcing Target")[:45]
        vendor = deal.get("vendor", "Global Industrial")
        wholesale_cost = float(deal.get("wholesale_cost", 0.0))
        amz_price = float(deal.get("amazon_price", 0.0))
        roi = float(deal.get("roi_pct", 0.0))
        net_profit = float(deal.get("net_profit", 0.0))
        vendor_url = deal.get("vendor_url", "https://www.globalindustrial.com")
        amz_url = f"https://www.amazon.com/dp/{asin}"

        text = (
            f"⚡ <b>0-SELLER VACUUM DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>{title}</b>\n"
            f"• ASIN: <code>{asin}</code>\n"
            f"• Vendor: <b>{vendor}</b>\n"
            f"─────────────────────────────\n"
            f"💵 <b>Wholesale Cost:</b> ${wholesale_cost:.2f}\n"
            f"🛒 <b>Amazon Price:</b> ${amz_price:.2f}\n"
            f"📈 <b>Net Margin:</b> ${net_profit:.2f} (<b>{roi:.1f}% ROI</b>)\n"
            f"📍 <b>Fulfillment:</b> Parkland Self-Prep ($0.35/unit)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔗 Open Vendor Link", "url": vendor_url},
                    {"text": "📦 Amazon Listing", "url": amz_url}
                ],
                [
                    {"text": "🛒 Buy & Ship to Parkland", "callback_data": f"buy:{asin}:{wholesale_cost}:{vendor}"},
                    {"text": "❌ Pass / Exclude", "callback_data": f"decline:{asin}"}
                ]
            ]
        }

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
            "disable_web_page_preview": False
        }

        try:
            res = requests.post(f"{self.api_url}/sendMessage", json=payload, timeout=12)
            data = res.json()
            if data.get("ok"):
                print(f"[telegram] Successfully sent candidate card for ASIN {asin}")
                return True
            else:
                print(f"[telegram error] {data.get('description')}")
                return False
        except Exception as e:
            print(f"[telegram exception] {e}")
            return False

    def test_connection(self) -> bool:
        print("[*] Testing Telegram link...")
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": "🎯 <b>Connection Test:</b> Telegram bot link is active, credentials loaded, and secure.",
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(url, json=payload, timeout=10)
            print(f"Status: {res.status_code} | Response: {res.text}")
            return res.json().get("ok", False)
        except Exception as e:
            print(f"[error] {e}")
            return False


if __name__ == "__main__":
    handler = TelegramActionHandler()
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        handler.test_connection()
    else:
        # Example test dispatch
        sample_deal = {
            "asin": "B08N5XYZ12",
            "title": "Industrial Heavy Duty Brass Ball Valve 1/2\"",
            "vendor": "Global Industrial",
            "wholesale_cost": 11.50,
            "amazon_price": 34.99,
            "roi_pct": 78.4,
            "net_profit": 9.02,
            "vendor_url": "https://www.globalindustrial.com/search?q=BRASS-VALVE"
        }
        handler.send_deal_card(sample_deal)
