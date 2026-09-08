#!/usr/bin/env python3
"""How long must a write sit in managed session storage before StopRuntimeSession
so that a fresh environment sees it? For each settle time: new session -> write
marker -> wait N s -> stop -> wait -> new environment -> list.

Usage: uv run python scripts/flush_probe.py --settle 2,5,10,20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "router"))

from config import Settings  # noqa: E402
from fanout_probe import DIR, LIST_SCRIPT, env_of, listing_of, now, sh  # noqa: E402
from invoker import AgentCoreInvoker  # noqa: E402
from runtime_session import RuntimeSession, atomic_write_json, create_agentcore_client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "pool.json"))
    parser.add_argument("--settle", default="2,5,10,20")
    parser.add_argument("--teardown-wait-s", type=int, default=30)
    args = parser.parse_args(argv)
    settles = [int(x) for x in args.settle.split(",") if x.strip()]

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    settings = Settings(region=config["region"], table_name=config["tableName"], runtime_arn=config["runtimeArn"])
    invoker = AgentCoreInvoker(settings)
    runtime = {"region": config["region"], "runtimeArn": config["runtimeArn"]}
    client = create_agentcore_client(runtime, read_timeout=300, max_connections=8)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "results" / f"flush_probe_{stamp}.json"
    rows = []
    for settle in settles:
        sid = f"flush-{uuid.uuid4().hex}"
        session = RuntimeSession(runtime, sid, client)
        row = {"settle_s": settle, "session_id": sid}
        try:
            invoker.warmup(sid)
            w = sh(session, f"mkdir -p {DIR}; echo settle-{settle} > {DIR}/marker.txt; sync; " + LIST_SCRIPT)
            row["written_in_env"] = env_of(w["stdout"])
            row["files_before_stop"] = listing_of(w["stdout"])
            print(f"{now()} settle={settle}s written in env {row['written_in_env']}; waiting")
            time.sleep(settle)
            row["stop"] = invoker.stop_session(sid)
            time.sleep(args.teardown_wait_s)
            invoker.warmup(sid)
            r = sh(session, LIST_SCRIPT)
            row["restored_env"] = env_of(r["stdout"])
            row["files_after_restore"] = listing_of(r["stdout"])
            row["survived"] = any(f.startswith("marker.txt") for f in row["files_after_restore"])
            print(f"{now()} settle={settle}s -> restored in env {row['restored_env']}: {row['files_after_restore']}  survived={row['survived']}")
        finally:
            invoker.stop_session(sid)
        rows.append(row)
        atomic_write_json(out, {"rows": rows})
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
