#!/usr/bin/env python3
"""
Reverse Amazon Infill Hunter & Predictive Sourcing Engine ($10+ Profit Per Sale Edition)
---------------------------------------------------------------------------------------
- Minimum 75% ROI hurdle AND $10.00 Net Profit PER ORDER/SALE
- Pack/bundle aware: computes aggregate order COGS and total order profit
- Dynamic MABC calculated per order so multipacks adjust allowable cost correctly
- IP protection enforced via centralized core.ip_guard
- Live Block IP button in Telegram permanently mutes ASIN & writes brand to blocklist
- Button UI changes immediately on decision so you know it was reviewed
- High velocity targeting: ~150+ units/mo floor across all categories
- Form-data Keepa /query serialization (perPage >= 50, page=0)
- Manufacturer / private label trap blocker (totalOfferCount >= 2)
- Zero-FBA buy box monopoly gate (fbaOfferCount == 0, buyBoxIsFBA == False)
- Full CLI argument compatibility for Railway startup scripts
"""

import os
import sys
import time
import json
import math
import re
import logging
import argparse
import threading
import urllib.parse
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# Ensure project root is available on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.ip_guard import is_ip_blocked, append_brand_to_blocklist

load_dotenv("/app/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ReverseHunter: %(message)s"
)
logger = logging.getLogger("ReverseHunter")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_MIN_ROI = 0.75               # 75% minimum ROI hurdle
MIN_PROFIT_PER_SALE = 10.00         # $10.00 minimum net profit per order/sale

DATA_DIR = Path("/app/data")
REPORTS_DIR = DATA_DIR / "reports"
CATALOGS_DIR = DATA_DIR / "catalogs" / "incoming"
DECISIONS_FILE = DATA_DIR / "lead_decisions.json"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Velocity rank ceilings calibrated for ~150+ units/mo minimum
CATEGORY_CONFIG = {
    16310091: {
        "name": "Industrial & Scientific",
        "max_rank": 15000,
        "velocity_curve": {"a": 120000, "b": -0.68}
    },
    228013: {
        "name": "Tools & Home Improvement",
        "max_rank": 12000,
        "velocity_curve": {"a": 140000, "b": -0.70}
    },
    1064954: {
        "name": "Office Products",
        "max_rank": 10000,
        "velocity_curve": {"a": 160000, "b": -0.72}
    },
    3760901: {
        "name": "Health & Household",
        "max_rank": 8000,
        "velocity_curve": {"a": 200000, "b": -0.74}
    }
}

BULK_BLOCKLIST = [
    "toilet paper", "bath tissue", "paper towel", "facial tissue",
    "can liner", "trash bag", "gallon drum", "drum of", "pallet",
    "case of 48", "case of 96", "case of 80"
]

decisions_lock = threading.Lock()


def load_decisions() -> dict:
    if DECISIONS_FILE.exists():
        try:
            return json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_decisions(data: dict):
    with decisions_lock:
        DECISIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_asin_suppressed(asin: str) -> bool:
    with decisions_lock:
        decisions = load_decisions()
        entry = decisions.get(asin.upper())
        if not entry:
            return False

        status = entry.get("status")
        if status in ["BOUGHT", "IP_BLOCKED"]:
            return True

        if status == "DENIED":
            expires_ts = entry.get("expires_ts", 0)
            if time.time() < expires_ts:
                return True
    return False


def extract_package_quantity(title: str, package_quantity_field: int = 1) -> int:
    if package_quantity_field and int(package_quantity_field) > 1:
        return int(package_quantity_field)
    
    t_lower = (title or "").lower()
    match = re.search(r'\b(?:pack of|pack|pk|count of|ct|case of)\s*([0-9]{1,3})\b', t_lower)
    if match:
        try:
            qty = int(match.group(1))
            if 1 < qty <= 100:
                return qty
        except Exception:
            pass

    match2 = re.search(r'\b([0-9]{1,3})[\s-]*(?:pack|pk|count|ct)\b', t_lower)
    if match2:
        try:
            qty = int(match2.group(1))
            if 1 < qty <= 100:
                return qty
        except Exception:
            pass

    return 1


