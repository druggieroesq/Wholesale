import os, requests, json
import pandas as pd
import numpy as np

manifest_path = '/app/data/reports/mro_sheet_leads_latest.csv'
if not os.path.exists(manifest_path):
    print(f"Error: Manifest {manifest_path} does not exist.")
    exit(1)

df = pd.read_csv(manifest_path, low_memory=False)

# 1. Purge B0FHHLY2D7 and high-return goods
blocked_terms = ['fan', 'ceiling fan', 'light', 'chandelier', 'pendant', 'lamp', 'mirror', 'rug', 'curtain', 'apparel', 'dress', 'bedding']
df = df[df['asin'] != 'B0FHHLY2D7'].copy()
if 'title' in df.columns:
    df = df[~df['title'].astype(str).str.lower().str.contains('|'.join(blocked_terms), na=False)].copy()

# 2. Extract supplier single cost & target pack
raw_cost = df['Effective Landed COGS'].astype(str).str.replace('$', '', regex=False).str.replace(',', '', regex=False)
unit_cogs = pd.to_numeric(raw_cost, errors='coerce').fillna(0.0)

mult = pd.to_numeric(df['Unit Multiplier'], errors='coerce').fillna(1.0) if 'Unit Multiplier' in df.columns else pd.Series(1.0, index=df.index)
mult = mult.where(mult > 0, 1.0)

df['supplier_single_cost'] = np.where(mult > 1.0, unit_cogs / mult, unit_cogs)
df['target_amazon_pack'] = pd.to_numeric(df['Amazon Pack Size'], errors='coerce').fillna(1).astype(int) if 'Amazon Pack Size' in df.columns else pd.Series(1, index=df.index)

# 3. Dynamic Prep fee schedule
df['prep_materials_fee'] = np.where(df['target_amazon_pack'] == 1, 0.50,
                           np.where(df['target_amazon_pack'] <= 3, 0.85, 1.25))

df['landed_bundle_cogs'] = (df['supplier_single_cost'] * df['target_amazon_pack']) + df['prep_materials_fee']

# Effective Buy Box (fall back to 90-day avg if suppressed)
bb_cur = pd.to_numeric(df['current_bb'], errors='coerce').fillna(0.0) if 'current_bb' in df.columns else pd.Series(0.0, index=df.index)
bb_90 = pd.to_numeric(df['avg90_bb'], errors='coerce').fillna(0.0) if 'avg90_bb' in df.columns else pd.Series(0.0, index=df.index)
df['effective_bb'] = np.where(bb_cur > 0, bb_cur, bb_90)

# True Amazon Fees: 15% Referral + Scaled FBA Pick Fee
df['fba_pick_fee'] = np.where(df['target_amazon_pack'] > 3, 6.50,
                     np.where(df['target_amazon_pack'] > 1, 5.25, 4.25))
df['referral_fee'] = df['effective_bb'] * 0.15
df['total_amazon_fees'] = df['referral_fee'] + df['fba_pick_fee']

# Net Bundle Economics
df['net_profit'] = df['effective_bb'] - df['landed_bundle_cogs'] - df['total_amazon_fees']
df['net_roi'] = np.where(df['landed_bundle_cogs'] > 0, (df['net_profit'] / df['landed_bundle_cogs']) * 100, 0.0)

# 4. Safe 0-3 FBA Competition & $750/mo profit hurdle
fba_col = df['num_fba_sellers'] if 'num_fba_sellers' in df.columns else pd.Series(0, index=df.index)
df['fba_sellers'] = pd.to_numeric(fba_col, errors='coerce').fillna(0).astype(int)

vel_col = df['monthly_sold'] if 'monthly_sold' in df.columns else pd.Series(0, index=df.index)
df['monthly_velocity'] = pd.to_numeric(vel_col, errors='coerce').fillna(0).astype(int)

df['rotational_share_units'] = df['monthly_velocity'] / (df['fba_sellers'] + 1)
df['projected_monthly_profit'] = df['rotational_share_units'] * df['net_profit']

