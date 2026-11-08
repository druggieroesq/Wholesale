import os
import sys
import json
import re
import pandas as pd

PARQUET_PATH = "/app/data/catalogs/wholesale_catalogs/indexed/bci_normalized.parquet"
OUTPUT_DIR = "/app/data/reports"
BLOCKLIST_JSON = "/app/data/ip_blocklist.json"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def canonical_name(s: str) -> str:
    """Strips all non-alphanumerics and lowercases: 'Arm & Hammer' -> 'armhammer'"""
    if not s:
        return ""
    # Replace 'and' with '' or keep it uniform
    clean = re.sub(r'\band\b', '', str(s).lower())
    return re.sub(r'[^a-z0-9]', '', clean)

# 1. Compile Comprehensive Normalized Blocklist
raw_blocklist = {
    "3M", "ARM & HAMMER", "ARM HAMMER", "ARM AND HAMMER", "BAG BALM", "BENEBONE", "BIL JAC",
    "BLACK FLAG", "BOEHRINGER INGELHEIM", "KONG", "FRONTGUARD", "FRONTLINE", "SERESTO",
    "ELANCO", "ZOETIS", "BAYER", "HARTZ", "PURINA", "GREENIES", "CHUCKIT", "PETSAFE",
    "RADIO SYSTEMS", "ANDIS", "WAHL", "OSTER", "SCOTCH", "COMMAND", "POST-IT", "STANLEY",
    "DEWALT", "MILWAUKEE", "MAKITA", "IRWIN", "CRAFTSMAN", "KIDDE", "FIRST ALERT",
    "BALL", "CADET", "BONIDE"  # High-scrutiny brands added for defensive posture
}

if os.path.exists(BLOCKLIST_JSON):
    try:
        with open(BLOCKLIST_JSON) as f:
            b_data = json.load(f)
            if isinstance(b_data, list):
                raw_blocklist.update(str(x).upper().strip() for x in b_data)
            elif isinstance(b_data, dict):
                raw_blocklist.update(str(x).upper().strip() for x in b_data.keys())
    except Exception as e:
        print(f"[!] Warning reading {BLOCKLIST_JSON}: {e}")

canonical_blocklist = {canonical_name(b) for b in raw_blocklist if canonical_name(b)}
print(f"[*] Loaded {len(canonical_blocklist)} canonicalized brand roots into IP Gatekeeper.")

# 2. Ingest BCI Catalog
if not os.path.exists(PARQUET_PATH):
    print("[-] Missing bci_normalized.parquet!")
    sys.exit(1)

df = pd.read_parquet(PARQUET_PATH)

# Normalize column names
col_map = {}
for c in df.columns:
    cl = str(c).lower().strip()
    if "brand" in cl:
        col_map[c] = "brand"
    elif any(k in cl for k in ["upc", "barcode"]):
        col_map[c] = "upc"
    elif "asin" in cl:
        col_map[c] = "asin"
    elif any(k in cl for k in ["sku", "item_no", "item", "mpn"]):
        if "sku" not in col_map.values():
            col_map[c] = "sku"
    elif any(k in cl for k in ["cost", "wholesale", "price", "unit_cost"]):
        if "cost" not in col_map.values():
            col_map[c] = "cost"
    elif any(k in cl for k in ["desc", "title", "name"]):
        if "title" not in col_map.values():
            col_map[c] = "title"

df = df.rename(columns=col_map)
df["cost"] = pd.to_numeric(df["cost"].astype(str).str.replace(r"[\$,]", "", regex=True), errors="coerce").fillna(0.0)
df = df[df["cost"] > 0]
df["brand"] = df["brand"].astype(str).str.strip()
df = df[df["brand"].str.len() > 1]
df = df[~df["brand"].str.lower().isin(["nan", "unknown", "none", "generic"])]

# 3. Filter with Canonical Matching
def is_blocked(brand_name: str) -> bool:
    canon = canonical_name(brand_name)
    if not canon:
        return True
    if canon in canonical_blocklist:
        return True
    # Substring check against canonical blocks >= 4 chars
    for blk in canonical_blocklist:
        if len(blk) >= 4 and (blk in canon or canon in blk):
            return True
    return False

initial_brands = df["brand"].nunique()
df = df[~df["brand"].apply(is_blocked)].copy()
purged = initial_brands - df["brand"].nunique()
print(f"[*] Canonical IP Gatekeeper purged {purged} brands. {df['brand'].nunique()} safe brands remaining.")

# 4. Filter for sub-$4.50 sacrifice items
cheap_df = df[(df["cost"] >= 0.50) & (df["cost"] <= 4.50)].copy()
best_per_brand = cheap_df.sort_values("cost").groupby("brand").first().reset_index()
print(f"[*] Found {len(best_per_brand)} clean, unencumbered qualifying brands.")

# 5. Build $750 Cart
target_moq = 750.00
selected = []
current_total = 0.0

for _, row in best_per_brand.iterrows():
    b_name = str(row["brand"])
    prefix = re.sub(r'[^A-Z0-9]', '', b_name.upper())[:4] or "BCI"
    default_sku = f"BCI-{prefix}-01"
    
    qty = 10
    unit_cost = round(float(row["cost"]), 2)
    line_total = round(unit_cost * qty, 2)
    
    selected.append({
        "brand": b_name,
        "sku": str(row.get("sku", default_sku)),
        "upc": str(row.get("upc", "")),
        "asin": str(row.get("asin", "")),
        "title": str(row.get("title", f"{b_name} Line Item"))[:70],
        "unit_cost": unit_cost,
        "quantity": qty,
        "line_total": line_total
    })
    current_total += line_total
    if current_total >= target_moq and len(selected) >= 18:
        break

# Top-up units across items if under $750 MOQ
i = 0
while current_total < target_moq and selected:
    selected[i % len(selected)]["quantity"] += 2
    selected[i % len(selected)]["line_total"] = round(
        selected[i % len(selected)]["unit_cost"] * selected[i % len(selected)]["quantity"], 2
    )
    current_total = sum(x["line_total"] for x in selected)
    i += 1

cart_df = pd.DataFrame(selected)

print("\n" + "=" * 70)
print("BCI $750 FULLY CANONICALIZED & IP-CLEAN CANARY CART")
print(f"  [+] Clean Brands Selected: {len(cart_df)}")
print(f"  [+] Total Units Staged: {cart_df['quantity'].sum()}")
print(f"  [+] Total Order Spend: ${current_total:,.2f} (BCI $750 MOQ Met)")
print("=" * 70)

display_cols = ["brand", "unit_cost", "quantity", "line_total"]
print(cart_df[display_cols].to_string(index=False))

# Export clean files
po_csv = os.path.join(OUTPUT_DIR, "bci_canary_order_750.csv")
cart_df.to_csv(po_csv, index=False)

loader_tsv = os.path.join(OUTPUT_DIR, "Inventory_Loader_Canary.txt")
with open(loader_tsv, "w") as f:
    f.write("sku\tprice\tquantity\tproduct-id\tproduct-id-type\tcondition-type\tleadtime-to-ship\n")
    for item in selected:
        pid = item["asin"] if len(item["asin"]) == 10 else item["upc"]
        ptype = "1" if len(item["asin"]) == 10 else "4"
        f.write(f"{item['sku']}\t99.99\t0\t{pid}\t{ptype}\t11\t2\n")

print(f"\n[+] Sanitized PO Saved: {po_csv}")
print(f"[+] Sanitized Flat File Saved: {loader_tsv}")
