"""Unit tests for the Session Router policy and orchestration; no AWS calls.

The `StubStore` mimics the PoolStore contract in memory so `reserve()` /
`execute()` can be driven end to end, including the §8.2 acquire semantics
(status ACTIVE, inflight < maxInflight) and §8.3 token-checked release.
"""

from __future__ import annotations

import asyncio
import random
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router"))

from config import Settings  # noqa: E402
from invoker import iter_sse_events, retry_conflicts  # noqa: E402
from scheduler import (  # noqa: E402
    Reservation,
    RouteError,
    RouteRequest,
    SessionRouter,
    affinity_is_valid,
    order_candidates,
    remap_allowed,
    required_sessions,
    scale_out_count,
)
from store import STATUS_ACTIVE, STATUS_COLD, STATUS_DRAINING, STATUS_WARMING  # noqa: E402

FAR_FUTURE = 10**11  # the router compares leases against the real clock


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        runtime_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo",
        max_inflight=10,
        target_inflight=7,
        max_sessions=4,
        min_warm_sessions=1,
        affinity_wait_s=0.0,
        queue_wait_s=0.3,
        queue_poll_s=0.01,
        user_lease_wait_s=0.05,
        heartbeat_interval_s=1000.0,
        warmup_timeout_s=5,
        context_externalized=False,  # per-session storage semantics unless a test opts in
    )
    base.update(overrides)
    return Settings(**base)


def session(sid: str, status: str = STATUS_ACTIVE, inflight: int = 0, **extra: Any) -> dict:
    item = {
        "runtimeSessionId": sid,
        "schedulerStatus": status,
        "inflight": inflight,
        "maxInflight": 10,
        "generation": 1,
        "assignedUsers": 0,
        "createdAt": 0,
        "lastActiveAt": FAR_FUTURE,  # "just active": no idle re-probe by default
    }
    item.update(extra)
    return item


class PolicyTests(unittest.TestCase):
    def test_required_sessions_uses_target(self) -> None:
        self.assertEqual(required_sessions(0, 0, 7), 1)
        self.assertEqual(required_sessions(7, 0, 7), 1)
        self.assertEqual(required_sessions(8, 0, 7), 2)
        self.assertEqual(required_sessions(20, 30, 7), 8)

    def test_scale_out_bounded_and_counts_pending(self) -> None:
        s = settings(max_sessions=4)
        pool = [session("a", inflight=10), session("b", STATUS_WARMING)]
        # 10 inflight + 30 waiting -> ceil(40/7)=6 -> capped at 4 -> minus 2 live = 2
        self.assertEqual(scale_out_count(pool, 30, s), 2)
        self.assertEqual(scale_out_count(pool, 30, s, pending=1), 1)
        self.assertEqual(scale_out_count(pool, 0, s), 0)
        self.assertEqual(scale_out_count([], 0, s), 1, "min warm pool")

    def test_order_candidates_prefers_under_target(self) -> None:
        s = settings()
        pool = [
            session("full", inflight=10),
            session("hot", inflight=8),
            session("cool", inflight=2),
            session("cold", STATUS_COLD),
            session("drain", STATUS_DRAINING, inflight=1),
            session("skip", inflight=0),
        ]
        ordered = order_candidates(pool, exclude={"skip"}, settings=s, rng=random.Random(1))
        self.assertEqual([c["runtimeSessionId"] for c in ordered], ["cool", "hot"])

    def test_affinity_validation(self) -> None:
        s = settings()
        good = {
            "runtimeSessionId": "x",
            "leaseUntil": 200,
            "modelId": s.model_id,
            "appVersion": s.app_version,
            "tenantClass": s.tenant_class,
        }
        self.assertTrue(affinity_is_valid(good, s, 100))
        self.assertFalse(affinity_is_valid(good, s, 200), "expired lease")
        self.assertFalse(affinity_is_valid({**good, "appVersion": "v0"}, s, 100))
        self.assertFalse(affinity_is_valid(None, s, 100))

    def test_remap_policy(self) -> None:
        self.assertTrue(remap_allowed(True, session("a")))
        self.assertFalse(remap_allowed(False, session("a")), "resume must stay put")
        self.assertTrue(remap_allowed(False, session("a"), externalized=True), "shared FS: resume may move")
        self.assertTrue(remap_allowed(False, session("a", STATUS_DRAINING)))
        self.assertTrue(remap_allowed(False, None))


