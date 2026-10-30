import os
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RetroSweep")

CANDIDATES_FILE = Path("/app/data/discovered_candidates.json")
BCI_REPORT = Path("/app/data/bci_full_opportunity_report.json")
OUTPUT_FILE = Path("/app/data/master_recovered_multipacks.json")

def run_recovery():
    logger.info("Initiating retro-sweep across cached logs and candidate pools...")
    recovered = []
    
    # 1. Audit BCI items that may have failed single-unit margin
    if BCI_REPORT.exists():
        try:
            bci_data = json.loads(BCI_REPORT.read_text())
            items = bci_data if isinstance(bci_data, list) else bci_data.get("items", [])
            for item in items:
                # Mark for multipack inspection if demand was confirmed
                if item.get("bsr", 999999) <= 65000:
                    item["audit_flag"] = "RETRO_MULTIPACK_RECOVERY"
                    recovered.append(item)
            logger.info(f"Recovered {len(recovered)} BCI candidates for multipack expansion.")
        except Exception as e:
            logger.error(f"Error parsing BCI report: {e}")

    # 2. Audit background Keepa candidates
    if CANDIDATES_FILE.exists():
        try:
            candidates = json.loads(CANDIDATES_FILE.read_text())
            for c in candidates:
                c["audit_flag"] = "KEEPA_VARIATION_POOLED"
                recovered.append(c)
            logger.info(f"Appended {len(candidates)} Keepa background discoveries.")
        except Exception as e:
            logger.error(f"Error parsing candidates file: {e}")
            
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(recovered, indent=2))
    logger.info(f"✅ Recovery sweep complete. Saved {len(recovered)} items to {OUTPUT_FILE}.")

if __name__ == "__main__":
    run_recovery()
