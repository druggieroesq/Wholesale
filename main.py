import os
import sys
import pandas as pd
from config import KEEPA_API_KEY
from core.keepa_client import KeepaClient
from core.profit_engine import ProfitFirstEngine
from core.state_manager import StateManager

def run_pipeline(catalog_path: str, output_path: str = "data/output/roi_first_purchase_order.csv", min_roi: float = 35.0):
    if not os.path.exists(catalog_path):
        print(f"[!] Catalog file not found at: {catalog_path}")
        return

    print(f"[*] Ingesting catalog from: {catalog_path}")
    df = pd.read_csv(catalog_path)
    df.columns = [c.lower().strip() for c in df.columns]

    client = KeepaClient(api_key=KEEPA_API_KEY)
    engine = ProfitFirstEngine(keepa_client=client)

    po_df = engine.scan_catalog_for_profit(df, min_roi=min_roi)
    
    if po_df.empty:
        print("[!] No products met the safety, velocity, and ROI thresholds.")
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    po_df.to_csv(output_path, index=False)
    print(f"\n[✓] High-ROI Purchase Order generated successfully: {output_path}")
    print("\n--- PURCHASE ORDER SUMMARY ---")
    summary = po_df.groupby("type")["line_total"].agg(["count", "sum"])
    print(summary)
    print(f"\nGrand Total: ${po_df['line_total'].sum():,.2f}")

def print_help():
    print("""
Wholesale ROI-First Engine CLI
==============================
Usage:
  python main.py                    Run ROI-first scanner on data/sample_catalog.csv
  python main.py --catalog <path>   Run scanner on a specific distributor CSV
  python main.py --roi <percent>    Set custom min ROI (e.g. python main.py --roi 45)
  python main.py --ungate <brand>   Mark a brand as UNGATED (buys directly for profit)
  python main.py --list             List all currently ungated and pending brands
    """)

if __name__ == "__main__":
    sm = StateManager()

    if len(sys.argv) > 1:
        flag = sys.argv[1].lower()

        if flag == "--ungate":
            if len(sys.argv) < 3:
                print("[!] Error: Specify brand name. Example: python main.py --ungate CeraVe")
            else:
                brand_arg = " ".join(sys.argv[2:])
                sm.mark_brand_as_ungated(brand_arg)

        elif flag == "--list":
            state = sm.load_state()
            ungated = state.get("ungated_brands", [])
            pending = state.get("pending_applications", [])
            print("\n--- UNGATED BRANDS ---")
            for b in ungated:
                print(f" • {b.title()}")
            print(f"\nTotal Ungated: {len(ungated)}")

            if pending:
                print("\n--- PENDING REVIEW ---")
                for p in pending:
                    print(f" • {p.title()}")

        elif flag == "--catalog" and len(sys.argv) > 2:
            custom_catalog = sys.argv[2]
            run_pipeline(custom_catalog)

        elif flag == "--roi" and len(sys.argv) > 2:
            custom_roi = float(sys.argv[2])
            sample_csv = os.path.join("data", "sample_catalog.csv")
            run_pipeline(sample_csv, min_roi=custom_roi)

        elif flag in ("--help", "-h"):
            print_help()
        else:
            print(f"[!] Unknown argument: {flag}")
            print_help()
    else:
        sample_csv = os.path.join("data", "sample_catalog.csv")
        run_pipeline(sample_csv)
