#!/usr/bin/env python3
"""
Wholesale Lead Pipeline Streamer (v18.5 Capital Velocity & Turn Engine)
----------------------------------------------------------------------
Ranks deals by monthly cash flow ($/mo) and turn speed rather than
just zero-competition status.
"""

import os
import time
import math
import json
import sqlite3
import requests
import pandas as pd
import numpy as np

WEBHOOK_URL = os.getenv(
    "SHEET_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y/exec"
)
DB_PATH = os.getenv("DB_PATH", "/app/data/hunter_engine.db")


def calculate_capital_velocity_score(row):
    """Calculates Deal Score prioritizing turn speed and monthly cash flow."""
    clean_brand = str(row.get("brand", "")).lower()
    net_profit = float(row.get("net_profit", 0.0) or 0.0)
    monthly_pool = float(row.get("monthly_profit_pool", 0.0) or 0.0)
    our_share = float(row.get("our_share_velocity", 1.0) or 1.0)
    roi_pct = float(row.get("roi_percent", 0.0) or 0.0)
    comp_fba = int(row.get("competitive_fba", 0) or 0)

    # Pinned priority deals
    if any(p in clean_brand for p in ["anysun", "flir", "leatherman", "anycubic", "klein", "dewalt", "milwaukee"]) and net_profit >= 25.0:
        return 100

    # 1. Monthly Dollar Profit Flow (40 pts for >= $1,200/mo net profit)
    pool_score = 40.0 * min(1.0, monthly_pool / 1200.0)

    # 2. Rotational Turn Speed (30 pts for >= 60 units/mo our share)
    velocity_score = 30.0 * min(1.0, our_share / 60.0)

    # 3. Margin & ROI Safety (20 pts for >= 40% Net ROI)
    roi_score = 20.0 * min(1.0, roi_pct / 40.0)

    # 4. Competition Efficiency (10 pts for 0-2 FBA; drops for 3-4 FBA)
    comp_score = 10.0 if comp_fba == 0 else (8.0 if comp_fba <= 2 else 4.0)

    return int(np.clip(round(pool_score + velocity_score + roi_score + comp_score), 50, 99))


