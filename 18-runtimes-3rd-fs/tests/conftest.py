import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "demo/fs_workaround", "demo/secretless_auth", "demo/secretless_auth/github_app_broker", "demo/audit"):
    sys.path.insert(0, str(ROOT / sub))
