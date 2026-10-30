#!/usr/bin/env bash
# ==============================================================================
# Master Wholesale Catalog Pipeline Daemon
# Monitors /app/data/catalogs/incoming/ for incoming .xlsx/.csv files,
# prioritizes distributor scans, and runs the Keepa finder during idle windows.
# ==============================================================================
set -euo pipefail

APP_ROOT="/app"
INCOMING_DIR="${APP_ROOT}/data/catalogs/incoming"
PROCESSED_DIR="${APP_ROOT}/data/catalogs/processed"
LOG_DIR="${APP_ROOT}/data/logs"

mkdir -p "$INCOMING_DIR" "$PROCESSED_DIR" "$LOG_DIR"

# Source environment variables if .env exists
if [ -f "${APP_ROOT}/.env" ]; then
  export $(grep -v '^#' "${APP_ROOT}/.env" | xargs)
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Master Wholesale Daemon Initialized ==="

while true; do
  # Find any Excel or CSV catalog files staged in incoming
  QUEUE=()
  while IFS= read -r -d $'\0' file; do
    QUEUE+=("$file")
  done < <(find "$INCOMING_DIR" -maxdepth 1 -type f \( -iname "*.xlsx" -o -iname "*.csv" \) -print0)

  if [ ${#QUEUE[@]} -gt 0 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Found ${#QUEUE[@]} file(s) in incoming catalog queue."

    for catalog_file in "${QUEUE[@]}"; do
      filename=$(basename "$catalog_file")
      lower_name=$(echo "$filename" | tr '[:upper:]' '[:lower:]')
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting processing for: $filename"

      # Determine distributor profile
      dist_key="general"
      if [[ "$lower_name" =~ "frontier" ]]; then dist_key="frontier"; fi
      if [[ "$lower_name" =~ "contour" ]]; then dist_key="contour"; fi
      if [[ "$lower_name" =~ "bci" ]]; then dist_key="bci"; fi

      # Execute universal catalog scanner with fees and multipack logic
      python3 "${APP_ROOT}/tools/universal_catalog_scanner.py" \
        --input "$catalog_file" \
        --distributor "$dist_key"

      # Archive processed catalog
      timestamp=$(date +%Y%m%d_%H%M%S)
      mv "$catalog_file" "${PROCESSED_DIR}/${timestamp}_${filename}"
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Finished $filename. Archived to processed directory."
    done

  else
    # Queue is idle: Run one single pass of the reverse finder
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] No pending catalogs. Running reverse finder check..."
    python3 "${APP_ROOT}/tools/reverse_amazon_finder.py" --single-pass 2>/dev/null || true
  fi

  # Sleep 15 minutes before the next poll loop
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Pipeline idle. Sleeping 900s..."
  sleep 900
done
