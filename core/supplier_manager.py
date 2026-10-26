import os
import sys
import argparse
import json
from typing import Dict, Any

from core.supplier_manager import SupplierManager

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

def list_suppliers(mgr: SupplierManager):
    suppliers = mgr.get_all_suppliers()
    print("=" * 85)
    print(f"{'REGISTERED WHOLESALE DISTRIBUTORS':^85}")
    print("=" * 85)
    if not suppliers:
        print("[-] No distributors found in data/distributors.json.")
        print("=" * 85)
        return

    fmt = "{:<25} | {:<16} | {:<14} | {:<10} | {:<8}"
    print(fmt.format("Distributor Name", "Location", "Payment Method", "CC Surcharge", "Pickup"))
    print("-" * 85)
    for s in suppliers:
        name = s.get("name", "Unknown")[:25]
        loc = f"{s.get('city', '')}, {s.get('state', '')}"[:16]
        terms = s.get("payment_terms", {})
        method = terms.get("payment_method", "ACH")[:14]
        cc_fee = f"{float(terms.get('cc_surcharge_pct', 0.0)) * 100:.1f}%"
        pickup = "YES ($0)" if s.get("self_pickup", False) else "NO"
        print(fmt.format(name, loc, method, cc_fee, pickup))
    print("=" * 85)

def main():
    parser = build_parser()
    args = parser.parse_args()
    mgr = SupplierManager()

    if args.list or len(sys.argv) == 1:
        list_suppliers(mgr)
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
        
        # If supplier already exists, overwrite cleanly
        existing = [s for s in mgr.suppliers if s.get("name", "").strip().lower() != name.lower()]
        existing.append(data)
        mgr.suppliers = existing
        with open(mgr.REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(mgr.suppliers, f, indent=2)

        print(f"[✓] Successfully registered/updated: {name}")
        print(f"    Location: {data['city']}, {data['state']} {data['zip']}")
        print(f"    Payment: {args.payment} (Fee: {args.cc_fee * 100:.1f}%)")
        print(f"    Email: {data['email']}")
        return

    # Toggle Self-Pickup
    if args.pickup:
        name = args.pickup.strip().lower()
        matched = False
        for s in mgr.suppliers:
            if s.get("name", "").strip().lower() == name:
                matched = True
                if args.enable:
                    s["self_pickup"] = True
                    print(f"[✓] Enabled in-house hub self-pickup for: {s.get('name')} ($0 freight, $0 3PL prep)")
                elif args.disable:
                    s["self_pickup"] = False
                    print(f"[✓] Disabled self-pickup for: {s.get('name')} (Reverted to commercial inbound freight)")
                else:
                    print("[!] Specify --enable or --disable when using --pickup.")
                break

        if matched:
            with open(mgr.REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(mgr.suppliers, f, indent=2)
        else:
            print(f"[!] Supplier '{args.pickup}' not found in registry.")
        return

    # Delete Supplier
    if args.delete:
        name = args.delete.strip().lower()
        initial_len = len(mgr.suppliers)
        mgr.suppliers = [s for s in mgr.suppliers if s.get("name", "").strip().lower() != name]
        if len(mgr.suppliers) < initial_len:
            with open(mgr.REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(mgr.suppliers, f, indent=2)
            print(f"[✓] Removed supplier '{args.delete}' from registry.")
        else:
            print(f"[!] Supplier '{args.delete}' not found.")
        return

if __name__ == "__main__":
    main()
