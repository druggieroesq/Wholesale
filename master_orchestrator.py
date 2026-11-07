#!/usr/bin/env python3
"""
Master Orchestrator & Centralized Telegram Gateway (v7.6 Self-Healing Engine)
-----------------------------------------------------------------------------
Supervises seven dedicated background workers:
1. ReverseAmazonHunter       (tools/reverse_amazon_finder.py)            -> Evaluates Keepa demand with sliding scale & $1,000/mo net profit pool floor
2. SheetSyncDaemon           (tools/sync_today_leads_to_sheet.py --daemon)-> Continuous automated 15-minute Google Sheets Buy List synchronization
3. WebDistributorScout       (tools/web_distributor_scout.py)            -> Live portal price crawler (Zoro, Midland)
4. AutonomousRecruiter       (tools/autonomous_recruiter.py)             -> Scrapes brand websites for wholesale dealer forms
5. DriveManufacturerSync     (tools/sync_drive_manufacturer_catalogs.py) -> Syncs shared Google Drive Manufacturer folder
6. ReorderPOGenerator        (tools/reorder_po_generator.py)             -> Fair share DoSR runway, price-scrub, & decoupled 3PL routing
7. AntiRegateDaemon          (tools/anti_regating_pulse_daemon.py)       -> Staggered FBM dormancy keeper for ungated brand permissions

Key Features:
- Self-Healing Webhook Routing: Automatically detects broken /library/ links and uses verified deployment /exec endpoint.
- Native HTTP health check server on $PORT with SO_REUSEADDR and retry backoff.
- Single-point exclusive Telegram polling worker with verified identity and inline button locking.
- Upstream IP Pre-Filter Shield with normalized corporate suffix matching.
- Embedded Two-Tier Catalog Hot-Reload Watcher:
    * Manufacturer tier (Priority 1): data/catalogs/manufacturer_catalogs/incoming/ -> indexed/
    * Wholesale tier (Priority 2):    data/catalogs/wholesale_catalogs/incoming/    -> indexed/
- Decoupled Logistics Routing: Makki Prep strictly for BCI; Detroit Metro 3PL ($1.10 TBD estimate) for Marlo / JB Tools.
- Enforced Sourcing Tiers (50-99: <=1 FBA | 100-249: <=2 FBA | 250+: share > 250 & <=5 FBA).
- Hard $1,000/Month Net Profit Pool per SKU requirement on all lead approvals.
- Auto-seeds ungated brand anchors into brand_ungate_registry.json upon Telegram BUYLIST approval.
- Persistent local CSV logging and Google Sheets Webhook synchronization.
"""

import os
import sys
import time
import json
import logging
import threading
import subprocess
import socket
import sqlite3
import re
import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Force .env override so container-level stale variables are replaced
load_dotenv(ROOT_DIR / ".env", override=True)

from config import (
    calculate_sliding_profit_floor,
    get_logistics_profile,
    validate_fba_competition,
    get_verified_webhook_url,
    MIN_MONTHLY_PROFIT_POOL
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] MasterGateway: %(message)s"
)
logger = logging.getLogger("MasterGateway")

KEEPA_KEY = os.getenv("KEEPA_API_KEY", "").strip()

# Robust Token Sanitization (Prevents 401 Unauthorized from quotes or duplicate 'bot' prefix)
_raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
if _raw_token.lower().startswith("bot"):
    TELEGRAM_BOT_TOKEN = _raw_token[3:]
else:
    TELEGRAM_BOT_TOKEN = _raw_token

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")
BUY_LIST_WEBHOOK_URL = get_verified_webhook_url()

DATA_DIR = ROOT_DIR / "data"
CATALOG_DIR = DATA_DIR / "catalogs"
REPORTS_DIR = DATA_DIR / "reports"
PO_DIR = DATA_DIR / "purchase_orders"
TELEGRAM_DECISIONS_FILE = DATA_DIR / "telegram_decisions.json"
LEAD_DECISIONS_FILE = DATA_DIR / "lead_decisions.json"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"
FALLBACK_IP_FILE = ROOT_DIR / "data" / "ip_blocklist.json"
SAVED_BUY_LIST_CSV = DATA_DIR / "saved_buy_list.csv"

