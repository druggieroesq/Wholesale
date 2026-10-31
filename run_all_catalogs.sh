#!/bin/bash
set -e

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Launching Resilient Dual Pipeline ==="

# Stop existing processes to prevent token contention
pkill -9 -f "reverse_amazon_finder.py" 2>/dev/null || true
pkill -9 -f "universal_catalog_scanner.py" 2>/dev/null || true
sleep 3

mkdir -p /app/data/logs /app/data/reports /app/data/checkpoints

# 1. Start Catalog Scanner for Frontier
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Frontier Catalog Scanner..."
nohup python3 /app/tools/universal_catalog_scanner.py \
    --input /app/data/catalogs/incoming/Frontier_Catalog.xlsx \
    --distributor frontier \
    >> /app/data/logs/catalog_scanner.log 2>&1 &
SCANNER_PID=$!
echo "Frontier Catalog Scanner PID: $SCANNER_PID"

# 2. Start Reverse Amazon Infill Hunter
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting Reverse Amazon Hunter (Min Tokens: 50)..."
nohup python3 /app/tools/reverse_amazon_finder.py \
    --min-rank 1 \
    --max-rank 120000 \
    --max-fba 2 \
    --min-fbm-price 25.0 \
    --min-tokens 50 \
    >> /app/data/logs/reverse_hunter.log 2>&1 &
HUNTER_PID=$!
echo "Reverse Hunter PID: $HUNTER_PID"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Dual engines running. Tail logs with:"
echo "tail -f /app/data/logs/catalog_scanner.log"
echo "tail -f /app/data/logs/reverse_hunter.log"

wait