class SSETests(unittest.TestCase):
    def test_dynamodb_conflict_classification(self) -> None:
        from botocore.exceptions import ClientError

        from store import is_conditional_failure, is_transaction_conflict, with_conflict_retry

        def err(code: str, reasons: list[str] | None = None) -> ClientError:
            response: dict[str, Any] = {"Error": {"Code": code, "Message": "x"}}
            if reasons is not None:
                response["CancellationReasons"] = [{"Code": r} for r in reasons]
            return ClientError(response, "TransactWriteItems")

        cond = err("TransactionCanceledException", ["ConditionalCheckFailed", "None"])
        conflict = err("TransactionCanceledException", ["TransactionConflict", "None"])
        self.assertTrue(is_conditional_failure(cond))
        self.assertFalse(is_transaction_conflict(cond))
        self.assertTrue(is_transaction_conflict(conflict))
        self.assertFalse(is_conditional_failure(conflict), "contention is not a policy refusal")
        self.assertTrue(is_transaction_conflict(err("TransactionConflictException")))

        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise conflict
            return "ok"

        self.assertEqual(with_conflict_retry(flaky, sleep=lambda _s: None), "ok")
        with self.assertRaises(ClientError):
            with_conflict_retry(lambda: (_ for _ in ()).throw(cond), sleep=lambda _s: None)
        self.assertIsNone(with_conflict_retry(lambda: (_ for _ in ()).throw(cond), sleep=lambda _s: None, swallow_conditional=True))
    def test_incremental_frames_across_chunks(self) -> None:
        raw = b'data: {"event":"delta","text":"a"}\n\ndata: {"event":"complete","result":"ok"}\n\n'
        events = list(iter_sse_events(iter([raw[:15], raw[15:40], raw[40:]])))
        self.assertEqual([e["event"] for e in events], ["delta", "complete"])

    def test_keepalive_and_done_are_ignored(self) -> None:
        raw = b": keepalive\n\ndata: [DONE]\n\ndata: {\"event\":\"x\"}\n\n"
        self.assertEqual([e["event"] for e in iter_sse_events(iter([raw]))], ["x"])

    def test_retry_conflicts_only_on_409(self) -> None:
        class Conflict(Exception):
            response = {"ResponseMetadata": {"HTTPStatusCode": 409}}

        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise Conflict()
            return "ok"

        self.assertEqual(retry_conflicts(flaky, sleep=lambda _s: None), "ok")
        with self.assertRaises(ValueError):
            retry_conflicts(lambda: (_ for _ in ()).throw(ValueError("x")), sleep=lambda _s: None)


