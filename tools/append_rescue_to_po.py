import pandas as pd

po_path = "/app/data/reports/bci_profitable_order_750.csv"
df = pd.read_csv(po_path)

# Add REScue 4-pack (5 cases = 20 total gallons, clears 10-unit brand gate)
rescue_entry = {
    "brand": "REScue",
    "asin": "B094B72TDJ",
    "title": "REScue Disinfectant Sanitizing Concentrate 1 Gallon (4 Pack)",
    "sku": "RESC-DISI-4PK",
    "upc": "684608000000",
    "unit_cost": 54.00,
    "retail_price": 217.98,
    "net_profit_unit": 122.78,
    "quantity": 5,
    "line_total": 270.00,
    "order_profit": 613.90
}

if not (df["asin"] == "B094B72TDJ").any():
    df = pd.concat([df, pd.DataFrame([rescue_entry])], ignore_index=True)
    df.to_csv(po_path, index=False)
    print(f"[+] Added REScue 4-Pack to {po_path}")
    print(f"    New Total Spend: ${df['line_total'].sum():,.2f}")
    print(f"    New Projected Profit: ${df['order_profit'].sum():,.2f}")
else:
    print("[*] REScue already present in PO.")
