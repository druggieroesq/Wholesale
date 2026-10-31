#!/bin/bash
set -e

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Launching Resilient Dual Pipeline ==="

# Ensure all runtime directory trees exist
mkdir -p /app/data/logs /app/data/reports /app/data/checkpoints /app/data/catalogs/incoming

# Initialize log files so tail never blocks on missing descriptors
touch /app/data/logs/catalog_scanner.log /app/data/logs/reverse_hunter.log

# 1. Start Frontier Catalog Scanner if not already running
if ! pgrep -f "universal_catalog_scanner.py" > /dev/null; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Frontier Catalog Scanner..."
    python3 /app/tools/universal_catalog_scanner.py \
        --input /app/data/catalogs/incoming/Frontier_Catalog.xlsx \
        --distributor frontier \
        >> /app/data/logs/catalog_scanner.log 2>&1 &
    SCANNER_PID=$!
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Frontier Scanner spawned with PID: $SCANNER_PID"
else
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Frontier Scanner already running."
fi

# 2. Start Upgraded Reverse Amazon Hunter v2 if not already running
if ! pgrep -f "reverse_amazon_finder.py" > /dev/null; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Reverse Amazon Hunter v2 (Min Tokens: 50)..."
    python3 /app/tools/reverse_amazon_finder.py \
        --min-rank 1 \
        --max-rank 250000 \
        --min-fbm-price 18.0 \
        --min-tokens 50 \
        --max-weight 8.0 \
        --max-dim 16.0 \
        >> /app/data/logs/reverse_hunter.log 2>&1 &
    HUNTER_PID=$!
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Reverse Hunter spawned with PID: $HUNTER_PID"
else
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Reverse Hunter already running."
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Pipeline active. Binding PID 1 to streaming logs..."

# Block PID 1 permanently so Railway container stays in 'Active' status
exec tail -n 20 -f /app/data/logs/catalog_scanner.log /app/data/logs/reverse_hunter.log
