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

# 3. Start background daemons (Cross-Catalog Matcher + SP-API Restrictions Daemon)
python3 /app/tools/auto_cross_catalog_matcher_daemon.py --interval 1800 > /app/data/reports/cross_catalog_daemon.log 2>&1 &
if [ -f /app/tools/sp_api_restrictions_daemon.py ]; then
  python3 /app/tools/sp_api_restrictions_daemon.py --loop --max-brands 100 > /app/data/reports/sp_api_restrictions.log 2>&1 &
fi

# 4. Launch primary web/worker service (keep your existing foreground CMD below if Railway uses a web server)
exec tail -f /app/data/reports/cross_catalog_daemon.log
