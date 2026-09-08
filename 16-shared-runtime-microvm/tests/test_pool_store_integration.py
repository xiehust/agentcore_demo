"""Integration test for router/store.py against a real DynamoDB table.

Skipped unless POOL_TABLE is set (e.g. `POOL_TABLE=srpool-session-pool
AWS_REGION=us-west-2 uv run python -m unittest tests.test_pool_store_integration`).
Uses a throw-away tenant/session namespace and deletes what it creates.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router"))

from config import Settings  # noqa: E402
from store import (  # noqa: E402
    REQ_COMPLETED,
    STATUS_ACTIVE,
    STATUS_COLD,
    STATUS_WARMING,
    PoolStore,
    new_session_id,
)

TABLE = os.environ.get("POOL_TABLE")


@unittest.skipUnless(TABLE, "POOL_TABLE not set")
class StoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            table_name=TABLE or "",
            runtime_arn="arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/itest",
            tenant_class="itest",
            app_version=f"itest-{uuid.uuid4().hex[:6]}",  # private pool key
            max_inflight=2,
            target_inflight=1,
            scheduler_shards=1,
        )
        self.store = PoolStore(self.settings)
        self.tenant = f"itest-{uuid.uuid4().hex[:6]}"
        self.sid = new_session_id("itest")
        self.cleanup: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        for pk, sk in self.cleanup:
            self.store.client.delete_item(TableName=self.settings.table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        for lease in self.store.list_leases(self.sid):
            self.store.delete_lease(self.sid, lease["requestId"])
        self.store.delete_session(self.sid)

    def test_session_lifecycle_acquire_release_generation(self) -> None:
        store = self.store
        store.create_session(self.sid, 0)
        self.assertEqual(store.get_session(self.sid)["schedulerStatus"], STATUS_WARMING)
        self.assertEqual([s["runtimeSessionId"] for s in store.list_sessions()], [self.sid])

        # WARMING session must refuse leases.
        self.assertIsNone(store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u1", request_id="r0"))

        changed, generation = store.record_generation(self.sid, "boot-a", "run-1", "2026-01-01T00:00:00+00:00")
        self.assertTrue(changed)
        self.assertEqual(generation, 1)
        changed, generation = store.record_generation(self.sid, "boot-a", "run-1", "x")
        self.assertFalse(changed)
        changed, generation = store.record_generation(self.sid, "boot-b", "run-2", "x")
        self.assertTrue(changed)
        self.assertEqual(generation, 2)

        self.assertTrue(store.set_session_status(self.sid, STATUS_ACTIVE, expected=(STATUS_WARMING,), extra={"warmupMs": 123}))
        self.assertFalse(store.set_session_status(self.sid, STATUS_ACTIVE, expected=(STATUS_WARMING,)), "CAS on status")
        self.assertEqual(store.list_sessions(statuses=(STATUS_ACTIVE,))[0]["warmupMs"], 123)

        # First mapping (affinity_write with expected_version None) + count_new_user.
        self.cleanup.append((f"USER#{self.tenant}#u1", "AFFINITY"))
        lease1 = store.try_acquire(
            self.sid, tenant_id=self.tenant, user_id="u1", request_id="r1",
            affinity_write={"generation": 2, "expected_version": None}, count_new_user=True,
        )
        self.assertIsNotNone(lease1)
        affinity = store.get_affinity(self.tenant, "u1")
        self.assertEqual(affinity["runtimeSessionId"], self.sid)
        self.assertEqual(affinity["affinityVersion"], 1)
        self.assertEqual(affinity["sessionGeneration"], 2)

        # Affinity hit path (no affinity write, no :zero placeholder).
        lease2 = store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u1", request_id="r2")
        self.assertIsNotNone(lease2)
        meta = store.get_session(self.sid)
        self.assertEqual(meta["inflight"], 2)
        self.assertEqual(meta["assignedUsers"], 1)

        # Hard cap: maxInflight=2 reached.
        self.assertIsNone(store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u2", request_id="r3"))
        # Duplicate requestId also refused.
        self.assertIsNone(store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u1", request_id="r2"))

        # Stale CAS remap must fail (version 1 expected, pass 0).
        self.assertIsNone(store.try_acquire(
            self.sid, tenant_id=self.tenant, user_id="u1", request_id="r4",
            affinity_write={"generation": 2, "expected_version": 0},
        ))

        self.assertTrue(store.heartbeat(self.sid, "r1", lease1["leaseToken"]))
        self.assertFalse(store.heartbeat(self.sid, "r1", "wrong-token"))
        self.assertTrue(store.touch_affinity(self.tenant, "u1", 1))
        self.assertFalse(store.touch_affinity(self.tenant, "u1", 99))

        # Release: wrong token is refused, right token succeeds, repeat is idempotent.
        self.assertFalse(store.release(self.sid, "r1", "wrong"))
        self.assertTrue(store.release(self.sid, "r1", lease1["leaseToken"]))
        self.assertFalse(store.release(self.sid, "r1", lease1["leaseToken"]))
        self.assertTrue(store.release(self.sid, "r2", None), "reconciler-style release without token")
        self.assertEqual(store.get_session(self.sid)["inflight"], 0)
        self.assertEqual(store.list_leases(self.sid), [])

        self.assertEqual(store.add_strike(self.sid), 1)
        store.reset_strikes(self.sid)
        self.assertEqual(store.get_session(self.sid)["strikes"], 0)

        # Probe lock: only for idle sessions, exclusive, and it blocks acquire.
        now = store.now()
        self.assertFalse(store.claim_probe(self.sid, idle_before=now - 3600, lock_s=30), "just active -> no probe")
        store.client.update_item(
            TableName=self.settings.table_name,
            Key={"PK": {"S": f"SESSION#{self.sid}"}, "SK": {"S": "META"}},
            UpdateExpression="SET lastActiveAt = :old",
            ExpressionAttributeValues={":old": {"N": str(now - 100)}},
        )
        self.assertTrue(store.claim_probe(self.sid, idle_before=now - 20, lock_s=30))
        self.assertFalse(store.claim_probe(self.sid, idle_before=now - 20, lock_s=30), "exclusive")
        self.assertIsNone(store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u1", request_id="r9"), "locked")
        store.finish_probe(self.sid)
        lease9 = store.try_acquire(self.sid, tenant_id=self.tenant, user_id="u1", request_id="r9")
        self.assertIsNotNone(lease9)
        self.assertTrue(store.release(self.sid, "r9", lease9["leaseToken"]))

        store.set_inflight(self.sid, 0)
        self.assertTrue(store.set_session_status(self.sid, STATUS_COLD, extra={"drainAt": 1}))

    def test_idempotency_and_user_lease(self) -> None:
        store = self.store
        rid = f"req-{uuid.uuid4().hex[:8]}"
        self.cleanup.append((f"REQUEST#{self.tenant}#{rid}", "IDEMPOTENCY"))
        self.cleanup.append((f"USER#{self.tenant}#u9", "ULEASE"))
        self.assertIsNone(store.claim_request(self.tenant, rid, "u9"))
        existing = store.claim_request(self.tenant, rid, "u9")
        self.assertEqual(existing["status"], "CLAIMED")
        store.finish_request(self.tenant, rid, REQ_COMPLETED, result_ref={"runtimeSessionId": self.sid, "resultTail": "ok"})
        self.assertEqual(store.claim_request(self.tenant, rid, "u9")["resultRef"]["resultTail"], "ok")
        store.release_claim(self.tenant, rid)
        self.assertIsNone(store.claim_request(self.tenant, rid, "u9"))

        token = store.acquire_user_lease(self.tenant, "u9", rid)
        self.assertTrue(token)
        self.assertIsNone(store.acquire_user_lease(self.tenant, "u9", "other"), "serialised per user")
        self.assertFalse(store.release_user_lease(self.tenant, "u9", "bad"))
        self.assertTrue(store.release_user_lease(self.tenant, "u9", token))
        self.assertTrue(store.acquire_user_lease(self.tenant, "u9", "again"))


if __name__ == "__main__":
    unittest.main()