def run_pipeline_sync():
    print("=" * 60)
    print("STARTING CAPITAL VELOCITY PIPELINE SYNC (v18.5)")
    print("=" * 60)

    # 1. 0-FBA Candidates
    steals = []
    steals_path = "/app/data/reports/audited_0fba_prime_steals.json"
    if os.path.exists(steals_path):
        with open(steals_path) as f:
            d = json.load(f)
            if isinstance(d, list):
                steals = d

    df_steals = pd.DataFrame(steals)
    if not df_steals.empty:
        df_steals = df_steals[df_steals["asin"] != "B0FHHLY2D7"].copy()
        df_steals["confidence_tier"] = "[0-FBA PRIME STEAL]"
        df_steals["competitive_fba"] = 0
        df_steals["status"] = "0-FBA DOMINANCE"
        df_steals["notes"] = "0 FBA Sellers — 100% Buy Box Capture"

        if "monthly_sold" in df_steals.columns:
            vel = pd.to_numeric(df_steals["monthly_sold"], errors="coerce").fillna(20).astype(int)
        else:
            vel = pd.Series([20] * len(df_steals))
        df_steals["monthly_velocity"] = np.where(vel > 0, vel, 20)
        df_steals["our_share_velocity"] = df_steals["monthly_velocity"]

        bb_col = df_steals["current_bb"] if "current_bb" in df_steals.columns else (df_steals["price"] if "price" in df_steals.columns else pd.Series([45.0] * len(df_steals)))
        cost_col = df_steals["cost"] if "cost" in df_steals.columns else (df_steals["wholesale_cost"] if "wholesale_cost" in df_steals.columns else pd.Series([20.0] * len(df_steals)))
        bb = pd.to_numeric(bb_col, errors="coerce").fillna(45.0)
        cost = pd.to_numeric(cost_col, errors="coerce").fillna(20.0)

        df_steals["buy_box_price"] = bb
        df_steals["wholesale_cost"] = cost
        df_steals["net_profit"] = np.round(bb - cost - (bb * 0.15) - 5.50, 2)
        df_steals["roi_percent"] = np.where(cost > 0, np.round((df_steals["net_profit"] / cost) * 100, 1), 0.0)
        df_steals["monthly_profit_pool"] = np.round(df_steals["our_share_velocity"] * df_steals["net_profit"], 2)

    # 2. SQLite Verified Wholesale Deals
    df_verified = pd.DataFrame()
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        sql = """
        SELECT 
            v.asin, v.title, v.brand, v.vendor, 
            CAST(v.buy_box_price AS FLOAT) AS buy_box_price, 
            CAST(v.wholesale_cost AS FLOAT) AS wholesale_cost, 
            CAST(v.net_profit AS FLOAT) AS net_profit, 
            CAST(v.roi_percent AS FLOAT) AS roi_percent,
            COALESCE(c.monthly_sold, 35) AS monthly_sold,
            COALESCE(c.sales_rank, v.sales_rank, 45000) AS sales_rank,
            COALESCE(c.num_sellers, 1) AS fba_sellers
        FROM verified_deals v
        LEFT JOIN candidate_cache c ON v.asin = c.asin
        WHERE v.asin != "B0FHHLY2D7"
        ORDER BY v.net_profit DESC
        """
        df_verified = pd.read_sql(sql, conn)
        conn.close()

    if not df_verified.empty:
        df_verified["confidence_tier"] = "[VERIFIED 95%+]"
        df_verified["competitive_fba"] = pd.to_numeric(df_verified["fba_sellers"], errors="coerce").fillna(1).astype(int)
        df_verified["monthly_velocity"] = pd.to_numeric(df_verified["monthly_sold"], errors="coerce").fillna(35).astype(int)
        df_verified["our_share_velocity"] = np.maximum(1, np.round(df_verified["monthly_velocity"] / (df_verified["competitive_fba"] + 1)).astype(int))
        df_verified["monthly_profit_pool"] = np.round(df_verified["our_share_velocity"] * df_verified["net_profit"], 2)
        df_verified["status"] = "READY_TO_BUY"
        df_verified["notes"] = "Authorized Distributor Deal"

    # 3. Target Catalog Expansion Leads
    targets = []
    for p in ["/app/data/waterfall_enriched_leads.json", "/app/data/discovered_candidates.json"]:
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
                if isinstance(d, list):
                    targets.extend(d)

    df_target = pd.DataFrame(targets)
    if not df_target.empty:
        df_target = df_target[df_target["asin"] != "B0FHHLY2D7"].copy()
        df_target["confidence_tier"] = "[TARGET ACQUISITION]"
        df_target["competitive_fba"] = 1
        df_target["status"] = "OPEN_DEALER_ACCOUNT"
        df_target["notes"] = "Distributor Expansion Lead"

        if "monthly_sold" in df_target.columns:
            t_vel = pd.to_numeric(df_target["monthly_sold"], errors="coerce").fillna(25).astype(int)
        else:
            t_vel = pd.Series([25] * len(df_target))

        df_target["monthly_velocity"] = np.where(t_vel > 0, t_vel, 25)
        df_target["our_share_velocity"] = np.maximum(1, np.round(df_target["monthly_velocity"] / 2).astype(int))

        if "current_bb" in df_target.columns and "buy_box_price" not in df_target.columns:
            df_target["buy_box_price"] = df_target["current_bb"]
        if "supplier_cost" in df_target.columns and "wholesale_cost" not in df_target.columns:
            df_target["wholesale_cost"] = df_target["supplier_cost"]

    # 4. Merge, Filter & Score
    combined = pd.concat([df_steals, df_verified, df_target], ignore_index=True).drop_duplicates(subset=["asin"])
    if "title" in combined.columns:
        combined = combined[~combined["title"].astype(str).str.lower().str.contains("ceiling fan|chandelier|pendant light|floor lamp|table lamp|area rug", na=False)].copy()

    combined["deal_score"] = combined.apply(calculate_capital_velocity_score, axis=1)
    combined = combined.sort_values(by=["deal_score", "monthly_profit_pool"], ascending=[False, False])
    combined = combined.loc[:, ~combined.columns.duplicated()].copy()

    def sanitize_val(v):
        if v is None:
            return ""
        if isinstance(v, (float, np.floating)):
            return 0.0 if math.isnan(v) or math.isinf(v) else float(v)
        if isinstance(v, (int, np.integer)):
            return int(v)
        return str(v).strip()

    records = []
    for row in combined.to_dict(orient="records"):
        clean_row = {str(k): sanitize_val(v) for k, v in row.items()}
        clean_row["monthly_velocity"] = int(clean_row.get("monthly_velocity") or 25)
        clean_row["competitive_fba"] = int(clean_row.get("competitive_fba") or 0)
        clean_row["our_share_velocity"] = int(clean_row.get("our_share_velocity") or 25)
        clean_row["deal_score"] = int(clean_row.get("deal_score") or 75)
        records.append(clean_row)

    total_records = len(records)
    print(f"\nTotal leads ready to stream: {total_records}")

    chunk_size = 300
    for target_tab in ["Sourcing & Buy List", "Hunter v17.5 Master"]:
        for idx in range(0, total_records, chunk_size):
            chunk = records[idx : idx + chunk_size]
            action_name = "BATCH_UPDATE_LEADS" if idx == 0 else "APPEND_LEADS"
            payload = {
                "action": action_name,
                "sheet_name": target_tab,
                "tab_name": target_tab,
                "leads": chunk
            }
            try:
                requests.post(WEBHOOK_URL, json=payload, timeout=60)
            except Exception:
                pass
            time.sleep(1.5)


if __name__ == "__main__":
    run_pipeline_sync()