def start_telegram_feedback_listener():
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("No TELEGRAM_BOT_TOKEN set; feedback loop inactive.")
        return

    def listener_loop():
        logger.info("Telegram feedback callback listener started.")
        last_update_id = 0
        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                params = {"offset": last_update_id + 1, "timeout": 20}
                r = requests.get(url, params=params, timeout=25).json()

                for update in r.get("result", []):
                    last_update_id = update["update_id"]
                    cb = update.get("callback_query")
                    if not cb:
                        continue

                    cb_id = cb.get("id")
                    data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    msg_id = msg.get("message_id")

                    if data == "noop":
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Decision already recorded.", "show_alert": False},
                            timeout=5
                        )
                        continue

                    parts = data.split(":", 2)
                    if len(parts) < 2:
                        continue

                    action = parts[0]
                    asin = parts[1].strip().upper()
                    brand_context = parts[2].strip() if len(parts) > 2 else ""
                    now_ts = int(time.time())

                    with decisions_lock:
                        decisions = load_decisions()

                        if action == "BUY":
                            decisions[asin] = {
                                "status": "BOUGHT",
                                "timestamp": now_ts,
                                "expires_ts": None
                            }
                            save_decisions(decisions)
                            status_text = "🛒 BOUGHT (Muted)"

                        elif action == "BLOCKIP":
                            decisions[asin] = {
                                "status": "IP_BLOCKED",
                                "brand": brand_context,
                                "timestamp": now_ts,
                                "expires_ts": None
                            }
                            save_decisions(decisions)
                            if brand_context:
                                append_brand_to_blocklist(brand_context)
                            status_text = f"🚫 BLOCKED IP ({brand_context})" if brand_context else "🚫 BLOCKED IP"

                        elif action == "DENY":
                            decisions[asin] = {
                                "status": "DENIED",
                                "timestamp": now_ts,
                                "expires_ts": now_ts + (180 * 86400)
                            }
                            save_decisions(decisions)
                            status_text = "⛔ DENIED (180d)"

                        elif action == "PASS":
                            status_text = "⏩ SKIPPED"
                        else:
                            status_text = "REVIEWED"

                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": f"Saved: {status_text}", "show_alert": False},
                            timeout=5
                        )

                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "reply_markup": {
                                    "inline_keyboard": [[{"text": f"✅ {status_text}", "callback_data": "noop"}]]
                                }
                            },
                            timeout=5
                        )
                        logger.info(f"Feedback logged: {asin} -> {status_text}")

            except Exception as e:
                time.sleep(5)

    listener_thread = threading.Thread(target=listener_loop, daemon=True)
    listener_thread.start()


def get_keepa_meta():
    try:
        r = requests.get(f"https://api.keepa.com/token?key={KEEPA_KEY}", timeout=10).json()
        return int(r.get("tokensLeft", 0)), int(r.get("refillRate", 20))
    except Exception as e:
        logger.warning(f"Keepa token check failed: {e}")
        return 0, 20


def estimate_monthly_velocity(sales_rank: int, category_id: int) -> int:
    if sales_rank <= 0:
        return 0
    config = CATEGORY_CONFIG.get(category_id, {})
    curve = config.get("velocity_curve", {"a": 130000, "b": -0.70})
    est = int(curve["a"] * math.pow(sales_rank, curve["b"]))
    return max(150, est) if sales_rank <= config.get("max_rank", 15000) else max(10, est)


