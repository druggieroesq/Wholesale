mkdir -p /app/tools && cat << 'EOF' > /app/tools/patch_and_purge_dtc.py
#!/usr/bin/env python3
"""
tools/patch_and_purge_dtc.py
----------------------------
1. Injects known private-label / DTC brands into data/ip_blocklist.json.
2. Purges bad ASINs from hunter_engine.db (candidate_cache, verified_deals, sheet_synced_leads).
3. Posts a PURGE_ASINS action to the Google Apps Script Web App endpoint.
"""

import json
import sqlite3
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

ip_file = ROOT_DIR / "data" / "ip_blocklist.json"
db_file = ROOT_DIR / "data" / "hunter_engine.db"

RESTRICTED_DTC_BRANDS = [
    "VEVOR",
    "CANTONAPE",
    "AUTOONE",
    "VIVOHOME",
    "NOCO",
    "HULKMAN",
    "A-IPOWER",
    "JACKERY",
    "BLUETTI",
    "ECOFLOW",
    "DEWALT_CLONE"
]

CORRUPTED_ASINS = [
    "B0DPHF1P9V",
    "B07LBLDZ89",
    "B0FBRV118J",
    "B0DLKP7BNY",
    "B0DPQ7LV8B"
]

# 1. Update IP Blocklist
print("--- [1/3] Updating IP Blocklist ---")
ip_file.parent.mkdir(parents=True, exist_ok=True)
if not ip_file.exists():
    data = {"version": "6.1.0", "updated_at": "2026-11-05", "brands": [], "skus": []}
else:
    try:
        data = json.loads(ip_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"version": "6.1.0", "brands": data, "skus": []}
    except Exception:
        data = {"version": "6.1.0", "brands": [], "skus": []}

existing_brands = set(b.upper() for b in data.get("brands", []))
newly_added = []

for b in RESTRICTED_DTC_BRANDS:
    b_upper = b.upper().strip()
    if b_upper not in existing_brands:
        data["brands"].append(b_upper)
        newly_added.append(b_upper)

data["brands"] = sorted(list(set(data["brands"])))
data["updated_at"] = "2026-11-05"
ip_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
print(f"Added {len(newly_added)} DTC brands to {ip_file.name}: {newly_added}")

# 2. Purge from SQLite Database
print("\n--- [2/3] Purging SQLite Candidate Cache ---")
if db_file.exists():
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    
    for asin in CORRUPTED_ASINS:
        cur.execute("DELETE FROM candidate_cache WHERE asin = ?", (asin,))
        cur.execute("DELETE FROM verified_deals WHERE asin = ?", (asin,))
        cur.execute("DELETE FROM sheet_synced_leads WHERE asin = ?", (asin,))
    
    purged_brands_count = 0
    for b in RESTRICTED_DTC_BRANDS:
        cur.execute("DELETE FROM candidate_cache WHERE UPPER(brand) LIKE ?", (f"%{b}%",))
        purged_brands_count += cur.rowcount
        cur.execute("DELETE FROM verified_deals WHERE UPPER(brand) LIKE ?", (f"%{b}%",))
    
    conn.commit()
    conn.close()
    print(f"Purged {len(CORRUPTED_ASINS)} corrupted ASINs and {purged_brands_count} DTC records from {db_file.name}.")
else:
    print(f"Database {db_file.name} not found, skipping SQLite clean.")

# 3. Trigger Google Sheet Webhook Purge
print("\n--- [3/3] Triggering Google Sheet Webhook Purge ---")
webhook_url = (os.getenv("BUY_LIST_WEBHOOK_URL") or os.getenv("GOOGLE_SHEET_WEBHOOK_URL") or "").strip().strip('"')
if not webhook_url or "/library/" in webhook_url or "PASTE_YOUR" in webhook_url:
    webhook_url = "https://script.google.com/macros/s/AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y/exec"

try:
    print(f"Targeting: {webhook_url}")
    res = requests.post(webhook_url, json={"action": "PURGE_ASINS", "asins": CORRUPTED_ASINS}, timeout=20, allow_redirects=True)
    print(f"Google Sheet Webhook Response: HTTP {res.status_code} | {res.text}")
except Exception as e:
    print(f"Webhook purge request failed: {e}")
EOF
chmod +x /app/tools/patch_and_purge_dtc.py
python3 /app/tools/patch_and_purge_dtc.py
