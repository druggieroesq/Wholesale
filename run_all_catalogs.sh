#!/usr/bin/env bash

INCOMING_DIR="/app/data/catalogs/incoming"
PROCESSED_DIR="/app/data/catalogs/processed"
LOGS_DIR="/app/data/logs"
CHECKPOINTS_DIR="/app/data/checkpoints"
REPORTS_DIR="/app/data/reports"

mkdir -p "$INCOMING_DIR" "$PROCESSED_DIR" "$LOGS_DIR" "$CHECKPOINTS_DIR" "$REPORTS_DIR"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] === Initializing Autonomous 50/50 Dual Pipeline ==="

# -------------------------------------------------------------------
# 0. Sync IP Blocklist across persistent volume mount
# -------------------------------------------------------------------
if [ -f "/app/data/ip_blocklist.json" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] IP Blocklist confirmed at /app/data/ip_blocklist.json."
elif [ -f "/app/ip_blocklist.json" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Copying /app/ip_blocklist.json to /app/data/ip_blocklist.json..."
    cp /app/ip_blocklist.json /app/data/ip_blocklist.json
fi

# -------------------------------------------------------------------
# 1. Launch 24/7 Reverse Hunter Immediately in Background (50% tokens)
# -------------------------------------------------------------------
if ! pgrep -f "reverse_amazon_finder.py" > /dev/null; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Reverse Amazon Hunter (0-FBA Prime targets)..."
    nohup python3 /app/tools/reverse_amazon_finder.py \
        --min-rank 1 \
        --max-rank 80000 \
        --max-fba 0 \
        --min-fbm-price 25.0 \
        > "$LOGS_DIR/reverse_hunter.log" 2>&1 &
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Reverse Hunter running (PID: $!). Streaming to Telegram."
fi

# -------------------------------------------------------------------
# 2. Auto-download Catalog Sheets if Missing in Incoming
# -------------------------------------------------------------------
if [ ! -f "$INCOMING_DIR/Frontier_Catalog.xlsx" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching Frontier Catalog..."
    curl -sL "https://docs.google.com/spreadsheets/d/122YeUPONFQuwy5UfoE3pz8RNK1pdb9l0/export?format=xlsx" -o "$INCOMING_DIR/Frontier_Catalog.xlsx"
fi

if [ ! -f "$INCOMING_DIR/Contour_Catalog.csv" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching Contour Catalog..."
    curl -sL "https://drive.google.com/uc?export=download&id=1-YRPv_YSM82L38E7TnUNDdfSxRDWK0sA" -o "$INCOMING_DIR/Contour_Catalog.csv"
fi

if [ ! -f "$INCOMING_DIR/BCI_Catalog.xlsx" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching BCI Catalog..."
    curl -sL "https://docs.google.com/spreadsheets/d/1D1hUS38LK-8XsQIdIOeluUM1XKUu-n8-/export?format=xlsx" -o "$INCOMING_DIR/BCI_Catalog.xlsx"
fi

# -------------------------------------------------------------------
# 3. Prioritized Catalog Queue: Frontier First, then Contour, then BCI
# -------------------------------------------------------------------
queue=()

[[ -f "$INCOMING_DIR/Frontier_Catalog.xlsx" ]] && queue+=("$INCOMING_DIR/Frontier_Catalog.xlsx")
[[ -f "$INCOMING_DIR/Contour_Catalog.csv" ]]    && queue+=("$INCOMING_DIR/Contour_Catalog.csv")
[[ -f "$INCOMING_DIR/BCI_Catalog.xlsx" ]]       && queue+=("$INCOMING_DIR/BCI_Catalog.xlsx")

for cat in "$INCOMING_DIR"/*; do
    if [[ -f "$cat" && ("$cat" == *.xlsx || "$cat" == *.xls || "$cat" == *.csv) ]]; then
        if [[ ! " ${queue[*]} " =~ " ${cat} " ]]; then
            queue+=("$cat")
        fi
    fi
done

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Total catalogs ready for processing: ${#queue[@]}"

# -------------------------------------------------------------------
# 4. Process Forward Catalogs
# -------------------------------------------------------------------
for cat in "${queue[@]}"; do
    filename=$(basename "$cat")
    distributor="generic"

    if [[ "$filename" =~ [Ff]rontier ]]; then
        distributor="frontier"
    elif [[ "$filename" =~ [Cc]ontour ]]; then
        distributor="contour"
    elif [[ "$filename" =~ [Bb][Cc][Ii] ]]; then
        distributor="bci"
    fi

    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] >>> Starting $distributor processing: $filename"
    python3 /app/tools/universal_catalog_scanner.py --input "$cat" --distributor "$distributor"
    status=$?

    if [ $status -eq 0 ]; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] >>> Completed $filename. Archiving..."
        mv "$cat" "$PROCESSED_DIR/$filename"
    else
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [WARNING] Scanner exited on $filename with code $status. Continuing pipeline..."
    fi
done

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] All initial catalog queues processed. Continuous hunting remains active at 100% capacity."
wait
