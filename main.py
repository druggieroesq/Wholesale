import os
import sys
import glob
from config import KEEPA_API_KEY
from core.keepa_client import KeepaClient
from core.profit_engine import ProfitFirstEngine
from core.state_manager import StateManager
from core.normalizer import SmartCatalogNormalizer
from core.email_fetcher import DistributorEmailWatcher

def process_file(file_path: str, min_roi: float = 35.0):
    print(f"\n[*] Normalizing file: {file_path}")
    try:
        normalized_df = SmartCatalogNormalizer.normalize(file_path)
    except Exception as e:
        print(f"[!] Error normalizing catalog: {e}")
        return

    client = KeepaClient(api_key=KEEPA_API_KEY)
    engine = ProfitFirstEngine(keepa_client=client)

    po_df = engine.scan_catalog_for_profit(normalized_df, min_roi=min_roi)
    
    if po_df.empty:
        print("[!] No products met the safety, velocity, and ROI thresholds.")
        return

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join("data", "output", f"PO_{base_name}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    po_df.to_csv(out_path, index=False)
    
    print(f"\n[✓] Purchase order generated: {out_path}")
    print(f"Total Lines: {len(po_df)} | Total Cart: ${po_df['line_total'].sum():,.2f}")

if __name__ == "__main__":
    sm = StateManager()

    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        # Mode 1: Automated Email Poller
        watcher = DistributorEmailWatcher()
        files = watcher.fetch_latest_catalogs()
        for f in files:
            process_file(f)

    elif len(sys.argv) > 1 and sys.argv[1] == "--ungate":
        brand_arg = " ".join(sys.argv[2:])
        sm.mark_brand_as_ungated(brand_arg)

    elif len(sys.argv) > 1 and sys.argv[1] == "--list":
        state = sm.load_state()
        print("\n--- UNGATED BRANDS ---")
        for b in state.get("ungated_brands", []):
            print(f" • {b.title()}")

    else:
        # Mode 2: Minimal Effort Manual
        # Checks data/incoming/ for any file, otherwise runs sample_catalog.csv
        incoming_files = glob.glob("data/incoming/*.csv") + glob.glob("data/incoming/*.xlsx")
        if incoming_files:
            latest_file = max(incoming_files, key=os.path.getctime)
            process_file(latest_file)
        else:
            sample_path = os.path.join("data", "sample_catalog.csv")
            process_file(sample_path)
