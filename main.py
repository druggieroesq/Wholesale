import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pandas as pd

from core.profit_engine import ProfitEngine, ProfitFirstEngine
from core.supplier_manager import SupplierManager
from tools.reverse_gap_engine import ReverseGapEngine

# Lightweight Health-Check Server for Railway port binding
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "online", "service": "wholesale_engine", "hubs": ["Parkland_FL", "West_Bloomfield_MI"]}')

    def log_message(self, format, *args):
        return  # Suppress noisy HTTP access logs

def start_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[Railway] Health check listening on port {port}...")
    server.serve_forever()

def run_scheduled_pipeline():
    """Background engine loop running catalog scans and delta checks."""
    print("[Pipeline] Background worker thread initialized.")
    engine = ProfitEngine()
    supplier_mgr = SupplierManager()

    while True:
        try:
            catalog_dir = "data/incoming_catalogs"
            if os.path.exists(catalog_dir):
                files = [f for f in os.listdir(catalog_dir) if f.endswith((".csv", ".xlsx"))]
                for filename in files:
                    file_path = os.path.join(catalog_dir, filename)
                    print(f"\n[Worker] Ingesting catalog: {filename}...")
                    
                    if filename.endswith(".csv"):
                        df = pd.read_csv(file_path)
                    else:
                        df = pd.read_excel(file_path)

                    # Ingest and scan using confirmed distributor metadata
                    po = engine.scan_catalog_for_profit(df)
                    if not po.empty:
                        out_path = f"data/approved_po_{int(time.time())}.csv"
                        po.to_csv(out_path, index=False)
                        print(f"[✓] Generated Purchase Order with {len(po)} deals: {out_path}")
                        
                    # Move or rename processed file
                    processed_path = os.path.join(catalog_dir, f"processed_{filename}")
                    os.rename(file_path, processed_path)

        except Exception as e:
            print(f"[!] Worker loop error: {e}")

        # Sleep for 15 minutes before checking for new catalogs
        time.sleep(900)

if __name__ == "__main__":
    print("=" * 65)
    print("WHOLESALE ARBITRAGE DUAL-HUB ENGINE INITIALIZED")
    print("Operating Hubs: Parkland, FL & West Bloomfield, MI")
    print("=" * 65)

    # Start background processing thread
    worker_thread = threading.Thread(target=run_scheduled_pipeline, daemon=True)
    worker_thread.start()

    # Start main blocking HTTP server for Railway
    start_http_server()
