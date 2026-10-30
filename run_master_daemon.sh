#!/usr/bin/env bash
# ==============================================================================
# Master Wholesale Catalog Pipeline Daemon
# Monitors /app/data/catalogs/incoming/ for incoming .xlsx/.csv files,
# prioritizes distributor catalog scans, executes IP Guard pre-filtering,
# and archives processed sheets.
# ==============================================================================
set -euo pipefail

APP_ROOT="/app"
INCOMING_DIR="${APP_ROOT}/data/catalogs/incoming"
PROCESSED_DIR="${APP_ROOT}/data/catalogs/processed"
LOG_DIR="${APP_ROOT}/data/logs"

mkdir -p "$INCOMING_DIR" "$PROCESSED_DIR" "$LOG_DIR"

if [ -f "${APP_ROOT}/.env" ]; then
  export $(grep -v '^#' "${APP_ROOT}/.env" | xargs)
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Master Wholesale Daemon Initialized ==="

while true; do
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

      dist_key="general"
      if [[ "$lower_name" =~ "frontier" ]]; then dist_key="frontier"; fi
      if [[ "$lower_name" =~ "contour" ]]; then dist_key="contour"; fi
      if [[ "$lower_name" =~ "bci" ]]; then dist_key="bci"; fi

      python3 "${APP_ROOT}/tools/universal_catalog_scanner.py" \
        --input "$catalog_file" \
        --distributor "$dist_key"

      timestamp=$(date +%Y%m%d_%H%M%S)
      mv "$catalog_file" "${PROCESSED_DIR}/${timestamp}_${filename}"
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Archived $filename to processed directory."
    done
  fi

  sleep 60
done