# Two-Tier Directory Structure
WHOLESALE_INCOMING = CATALOG_DIR / "wholesale_catalogs" / "incoming"
WHOLESALE_INDEXED = CATALOG_DIR / "wholesale_catalogs" / "indexed"
MFR_INCOMING = CATALOG_DIR / "manufacturer_catalogs" / "incoming"
MFR_INDEXED = CATALOG_DIR / "manufacturer_catalogs" / "indexed"

# Log files for supervised workers
HUNTER_LOG_FILE = DATA_DIR / "hunter_live.log"
SHEET_SYNC_LOG_FILE = DATA_DIR / "sheet_sync.log"
SCOUT_LOG_FILE = DATA_DIR / "scout_live.log"
RECRUITER_LOG_FILE = DATA_DIR / "recruiter_live.log"
DRIVE_SYNC_LOG_FILE = DATA_DIR / "drive_sync.log"
REORDER_LOG_FILE = DATA_DIR / "reorder_po.log"
ANTI_REGATE_LOG_FILE = DATA_DIR / "anti_regate.log"

REORDER_INTERVAL_SECONDS = int(os.getenv("REORDER_INTERVAL_SECONDS", 21600))

for p in [REPORTS_DIR, DATA_DIR, CATALOG_DIR, PO_DIR, WHOLESALE_INCOMING, WHOLESALE_INDEXED, MFR_INCOMING, MFR_INDEXED]:
    p.mkdir(parents=True, exist_ok=True)

decisions_lock = threading.Lock()
ip_cache_lock = threading.Lock()
catalog_watch_lock = threading.Lock()
_IP_CACHE_DATA = None
_IP_CACHE_MTIME = 0
_KNOWN_CATALOG_MTIMES = {}
_PROCESSED_PO_FILES = set()


# ==========================================
# 1. REUSABLE HTTP SERVER FOR HEALTH CHECKS
# ==========================================
class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/health", "/", "/healthz"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","service":"wholesale_mro_engine","engine":"v7_6_self_healing_orchestrator"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", 8080))
    for attempt in range(5):
        try:
            server = ReusableHTTPServer(("0.0.0.0", port), HealthCheckHandler)
            logger.info(f"Native container /health listener running on port {port}.")
            server.serve_forever()
            break
        except OSError as e:
            logger.warning(f"Health server port {port} bind attempt {attempt+1} failed: {e}. Retrying in 2s...")
            time.sleep(2)


# ==========================================
# 2. STORAGE & UPSTREAM IP SHIELD
# ==========================================
def get_ip_blocklist_path() -> Path:
    if PRIMARY_IP_FILE.parent.exists():
        return PRIMARY_IP_FILE
    FALLBACK_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
    return FALLBACK_IP_FILE


def clean_brand_text(brand: str) -> str:
    if not brand: return ""
    b = str(brand).upper().strip()
    b = re.sub(r'[^A-Z0-9\s]', ' ', b)
    b = re.sub(r'\b(LLC|INC|CORP|CORPORATION|CO|COMPANY|LTD|HOLDINGS|USA|ENTERPRISES)\b', '', b)
    return re.sub(r'\s+', ' ', b).strip()


def load_ip_blocklist_cached() -> set:
    global _IP_CACHE_DATA, _IP_CACHE_MTIME
    target = get_ip_blocklist_path()
    if not target.exists(): return set()

    try:
        mtime = target.stat().st_mtime
        with ip_cache_lock:
            if _IP_CACHE_DATA is not None and mtime == _IP_CACHE_MTIME:
                return _IP_CACHE_DATA

            raw = json.loads(target.read_text(encoding="utf-8"))
            brand_list = raw if isinstance(raw, list) else raw.get("brands", [])
            normalized = {clean_brand_text(b) for b in brand_list if b}
            _IP_CACHE_DATA = normalized
            _IP_CACHE_MTIME = mtime
            logger.info(f"Loaded {len(_IP_CACHE_DATA)} normalized brands into active IP shield cache.")
            return _IP_CACHE_DATA
    except Exception as e:
        logger.error(f"Error reading IP blocklist: {e}")
        return _IP_CACHE_DATA or set()


