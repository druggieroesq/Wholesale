import os
import sys
import pandas as pd
from config import KEEPA_API_KEY
from core.keepa_client import KeepaClient
from core.optimizer import UngateOptimizer
from core.state_manager import StateManager

def run_pipeline(catalog_path: str, output_path: str = "data/output/ungate_purchase_order.csv"):
    if not os.path.exists(catalog_path):
        print(f"[!] Catalog file not found at: {catalog_path}")
        return

    print(f"[*] Ingesting catalog from: {catalog_path}")
    df = pd.read_csv(catalog_path)
    df.columns = [c.lower().strip() for c in df.columns]

    client = KeepaClient(api_key=KEEPA_API_KEY)
    optimizer = UngateOptimizer(keepa_client=client)

    po_df = optimizer.process_catalog(df)
    
    if po_df.empty:
        print("[!] No new ungate candidates found or target MOQ already met.")
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    po_df.to_csv(output_path, index=False)
    print(f"[✓] Purchase order saved successfully to {output_path}")

def print_help():
    print("""
Wholesale Ungate Sniper CLI
============================
Usage:
  python main.py                    Run the ungate optimizer on default catalog
  python main.py --catalog <path>   Run the optimizer on a specific catalog CSV
  python main.py --ungate <brand>   Mark a brand as UNGATED (skips cheap keys permanently)
  python main.py --list             List all currently ungated and pending brands
    """)

if __name__ == "__main__":
    sm = StateManager()

    if len(sys.argv) > 1:
        flag = sys.argv[1].lower()

        if flag == "--ungate":
            if len(sys.argv) < 3:
                print("[!] Error: Please specify the brand name. Example: python main.py --ungate CeraVe")
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

        elif flag in ("--help", "-h"):
            print_help()
        else:
            print(f"[!] Unknown argument: {flag}")
            print_help()
    else:
        # Default run using sample catalog
        sample_csv = os.path.join("data", "sample_catalog.csv")
        run_pipeline(sample_csv)