def predict_distributors(brand: str, title: str, category_name: str) -> list:
    b = brand.lower()
    t = title.lower()
    targets = []

    if any(k in b or k in t for k in ["brass", "fitting", "valve", "hose", "pneumatic", "coupler", "flange"]):
        targets.extend(["Midland Metal Wholesale", "Brennan Industries", "Dixon Valve"])
    elif any(k in b or k in t for k in ["abrasive", "sanding", "tape", "sealant", "adhesion", "epoxy"]):
        targets.extend(["Contour Wholesale", "Motion Industries", "Grainger Industrial"])
    elif any(k in b or k in t for k in ["wire", "cable", "conduit", "breaker", "fuse", "electrical"]):
        targets.extend(["Wesco / Anixter", "Graybar Electric", "City Electric Supply"])
    elif any(k in b or k in t for k in ["screw", "bolt", "anchor", "fastener", "hardware"]):
        targets.extend(["Orgill Hardware", "Fastenal Industrial", "Do It Best Corp"])
    elif any(k in b or k in t for k in ["cleaner", "degreaser", "dispenser", "sanitizer", "wipe", "solvent"]):
        targets.extend(["Essendant", "S.P. Richards", "Imperial Dade"])
    elif any(k in b or k in t for k in ["organic", "herb", "essential oil", "extract", "tea", "botanical"]):
        targets.extend(["Frontier Co-op", "UNFI", "KeHE Distributors"])
    else:
        if category_name == "Industrial & Scientific":
            targets.extend(["Motion Industries", "MSC Industrial Direct"])
        elif category_name == "Tools & Home Improvement":
            targets.extend(["Orgill Hardware Wholesale", "PrimeSource Building"])
        else:
            targets.extend(["Regional Master Wholesaler", "Authorized Brand Distributor"])

    return targets[:2]


def build_local_catalog_index():
    catalog_index = {}
    if not CATALOGS_DIR.exists():
        return catalog_index

    for file_path in CATALOGS_DIR.glob("*.*"):
        if file_path.suffix.lower() not in [".xlsx", ".xls", ".csv"]:
            continue
        try:
            df = pd.read_excel(file_path, nrows=50000) if file_path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(file_path, nrows=50000, dtype=str)
            upc_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["UPC", "BARCODE", "EAN"])), None)
            cost_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["PRICE", "COST", "DEALER"])), None)
            sku_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["ITEM", "SKU", "CODE"])), None)

            if upc_col and cost_col:
                vendor = file_path.stem.replace("_Catalog", "").upper()
                for _, row in df.iterrows():
                    upc = str(row[upc_col]).strip().replace(".0", "")
                    cost = pd.to_numeric(str(row[cost_col]).replace("$", "").replace(",", ""), errors="coerce")
                    if upc and cost and cost > 0:
                        catalog_index[upc] = {"vendor": vendor, "sku": str(row[sku_col]) if sku_col else "N/A", "cost": float(cost)}
        except Exception as e:
            logger.warning(f"Catalog indexing failed for {file_path.name}: {e}")

    logger.info(f"Indexed {len(catalog_index)} UPCs across incoming distributor catalogs.")
    return catalog_index


CATALOG_INDEX = build_local_catalog_index()


