#!/usr/bin/env python3
"""
Wrapper entrypoint for Railway start command: python /app/run_master_pipeline.py
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import master_orchestrator

if __name__ == "__main__":
    master_orchestrator.main()