# Gate evaluation
is_zero_fba = (df['fba_sellers'] == 0) & (df['net_profit'] >= 5.0)
is_rotational_win = (df['fba_sellers'].between(1, 3)) & (df['projected_monthly_profit'] >= 750.0) & (df['net_profit'] >= 4.0)

df['competition_qualified'] = is_zero_fba | is_rotational_win
df['competition_type'] = np.where(df['fba_sellers'] == 0, '0-FBA PRIME STEAL',
                         np.where(is_rotational_win, 'SHARED FBA (>=$750/mo)', 'DISQUALIFIED'))

# Actionable Prep Instruction
df['prep_instruction'] = np.where(
    df['target_amazon_pack'] > 1,
    'Bundle ' + df['target_amazon_pack'].astype(str) + ' singles into polybag -> Add "Sold as Set / Do Not Separate" label',
    'Single unit prep -> FNSKU barcode over UPC'
)

# 5. Cost Certainty Scoring
df['score'] = 0.0
status_col = df['status'] if 'status' in df.columns else pd.Series('NO_CATALOG_MATCH', index=df.index)

deal_mask = (status_col == 'SCOUTED_DEAL') & df['competition_qualified']
df.loc[deal_mask & (df['net_roi'] >= 50), 'score'] = 100.0
df.loc[deal_mask & (df['net_roi'] < 50) & (df['net_roi'] >= 25), 'score'] = 85.0

reverse_mask = (status_col == 'NO_CATALOG_MATCH') & df['competition_qualified']
df.loc[reverse_mask & (df['fba_sellers'] == 0), 'score'] = 25.0
df.loc[reverse_mask & (df['fba_sellers'] > 0), 'score'] = 20.0

# 6. Save buylist
output_cols = [
    'score', 'competition_type', 'fba_sellers', 'monthly_velocity', 'projected_monthly_profit',
    'asin', 'brand', 'supplier_single_cost', 'target_amazon_pack', 'prep_materials_fee',
    'landed_bundle_cogs', 'effective_bb', 'total_amazon_fees', 'net_profit', 'net_roi',
    'status', 'prep_instruction', 'title'
]

available_cols = [c for c in output_cols if c in df.columns]
qualified_df = df[df['competition_qualified'] & (df['effective_bb'] > 0)].sort_values(
    by=['score', 'projected_monthly_profit'], ascending=[False, False]
)

clean_path = '/app/data/reports/transparent_multipack_buylist.csv'
qualified_df[available_cols].to_csv(clean_path, index=False)
print(f"Generated {clean_path} with {len(qualified_df)} qualified opportunities.")

# 7. Push Overwrite to Google Sheet Webhook
webhook_url = os.environ.get('TARGET_SHEET_WEBHOOK') or 'https://script.google.com/macros/s/AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y/exec'

upload_df = qualified_df[available_cols].copy()
upload_df['supplier_single_cost'] = upload_df['supplier_single_cost'].apply(lambda x: f"${x:.2f}")
upload_df['prep_materials_fee'] = upload_df['prep_materials_fee'].apply(lambda x: f"${x:.2f}")
upload_df['landed_bundle_cogs'] = upload_df['landed_bundle_cogs'].apply(lambda x: f"${x:.2f}")
upload_df['effective_bb'] = upload_df['effective_bb'].apply(lambda x: f"${x:.2f}")
upload_df['total_amazon_fees'] = upload_df['total_amazon_fees'].apply(lambda x: f"${x:.2f}")
upload_df['net_profit'] = upload_df['net_profit'].apply(lambda x: f"${x:.2f}")
upload_df['projected_monthly_profit'] = upload_df['projected_monthly_profit'].apply(lambda x: f"${x:,.2f}/mo")
upload_df['net_roi'] = upload_df['net_roi'].apply(lambda x: f"{x:.1f}%")

payload = {
    'action': 'replace_all',
    'clear_sheet': True,
    'sheet_name': 'Hunter v17.5 Master',
    'records': upload_df.head(500).to_dict(orient='records')
}

res = requests.post(webhook_url, json=payload, timeout=40)
print(f"Google Sheet Overwrite Status: {res.status_code}")
