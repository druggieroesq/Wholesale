#!/bin/bash
set -e

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Launching Resilient Dual Pipeline ==="

mkdir -p /app/data/logs /app/data/reports /app/data/checkpoints

# 1. Start Frontier Catalog Scanner only if not currently active
if ! pgrep -f "universal_catalog_scanner.py" > /dev/null; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Frontier Catalog Scanner..."
    nohup python3 /app/tools/universal_catalog_scanner.py \
        --input /app/data/catalogs/incoming/Frontier_Catalog.xlsx \
        --distributor frontier \
        >> /app/data/logs/catalog_scanner.log 2>&1 &
    echo "Frontier Scanner launched."
else
    echo "Frontier Scanner already running."
fi

# 2. Start Upgraded Reverse Amazon Hunter only if not currently active
if ! pgrep -f "reverse_amazon_finder.py" > /dev/null; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Reverse Amazon Hunter v2 (Min Tokens: 50)..."
    nohup python3 /app/tools/reverse_amazon_finder.py \
        --min-rank 1 \
        --max-rank 250000 \
        --min-fbm-price 18.0 \
        --min-tokens 50 \
        --max-weight 8.0 \
        --max-dim 16.0 \
        >> /app/data/logs/reverse_hunter.log 2>&1 &
    echo "Reverse Hunter v2 launched."
else
    echo "Reverse Hunter already running."
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Pipeline confirmed active."
