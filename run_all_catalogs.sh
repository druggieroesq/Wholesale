#!/usr/bin/env bash

INCOMING_DIR="/app/data/catalogs/incoming"
PROCESSED_DIR="/app/data/catalogs/processed"
LOGS_DIR="/app/data/logs"

mkdir -p "$INCOMING_DIR" "$PROCESSED_DIR" "$LOGS_DIR"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] === Initializing Autonomous Catalog Pipeline ==="

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
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [ERROR] Scanner failed on $filename with exit code $status. Halting batch runner."
        exit 1
    fi
done

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] All catalog batches complete. Launching 24/7 background finder..."
nohup python3 /app/tools/reverse_amazon_finder.py > "$LOGS_DIR/reverse_finder.log" 2>&1 &
