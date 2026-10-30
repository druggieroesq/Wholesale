#!/usr/bin/env bash

INCOMING_DIR="/app/data/catalogs/incoming"
PROCESSED_DIR="/app/data/catalogs/processed"
LOGS_DIR="/app/data/logs"
CHECKPOINTS_DIR="/app/data/checkpoints"
REPORTS_DIR="/app/data/reports"

mkdir -p "$INCOMING_DIR" "$PROCESSED_DIR" "$LOGS_DIR" "$CHECKPOINTS_DIR" "$REPORTS_DIR"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] === Initializing Autonomous Catalog Pipeline ==="

# Auto-download Frontier if missing
if [ ! -f "$INCOMING_DIR/Frontier_Catalog.xlsx" ] && [ ! -f "$PROCESSED_DIR/Frontier_Catalog.xlsx" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching Frontier Catalog..."
    curl -sL "https://docs.google.com/spreadsheets/d/122YeUPONFQuwy5UfoE3pz8RNK1pdb9l0/export?format=xlsx" -o "$INCOMING_DIR/Frontier_Catalog.xlsx"
fi

# Auto-download BCI if missing
if [ ! -f "$INCOMING_DIR/BCI_Catalog.xlsx" ] && [ ! -f "$PROCESSED_DIR/BCI_Catalog.xlsx" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching BCI Catalog..."
    curl -sL "https://docs.google.com/spreadsheets/d/1D1hUS38LK-8XsQIdIOeluUM1XKUu-n8-/export?format=xlsx" -o "$INCOMING_DIR/BCI_Catalog.xlsx"
fi

# Auto-download Contour if missing
if [ ! -f "$INCOMING_DIR/Contour_Catalog.csv" ] && [ ! -f "$PROCESSED_DIR/Contour_Catalog.csv" ]; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Fetching Contour Catalog..."
    curl -sL "https://drive.google.com/uc?export=download&id=1-YRPv_YSM82L38E7TnUNDdfSxRDWK0sA" -o "$INCOMING_DIR/Contour_Catalog.csv"
fi

catalogs=( "$INCOMING_DIR"/* )
valid_catalogs=()

for cat in "${catalogs[@]}"; do
    if [[ -f "$cat" && ("$cat" == *.xlsx || "$cat" == *.xls || "$cat" == *.csv) ]]; then
        valid_catalogs+=("$cat")
    fi
done

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Total catalogs ready for processing: ${#valid_catalogs[@]}"

for cat in "${valid_catalogs[@]}"; do
    filename=$(basename "$cat")
    distributor="generic"

    if [[ "$filename" =~ [Bb][Cc][Ii] ]]; then
        distributor="bci"
    elif [[ "$filename" =~ [Ff]rontier ]]; then
        distributor="frontier"
    elif [[ "$filename" =~ [Cc]ontour ]]; then
        distributor="contour"
    fi

    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] >>> Starting $distributor processing: $filename"
    python3 /app/tools/universal_catalog_scanner.py --input "$cat" --distributor "$distributor"
    status=$?

    if [ $status -eq 0 ]; then
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] >>> Successfully completed $filename. Archiving..."
        mv "$cat" "$PROCESSED_DIR/$filename"
    else
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [ERROR] Scanner exited on $filename with code $status. Halting pipeline."
        exit 1
    fi
done

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] All catalog batches complete. Launching 24/7 background finder..."
python3 /app/tools/reverse_amazon_finder.py
