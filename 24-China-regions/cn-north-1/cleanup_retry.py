#!/usr/bin/env python3
"""Retry only this run's EFS network cleanup without detaching service interfaces."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results/20260922"
spec = importlib.util.spec_from_file_location(
    "beijing_efs_cleanup", ROOT.parent / "code_interpreter/efs/verify_efs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main():
    extra = json.loads((OUT / "control.json").read_text())
    deadline = time.monotonic() + 1800
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        lab = module.Lab(OUT / "efs", region="cn-north-1", subnet_id=extra["efs_subnet_id"])
        lab.network_cleanup_wait_seconds = 20
        try:
            lab.cleanup()
            module.save(OUT / "efs/cleanup-retry.json", {
                "at": datetime.now(timezone.utc).isoformat(), "status": "complete", "attempt": attempt})
            return
        except RuntimeError as exc:
            module.save(OUT / "efs/cleanup-retry.json", {
                "at": datetime.now(timezone.utc).isoformat(), "status": "waiting_for_service_release",
                "attempt": attempt, "error": str(exc)})
            time.sleep(20)
    module.save(OUT / "efs/cleanup-retry.json", {
        "at": datetime.now(timezone.utc).isoformat(), "status": "pending_after_timeout", "attempt": attempt})


if __name__ == "__main__":
    main()