def dispatch_telegram_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    asin = deal["asin"]
    brand = deal["brand"]
    safe_brand = brand.replace(":", " ")[:20].strip()
    upc_str = deal["upcs"][0] if deal.get("upcs") else ""
    sourcing_query = urllib.parse.quote(upc_str if upc_str else f"{deal['brand']} {deal['title'][:25]}")
    google_shopping = f"https://www.google.com/search?tbm=shop&q={sourcing_query}"
    zoro_search = f"https://www.zoro.com/search?q={sourcing_query}"

    pack_info = f" ({deal['pack_qty']}-Pack)" if deal['pack_qty'] > 1 else ""

    if deal.get("matched_vendor"):
        match_section = (
            f"✅ *Direct Catalog Match:* `{deal['matched_vendor']}`\n"
            f"🏷 *SKU:* `{deal['matched_sku']}` | *Cost:* ${deal['matched_cost']:.2f}/ea (${deal['total_order_cogs']:.2f} total order)\n"
            f"📈 *ROI:* *{deal['est_roi']:.1f}%* (Floor: 75%)\n"
            f"💵 *Profit per Sale:* *${deal['est_net_profit']:.2f}* (Floor: $10.00/order)\n"
        )
    else:
        target_dist_str = " / ".join(deal["target_distributors"])
        unit_breakdown = f" (~${deal['max_buy_cost'] / deal['pack_qty']:.2f}/ea)" if deal['pack_qty'] > 1 else ""
        match_section = (
            f"🏢 *Suggested Wholesalers:* `{target_dist_str}`\n"
            f"🎯 *Max Allowable Buy Cost:* *${deal['max_buy_cost']:.2f} total order*{unit_breakdown}\n"
            f"   *(Guarantees $\\ge$ $10 Profit Per Sale & 75% ROI)*\n"
            f"🔎 *Wholesale Search:* [Google Shopping]({google_shopping}) | [Zoro]({zoro_search})\n"
        )

    text = (
        f"🔥 *HIGH-VELOCITY PRIME MONOPOLY LEAD*\n\n"
        f"📦 *Product:* {deal['title']}{pack_info}\n"
        f"🏷 *Brand:* `{brand}`\n"
        f"🆔 *ASIN:* `{asin}` | *UPC:* `{upc_str if upc_str else 'N/A'}`\n"
        f"🚀 *Estimated Velocity:* *~{deal['est_monthly_units']:,} units/mo* (BSR: {deal['sales_rank']:,})\n"
        f"🛒 *Baseline FBM Price:* ${deal['baseline_price']:.2f}\n"
        f"🎯 *Target Prime Price:* ${deal['target_prime_price']:.2f} (+10% Premium)\n"
        f"👥 *Competition:* FBM Sellers: {deal['seller_count']} | FBA: 0\n"
        f"⚖️ *Specs:* {deal['weight_lbs']} lbs | {deal['max_dim_in']}\"\n\n"
        f"{match_section}\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{asin})"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🛒 Bought", "callback_data": f"BUY:{asin}"},
                {"text": "⛔ Deny (180d)", "callback_data": f"DENY:{asin}"}
            ],
            [
                {"text": "🚫 Block IP Brand", "callback_data": f"BLOCKIP:{asin}:{safe_brand}"},
                {"text": "⏩ Ignore", "callback_data": f"PASS:{asin}"}
            ]
        ]
    }

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def query_keepa_product_finder(category_id: int, min_rank: int, max_rank: int, min_price: float, per_page: int = 50):
    payload = {
        "rootCategory": category_id,
        "current_SALES_gte": min_rank,
        "current_SALES_lte": max_rank,
        "current_NEW_gte": int(min_price * 100),
        "productType": [0, 1, 2],
        "page": 0,
        "perPage": max(50, per_page)
    }
    try:
        url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1"
        r = requests.post(url, data={"selection": json.dumps(payload)}, timeout=30)
        data = r.json()
        return data.get("asinList", []) or [], data.get("tokensLeft", 0)
    except Exception as e:
        logger.error(f"Keepa query failed: {e}")
        return [], 0


def fetch_products_detail(asins: list):
    if not asins:
        return [], 0
    codes = ",".join(asins)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&asin={codes}&stats=90"
    try:
        r = requests.get(url, timeout=30)
        data = r.json()
        return data.get("products", []) or [], data.get("tokensLeft", 0)
    except Exception as e:
        logger.error(f"Keepa detail fetch failed: {e}")
        return [], 0


