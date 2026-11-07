#!/usr/bin/env python3
"""
Wholesale Distributor & Will-Call Registry Manager
--------------------------------------------------
Self-contained CLI utility for managing master wholesale distributors,
payment terms, and in-house hub will-call pickup status.
Directly reads and persists data to data/distributors.json.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any, List

ROOT_DIR = Path(__file__).resolve().parent
DISTRIBUTORS_FILE = ROOT_DIR / "data" / "distributors.json"


def load_suppliers() -> List[Dict[str, Any]]:
    if DISTRIBUTORS_FILE.exists():
        try:
            with open(DISTRIBUTORS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[!] Warning loading {DISTRIBUTORS_FILE.name}: {e}")
    return []


def save_suppliers(suppliers: List[Dict[str, Any]]):
    DISTRIBUTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DISTRIBUTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(suppliers, f, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CLI Utility for Managing Master Wholesale Distributors & Hub Pickup Rules"
    )
    parser.add_argument("--list", action="store_true", help="List all registered distributors and terms")
    parser.add_argument("--add", type=str, metavar="NAME", help="Add or update a wholesale distributor")
    parser.add_argument("--city", type=str, default="National Hub", help="Warehouse city")
    parser.add_argument("--state", type=str, default="FL", help="Warehouse state (e.g. FL, MI, IA)")
    parser.add_argument("--zip", type=str, default="33076", help="Warehouse ZIP code")
    parser.add_argument("--email", type=str, default="", help="Dealer sales desk email")
    parser.add_argument(
        "--payment",
        type=str,
        choices=["CREDIT_CARD", "ACH", "TERMS_2_10_NET_30"],
        default="CREDIT_CARD",
        help="Primary settlement method"
    )
    parser.add_argument("--cc-fee", type=float, default=0.0, help="Credit card surcharge (e.g. 0.0 for 0%, 0.03 for 3%)")
    parser.add_argument("--early-discount", type=float, default=0.0, help="Early payment cash discount (e.g. 0.02 for 2%)")
    parser.add_argument("--pickup", type=str, metavar="NAME", help="Toggle self-pickup status for a supplier")
    parser.add_argument("--enable", action="store_true", help="Enable self-pickup ($0 freight / $0 prep)")
    parser.add_argument("--disable", action="store_true", help="Disable self-pickup")
    parser.add_argument("--delete", type=str, metavar="NAME", help="Delete a supplier from registry")
    return parser


def list_suppliers(suppliers: List[Dict[str, Any]]):
    print("=" * 85)
    print(f"{'REGISTERED WHOLESALE DISTRIBUTORS':^85}")
    print("=" * 85)
    if not suppliers:
        print(f"[-] No distributors found in {DISTRIBUTORS_FILE.relative_to(ROOT_DIR)}.")
        print("=" * 85)
        return

    fmt = "{:<25} | {:<16} | {:<14} | {:<10} | {:<8}"
    print(fmt.format("Distributor Name", "Location", "Payment Method", "CC Surcharge", "Pickup"))
    print("-" * 85)
    for s in suppliers:
        name = s.get("name", "Unknown")[:25]
        loc = f"{s.get('city', '')}, {s.get('state', '')}"[:16]
        terms = s.get("payment_terms", {})
        method = str(terms.get("payment_method", "ACH"))[:14]
        cc_fee = f"{float(terms.get('cc_surcharge_pct', 0.0)) * 100:.1f}%"
        pickup = "YES ($0)" if s.get("self_pickup", False) else "NO"
        print(fmt.format(name, loc, method, cc_fee, pickup))
    print("=" * 85)


def main():
    parser = build_parser()
    args = parser.parse_args()
    suppliers = load_suppliers()

    if args.list or len(sys.argv) == 1:
        list_suppliers(suppliers)
        return

    # Add / Update Supplier
    if args.add:
        name = args.add.strip()
        data = {
            "name": name,
            "city": args.city.strip(),
            "state": args.state.strip().upper(),
            "zip": args.zip.strip(),
            "email": args.email.strip(),
            "self_pickup": False,
            "payment_terms": {
                "payment_method": args.payment,
                "cc_surcharge_pct": float(args.cc_fee),
                "early_pay_discount_pct": float(args.early_discount),
                "settlement_window_days": 30
            },
            "notes": "Added via manage_suppliers.py CLI"
        }

        existing = [s for s in suppliers if s.get("name", "").strip().lower() != name.lower()]
        existing.append(data)
        save_suppliers(existing)

        print(f"[✓] Successfully registered/updated: {name}")
        print(f"    Location: {data['city']}, {data['state']} {data['zip']}")
        print(f"    Payment: {args.payment} (Fee: {args.cc_fee * 100:.1f}%)")
        print(f"    Email: {data['email']}")
        return

    # Toggle Self-Pickup
    if args.pickup:
        name = args.pickup.strip().lower()
        matched = False
        for s in suppliers:
            if s.get("name", "").strip().lower() == name:
                matched = True
                if args.enable:
                    s["self_pickup"] = True
                    print(f"[✓] Enabled in-house hub self-pickup for: {s.get('name')} ($0 freight, $0 prep)")
                elif args.disable:
                    s["self_pickup"] = False
                    print(f"[✓] Disabled self-pickup for: {s.get('name')} (Reverted to commercial inbound freight)")
                else:
                    print("[!] Specify --enable or --disable when using --pickup.")
                break

        if matched:
            save_suppliers(suppliers)
        else:
            print(f"[!] Supplier '{args.pickup}' not found in registry.")
        return

    # Delete Supplier
    if args.delete:
        name = args.delete.strip().lower()
        initial_len = len(suppliers)
        filtered = [s for s in suppliers if s.get("name", "").strip().lower() != name]
        if len(filtered) < initial_len:
            save_suppliers(filtered)
            print(f"[✓] Removed supplier '{args.delete}' from registry.")
        else:
            print(f"[!] Supplier '{args.delete}' not found.")
        return


if __name__ == "__main__":
    main()
