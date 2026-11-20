#!/usr/bin/env bash
set -e

mkdir -p /app/data/catalogs/wholesale_catalogs/incoming \
         /app/data/catalogs/wholesale_catalogs/indexed \
         /app/data/reports

# 1. Keep legacy ip_blacklist.json synced to the full 784-brand ip_blocklist.json on every boot
if [ -f /app/data/ip_blocklist.json ]; then
  cp -f /app/data/ip_blocklist.json /app/data/ip_blacklist.json
fi

# 2. Sync all incoming wholesale CSV catalogs into indexed/
cp -f /app/data/catalogs/wholesale_catalogs/incoming/*.csv /app/data/catalogs/wholesale_catalogs/indexed/ 2>/dev/null || true

# 3. Defensive guards for typing.Optional and unlocked_at schema safety
grep -q "Optional" /app/tools/auto_cross_catalog_matcher_daemon.py 2>/dev/null || \
  sed -i '1i from typing import Optional' /app/tools/auto_cross_catalog_matcher_daemon.py

if [ -f /app/tools/sp_api_restrictions_daemon.py ]; then
  python3 - << 'EOF'
from pathlib import Path
p = Path("/app/tools/sp_api_restrictions_daemon.py")
txt = p.read_text()
old = 'prev["unlocked_at"] if prev else 0'
new = '(prev["unlocked_at"] if (prev and "unlocked_at" in prev.keys() and prev["unlocked_at"]) else 0)'
if old in txt:
    p.write_text(txt.replace(old, new))
EOF
fi

# 4. Launch background daemons cleanly with trap cleanup on container shutdown
python3 -u /app/tools/auto_cross_catalog_matcher_daemon.py --interval 1800 > /app/data/reports/cross_catalog_daemon.log 2>&1 &
MATCHER_PID=$!

RESTRICTIONS_PID=""
if [ -f /app/tools/sp_api_restrictions_daemon.py ]; then
  python3 -u /app/tools/sp_api_restrictions_daemon.py --loop --max-brands 100 > /app/data/reports/sp_api_restrictions.log 2>&1 &
  RESTRICTIONS_PID=$!
fi

SHEET_PID=""
if [ -f /app/tools/push_full_backlog_to_sheet.py ]; then
  (while true; do sleep 7200; python3 -u /app/tools/push_full_backlog_to_sheet.py >> /app/data/reports/sheet_sync.log 2>&1 || true; done) &
  SHEET_PID=$!
fi

trap "kill $MATCHER_PID $RESTRICTIONS_PID $SHEET_PID 2>/dev/null || true" EXIT INT TERM

# 5. Launch primary foreground orchestrator
exec python3 -u /app/master_orchestrator.py
