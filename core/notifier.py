import os
import sys
import glob
import json
import pandas as pd
from config import KEEPA_API_KEY
from core.keepa_client import KeepaClient
from core.profit_engine import ProfitFirstEngine
from core.state_manager import StateManager
from core.normalizer import SmartCatalogNormalizer
from core.email_fetcher import EmailCatalogFetcher, DISTRIBUTORS_FILE
from core.notifier import AlertDispatcher

def process_catalog_file(file_path: str, min_roi: float = 35.0, moq: float = 3000.00):
    print(f"\n[*] Processing catalog: {file_path} (Target MOQ: ${moq:,.2f})")
    try:
        normalized_df = SmartCatalogNormalizer.normalize(file_path)
    except Exception as e:
        print(f"[!] Skipping {file_path} - Normalization error: {e}")
        return

    client = KeepaClient(api_key=KEEPA_API_KEY)
    engine = ProfitFirstEngine(keepa_client=client)

    po_df = engine.scan_catalog_for_profit(normalized_df, min_roi=min_roi, moq=moq)
    
    if po_df.empty:
        print(f"[!] No products in {file_path} met safety and ROI thresholds.")
        return

    # 1. Save PO CSV locally and commit-ready
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join("data", "output", f"PO_{base_name}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    po_df.to_csv(out_path, index=False)

    total_val = po_df['line_total'].sum()
    sample_count = len(po_df[po_df["type"] == "UNGATE_SAMPLE_KEY"])
    profit_count = len(po_df[po_df["type"] == "PROFIT_INVENTORY"])
    peak_roi = po_df["roi_%"].max()

    print(f"\n[✓] Purchase order generated successfully: {out_path}")
    print(f"Total Lines: {len(po_df)} | Total Cart Value: ${total_val:,.2f}")

    # 2. Format and dispatch Telegram / Email alert
    catalog_name = os.path.basename(file_path)
    alert_text = (
        f"🎯 *New Purchase Order Generated!*\n\n"
        f"• *Catalog:* `{catalog_name}`\n"
        f"• *Total Order Value:* `${total_val:,.2f}`\n"
        f"• *Profit SKUs:* `{profit_count}` | *Ungate Keys:* `{sample_count}`\n"
        f"• *Peak ROI:* `{peak_roi}%`\n\n"
        f"The complete PO spreadsheet with one-click Amazon ungate links is attached below."
    )

    dispatcher = AlertDispatcher()
    dispatcher.send_telegram_alert(alert_text, file_path=out_path)
    dispatcher.send_email_alert(
        subject=f"New Wholesale PO: {catalog_name} (${total_val:,.2f})",
        body=alert_text.replace("*", "").replace("`", ""),
        file_path=out_path
    )

def run_auto_mode():
    """Executed by GitHub Actions or local automated schedules."""
    fetcher = EmailCatalogFetcher()
    downloaded_jobs = fetcher.download_catalog_attachments()
    
    for job in downloaded_jobs:
        f_path = job["file_path"]
        dist_moq = job["distributor"].get("moq", 3000.00)
        process_catalog_file(f_path, moq=dist_moq)

    # Process any new spreadsheets dropped into data/incoming/
    pending_files = glob.glob("data/incoming/*.csv") + glob.glob("data/incoming/*.xlsx")
    for f in pending_files:
        if not any(j["file_path"] == f for j in downloaded_jobs):
            process_catalog_file(f)

    # Fallback to test catalog if inbox and incoming directory are empty
    if not downloaded_jobs and not pending_files:
        sample_path = os.path.join("data", "sample_catalog.csv")
        if os.path.exists(sample_path):
            process_catalog_file(sample_path)

def print_help():
    print("""
Wholesale ROI-First Engine CLI
==============================
Usage:
  python main.py --auto                          Run automated cloud polling & notifications
  python main.py --catalog <path>                Process a specific distributor CSV/Excel
  python main.py --verify-amazon <brand>         Verify brand is approved in Seller Central
  python main.py --add-distributor <n> <e> <moq> Register a new distributor (Option A)
  python main.py --list-distributors             View all registered distributors
  python main.py --status                        Inspect verified vs pending brands
    """)

if __name__ == "__main__":
    sm = StateManager()

    if len(sys.argv) > 1:
        flag = sys.argv[1].lower()

        if flag == "--auto":
            run_auto_mode()

        elif flag == "--verify-amazon" and len(sys.argv) > 2:
            brand_name = " ".join(sys.argv[2:])
            sm.verify_ungated_on_amazon(brand_name)

        elif flag == "--add-distributor" and len(sys.argv) >= 5:
            name = sys.argv[2]
            email_addr = sys.argv[3]
            moq_val = float(sys.argv[4])
            EmailCatalogFetcher.register_distributor(name, email_addr, moq_val)

        elif flag == "--list-distributors":
            if os.path.exists(DISTRIBUTORS_FILE):
                with open(DISTRIBUTORS_FILE, "r") as f:
                    d_data = json.load(f)
                print("\n--- REGISTERED DISTRIBUTORS (SAVED ON GITHUB) ---")
                for d in d_data.get("distributors", []):
                    print(f" • {d['name']} | MOQ: ${d.get('moq', 0):,.2f} | Domain: {d.get('domain_match')}")
            else:
                print("No distributors.json file found.")

        elif flag == "--status":
            state = sm.load_state()
            print("\n=== AMAZON SELLER CENTRAL VERIFIED UNGATED ===")
            verified = state.get("verified_ungated_brands", [])
            for b in verified:
                print(f" [APPROVED] {b.title()}")
            print(f"Total Approved: {len(verified)}")

            print("\n=== PENDING INVOICE APPROVAL (DO NOT BUY IN BULK) ===")
            pending = state.get("pending_invoice_review", [])
            for p in pending:
                print(f" [WAITING REVIEW] {p.title()}")
            print(f"Total Pending: {len(pending)}")

        elif flag == "--catalog" and len(sys.argv) > 2:
            process_catalog_file(sys.argv[2])

        else:
            print_help()
    else:
        sample_path = os.path.join("data", "sample_catalog.csv")
        process_catalog_file(sample_path)
