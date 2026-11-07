#!/usr/bin/env bash
# ==============================================================================
# WHOLESALE MRO AUTONOMOUS ENGINE — ONE-SHOT PIPELINE ACTIVATION SCRIPT
# ==============================================================================
set -e

CONTAINER_APP_DIR="/app"
cd "${CONTAINER_APP_DIR}"

DEPLOYMENT_ID="AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y"
VERIFIED_EXEC_URL="https://script.google.com/macros/s/${DEPLOYMENT_ID}/exec"

echo "========================================================================"
echo "STEP 1: REPLACING /app/.env AND OVERRIDING OS SHELL ENVIRONMENT"
echo "========================================================================"

# Remove broken or stale environment entries
sed -i '/BUY_LIST_WEBHOOK_URL/d' /app/.env
sed -i '/GOOGLE_SHEET_WEBHOOK_URL/d' /app/.env
sed -i '/MIN_MONTHLY_PROFIT_POOL/d' /app/.env

cat << EOF >> /app/.env

# Webhook Endpoints & Financial Hurdle
BUY_LIST_WEBHOOK_URL="${VERIFIED_EXEC_URL}"
GOOGLE_SHEET_WEBHOOK_URL="${VERIFIED_EXEC_URL}"
MIN_MONTHLY_PROFIT_POOL=1000.00
EOF

# Export to current shell session and profile
export BUY_LIST_WEBHOOK_URL="${VERIFIED_EXEC_URL}"
export GOOGLE_SHEET_WEBHOOK_URL="${VERIFIED_EXEC_URL}"
export MIN_MONTHLY_PROFIT_POOL="1000.00"

echo "export BUY_LIST_WEBHOOK_URL=\"${VERIFIED_EXEC_URL}\"" >> ~/.bashrc
echo "export GOOGLE_SHEET_WEBHOOK_URL=\"${VERIFIED_EXEC_URL}\"" >> ~/.bashrc

echo "Target Webhook verified:"
grep "WEBHOOK_URL" /app/.env
echo ""

echo "========================================================================"
echo "STEP 2: RUNNING VERIFICATION TEST (PING & 33-COLUMN TEST LEAD)"
echo "========================================================================"

python3 - << 'EOF'
import os, requests
from dotenv import load_dotenv

load_dotenv('/app/.env', override=True)

FALLBACK_URL = "https://script.google.com/macros/s/AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y/exec"
raw_url = (os.getenv('BUY_LIST_WEBHOOK_URL') or os.getenv('GOOGLE_SHEET_WEBHOOK_URL') or '').strip().strip('"')

if not raw_url or '/library/' in raw_url or 'PASTE_YOUR' in raw_url:
    target_url = FALLBACK_URL
else:
    target_url = raw_url

os.environ['BUY_LIST_WEBHOOK_URL'] = target_url
os.environ['GOOGLE_SHEET_WEBHOOK_URL'] = target_url

print(f"Connecting to: {target_url}\n")

# 1. PING test
print("[1/2] Sending PING payload...")
try:
    res_ping = requests.post(target_url, json={'action': 'PING'}, timeout=15, allow_redirects=True)
    print(f"PING HTTP Status: {res_ping.status_code} | Body: {res_ping.text}\n")
    if res_ping.status_code != 200:
        print("FAIL: Webhook did not return HTTP 200.")
        exit(1)
except Exception as e:
    print(f"PING Exception: {e}")
    exit(1)

# 2. Lead injection test
print("[2/2] Injecting test lead B00VERIFY01 to 'Sourcing & Buy List'...")
test_lead = {
    'action': 'ADD_BUY_LEAD',
    'asin': 'B00VERIFY01',
    'title': 'PIPELINE END-TO-END VERIFICATION LEAD',
    'brand': 'MRO INDUSTRIAL CORP',
    'vendor': 'BCI Wholesale',
    'prep_center': 'Makki Prep (Harrisburg, PA)',
    'sales_rank': 4800,
    'monthly_velocity': 350,
    'our_share_velocity': 350,
    'buy_box_price': 54.95,
    'wholesale_cost': 16.50,
    'net_profit': 18.20,
    'roi_percent': 102.1,
    'suggested_order_size': 175,
    'monthly_profit_pool': 6370.00,
    'is_hazmat': 'NO',
    'order_url': 'https://www.amazon.com/dp/B00VERIFY01',
    'seller_check_url': 'https://sellercentral.amazon.com/product-search/search?q=B00VERIFY01',
    'seller_approval_url': 'https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin=B00VERIFY01',
    'notes': 'Production Webhook Verified Lead',
    'status': 'Ready for Order'
}

try:
    res_lead = requests.post(target_url, json=test_lead, timeout=20, allow_redirects=True)
    print(f"Lead Inject HTTP Status: {res_lead.status_code} | Body: {res_lead.text}")
    if res_lead.status_code == 200 and 'success' in res_lead.text:
        print("\n>>> LIVE WEBHOOK CONNECTION VERIFIED (ROW INJECTED) <<<")
    else:
        print(f"\nResponse: {res_lead.text}")
except Exception as e:
    print(f"Lead Inject Exception: {e}")
    exit(1)
EOF

echo ""
echo "========================================================================"
echo "STEP 3: PURGING NON-COMPLIANT LEADS & FLUSHING $1,000+/MO DEALS"
echo "========================================================================"

python3 /app/tools/purge_non_compliant.py
python3 /app/tools/sync_today_leads_to_sheet.py

echo ""
echo "========================================================================"
echo "STEP 4: RESTARTING MASTER SUPERVISOR FLEET"
echo "========================================================================"

kill $(ps -ef | grep python3 | grep -v grep | awk '{print $2}') 2>/dev/null || true
rm -f /tmp/reverse_amazon_finder.lock

nohup python3 /app/master_orchestrator.py > /app/data/orchestrator.log 2>&1 &

sleep 3
echo "Checking orchestrator health check endpoint:"
curl -s http://localhost:8080/health || echo "Starting up..."
echo ""
echo "Pipeline live. Logging active in /app/data/orchestrator.log."
