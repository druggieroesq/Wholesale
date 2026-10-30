#!/usr/bin/env bash

BASE_DIR="/app"
INCOMING="${BASE_DIR}/data/catalogs/incoming"
PROCESSED="${BASE_DIR}/data/catalogs/processed"
LOG_DIR="${BASE_DIR}/data/logs"
REPORTS_DIR="${BASE_DIR}/data/reports"

mkdir -p "$INCOMING" "$PROCESSED" "$LOG_DIR" "$REPORTS_DIR"

export MIN_ROI="${MIN_ROI:-0.50}"
export MIN_ROI_FLOOR="${MIN_ROI_FLOOR:-0.50}"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] === Initializing Autonomous Catalog Pipeline ==="

shopt -s nullglob
files=("$INCOMING"/*.xlsx "$INCOMING"/*.csv)

if [ ${#files[@]} -eq 0 ]; then
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Incoming queue empty. Auto-syncing catalogs from Google Drive..."
  python3 -c '
import gdown
from pathlib import Path

targets = [
    ("https://drive.google.com/uc?id=1D1hUS38LK-8XsQIdIOeluUM1XKUu-n8-", "/app/data/catalogs/incoming/BCI_Catalog.xlsx"),
    ("https://drive.google.com/uc?id=1-YRPv_YSM82L38E7TnUNDdfSxRDWK0sA", "/app/data/catalogs/incoming/Contour_Catalog.csv"),
    ("https://drive.google.com/uc?id=122YeUPONFQuwy5UfoE3pz8RNK1pdb9l0", "/app/data/catalogs/incoming/Frontier_Catalog.xlsx")
]

for url, dest in targets:
    p = Path(dest)
    if not p.exists() or p.stat().st_size == 0:
        print(f"Fetching: {p.name}")
        try:
            gdown.download(url, dest, quiet=True)
        except Exception as e:
            print(f"Failed downloading {p.name}: {e}")
'
fi

files=("$INCOMING"/*.xlsx "$INCOMING"/*.csv)
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Total catalogs ready for processing: ${#files[@]}"

for f in "${files[@]}"; do
  filename=$(basename "$f")
  lower=$(echo "$filename" | tr '[:upper:]' '[:lower:]')

  dist="general"
  if [[ "$lower" =~ "bci" ]]; then dist="bci"; fi
  if [[ "$lower" =~ "frontier" ]]; then dist="frontier"; fi
  if [[ "$lower" =~ "contour" ]] || [[ "$lower" =~ "contar" ]]; then dist="contour"; fi

  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] >>> Starting $dist processing: $filename"

  python3 "${BASE_DIR}/tools/universal_catalog_scanner.py" \
    --input "$f" \
    --distributor "$dist" 2>&1 | tee -a "${LOG_DIR}/${dist}_scan.log"

  timestamp=$(date +%Y%m%d_%H%M%S)
  mv "$f" "${PROCESSED}/${timestamp}_${filename}"
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] >>> Finished and archived: $filename"
done

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] All catalog batches complete. Launching 24/7 background finder..."
if [ -f "${BASE_DIR}/tools/reverse_amazon_finder.py" ]; then
  exec python3 "${BASE_DIR}/tools/reverse_amazon_finder.py"
fi
