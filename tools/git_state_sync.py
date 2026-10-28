import os
import sys
import subprocess
from datetime import datetime

class GitStateSync:
    """
    State persistence daemon to commit local runtime ledgers
    (product_actions.json, daily manifests) back to GitHub from container environments.
    """
    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN", "").strip()
        self.repo_url = os.getenv("GIT_REPO_URL", "https://github.com/druggieroesq/Wholesale.git").strip()

    def run_command(self, cmd: list) -> bool:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if res.stdout:
                print(f"[git] {res.stdout.strip()}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"[git error] Command failed: {' '.join(cmd)}")
            print(f"[git error] Details: {e.stderr.strip()}")
            return False

    def sync_state(self, commit_msg: str = "chore(state): automated ledger persistence") -> bool:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        full_msg = f"{commit_msg} [{timestamp}]"

        print(f"[*] Committing state changes: {full_msg}")
        
        # Configure Git bot identity
        self.run_command(["git", "config", "user.name", "WholesaleAutomationBot"])
        self.run_command(["git", "config", "user.email", "bot@wholesale.automation"])

        # Stage data ledger files
        self.run_command(["git", "add", "data/product_actions.json"])
        if os.path.exists("data/distributors.json"):
            self.run_command(["git", "add", "data/distributors.json"])

        # Check if changes exist
        diff_res = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff_res.returncode == 0:
            print("[*] No state ledger modifications to commit.")
            return True

        if not self.run_command(["git", "commit", "-m", full_msg]):
            return False

        # Set remote URL with token if provided in Railway env
        if self.github_token:
            clean_url = self.repo_url.replace("https://", "")
            authed_url = f"https://x-access-token:{self.github_token}@{clean_url}"
            self.run_command(["git", "remote", "set-url", "origin", authed_url])

        print("[*] Pushing ledger to GitHub origin main...")
        return self.run_command(["git", "push", "origin", "main"])


if __name__ == "__main__":
    syncer = GitStateSync()
    success = syncer.sync_state()
    sys.exit(0 if success else 1)