class StubStore:
    """Minimal in-memory PoolStore double honouring the acquire/release rules."""

    def __init__(self, s: Settings) -> None:
        self.settings = s
        self.sessions: dict[str, dict[str, Any]] = {}
        self.leases: dict[tuple[str, str], dict[str, Any]] = {}
        self.affinity: dict[tuple[str, str], dict[str, Any]] = {}
        self.user_lease: dict[tuple[str, str], str] = {}
        self.requests: dict[tuple[str, str], dict[str, Any]] = {}
        self.strikes: dict[str, int] = {}
        self.generation_calls: list[tuple] = []
        self._t = 1000

    def now(self) -> int:
        return self._t

    # idempotency
    def claim_request(self, t: str, r: str, u: str):
        if (t, r) in self.requests:
            return self.requests[(t, r)]
        self.requests[(t, r)] = {"status": "CLAIMED", "userId": u}
        return None

    def finish_request(self, t: str, r: str, status: str, result_ref=None, error_code=None):
        self.requests[(t, r)].update(status=status, resultRef=result_ref, errorCode=error_code)

    def release_claim(self, t: str, r: str):
        self.requests.pop((t, r), None)

    # user lease
    def acquire_user_lease(self, t: str, u: str, r: str):
        if (t, u) in self.user_lease:
            return None
        self.user_lease[(t, u)] = r
        return r

    def release_user_lease(self, t: str, u: str, token: str):
        return self.user_lease.pop((t, u), None) is not None

    # affinity
    def get_affinity(self, t: str, u: str):
        return self.affinity.get((t, u))

    def touch_affinity(self, t: str, u: str, v: int):
        return True

    # sessions
    def list_sessions(self, statuses=None):
        return [dict(s) for s in self.sessions.values()]

    def get_session(self, sid: str):
        return dict(self.sessions[sid]) if sid in self.sessions else None

    def create_session(self, sid: str, shard: int):
        self.sessions[sid] = session(sid, STATUS_WARMING, generation=0)
        return self.sessions[sid]

    def set_session_status(self, sid: str, status: str, expected=None, extra=None):
        if sid not in self.sessions:
            return False
        if expected and self.sessions[sid]["schedulerStatus"] not in expected:
            return False
        self.sessions[sid]["schedulerStatus"] = status
        self.sessions[sid].update(extra or {})
        return True

    def record_generation(self, sid: str, boot_id, run_id, started):
        marker = f"{boot_id}:{run_id}"
        meta = self.sessions[sid]
        self.generation_calls.append((sid, marker))
        if meta.get("bootId") == marker:
            return False, meta["generation"]
        meta["bootId"] = marker
        meta["generation"] = meta.get("generation", 0) + 1
        return True, meta["generation"]

    def add_strike(self, sid: str):
        self.strikes[sid] = self.strikes.get(sid, 0) + 1
        return self.strikes[sid]

    def reset_strikes(self, sid: str):
        self.strikes[sid] = 0

    # leases
    def list_leases(self, sid: str):
        return [dict(l) for (s, _), l in self.leases.items() if s == sid]

    def try_acquire(self, sid, *, tenant_id, user_id, request_id, affinity_write=None, count_new_user=False):
        meta = self.sessions.get(sid)
        if meta is None or meta["schedulerStatus"] != STATUS_ACTIVE:
            return None
        if meta["inflight"] >= meta["maxInflight"]:
            return None
        if (sid, request_id) in self.leases:
            return None
        if affinity_write is not None:
            current = self.affinity.get((tenant_id, user_id))
            expected = affinity_write.get("expected_version")
            if expected is None and current is not None:
                return None
            if expected is not None and (current or {}).get("affinityVersion") != expected:
                return None
            self.affinity[(tenant_id, user_id)] = {
                "runtimeSessionId": sid,
                "affinityVersion": (expected or 0) + 1,
                "leaseUntil": self._t + self.settings.affinity_ttl_s,
                "modelId": self.settings.model_id,
                "appVersion": self.settings.app_version,
                "tenantClass": self.settings.tenant_class,
            }
        meta["inflight"] += 1
        if count_new_user:
            meta["assignedUsers"] += 1
        token = f"tok-{request_id}"
        self.leases[(sid, request_id)] = {
            "requestId": request_id,
            "userId": user_id,
            "leaseToken": token,
            "leaseUntil": self._t + 120,
        }
        return {"runtimeSessionId": sid, "requestId": request_id, "leaseToken": token,
                "leaseUntil": self._t + 120,
                "affinityVersion": ((affinity_write or {}).get("expected_version") or 0) + 1
                if affinity_write is not None else None}

    def heartbeat(self, sid, request_id, token):
        return (sid, request_id) in self.leases

    def claim_probe(self, sid, *, idle_before, lock_s):
        meta = self.sessions.get(sid)
        if meta is None or meta["schedulerStatus"] != STATUS_ACTIVE:
            return False
        idle = meta["inflight"] == 0 and meta.get("lastActiveAt", 0) <= idle_before
        if not (meta.get("probeRequired") or idle) or meta.get("probeLockUntil", 0) >= self._t:
            return False
        meta["probeLockUntil"] = self._t + lock_s
        self.probes = getattr(self, "probes", 0) + 1
        return True

    def mark_probe_required(self, sid):
        self.sessions[sid]["probeRequired"] = True

    def finish_probe(self, sid):
        meta = self.sessions[sid]
        meta.pop("probeLockUntil", None)
        meta.pop("probeRequired", None)
        meta["lastActiveAt"] = FAR_FUTURE

    def release(self, sid, request_id, token):
        lease = self.leases.get((sid, request_id))
        if lease is None or (token is not None and lease["leaseToken"] != token):
            return False
        del self.leases[(sid, request_id)]
        self.sessions[sid]["inflight"] -= 1
        return True


