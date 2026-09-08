#!/usr/bin/env python3
"""Operator helper for the session pool table.

  show   print every SessionPool record and live leases
  reset  StopRuntimeSession for every known session, then delete ALL items
         (sessions, leases, affinities, idempotency) so the next run starts
         from an empty pool. User workspaces live on the shared S3 Files file
         system and are NOT touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "router"))

from config import Settings  # noqa: E402
from invoker import AgentCoreInvoker  # noqa: E402
from store import PoolStore  # noqa: E402


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scan_all(client, table: str) -> list[dict]:
    items: list[dict] = []
    kwargs = {"TableName": table, "ProjectionExpression": "PK, SK"}
    while True:
        page = client.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("show", "reset"))
    parser.add_argument("--config", default=str(ROOT / "pool.json"))
    parser.add_argument("--yes", action="store_true", help="skip the reset confirmation")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    settings = Settings(
        region=config["region"],
        table_name=config["tableName"],
        runtime_arn=config["runtimeArn"],
        model_id=config["modelId"],
        scheduler_shards=int(config.get("schedulerShards", 2)),
    )
    store = PoolStore(settings)
    sessions = store.list_sessions()

    if args.action == "show":
        for s in sorted(sessions, key=lambda x: x.get("createdAt", 0)):
            leases = store.list_leases(s["runtimeSessionId"])
            print(
                f"{s['runtimeSessionId']}  {s.get('schedulerStatus'):<11} inflight={s.get('inflight', 0)} "
                f"leases={len(leases)} users={s.get('assignedUsers', 0)} gen={s.get('generation', 0)} "
                f"strikes={s.get('strikes', 0)} warmup_ms={s.get('warmupMs')}"
            )
        print(f"{len(sessions)} sessions")
        return 0

    if not args.yes:
        answer = input(f"Stop {len(sessions)} sessions and delete ALL items in {settings.table_name}? [y/N] ")
        if answer.strip().lower() != "y":
            return 1
    invoker = AgentCoreInvoker(settings)
    for s in sessions:
        result = invoker.stop_session(s["runtimeSessionId"])
        print(f"stop {s['runtimeSessionId']}: {result}")
    client = boto3.client("dynamodb", region_name=settings.region)
    keys = scan_all(client, settings.table_name)
    for i in range(0, len(keys), 25):
        batch = [{"DeleteRequest": {"Key": k}} for k in keys[i : i + 25]]
        client.batch_write_item(RequestItems={settings.table_name: batch})
    print(f"deleted {len(keys)} items from {settings.table_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
