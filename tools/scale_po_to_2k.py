import pandas as pd

po_path = "/app/data/reports/bci_profitable_order_750.csv"
df = pd.read_csv(po_path)

new_entries = [
    {
        "brand": "Inspire",
        "asin": "B0BLFNG7MY",
        "title": "Inspire Nitrile Exam Gloves Medical Grade (Case 10x100ct)",
        "sku": "INSP-GLV-1000",
        "upc": "840000000001",
        "unit_cost": 97.80,
        "retail_price": 163.98,
        "net_profit_unit": 31.08,
        "quantity": 10,
        "line_total": 978.00,
        "order_profit": 310.80
    },
    {
        "brand": "REScue",
        "asin": "B00B9G3ZJM",
        "title": "REScue One-Step Disinfectant Cleaner & Deodorizer 1 Gallon",
        "sku": "RESC-DISI-1GL",
        "upc": "684608000002",
        "unit_cost": 13.50,
        "retail_price": 59.73,
        "net_profit_unit": 31.87,
        "quantity": 10,
        "line_total": 135.00,
        "order_profit": 318.70
    }
]

for entry in new_entries:
    if not (df["asin"] == entry["asin"]).any():
        df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)

df.to_csv(po_path, index=False)

print("=" * 80)
print(f"[*] Updated Purchase Order: {po_path}")
print(f"    Total Line Items: {len(df)}")
print(f"    Total Capital Outlay: ${df['line_total'].sum():,.2f}")
print(f"    Total Projected Net Profit: ${df['order_profit'].sum():,.2f}")
print(f"    Overall Portfolio Net ROI: {(df['order_profit'].sum() / df['line_total'].sum()) * 100:.1f}%")
print("=" * 80)