def is_brand_ip_blocked(brand_name: str) -> bool:
    clean = clean_brand_text(brand_name)
    if not clean: return False
    blocked = load_ip_blocklist_cached()
    if clean in blocked: return True
    for b in blocked:
        if b and (b == clean or b in clean.split() or clean in b.split()):
            return True
    return False


def append_brand_to_blocklist(brand: str) -> bool:
    brand_raw = (brand or "").strip()
    if not brand_raw: return False
    target = get_ip_blocklist_path()
    data = {"version": "6.0.0", "updated_at": "2026-11-02", "brands": [], "skus": []}

    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                data["brands"] = [str(b).strip().upper() for b in raw if str(b).strip()]
            elif isinstance(raw, dict):
                data["brands"] = [str(b).strip().upper() for b in raw.get("brands", []) if str(b).strip()]
                data["skus"] = raw.get("skus", [])
        except Exception:
            pass

    existing = set(b.upper() for b in data["brands"])
    brand_upper = brand_raw.upper()
    if brand_upper not in existing:
        data["brands"].append(brand_upper)
        data["brands"] = sorted(list(set(data["brands"])))
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"Appended brand '{brand_upper}' to IP blocklist at {target}")

        global _IP_CACHE_DATA
        with ip_cache_lock:
            _IP_CACHE_DATA = None
        return True
    return False


def load_telegram_decisions() -> dict:
    if TELEGRAM_DECISIONS_FILE.exists():
        try:
            d = json.loads(TELEGRAM_DECISIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict): return d
        except Exception:
            pass
    return {}