def run_high_velocity_hunter(min_fbm_price=22.0, max_weight=8.0, max_dim=16.0, min_rank_override=1, max_rank_override=250000):
    start_telegram_feedback_listener()

    tokens, refill = get_keepa_meta()
    logger.info(f"High-Velocity Hunter Online | Refill: {refill}/min | Tokens: {tokens} | Hurdle: 75%+ ROI & $10+ Profit Per Sale Floor")

    results_file = REPORTS_DIR / "reverse_prime_monopoly_leads.json"
    leads = []
    if results_file.exists():
        try:
            leads = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    seen_asins = {l["asin"] for l in leads if "asin" in l}
    step_size = 2500

    while True:
        for cat_id, cat_info in CATEGORY_CONFIG.items():
            cat_name = cat_info["name"]
            max_rank = cat_info["max_rank"]
            cur_min = min_rank_override

            logger.info(f"Targeting Category [{cat_name}] with Velocity Floor (Max Rank: {max_rank:,})")

            while cur_min < max_rank:
                tokens_left, refill_rate = get_keepa_meta()
                dynamic_reserve = max(10, int(refill_rate * 0.5))

                if tokens_left < dynamic_reserve:
                    sleep_sec = max(2.0, round(float(dynamic_reserve - tokens_left) / (refill_rate / 60.0), 2))
                    logger.info(f"Tokens ({tokens_left}) below reserve ({dynamic_reserve}). Resting {sleep_sec}s...")
                    time.sleep(sleep_sec)
                    continue

                cur_max = min(cur_min + step_size, max_rank)
                logger.info(f"Sweep [{cat_name}] High-Velocity BSR {cur_min:,}-{cur_max:,} | Tokens: {tokens_left}...")

                asin_candidates, _ = query_keepa_product_finder(
                    category_id=cat_id,
                    min_rank=cur_min,
                    max_rank=cur_max,
                    min_price=min_fbm_price,
                    per_page=50
                )

                cur_min += step_size
                fresh_asins = [a for a in asin_candidates if a not in seen_asins and not is_asin_suppressed(a)]

                if not fresh_asins:
                    time.sleep(max(0.5, round(2.0 / (refill_rate / 20.0), 2)))
                    continue

                batch_size = 30 if refill_rate >= 100 else (20 if refill_rate >= 50 else 10)

                for i in range(0, len(fresh_asins), batch_size):
                    tokens_left, refill_rate = get_keepa_meta()
                    dynamic_reserve = max(10, int(refill_rate * 0.5))
                    if tokens_left < dynamic_reserve:
                        sleep_sec = max(2.0, round(float(dynamic_reserve - tokens_left) / (refill_rate / 60.0), 2))
                        time.sleep(sleep_sec)
                        break

                    batch = fresh_asins[i:i + batch_size]
                    products, _ = fetch_products_detail(batch)

                    for p in products:
                        if not p or not isinstance(p, dict):
                            continue

                        asin = (p.get("asin") or "").strip().upper()
                        brand = (p.get("brand") or "").strip()
                        title = p.get("title") or "Unknown"

                        if is_asin_suppressed(asin):
                            seen_asins.add(asin)
                            continue

                        # Centralized IP Guard Check
                        if is_ip_blocked(brand=brand, title=title):
                            seen_asins.add(asin)
                            continue

                        # Bulky Commercial Goods Keyword Gate
                        if any(k in title.lower() for k in BULK_BLOCKLIST):
                            seen_asins.add(asin)
                            continue

                        # Physical Specs Extraction
                        weight_lbs = round((p.get("packageWeight") or 0) * 0.00220462, 2)
                        max_dim_in = round(max(
                            p.get("packageLength") or 0,
                            p.get("packageWidth") or 0,
                            p.get("packageHeight") or 0
                        ) * 0.0393701, 1)

                        if weight_lbs <= 0.05 or weight_lbs > max_weight or max_dim_in > max_dim:
                            seen_asins.add(asin)
                            continue

                        stats = p.get("stats") or {}
                        current = stats.get("current") or []

                        amazon_price = current[0] if len(current) > 0 else -1
                        fbm_price_raw = current[1] if len(current) > 1 else -1
                        sales_rank = current[3] if len(current) > 3 else -1

                        if amazon_price > 0:
                            seen_asins.add(asin)
                            continue

                        fba_count = stats.get("fbaOfferCount", 0)
                        buy_box_is_fba = stats.get("buyBoxIsFBA", False)
                        if fba_count > 0 or buy_box_is_fba is True:
                            seen_asins.add(asin)
                            continue

                        total_offers = stats.get("totalOfferCount", 0)
                        if total_offers < 2:
                            seen_asins.add(asin)
                            continue

                        if fbm_price_raw <= 0:
                            seen_asins.add(asin)
                            continue

                        baseline_price = fbm_price_raw / 100.0
                        if baseline_price < min_fbm_price:
                            seen_asins.add(asin)
                            continue

                        target_prime_price = round(baseline_price * 1.10, 2)
                        est_units = estimate_monthly_velocity(sales_rank, cat_id)
                        pack_qty = extract_package_quantity(title, p.get("packageQuantity", 1))

                        # Maximum Buy Cost guaranteeing BOTH >= 75% ROI and >= $10.00 Net Profit PER ORDER
                        net_payout = target_prime_price - 4.20 - (target_prime_price * 0.15)
                        
                        cost_cap_roi = net_payout / (1.0 + TARGET_MIN_ROI)
                        cost_cap_profit = net_payout - MIN_PROFIT_PER_SALE
                        
                        max_buy_cost = round(min(cost_cap_roi, cost_cap_profit), 2)

                        # Drop if impossible to clear $10 profit per sale
                        if max_buy_cost <= 3.00:
                            seen_asins.add(asin)
                            continue

                        upcs = p.get("upcList") or []
                        matched_distributor = None
                        for upc in upcs:
                            clean_upc = str(upc).strip()
                            if clean_upc in CATALOG_INDEX:
                                matched_distributor = CATALOG_INDEX[clean_upc]
                                break

                        est_roi = 0.0
                        net_profit = 0.0
                        total_order_cogs = 0.0
                        if matched_distributor:
                            unit_cogs = matched_distributor["cost"]
                            total_order_cogs = unit_cogs * pack_qty
                            est_fees = 4.20 + (target_prime_price * 0.15)
                            net_profit = target_prime_price - est_fees - total_order_cogs
                            est_roi = (net_profit / total_order_cogs) * 100.0 if total_order_cogs > 0 else 0.0

                            # Strict check: must clear 75% ROI AND $10.00 profit for the entire sale
                            if (est_roi / 100.0) < TARGET_MIN_ROI or net_profit < MIN_PROFIT_PER_SALE:
                                seen_asins.add(asin)
                                continue

                        target_distributors = predict_distributors(brand, title, cat_name)

                        lead = {
                            "asin": asin,
                            "title": title,
                            "brand": brand,
                            "pack_qty": pack_qty,
                            "category": cat_name,
                            "sales_rank": sales_rank,
                            "est_monthly_units": est_units,
                            "weight_lbs": weight_lbs,
                            "max_dim_in": max_dim_in,
                            "baseline_price": round(baseline_price, 2),
                            "target_prime_price": target_prime_price,
                            "max_buy_cost": max_buy_cost,
                            "target_distributors": target_distributors,
                            "seller_count": total_offers,
                            "fba_count": 0,
                            "upcs": upcs,
                            "matched_vendor": matched_distributor["vendor"] if matched_distributor else None,
                            "matched_sku": matched_distributor["sku"] if matched_distributor else None,
                            "matched_cost": matched_distributor["cost"] if matched_distributor else None,
                            "total_order_cogs": round(total_order_cogs, 2),
                            "est_roi": round(est_roi, 1),
                            "est_net_profit": round(net_profit, 2)
                        }

                        leads.append(lead)
                        seen_asins.add(asin)
                        logger.info(f"Verified Deal: {asin} ({pack_qty}-pk) | Profit/Sale: ${net_profit:.2f} | MABC: ${max_buy_cost:.2f}")
                        dispatch_telegram_alert(lead)

                    results_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")

                    inter_batch_delay = max(0.5, round(6.0 / (refill_rate / 20.0), 2))
                    time.sleep(inter_batch_delay)

                sweep_delay = max(1.0, round(4.0 / (refill_rate / 20.0), 2))
                time.sleep(sweep_delay)

        cycle_delay = max(2.0, round(10.0 / (refill_rate / 20.0), 2))
        logger.info(f"High-velocity sweep cycle complete. Recycling in {cycle_delay}s...")
        time.sleep(cycle_delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-Velocity Reverse Amazon Infill Hunter")
    parser.add_argument("--min-rank", type=int, default=1, help="Min BSR rank (Railway compatibility)")
    parser.add_argument("--max-rank", type=int, default=250000, help="Max BSR rank (Railway compatibility)")
    parser.add_argument("--min-fbm-price", type=float, default=22.0, help="Min FBM retail price floor")
    parser.add_argument("--min-tokens", type=int, default=50, help="Min token reserve (Railway compatibility)")
    parser.add_argument("--max-weight", type=float, default=8.0)
    parser.add_argument("--max-dim", type=float, default=16.0)
    args = parser.parse_args()

    run_high_velocity_hunter(
        min_fbm_price=args.min_fbm_price,
        max_weight=args.max_weight,
        max_dim=args.max_dim,
        min_rank_override=args.min_rank,
        max_rank_override=args.max_rank
    )
