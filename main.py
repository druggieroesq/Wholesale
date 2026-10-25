import os
import pandas as pd
from config import KEEPA_API_KEY
from core.keepa_client import KeepaClient
from core.optimizer import UngateOptimizer

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

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    po_df.to_csv(output_path, index=False)
    print(f"[✓] Purchase order saved successfully to {output_path}")

if __name__ == "__main__":
    sample_csv = "data/sample_catalog.csv"
    run_pipeline(sample_csv)
