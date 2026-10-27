import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from typing import Dict, Any, List

class AutonomousRecruiter:
    """
    Automated Wholesale Dealer Onboarding Engine:
    - Scans vacuum opportunities (data/vacuum_opportunities.json)
    - Checks data/distributors.json to see if the supplier is known
    - If NOT registered or contacted, registers them and fires professional onboarding email
    - Attaches Florida Form DR-13 Resale Certificate automatically
    - Tracks sent status in data/recruiter_audit_log.json
    """

    DISTRIBUTOR_FILE = "data/distributors.json"
    VACUUM_FILE = "data/vacuum_opportunities.json"
    LOG_FILE = "data/recruiter_audit_log.json"
    DRAFT_DIR = "data/outreach_drafts"
    DR13_PATH = "data/Florida_DR13_Resale_Certificate.pdf"

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        os.makedirs(self.DRAFT_DIR, exist_ok=True)

        # Business Identity Configuration
        self.company_name = os.getenv("COMPANY_NAME", "Bizbridge Advisors, Inc.")
        self.owner_name = os.getenv("OWNER_NAME", "Owner")
        self.fein = os.getenv("COMPANY_FEIN", "99-1254887 ")
        self.primary_hub = "Parkland, FL 33076"
        self.secondary_hub = "West Bloomfield, MI 48322"

        # Email SMTP Configuration (e.g. Gmail App Password)
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.sender_email = os.getenv("RECRUITER_EMAIL", "")
        self.sender_password = os.getenv("RECRUITER_EMAIL_PASSWORD", "")

    def _load_json(self, path: str, default: Any) -> Any:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[!] Warning reading {path}: {e}")
        return default

    def _save_json(self, path: str, data: Any):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def generate_email_body(self, distributor_name: str, brand_name: str) -> str:
        return f"""Good morning / afternoon,

My name is {self.owner_name}, Procurement Director at {self.company_name}. We operate an omni-channel commercial distribution network with primary warehousing in {self.primary_hub} and secondary distribution in {self.secondary_hub}.

We are seeking to establish a direct wholesale commercial account with {distributor_name} to source replenishment inventory for {brand_name} product lines.

Our Business & Tax Credentials:
- Entity Name: {self.company_name}
- State of Formation: Florida
- Federal EIN: {self.fein}
- Primary Receiving Facility: {self.primary_hub}
- Secondary Hub: {self.secondary_hub}
- Preferred Settlement: Credit Card / Net Terms on Invoice
- Sales Tax Exemption: Active Florida Form DR-13 Resale Certificate (attached)

Could you please send over your new dealer onboarding application alongside your latest wholesale price catalog (.xlsx or .csv format including UPC barcodes, MOQ, and case-pack tiers)? 

Our logistics team is prepared to submit opening purchase orders immediately upon account activation.

Thank you for your time and assistance.

Sincerely,

{self.owner_name}
Procurement & Supply Chain
{self.company_name}
Email: {self.sender_email or 'procurement@meridiancommerce.com'}
Primary Warehouse: {self.primary_hub}
"""

    def send_dealer_application(self, recipient_email: str, distributor_name: str, brand_name: str) -> bool:
        subject = f"New Wholesale Account Application & Catalog Request — {self.company_name}"
        body = self.generate_email_body(distributor_name, brand_name)

        # Check if live SMTP credentials exist
        if not self.sender_email or not self.sender_password:
            # Fallback: Save clean draft locally
            slug = "".join([c if c.isalnum() else "_" for c in distributor_name.lower()])
            draft_path = os.path.join(self.DRAFT_DIR, f"application_{slug}.txt")
            with open(draft_path, "w", encoding="utf-8") as f:
                f.write(f"TO: {recipient_email}\nSUBJECT: {subject}\n\n{body}")
            print(f"  [DRAFT SAVED] Live SMTP credentials not set. Saved application draft to: {draft_path}")
            return True

        # Send via Live SMTP
        try:
            msg = MIMEMultipart()
            msg["From"] = f"{self.company_name} <{self.sender_email}>"
            msg["To"] = recipient_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            # Attach DR-13 if present
            if os.path.exists(self.DR13_PATH):
                with open(self.DR13_PATH, "rb") as f:
                    part = MIMEBase("application", "pdf")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename=Florida_DR13_Resale_Certificate.pdf")
                    msg.attach(part)

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.send_message(msg)

            print(f"  [SENT ✓] Dispatched onboarding email to {distributor_name} ({recipient_email})")
            return True
        except Exception as e:
            print(f"  [!] Failed sending email to {recipient_email}: {e}")
            return False

    def process_unregistered_vacuums(self):
        print("=" * 80)
        print("AUTONOMOUS WHOLESALE RECRUITER & DEALER APPLICATION ENGINE")
        print("=" * 80)

        vacuums: List[Dict[str, Any]] = self._load_json(self.VACUUM_FILE, [])
        distributors: List[Dict[str, Any]] = self._load_json(self.DISTRIBUTOR_FILE, [])
        audit_log: Dict[str, Any] = self._load_json(self.LOG_FILE, {})

        if not vacuums:
            print("[-] No vacuum opportunities found in data/vacuum_opportunities.json.")
            print("    Run 'python -m tools.vacuum_demand_hunter' first to identify targets.")
            return

        known_supplier_names = {d.get("name", "").strip().lower() for d in distributors}
        contacted_emails = set(audit_log.keys())

        dispatched_count = 0

        for opp in vacuums:
            brand = opp.get("brand", "Unknown Brand")
            dist_name = opp.get("recommended_distributor", f"{brand} Wholesale Desk")
            email = opp.get("distributor_email", "")

            if not email:
                print(f"[-] Skipping {dist_name} (No sales desk email on file)")
                continue

            dist_name_clean = dist_name.strip().lower()
            is_registered = dist_name_clean in known_supplier_names
            is_already_contacted = email.lower() in contacted_emails

            # TRIGGER CONDITION: If distributor is NOT in distributors.json OR hasn't been emailed yet
            if not is_registered or not is_already_contacted:
                print(f"\n[*] New Target Found: {brand} -> Distributor: '{dist_name}'")
                print(f"    Recipient: {email}")

                # 1. Register into distributors.json if missing
                if not is_registered:
                    new_dist_entry = {
                        "name": dist_name,
                        "city": "National Hub",
                        "state": "IA" if "frontier" in dist_name_clean else "FL",
                        "zip": "52318" if "frontier" in dist_name_clean else "33076",
                        "email": email,
                        "self_pickup": False,
                        "payment_terms": {
                            "payment_method": "CREDIT_CARD",
                            "cc_surcharge_pct": 0.0,
                            "early_pay_discount_pct": 0.0,
                            "settlement_window_days": 30
                        },
                        "notes": f"Auto-recruited for vacuum brand: {brand}"
                    }
                    distributors.append(new_dist_entry)
                    known_supplier_names.add(dist_name_clean)
                    self._save_json(self.DISTRIBUTOR_FILE, distributors)
                    print(f"    [+] Added '{dist_name}' to {self.DISTRIBUTOR_FILE}")

                # 2. Fire the onboarding application
                success = self.send_dealer_application(email, dist_name, brand)

                # 3. Log the interaction to avoid duplicate outreach
                if success:
                    audit_log[email.lower()] = {
                        "distributor": dist_name,
                        "brand": brand,
                        "timestamp": datetime.now().isoformat(),
                        "status": "DISPATCHED" if (self.sender_email and self.sender_password) else "DRAFTED"
                    }
                    self._save_json(self.LOG_FILE, audit_log)
                    dispatched_count += 1
            else:
                print(f"[-] Supplier '{dist_name}' ({email}) already registered & contacted. Skipping.")

        print("\n" + "=" * 80)
        print(f"[✓] Completed. Total new dealer outreach actions: {dispatched_count}")
        print("=" * 80)

if __name__ == "__main__":
    recruiter = AutonomousRecruiter()
    recruiter.process_unregistered_vacuums()
