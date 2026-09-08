"""EventBridge-scheduled reconciler Lambda (SESSION_POOL_ARCHITECTURE §11, §13, §15).

Every run:
  1. drops expired RequestLeases and repairs `inflight` drift;
  2. moves stale WARMING sessions back to COLD;
  3. drains sessions whose execution environment nears maxLifetime;
  4. stops idle ACTIVE / drained sessions (StopRuntimeSession) -> COLD,
     keeping MIN_WARM_SESSIONS warm;
  5. quarantines sessions with too many strikes;
  6. tops the warm pool back up to MIN_WARM_SESSIONS;
  7. publishes a few CloudWatch gauges.

Packaged together with router/config.py, router/store.py, router/invoker.py.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from config import load_settings
from invoker import AgentCoreInvoker
from store import (
    STATUS_ACTIVE,
    STATUS_COLD,
    STATUS_DRAINING,
    STATUS_QUARANTINED,
    STATUS_WARMING,
    PoolStore,
    new_session_id,
)

log = logging.getLogger("reconciler")
log.setLevel(logging.INFO)
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "SharedRuntimeSessionPool")
INFLIGHT_REPAIR_QUIET_S = int(os.environ.get("INFLIGHT_REPAIR_QUIET_S", "30"))


def _env_age_s(meta: dict[str, Any], now: int) -> int | None:
    raw = meta.get("environmentStartedAt")
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return int(now - started.timestamp())


def reconcile_once(
    store: PoolStore, invoker: AgentCoreInvoker, *, now: int | None = None
) -> dict[str, Any]:
    settings = store.settings
    now = now or store.now()
    sessions = store.list_sessions()
    summary: dict[str, Any] = {
        "sessions": len(sessions),
        "expired_leases": 0,
        "inflight_repaired": 0,
        "stale_warming_reset": 0,
        "drained": 0,
        "stopped": 0,
        "quarantined": 0,
        "warmed": 0,
        "stop_failures": 0,
        "total_inflight": 0,
        "status_counts": {},
    }
    active_like = [
        s for s in sessions if s.get("schedulerStatus") in (STATUS_ACTIVE, STATUS_WARMING)
    ]
    active_count = len(active_like)

    for meta in sessions:
        sid = meta["runtimeSessionId"]
        status = meta.get("schedulerStatus")
        leases = store.list_leases(sid)
        valid = []
        for lease in leases:
            if int(lease.get("leaseUntil", 0)) < now:
                summary["expired_leases"] += 1
                if not store.release(sid, lease["requestId"], None):
                    # inflight already 0; drop the orphan lease alone.
                    store.delete_lease(sid, lease["requestId"])
            else:
                valid.append(lease)
        meta = store.get_session(sid) or meta
        inflight = int(meta.get("inflight", 0))
        quiet = now - int(meta.get("lastActiveAt", 0)) >= INFLIGHT_REPAIR_QUIET_S
        if inflight != len(valid) and quiet:
            store.set_inflight(sid, len(valid))
            summary["inflight_repaired"] += 1
            inflight = len(valid)
        summary["total_inflight"] += inflight

        strikes = int(meta.get("strikes", 0))
        if strikes >= settings.quarantine_strikes and status != STATUS_QUARANTINED:
            store.set_session_status(sid, STATUS_QUARANTINED)
            invoker.stop_session(sid)
            summary["quarantined"] += 1
            status = STATUS_QUARANTINED
            if meta in active_like:
                active_count -= 1

        if status == STATUS_WARMING:
            changed_at = int(meta.get("statusChangedAt", meta.get("createdAt", now)))
            if now - changed_at > settings.warmup_timeout_s * 2:
                if store.set_session_status(sid, STATUS_COLD, expected=(STATUS_WARMING,)):
                    summary["stale_warming_reset"] += 1
                    active_count -= 1
        elif status == STATUS_ACTIVE:
            age = _env_age_s(meta, now)
            if age is not None and age >= settings.drain_age_s:
                store.set_session_status(
                    sid, STATUS_DRAINING, expected=(STATUS_ACTIVE,), extra={"drainAt": now}
                )
                summary["drained"] += 1
                status = STATUS_DRAINING
            elif (
                inflight == 0
                and not valid
                and now - int(meta.get("lastActiveAt", 0)) >= settings.idle_stop_s
                and active_count > settings.min_warm_sessions
            ):
                if _stop_to_cold(store, invoker, sid, expected=(STATUS_ACTIVE,)):
                    summary["stopped"] += 1
                    active_count -= 1
                else:
                    summary["stop_failures"] += 1
        if status == STATUS_DRAINING and inflight == 0 and not valid:
            if _stop_to_cold(store, invoker, sid, expected=(STATUS_DRAINING,)):
                summary["stopped"] += 1
            else:
                summary["stop_failures"] += 1

    # Keep the warm pool at MIN_WARM_SESSIONS (§13.3).
    refreshed = store.list_sessions()
    live = [s for s in refreshed if s.get("schedulerStatus") in (STATUS_ACTIVE, STATUS_WARMING)]
    missing = settings.min_warm_sessions - len(live)
    cold = sorted(
        (s for s in refreshed if s.get("schedulerStatus") == STATUS_COLD),
        key=lambda s: -int(s.get("assignedUsers", 0)),
    )
    for _ in range(max(0, missing)):
        if cold:
            sid = cold.pop(0)["runtimeSessionId"]
            if not store.set_session_status(sid, STATUS_WARMING, expected=(STATUS_COLD,)):
                continue
        else:
            sid = new_session_id(settings.session_id_prefix)
            store.create_session(sid, random.randrange(settings.scheduler_shards))
        if _warm(store, invoker, sid):
            summary["warmed"] += 1

    for meta in store.list_sessions():
        st = meta.get("schedulerStatus", "UNKNOWN")
        summary["status_counts"][st] = summary["status_counts"].get(st, 0) + 1
    return summary


def _stop_to_cold(
    store: PoolStore, invoker: AgentCoreInvoker, sid: str, *, expected: tuple[str, ...]
) -> bool:
    # Mark DRAINING first so the router stops handing out slots, then stop.
    if not store.set_session_status(sid, STATUS_DRAINING, expected=expected, extra={"drainAt": store.now()}):
        return False
    result = invoker.stop_session(sid)
    if not result.get("success") and result.get("error") not in ("ResourceNotFoundException",):
        log.warning("StopRuntimeSession failed for %s: %s", sid, result)
        store.set_session_status(sid, STATUS_ACTIVE, expected=(STATUS_DRAINING,))
        return False
    store.set_session_status(sid, STATUS_COLD, expected=(STATUS_DRAINING,), extra={"inflight": 0})
    log.info("session %s stopped -> COLD", sid)
    return True


def _warm(store: PoolStore, invoker: AgentCoreInvoker, sid: str) -> bool:
    started = time.time()
    try:
        payload = invoker.warmup(sid)
    except Exception as exc:  # noqa: BLE001
        log.warning("warm-up failed for %s: %s", sid, exc)
        strikes = store.add_strike(sid)
        store.set_session_status(
            sid, STATUS_QUARANTINED if strikes >= store.settings.quarantine_strikes else STATUS_COLD
        )
        return False
    instance = payload.get("instance") or {}
    store.record_generation(
        sid, instance.get("boot_id"), instance.get("server_run_id"), instance.get("started_at")
    )
    store.set_session_status(
        sid,
        STATUS_ACTIVE,
        expected=(STATUS_WARMING,),
        extra={"lastActiveAt": store.now(), "warmupMs": round((time.time() - started) * 1000)},
    )
    return True


def _publish_metrics(summary: dict[str, Any], region: str) -> None:
    counts = summary.get("status_counts", {})
    data = [
        {"MetricName": "TotalInflight", "Value": summary["total_inflight"], "Unit": "Count"},
        {"MetricName": "ExpiredLeases", "Value": summary["expired_leases"], "Unit": "Count"},
        {"MetricName": "SessionsStopped", "Value": summary["stopped"], "Unit": "Count"},
    ]
    for status in (STATUS_ACTIVE, STATUS_WARMING, STATUS_COLD, STATUS_DRAINING, STATUS_QUARANTINED):
        data.append(
            {
                "MetricName": "Sessions",
                "Dimensions": [{"Name": "Status", "Value": status}],
                "Value": counts.get(status, 0),
                "Unit": "Count",
            }
        )
    try:
        boto3.client("cloudwatch", region_name=region).put_metric_data(
            Namespace=METRIC_NAMESPACE, MetricData=data
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("put_metric_data failed: %s", exc)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    settings = load_settings()
    store = PoolStore(settings)
    invoker = AgentCoreInvoker(settings)
    summary = reconcile_once(store, invoker)
    _publish_metrics(summary, settings.region)
    log.info("reconcile summary %s", json.dumps(summary))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(handler({}, None), indent=2))
