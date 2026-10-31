#!/usr/bin/env bash
# run_all_catalogs.sh - Complete Pipeline Orchestrator with Max-FBA 2 & Dynamic Allocation
set -e

DATA_DIR="/app/data"
INCOMING_DIR="${DATA_DIR}/catalogs/incoming"
LOGS_DIR="${DATA_DIR}/logs"
REPORTS_DIR="${DATA_DIR}/reports"
CHECKPOINTS_DIR="${DATA_DIR}/checkpoints"

mkdir -p "${INCOMING_DIR}" "${LOGS_DIR}" "${REPORTS_DIR}" "${CHECKPOINTS_DIR}"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] === Initializing Autonomous 50/50 Dual Pipeline ==="

# 1. Guarantee IP blocklist exists in persistent /app/data
if [ ! -f "${DATA_DIR}/ip_blocklist.json" ]; then
    if [ -f "/app/ip_blocklist.json.bak" ]; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Mirroring /app/ip_blocklist.json.bak to ${DATA_DIR}/ip_blocklist.json"
        cp /app/ip_blocklist.json.bak "${DATA_DIR}/ip_blocklist.json"
    elif [ -f "/app/ip_blocklist.json" ]; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Mirroring /app/ip_blocklist.json to ${DATA_DIR}/ip_blocklist.json"
        cp /app/ip_blocklist.json "${DATA_DIR}/ip_blocklist.json"
    fi
else
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] IP Blocklist confirmed at ${DATA_DIR}/ip_blocklist.json."
fi

# 2. Launch Reverse Amazon Hunter with --max-fba 2 in background
pkill -f "reverse_amazon_finder.py" || true
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Reverse Amazon Hunter (0-2 FBA targets)..."
python3 /app/tools/reverse_amazon_finder.py \
    --min-rank 1 \
    --max-rank 120000 \
    --max-fba 2 \
    --min-fbm-price 25.0 \
    > "${LOGS_DIR}/reverse_hunter.log" 2>&1 &
REVERSE_PID=$!
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Reverse Hunter running (PID: ${REVERSE_PID}). Streaming to Telegram."

# 3. Process Distributor Catalogs in sequence
CATALOG_COUNT=$(find "${INCOMING_DIR}" -maxdepth 1 -type f \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) | wc -l)
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Total catalogs ready for processing: ${CATALOG_COUNT}"

process_distributor() {
    local file_pattern="$1"
    local distributor_name="$2"
    local catalog_file
    catalog_file=$(find "${INCOMING_DIR}" -maxdepth 1 -type f -iname "*${file_pattern}*" | head -n 1)

    if [ -n "${catalog_file}" ] && [ -f "${catalog_file}" ]; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] >>> Starting ${distributor_name} processing: $(basename "${catalog_file}")"
        python3 /app/tools/universal_catalog_scanner.py \
            --input "${catalog_file}" \
            --distributor "${distributor_name}"
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] <<< Finished ${distributor_name} processing."
    else
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [SKIP] No file matching '*${file_pattern}*' in ${INCOMING_DIR}"
    fi
}

process_distributor "Frontier" "frontier"
process_distributor "Contour" "contour"
process_distributor "BCI" "bci"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] === Forward catalog passes complete. Reverse Hunter maintaining 100% token allocation ==="
wait "${REVERSE_PID}"
