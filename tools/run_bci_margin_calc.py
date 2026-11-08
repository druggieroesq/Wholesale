import os
import pandas as pd

REPORTS_DIR = "/app/data/reports"
BCI_PARQUET = "/app/data/catalogs/wholesale_catalogs/indexed/bci_normalized.parquet"
MATCHED_CSV = os.path.join(REPORTS_DIR, "bci_matched_prime_steals.csv")
OUT_PATH = os.path.join(REPORTS_DIR, "bci_prime_steals_margin_analysis.csv")

leads = pd.read_csv(MATCHED_CSV)
bci = pd.read_parquet(BCI_PARQUET)

print(f"[*] Analyzing margins for {len(leads)} top leads...")
bci["clean_brand"] = bci["brand"].astype(str).str.strip().str.upper()

results = []
for _, row in leads.iterrows():
    b_matches = bci[bci["clean_brand"] == str(row["brand"]).strip().upper()]
    avg_cost = 0.0
    if not b_matches.empty:
        avg_cost = float(pd.to_numeric(b_matches["unit_cost"], errors="coerce").dropna().median())
    
    pack = int(row["pack_size"])
    retail = float(row["price"])
    est_fba_fee = 8.50 if pack > 1 else 5.40
    ref_fee = retail * 0.15
    total_cost = avg_cost * pack
    profit = retail - total_cost - ref_fee - est_fba_fee
    roi = (profit / total_cost) * 100 if total_cost > 0 else 0.0
    
    results.append({
        "asin": row["asin"],
        "brand": row["brand"],
        "pack_size": pack,
        "retail": retail,
        "wholesale_unit": round(avg_cost, 2),
        "total_cost": round(total_cost, 2),
        "net_profit": round(profit, 2),
        "roi_pct": round(roi, 1)
    })

res_df = pd.DataFrame(results)
res_df.to_csv(OUT_PATH, index=False)

print("=" * 84)
print("{:<12} | {:<10} | {:<4} | {:<8} | {:<8} | {:<10} | {:<7}".format(
    "ASIN", "Brand", "Pack", "Retail", "Cost", "Net Profit", "ROI"
))
print("-" * 84)
for r in results:
    print("{:<12} | {:<10} | {:<4} | ${:>7.2f} | ${:>7.2f} | ${:>9.2f} | {:>6.1f}%".format(
        r["asin"], r["brand"], r["pack_size"], r["retail"], r["total_cost"], r["net_profit"], r["roi_pct"]
    ))
print("=" * 84)
print(f"[+] Output saved to: {OUT_PATH}")