class StubInvoker:
    def __init__(self, boot_id: str = "boot-1") -> None:
        self.boot_id = boot_id
        self.calls: list[dict[str, Any]] = []
        self.warmups: list[str] = []
        self.emit_complete = True

    def warmup(self, sid: str):
        self.warmups.append(sid)
        return {"warmup": True, "instance": {"boot_id": self.boot_id, "server_run_id": "r", "started_at": "t"}}

    def stream_invoke(self, sid, *, user_id, request_id, prompt, reset):
        self.calls.append({"sid": sid, "user": user_id, "reset": reset})
        yield {"event": "delta", "text": "hi"}
        if self.emit_complete:
            yield {
                "event": "complete",
                "result": "PONG",
                "is_error": False,
                "claude_session_id": "c1",
                "resumed_from": None if reset else "c0",
                "instance": {"boot_id": self.boot_id, "server_run_id": "r", "started_at": "t"},
            }

    def stop_session(self, sid):
        return {"success": True}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class RouterFlowTests(unittest.TestCase):
    def make(self, **overrides: Any) -> tuple[SessionRouter, StubStore, StubInvoker]:
        s = settings(**overrides)
        store = StubStore(s)
        invoker = StubInvoker()
        router = SessionRouter(s, store, invoker)  # type: ignore[arg-type]
        return router, store, invoker

    async def _collect(self, router: SessionRouter, res: Reservation) -> list[dict[str, Any]]:
        return [e async for e in router.execute(res)]

    def test_first_request_maps_user_and_releases_lease(self) -> None:
        router, store, invoker = self.make()
        store.sessions["s1"] = session("s1", bootId="boot-1:r")
        req = RouteRequest("t", "alice", "r1", "hi", reset=True)

        async def flow():
            res = await router.reserve(req)
            events = await self._collect(router, res)
            return res, events

        res, events = run(flow())
        self.assertEqual(res.session_id, "s1")
        self.assertFalse(res.affinity_hit)
        self.assertEqual(events[0]["event"], "routed")
        self.assertEqual(events[-1]["event"], "complete")
        self.assertEqual(events[-1]["router"]["runtime_session_id"], "s1")
        self.assertEqual(store.affinity[("t", "alice")]["runtimeSessionId"], "s1")
        self.assertEqual(store.sessions["s1"]["inflight"], 0, "released in finally")
        self.assertEqual(store.leases, {})
        self.assertEqual(store.user_lease, {})
        self.assertEqual(store.requests[("t", "r1")]["status"], "COMPLETED")

    def test_resume_sticks_to_affinity_and_backpressures_when_full(self) -> None:
        router, store, _ = self.make(queue_wait_s=0.05)
        store.sessions["s1"] = session("s1", inflight=10, bootId="boot-1:r")
        store.sessions["s2"] = session("s2", inflight=0, bootId="boot-1:r")
        store.affinity[("t", "bob")] = {
            "runtimeSessionId": "s1",
            "affinityVersion": 3,
            "leaseUntil": FAR_FUTURE,
            "modelId": router.settings.model_id,
            "appVersion": router.settings.app_version,
            "tenantClass": router.settings.tenant_class,
        }
        with self.assertRaises(RouteError) as ctx:
            run(router.reserve(RouteRequest("t", "bob", "r2", "continue", reset=False)))
        self.assertEqual(ctx.exception.code, "BACKPRESSURE")
        self.assertEqual(ctx.exception.http_status, 429)
        self.assertEqual(store.sessions["s2"]["inflight"], 0, "must not migrate a resume")
        self.assertNotIn(("t", "r2"), store.requests, "claim released for retry")
        self.assertEqual(store.user_lease, {})

    def test_new_conversation_migrates_with_cas(self) -> None:
        router, store, _ = self.make()
        store.sessions["s1"] = session("s1", inflight=10, bootId="boot-1:r")
        store.sessions["s2"] = session("s2", inflight=1, bootId="boot-1:r")
        store.affinity[("t", "bob")] = {
            "runtimeSessionId": "s1",
            "affinityVersion": 3,
            "leaseUntil": FAR_FUTURE,
            "modelId": router.settings.model_id,
            "appVersion": router.settings.app_version,
            "tenantClass": router.settings.tenant_class,
        }
        res = run(router.reserve(RouteRequest("t", "bob", "r3", "new", reset=True)))
        self.assertEqual(res.session_id, "s2")
        self.assertTrue(res.remapped)
        self.assertEqual(store.affinity[("t", "bob")]["affinityVersion"], 4)
        self.assertEqual(router.metrics["remaps"], 1)

    def test_resume_migrates_when_context_is_externalized(self) -> None:
        """S3 Files: the transcript is reachable from every session, so a resume
        whose affinity session is full moves instead of waiting for a 429."""
        router, store, _ = self.make(context_externalized=True)
        store.sessions["s1"] = session("s1", inflight=10, bootId="boot-1:r")
        store.sessions["s2"] = session("s2", inflight=1, bootId="boot-1:r")
        store.affinity[("t", "bob")] = {
            "runtimeSessionId": "s1",
            "affinityVersion": 3,
            "leaseUntil": FAR_FUTURE,
            "modelId": router.settings.model_id,
            "appVersion": router.settings.app_version,
            "tenantClass": router.settings.tenant_class,
        }
        res = run(router.reserve(RouteRequest("t", "bob", "r4", "continue", reset=False)))
        self.assertEqual(res.session_id, "s2")
        self.assertTrue(res.remapped)
        self.assertEqual(store.affinity[("t", "bob")]["runtimeSessionId"], "s2")

    def test_hard_cap_never_exceeded_under_concurrency(self) -> None:
        router, store, _ = self.make(max_sessions=1, min_warm_sessions=1, queue_wait_s=0.1)
        store.sessions["s1"] = session("s1", bootId="boot-1:r")

        async def flow():
            reqs = [RouteRequest("t", f"u{i}", f"q{i}", "x", reset=True) for i in range(14)]
            results = await asyncio.gather(*(router.reserve(r) for r in reqs), return_exceptions=True)
            return results

        results = run(flow())
        ok = [r for r in results if isinstance(r, Reservation)]
        rejected = [r for r in results if isinstance(r, RouteError)]
        self.assertEqual(len(ok), 10)
        self.assertEqual(len(rejected), 4)
        self.assertTrue(all(r.code == "BACKPRESSURE" for r in rejected))
        self.assertEqual(store.sessions["s1"]["inflight"], 10)

    def test_scale_out_warms_new_session_when_waiters_exceed_target(self) -> None:
        router, store, invoker = self.make(max_sessions=3, queue_wait_s=0.5)
        store.sessions["s1"] = session("s1", inflight=10, bootId="boot-1:r")

        async def flow():
            reqs = [RouteRequest("t", f"w{i}", f"w{i}", "x", reset=True) for i in range(8)]
            return await asyncio.gather(*(router.reserve(r) for r in reqs), return_exceptions=True)

        results = run(flow())
        ok = [r for r in results if isinstance(r, Reservation)]
        self.assertEqual(len(ok), 8, results)
        self.assertGreaterEqual(len(invoker.warmups), 1)
        self.assertLessEqual(len(store.sessions), 3)
        self.assertTrue(all(r.session_id != "s1" for r in ok))
        for sid, meta in store.sessions.items():
            if sid != "s1":
                self.assertEqual(meta["schedulerStatus"], STATUS_ACTIVE)
                self.assertEqual(meta["generation"], 1)

    def test_cold_revivals_count_toward_max_sessions(self) -> None:
        router, store, invoker = self.make(max_sessions=3, queue_wait_s=0.5)
        store.sessions["s1"] = session("s1", inflight=10, bootId="boot-1:r")
        for i in range(4):
            store.sessions[f"c{i}"] = session(f"c{i}", STATUS_COLD, bootId="boot-0:r", assignedUsers=i)

        async def flow():
            reqs = [RouteRequest("t", f"v{i}", f"v{i}", "x", reset=True) for i in range(20)]
            return await asyncio.gather(*(router.reserve(r) for r in reqs), return_exceptions=True)

        results = run(flow())
        ok = [r for r in results if isinstance(r, Reservation)]
        self.assertGreaterEqual(len(ok), 1)
        active = [s for s in store.sessions.values() if s["schedulerStatus"] == STATUS_ACTIVE]
        self.assertLessEqual(len(active), 3, "COLD revivals must respect MAX_SESSIONS")
        self.assertEqual(len(invoker.warmups), 2, "exactly MAX_SESSIONS - live warm-ups")
        self.assertTrue(all(w.startswith("c") for w in invoker.warmups), "reuse COLD before creating")

    def test_idle_session_is_probed_once_before_concurrent_admission(self) -> None:
        """Regression for the fan-out observed on AWS: after an unnoticed
        StopRuntimeSession, N concurrent first-invokes produced N microVMs."""
        router, store, invoker = self.make(queue_wait_s=0.5)
        store.sessions["s1"] = session("s1", bootId="boot-OLD:r", generation=1, lastActiveAt=0)
        invoker.boot_id = "boot-NEW"

        async def flow():
            # 6 waiters stay under TARGET_INFLIGHT so no scale-out warm-up is mixed in.
            reqs = [RouteRequest("t", f"p{i}", f"p{i}", "x", reset=True) for i in range(6)]
            return await asyncio.gather(*(router.reserve(r) for r in reqs), return_exceptions=True)

        results = run(flow())
        ok = [r for r in results if isinstance(r, Reservation)]
        self.assertEqual(len(ok), 6, results)
        self.assertEqual(invoker.warmups, ["s1"], "exactly one probe call")
        self.assertEqual(store.sessions["s1"]["generation"], 2, "new microVM recorded before admission")
        self.assertEqual(router.metrics["probes"], 1)
        self.assertTrue(all(r.generation == 2 for r in ok), "requests admitted after the probe see the new generation")

    def test_cold_affinity_session_is_rewarmed_not_remapped(self) -> None:
        router, store, invoker = self.make(queue_wait_s=0.5)
        store.sessions["s1"] = session("s1", STATUS_COLD, bootId="boot-0:r", generation=1)
        store.sessions["s2"] = session("s2", bootId="boot-1:r")
        store.affinity[("t", "carol")] = {
            "runtimeSessionId": "s1",
            "affinityVersion": 1,
            "leaseUntil": FAR_FUTURE,
            "modelId": router.settings.model_id,
            "appVersion": router.settings.app_version,
            "tenantClass": router.settings.tenant_class,
        }
        res = run(router.reserve(RouteRequest("t", "carol", "c1", "resume", reset=False)))
        self.assertEqual(res.session_id, "s1")
        self.assertTrue(res.warmed_cold_session)
        self.assertEqual(invoker.warmups, ["s1"])
        self.assertEqual(store.sessions["s1"]["generation"], 2, "new microVM generation")

    def test_generation_change_detected_on_complete(self) -> None:
        router, store, invoker = self.make()
        store.sessions["s1"] = session("s1", bootId="boot-OLD:r", generation=1)
        invoker.boot_id = "boot-NEW"

        async def flow():
            res = await router.reserve(RouteRequest("t", "dave", "g1", "hi", reset=False))
            return await self._collect(router, res)

        events = run(flow())
        kinds = [e["event"] for e in events]
        self.assertIn("generation_changed", kinds)
        self.assertEqual(store.sessions["s1"]["generation"], 2)
        self.assertEqual(events[-1]["router"]["generation"], 2)

    def test_missing_complete_is_failure_with_strike(self) -> None:
        router, store, invoker = self.make()
        store.sessions["s1"] = session("s1", bootId="boot-1:r")
        invoker.emit_complete = False

        async def flow():
            res = await router.reserve(RouteRequest("t", "erin", "m1", "hi", reset=True))
            return await self._collect(router, res)

        events = run(flow())
        self.assertEqual(events[-1]["event"], "router_error")
        self.assertEqual(events[-1]["code"], "INCOMPLETE")
        self.assertEqual(store.strikes["s1"], 1)
        self.assertEqual(store.requests[("t", "m1")]["status"], "FAILED")
        self.assertEqual(store.sessions["s1"]["inflight"], 0)
        self.assertTrue(store.sessions["s1"].get("probeRequired"), "failed request forces a probe before re-admission")

        # Next request must trigger exactly one probe even though the session was active a moment ago.
        invoker.emit_complete = True
        invoker.boot_id = "boot-NEW"
        res = run(router.reserve(RouteRequest("t", "erin2", "m2", "hi", reset=True)))
        self.assertEqual(res.session_id, "s1")
        self.assertEqual(invoker.warmups, ["s1"])
        self.assertNotIn("probeRequired", store.sessions["s1"])
        self.assertEqual(store.sessions["s1"]["generation"], 2)

    def test_duplicate_request_is_replayed_or_rejected(self) -> None:
        router, store, _ = self.make()
        store.sessions["s1"] = session("s1", bootId="boot-1:r")
        req = RouteRequest("t", "fay", "dup", "hi", reset=True)

        async def flow():
            res = await router.reserve(req)
            await self._collect(router, res)
            replay = await router.reserve(req)
            return replay

        replay = run(flow())
        self.assertIsNotNone(replay.replay)
        store.requests[("t", "dup")]["status"] = "RUNNING"
        with self.assertRaises(RouteError) as ctx:
            run(router.reserve(req))
        self.assertEqual(ctx.exception.code, "DUPLICATE_REQUEST")

    def test_user_requests_are_serialised(self) -> None:
        router, store, _ = self.make(user_lease_wait_s=0.02)
        store.sessions["s1"] = session("s1", bootId="boot-1:r")
        store.user_lease[("t", "gus")] = "other"
        with self.assertRaises(RouteError) as ctx:
            run(router.reserve(RouteRequest("t", "gus", "u1", "hi", reset=True)))
        self.assertEqual(ctx.exception.code, "USER_BUSY")


if __name__ == "__main__":
    unittest.main()