def save_telegram_decisions(data: dict):
    with decisions_lock:
        TELEGRAM_DECISIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def find_lead_metadata(asin: str) -> dict:
    search_files = [
        LEAD_DECISIONS_FILE,
        REPORTS_DIR / "reverse_prime_monopoly_leads.json",
        REPORTS_DIR / "reverse_unmatched_targets.json",
        DATA_DIR / "hunter_engine.db"
    ]

    for sf in search_files[:3]:
        if not sf.exists(): continue
        try:
            raw = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                match = next((item for item in raw if isinstance(item, dict) and item.get("asin") == asin), None)
                if match: return match
            elif isinstance(raw, dict):
                if asin in raw: return raw[asin]
                for v in raw.values():
                    if isinstance(v, dict) and v.get("asin") == asin:
                        return v
        except Exception:
            pass

    db_file = DATA_DIR / "hunter_engine.db"
    if db_file.exists():
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT asin, title, brand, sales_rank, avg90_bb, current_bb, drops_90, monthly_sold,
                       amazon_pack_qty, referral_pct, fba_fee, weight_lbs, num_sellers, is_hazmat
                FROM candidate_cache WHERE asin = ?
            """, (asin,))
            row = cur.fetchone()
            if row:
                conn.close()
                bb = float(row["avg90_bb"] or row["current_bb"] or 0.0)
                sold_val = row["monthly_sold"] or 0
                drops_val = row["drops_90"] or 0
                monthly_units = int(sold_val if sold_val >= 50 else max(sold_val, drops_val / 3.0))
                brand_name = row["brand"] or "MRO Commercial"
                fba_sellers = max(0, int(row["num_sellers"] or 0))

                valid_comp, _, our_share = validate_fba_competition(monthly_units, fba_sellers)
                if not valid_comp:
                    our_share = monthly_units / max(1, fba_sellers + 1)

                logistics = get_logistics_profile(brand_name)
                weight = float(row["weight_lbs"] or 1.0)
                is_hazmat = bool(row["is_hazmat"])
                prep_base = logistics["prep_base"] + (0.60 if is_hazmat else 0.0)
                freight_rate = logistics["freight_per_lb"] + (0.20 if is_hazmat else 0.0)
                inbound_freight = round(weight * freight_rate, 2)
                total_prep = round(prep_base + inbound_freight + 0.27, 2)

                overhead = round((bb * float(row["referral_pct"] or 0.15)) + float(row["fba_fee"] or 4.15) + total_prep + (bb * 0.05), 2)
                payout = round(bb - overhead, 2)

                floor, req_roi = calculate_sliding_profit_floor(our_share)
                mac = round(max(0.0, payout - floor), 2)
                net_profit = round(payout - mac, 2)
                roi = round((net_profit / (mac + total_prep)) * 100.0, 1) if (mac + total_prep) > 0 else 0.0
                monthly_pool = round(our_share * net_profit, 2)

                return {
                    "asin": row["asin"],
                    "title": row["title"] or "Commercial SKU",
                    "brand": brand_name,
                    "pack_count": row["amazon_pack_qty"] or 1,
                    "sales_rank": row["sales_rank"] or 0,
                    "monthly_velocity": monthly_units,
                    "our_share_velocity": int(our_share),
                    "competitive_fba": fba_sellers,
                    "buybox_price": bb,
                    "wholesale_cost": mac,
                    "vendor": logistics["facility"],
                    "prep_facility": logistics["facility"],
                    "net_profit": net_profit,
                    "roi_percent": roi,
                    "profit_floor": floor,
                    "req_roi": req_roi,
                    "monthly_profit_pool": monthly_pool,
                    "gating_status": "UNGATED"
                }
            conn.close()
        except Exception:
            pass

    return {"asin": asin, "title": "Pending Manual Lookup", "brand": "MRO Commercial"}


def auto_seed_anti_regate_anchor(asin: str, brand: str, ref_price: float, vendor: str = "Wholesale"):
    try:
        from tools.anti_regating_pulse_daemon import load_registry, save_registry
        registry = load_registry()
        brands = registry.setdefault("monitored_brands", {})
        clean_brand = brand.strip()

        if clean_brand and clean_brand not in brands:
            brands[clean_brand] = {
                "distributor": vendor,
                "approved_date": str(datetime.date.today()),
                "requires_ungate": True,
                "anchor_asin": asin,
                "anchor_sku": f"KEEPER-{clean_brand[:6].upper().replace(' ', '')}-{asin}",
                "buy_box_reference": float(ref_price or 20.0),
                "last_pulse_date": str(datetime.date.today()),
                "active_pulse": False,
                "pulse_end_date": None
            }
            save_registry(registry)
            logger.info(f"🛡️ Auto-registered brand anchor for Anti-Regate protection: {clean_brand} ({asin})")
    except Exception as e:
        logger.debug(f"Anti-regate auto-seed notice: {e}")


def append_to_buy_list_sheet(asin: str, status: str = "PENDING_VERIFICATION"):
    lead_data = find_lead_metadata(asin)
    brand = lead_data.get("brand", "MRO Commercial")

    if is_brand_ip_blocked(brand):
        logger.warning(f"Blocked ASIN {asin} from sheet write: Brand '{brand}' is in IP blocklist.")
        return

    title = lead_data.get("title", "Unknown")
    pack_count = lead_data.get("pack_count", lead_data.get("pack_qty", 1))
    sales_rank = lead_data.get("sales_rank", 0)
    buybox_price = lead_data.get("buybox_price", lead_data.get("target_prime_price", 0.0))
    wholesale_cost = lead_data.get("wholesale_cost", lead_data.get("unit_cost", 0.0))
    vendor = lead_data.get("vendor", "Distributor")
    net_profit = lead_data.get("net_profit", 0.0)
    roi_percent = lead_data.get("roi_percent", 0.0)
    gating_status = lead_data.get("gating_status", "UNGATED")
    monthly_pool = lead_data.get("monthly_profit_pool", 0.0)

    logistics = get_logistics_profile(vendor)

    try:
        csv_row = [
            asin, title, brand, pack_count, "MRO Commercial", sales_rank,
            lead_data.get("monthly_velocity", 0), lead_data.get("deal_rating", 85),
            buybox_price, wholesale_cost, vendor, f"https://www.amazon.com/dp/{asin}",
            "Yes" if "bci" in vendor.lower() else "No", wholesale_cost, logistics["facility"],
            f"Approved via Telegram - Net: ${net_profit:.2f} ({roi_percent:.1f}% ROI) | Pool: ${monthly_pool:.2f}/mo | Gate: {gating_status}",
            status
        ]

        df_row = pd.DataFrame([csv_row])
        if not SAVED_BUY_LIST_CSV.exists():
            df_row.to_csv(SAVED_BUY_LIST_CSV, index=False, header=False)
        else:
            df_row.to_csv(SAVED_BUY_LIST_CSV, mode="a", index=False, header=False)
        logger.info(f"Recorded ASIN {asin} [Status: {status}] to saved_buy_list.csv")
    except Exception as e:
        logger.error(f"Failed to record buy list CSV row: {e}")

    webhook_url = get_verified_webhook_url()
    if webhook_url:
        try:
            payload = {
                "action": "ADD_BUY_LEAD",
                "asin": asin,
                "title": title,
                "brand": brand,
                "vendor": vendor,
                "prep_center": logistics["facility"],
                "buy_box_price": buybox_price,
                "wholesale_cost": wholesale_cost,
                "net_profit": net_profit,
                "roi_percent": roi_percent,
                "sales_rank": sales_rank,
                "monthly_velocity": lead_data.get("monthly_velocity", 0),
                "our_share_velocity": lead_data.get("our_share_velocity", 0),
                "monthly_profit_pool": monthly_pool,
                "status": status,
                "gating_status": gating_status,
                "order_url": f"https://www.amazon.com/dp/{asin}",
                "seller_check_url": f"https://sellercentral.amazon.com/product-search/search?q={asin}",
                "seller_approval_url": f"https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin={asin}",
                "notes": f"Pool: ${monthly_pool:.2f}/mo | Net: ${net_profit:.2f} ({roi_percent:.1f}% ROI) | Hub: {logistics['facility']}"
            }
            res = requests.post(webhook_url, json=payload, timeout=20, allow_redirects=True)
            logger.info(f"Google Sheet Webhook Sync for {asin}: HTTP {res.status_code}")
        except Exception as w_err:
            logger.warning(f"Google Sheet Webhook Sync Notice: {w_err}")


# ==========================================
# 3. EXCLUSIVE MASTER TELEGRAM WORKER
# ==========================================
def verify_telegram_bot_identity() -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("No TELEGRAM_BOT_TOKEN configured.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("ok"):
            bot_info = data.get("result", {})
            logger.info(f"Telegram Bot Verified: @{bot_info.get('username')} (ID: {bot_info.get('id')})")
            return True
        else:
            logger.error(f"Telegram Bot Verification FAILED: {data.get('description')}")
            return False
    except Exception as e:
        logger.error(f"Telegram Bot check exception: {e}")
        return False


def purge_lingering_webhooks():
    if not TELEGRAM_BOT_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
        res = requests.post(url, json={"drop_pending_updates": True}, timeout=10)
        if res.json().get("ok"):
            logger.info("Telegram webhook cleanly purged. Polling worker ready.")
    except Exception as e:
        logger.warning(f"Webhook purge request warning: {e}")


def send_telegram_alert(text: str, reply_markup: dict = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = requests.post(url, json=payload, timeout=10)
        return res.json().get("ok", False)
    except Exception as e:
        logger.warning(f"Failed to send Telegram alert: {e}")
        return False


def run_master_telegram_listener():
    if not TELEGRAM_BOT_TOKEN or not verify_telegram_bot_identity():
        logger.warning("Telegram worker disabled due to missing/invalid token.")
        return

    purge_lingering_webhooks()
    logger.info("Exclusive Master Telegram polling loop active.")
    last_update_id = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            r = requests.get(url, params=params, timeout=25).json()

            for update in r.get("result", []):
                last_update_id = update["update_id"]
                cb = update.get("callback_query")
                if not cb: continue

                cb_id = cb.get("id")
                data = cb.get("data", "")
                msg = cb.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                msg_id = msg.get("message_id")
                orig_text = msg.get("text", "")

                if data == "noop":
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        json={"callback_query_id": cb_id, "text": "This lead has already been reviewed.", "show_alert": False},
                        timeout=5
                    )
                    continue

                parts = data.split(":", 2)
                if len(parts) < 2: continue

                action = parts[0]
                asin = parts[1].strip().upper()
                brand_context = parts[2].strip() if len(parts) > 2 else ""
                now_ts = int(time.time())

                with decisions_lock:
                    decisions = load_telegram_decisions()

                    if action == "BUYLIST":
                        decisions[asin] = {
                            "status": "PENDING_VERIFICATION",
                            "action": "BUY_LIST_INITIATED",
                            "timestamp": now_ts,
                            "verified": False,
                            "expires_ts": None
                        }
                        status_text = "⏳ QUEUED FOR BUY LIST"
                        append_to_buy_list_sheet(asin, status="PENDING_VERIFICATION")

                        lead_meta = find_lead_metadata(asin)
                        auto_seed_anti_regate_anchor(
                            asin=asin,
                            brand=lead_meta.get("brand", brand_context),
                            ref_price=lead_meta.get("buybox_price", 20.0),
                            vendor=lead_meta.get("vendor", "Wholesale")
                        )

                    elif action in ["BLOCKIP", "BLOCKAMAZON"]:
                        decisions[asin] = {
                            "status": "IP_BLOCKED",
                            "brand": brand_context,
                            "timestamp": now_ts,
                            "expires_ts": None
                        }
                        if brand_context:
                            append_brand_to_blocklist(brand_context)
                        tag = "🚫 BLOCKED AMAZON DIRECT" if action == "BLOCKAMAZON" else "🚫 BLOCKED IP"
                        status_text = f"{tag} ({brand_context})" if brand_context else tag

                    elif action in ["DENY", "PASS"]:
                        decisions[asin] = {
                            "status": "DENIED",
                            "timestamp": now_ts,
                            "expires_ts": now_ts + (180 * 86400)
                        }
                        status_text = "⛔ DECLINED (Muted 180 Days)"
                    else:
                        status_text = "REVIEWED"

                    save_telegram_decisions(decisions)

                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        json={"callback_query_id": cb_id, "text": f"Saved: {status_text}", "show_alert": False},
                        timeout=5
                    )

                    locked_text = f"🔒 *[DECISION LOCKED: {status_text}]*\n━━━━━━━━━━━━━━━━━━━━\n" + orig_text
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": locked_text,
                            "parse_mode": "Markdown",
                            "reply_markup": {
                                "inline_keyboard": [
                                    [{"text": f"✅ {status_text}", "callback_data": "noop"}]
                                ]
                            }
                        },
                        timeout=5
                    )
                    logger.info(f"Feedback Action Logged: {asin} -> {status_text}")

        except Exception as e:
            logger.warning(f"Telegram polling exception: {e}")
            time.sleep(5)


# =========================================================================
# 4. SEGREGATED TWO-TIER CATALOG WATCHER & AUTO-NORMALIZER (DYNAMIC ROUTING)
# =========================================================================
def sync_distributor_database():
    drive_sync = ROOT_DIR / "tools" / "sync_drive_manufacturer_catalogs.py"
    if drive_sync.exists():
        logger.info("Executing Google Drive Manufacturer_Catalogs sync on startup...")
        try:
            subprocess.run([sys.executable, str(drive_sync)], check=False, timeout=90)
        except Exception as exc:
            logger.warning(f"Drive sync startup notice: {exc}")

    for name in ["sync_distributor_catalogs.py", "distributor_credential_sync.py"]:
        sync_script = ROOT_DIR / "tools" / name
        if sync_script.exists():
            logger.info(f"Executing distributor catalog sync via {name}...")
            try:
                subprocess.run([sys.executable, str(sync_script)], check=False, timeout=60)
                break
            except Exception as exc:
                logger.warning(f"Distributor sync notice: {exc}")


def trigger_candidate_rescore():
    logger.info("⚡ Triggering candidate re-score against updated two-tier catalog index...")
    script = (
        "import sys; sys.path.insert(0, '.'); "
        "from tools.reverse_amazon_finder import ReverseAmazonHunter; "
        "h = ReverseAmazonHunter(); h.run_sweep()"
    )
    cmd = [sys.executable, "-c", script]
    try:
        subprocess.Popen(cmd)
    except Exception as e:
        logger.warning(f"Failed to trigger background re-score: {e}")


def run_catalog_watcher():
    logger.info("Two-Tier Catalog Hot-Reload Watcher is ACTIVE.")
    normalizer_script = ROOT_DIR / "tools" / "catalog_normalizer.py"

    watch_targets = [
        (MFR_INCOMING, MFR_INDEXED, "MANUFACTURER"),
        (WHOLESALE_INCOMING, WHOLESALE_INDEXED, "WHOLESALE"),
        (CATALOG_DIR / "incoming", WHOLESALE_INDEXED, "WHOLESALE"),
        (CATALOG_DIR, WHOLESALE_INDEXED, "WHOLESALE")
    ]

    while True:
        try:
            detected_changes = False
            for in_dir, out_dir, tier in watch_targets:
                if not in_dir.exists(): continue
                for ext in ("*.xlsx", "*.xls", "*.csv"):
                    for f in in_dir.glob(ext):
                        if "indexed" in f.parts or "normalized" in f.name.lower() or "processed" in f.parts:
                            continue

                        try:
                            mtime = f.stat().st_mtime
                            f_key = str(f.resolve())
                            if f_key not in _KNOWN_CATALOG_MTIMES or _KNOWN_CATALOG_MTIMES[f_key] < mtime:
                                logger.info(f"⚡ [{tier} TIER] New price list detected: {f.name}. Auto-normalizing to {out_dir.name}...")
                                time.sleep(1)

                                if normalizer_script.exists():
                                    cmd = [sys.executable, str(normalizer_script), str(f), str(out_dir)]
                                    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                                    if res.returncode == 0:
                                        logger.info(f"✅ Successfully normalized [{tier}]: {f.name} -> {out_dir.name}")
                                        _KNOWN_CATALOG_MTIMES[f_key] = mtime
                                        detected_changes = True
                                    else:
                                        logger.error(f"❌ Failed normalizing {f.name}:\n{res.stderr or res.stdout}")
                        except Exception as file_err:
                            logger.debug(f"Catalog scan file error: {file_err}")

            if detected_changes:
                trigger_candidate_rescore()

            time.sleep(15)
        except Exception as e:
            logger.warning(f"Catalog watcher loop notice: {e}")
            time.sleep(15)


# =========================================================================
# 5. PERIODIC REORDER & PRE-PO GENERATOR DAEMON (DECOUPLED LOGISTICS ROUTING)
# =========================================================================
def check_and_notify_new_purchase_orders():
    global _PROCESSED_PO_FILES
    if not PO_DIR.exists(): return

    for po_file in PO_DIR.glob("PO-*.json"):
        if po_file.name in _PROCESSED_PO_FILES: continue

        try:
            data = json.loads(po_file.read_text(encoding="utf-8"))
            po_num = data.get("po_number", po_file.stem)
            distributor = data.get("distributor", "Wholesale")
            total_units = data.get("total_units", 0)
            total_cost = data.get("total_cost", 0.0)

            logistics = get_logistics_profile(distributor)
            destination_3pl = data.get("destination_3pl", logistics["facility"])
            line_items = data.get("line_items", [])

            item_preview = ""
            for item in line_items[:3]:
                strategy_tag = "📦 Optimized Split" if item.get("inbound_strategy") == "AMAZON_OPTIMIZED_SPLIT" else f"📍 Hub: {destination_3pl}"
                item_preview += f"\n• `{item.get('sku')}`: {item.get('reorder_quantity')} units ({item.get('case_breakdown')})\n  ↳ _{strategy_tag}_"

            msg = (
                f"🚨 *NEW PURCHASE ORDER GENERATED*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"*PO Number:* `{po_num}`\n"
                f"*Distributor:* `{distributor}`\n"
                f"*Total Volume:* `{total_units}` units (${total_cost:,.2f})\n"
                f"*Routing 3PL:* `{destination_3pl}`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"*Line Items:* {item_preview}\n\n"
                f"📄 Work order JSON ready in `data/purchase_orders/{po_file.name}`."
            )
            send_telegram_alert(msg)
            _PROCESSED_PO_FILES.add(po_file.name)
            logger.info(f"Dispatched Telegram PO notification for {po_num}")
        except Exception as err:
            logger.warning(f"Error reading PO file {po_file.name}: {err}")


def run_periodic_reorder_worker():
    reorder_script = ROOT_DIR / "tools" / "reorder_po_generator.py"
    if not reorder_script.exists():
        logger.info("reorder_po_generator.py not found. Reorder daemon in standby.")
        return

    logger.info(f"Periodic Reorder & Pre-PO Worker ACTIVE (Interval: {REORDER_INTERVAL_SECONDS}s).")
    time.sleep(30)

    while True:
        try:
            logger.info("🔄 Running scheduled Inventory Reorder & Pre-PO Price-Scrub pass...")
            res = subprocess.run(
                [sys.executable, str(reorder_script)],
                capture_output=True,
                text=True,
                check=False,
                timeout=180
            )
            if res.returncode == 0:
                logger.info("Reorder evaluation pass completed successfully.")
                check_and_notify_new_purchase_orders()
            else:
                logger.warning(f"Reorder script completed with returncode {res.returncode}:\n{res.stderr or res.stdout}")
        except Exception as e:
            logger.warning(f"Reorder worker exception: {e}")

        time.sleep(REORDER_INTERVAL_SECONDS)


# ==========================================
# 6. SUPERVISOR WATCHDOG (SEVEN-WORKER)
# ==========================================
def spawn_worker(cmd_args: list, log_file: Path) -> subprocess.Popen:
    log_fp = open(log_file, "a")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Ensure correct webhook URL is propagated to every subprocess
    env["BUY_LIST_WEBHOOK_URL"] = get_verified_webhook_url()
    env["GOOGLE_SHEET_WEBHOOK_URL"] = get_verified_webhook_url()
    return subprocess.Popen(
        cmd_args,
        stdout=log_fp,
        stderr=log_fp,
        env=env
    )


def main():
    logger.info(f"Initializing Master Pipeline Orchestrator v7.6 (Target Webhook: {get_verified_webhook_url()})...")

    # 1. Warm up the in-memory IP blacklist
    load_ip_blocklist_cached()

    # 2. Start HTTP Health Server on $PORT
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    # 3. Start Exclusive Telegram Polling Loop
    telegram_thread = threading.Thread(target=run_master_telegram_listener, daemon=True)
    telegram_thread.start()

    # 4. Run Startup Catalog Sync Pass
    sync_distributor_database()

    # 5. Start Embedded Hot-Reload Catalog Watcher Thread
    watcher_thread = threading.Thread(target=run_catalog_watcher, daemon=True)
    watcher_thread.start()

    # 6. Start Embedded Periodic Reorder & Pre-PO Worker Thread
    reorder_thread = threading.Thread(target=run_periodic_reorder_worker, daemon=True)
    reorder_thread.start()

    # 7. Configure Supervised Background Workers
    workers = [
        ("ReverseAmazonHunter", [sys.executable, "-u", str(ROOT_DIR / "tools" / "reverse_amazon_finder.py")], HUNTER_LOG_FILE),
        ("SheetSyncDaemon", [sys.executable, "-u", str(ROOT_DIR / "tools" / "sync_today_leads_to_sheet.py"), "--daemon"], SHEET_SYNC_LOG_FILE),
        ("WebDistributorScout", [sys.executable, "-u", str(ROOT_DIR / "tools" / "web_distributor_scout.py")], SCOUT_LOG_FILE),
        ("AutonomousRecruiter", [sys.executable, "-u", str(ROOT_DIR / "tools" / "autonomous_recruiter.py")], RECRUITER_LOG_FILE),
        ("DriveManufacturerSync", [sys.executable, "-u", str(ROOT_DIR / "tools" / "sync_drive_manufacturer_catalogs.py"), "--daemon"], DRIVE_SYNC_LOG_FILE),
        ("AntiRegateDaemon", [sys.executable, "-u", str(ROOT_DIR / "tools" / "anti_regating_pulse_daemon.py"), "--daemon"], ANTI_REGATE_LOG_FILE)
    ]
    procs = {name: None for name, _, _ in workers}

    # 8. Persistent Watchdog Loop
    while True:
        try:
            for name, cmd, log_f in workers:
                script_path = Path(cmd[2])
                if script_path.exists():
                    p = procs[name]
                    if p is None or p.poll() is not None:
                        if p is not None:
                            logger.warning(f"{name} exited with code {p.poll()}. Respawning in 5s...")
                            time.sleep(5)
                        logger.info(f"Starting child worker: {name} -> logging to {log_f.name}")
                        procs[name] = spawn_worker(cmd, log_f)

            time.sleep(30)

        except KeyboardInterrupt:
            logger.info("Terminating all child workers...")
            for p in procs.values():
                if p and p.poll() is None:
                    p.terminate()
            break
        except Exception as e:
            logger.error(f"Supervisor watchdog loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
