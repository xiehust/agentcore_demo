"""Scheduling policy (pure functions) and the async routing orchestrator.

Pure helpers at the top are unit-tested without AWS. `SessionRouter` wires
them to `PoolStore` (DynamoDB) and `AgentCoreInvoker` (AgentCore data plane).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from config import Settings
from invoker import AgentCoreInvoker, IncompleteInvocationError, WarmupError
from store import (
    STATUS_ACTIVE,
    STATUS_COLD,
    STATUS_DRAINING,
    STATUS_QUARANTINED,
    STATUS_WARMING,
    REQ_COMPLETED,
    REQ_FAILED,
    REQ_RUNNING,
    PoolStore,
    new_session_id,
)

log = logging.getLogger("router.scheduler")


# --------------------------------------------------------------------- policy
def affinity_is_valid(affinity: dict[str, Any] | None, settings: Settings, now: int) -> bool:
    """§8.1 step 4: mapping unexpired and pool attributes match."""
    if not affinity:
        return False
    if int(affinity.get("leaseUntil", 0)) <= now:
        return False
    return (
        affinity.get("modelId") == settings.model_id
        and affinity.get("appVersion") == settings.app_version
        and affinity.get("tenantClass") == settings.tenant_class
        and bool(affinity.get("runtimeSessionId"))
    )


def remap_allowed(
    reset: bool, affinity_session: dict[str, Any] | None, *, externalized: bool = False
) -> bool:
    """§9/§10: a fresh conversation may move; a resumed one may move only when
    its context is externalized (shared file system), otherwise it must stay
    unless its session can no longer serve requests at all."""
    if reset or externalized or affinity_session is None:
        return True
    return affinity_session.get("schedulerStatus") in (
        STATUS_DRAINING,
        STATUS_QUARANTINED,
    )


def order_candidates(
    sessions: list[dict[str, Any]],
    *,
    exclude: set[str],
    settings: Settings,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """ACTIVE sessions with a free slot, least-loaded first; ties are shuffled
    (power-of-two-choices flavour without a global hot key)."""
    rng = rng or random.Random()
    usable = [
        s
        for s in sessions
        if s.get("schedulerStatus") == STATUS_ACTIVE
        and int(s.get("inflight", 0)) < int(s.get("maxInflight", settings.max_inflight))
        and s.get("runtimeSessionId") not in exclude
    ]
    rng.shuffle(usable)
    # Prefer sessions under the target; then plain load ordering.
    usable.sort(
        key=lambda s: (
            int(s.get("inflight", 0)) >= settings.target_inflight,
            int(s.get("inflight", 0)),
        )
    )
    return usable


def required_sessions(total_inflight: int, waiting: int, target: int) -> int:
    """§13.1 requiredSessions = ceil(predictedConcurrent / target)."""
    return max(1, math.ceil((total_inflight + waiting) / max(1, target)))


def scale_out_count(
    sessions: list[dict[str, Any]], waiting: int, settings: Settings, *, pending: int = 0
) -> int:
    """How many sessions to warm right now, bounded by MAX_SESSIONS.

    `pending` counts warm-ups already scheduled whose pool record is not yet
    visible, so concurrent waiters do not over-provision."""
    live = [s for s in sessions if s.get("schedulerStatus") in (STATUS_ACTIVE, STATUS_WARMING)]
    total_inflight = sum(int(s.get("inflight", 0)) for s in live)
    required = required_sessions(total_inflight, waiting, settings.target_inflight)
    required = max(required, settings.min_warm_sessions)
    required = min(required, settings.max_sessions)
    return max(0, required - len(live) - pending)


def pick_cold(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    cold = [s for s in sessions if s.get("schedulerStatus") == STATUS_COLD]
    cold.sort(key=lambda s: -int(s.get("assignedUsers", 0)))
    return cold[0] if cold else None


def probe_needed(meta: dict[str, Any], settings: Settings, now: int) -> bool:
    """An ACTIVE session must be probed once before admission when it is idle or
    when a request on it just failed: its microVM may be gone (§10, §15.2)."""
    if meta.get("schedulerStatus") != STATUS_ACTIVE:
        return False
    if int(meta.get("probeLockUntil", 0)) > now:
        return True  # probe in progress elsewhere; keep waiting
    if meta.get("probeRequired") is True:
        return True
    if int(meta.get("inflight", 0)) != 0:
        return False
    return now - int(meta.get("lastActiveAt", 0)) >= settings.reprobe_idle_s


# ------------------------------------------------------------------ data model
@dataclass
class RouteRequest:
    tenant_id: str
    user_id: str
    request_id: str
    prompt: str
    reset: bool = False


@dataclass(eq=False)
class RouteError(Exception):
    code: str
    http_status: int
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"event": "router_error", "code": self.code, "message": self.message, **self.extra}


@dataclass
class Reservation:
    request: RouteRequest
    session_id: str
    generation: int
    lease: dict[str, Any]
    user_lease_token: str
    affinity_version: int | None
    affinity_hit: bool
    remapped: bool
    queue_wait_ms: float
    warmed_cold_session: bool
    replay: dict[str, Any] | None = None  # idempotent replay of a completed result


# ---------------------------------------------------------------- orchestrator
class SessionRouter:
    def __init__(
        self,
        settings: Settings,
        store: PoolStore,
        invoker: AgentCoreInvoker,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.invoker = invoker
        self.clock = clock
        self.waiters = 0
        self._warming: dict[str, asyncio.Task[None]] = {}
        self._probing: dict[str, asyncio.Task[None]] = {}
        self.metrics: dict[str, int] = {
            "requests": 0,
            "affinity_hits": 0,
            "remaps": 0,
            "backpressure": 0,
            "scale_outs": 0,
            "generation_changes": 0,
            "incomplete": 0,
            "cold_resumes": 0,
            "probes": 0,
            "storage_reset_detected": 0,
        }

    async def _db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ----------------------------------------------------------- warm-up path
    def ensure_capacity(self, count: int, sessions: list[dict[str, Any]]) -> None:
        """Schedule warm-ups for `count` sessions (reuse COLD first)."""
        for _ in range(count):
            cold = pick_cold(
                [s for s in sessions if s.get("runtimeSessionId") not in self._warming]
            )
            if cold is not None:
                sid = cold["runtimeSessionId"]
                sessions = [s for s in sessions if s.get("runtimeSessionId") != sid]
                self._spawn_warm(sid, create=False)
            else:
                sid = new_session_id(self.settings.session_id_prefix)
                self._spawn_warm(sid, create=True)
            self.metrics["scale_outs"] += 1

    def _spawn_warm(self, session_id: str, *, create: bool) -> None:
        if session_id in self._warming:
            return
        task = asyncio.create_task(self._warm(session_id, create=create))
        self._warming[session_id] = task
        task.add_done_callback(lambda _t: self._warming.pop(session_id, None))

    async def _warm(self, session_id: str, *, create: bool) -> None:
        shard = random.randrange(self.settings.scheduler_shards)
        try:
            if create:
                await self._db(self.store.create_session, session_id, shard)
            else:
                moved = await self._db(
                    self.store.set_session_status,
                    session_id,
                    STATUS_WARMING,
                    expected=(STATUS_COLD,),
                )
                if not moved:
                    return
            log.info("warming session %s (create=%s)", session_id, create)
            started = self.clock()
            payload = await asyncio.wait_for(
                self._db(self.invoker.warmup, session_id),
                timeout=self.settings.warmup_timeout_s,
            )
            instance = payload.get("instance") or {}
            changed, generation = await self._db(
                self.store.record_generation,
                session_id,
                instance.get("boot_id"),
                instance.get("server_run_id"),
                instance.get("started_at"),
            )
            self._check_storage_marker(session_id, payload, generation)
            await self._db(
                self.store.set_session_status,
                session_id,
                STATUS_ACTIVE,
                expected=(STATUS_WARMING,),
                extra={"lastActiveAt": int(self.clock()), "warmupMs": round((self.clock() - started) * 1000)},
            )
            log.info(
                "session %s ACTIVE generation=%d changed=%s warmup_ms=%d",
                session_id,
                generation,
                changed,
                round((self.clock() - started) * 1000),
            )
        except (WarmupError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            log.warning("warm-up failed for %s: %s: %s", session_id, type(exc).__name__, exc)
            try:
                strikes = await self._db(self.store.add_strike, session_id)
                status = (
                    STATUS_QUARANTINED
                    if strikes >= self.settings.quarantine_strikes
                    else STATUS_COLD
                )
                await self._db(self.store.set_session_status, session_id, status)
            except Exception:  # noqa: BLE001
                log.exception("failed to record warm-up failure for %s", session_id)

    async def ensure_min_warm(self) -> None:
        sessions = await self._db(self.store.list_sessions)
        count = scale_out_count(sessions, 0, self.settings)
        if count:
            self.ensure_capacity(count, sessions)

    # ------------------------------------------------------------ probe path
    def _check_storage_marker(self, session_id: str, payload: dict[str, Any], generation: int) -> None:
        """A re-provisioned environment (generation > 1) should see the marker
        written at the first warm-up on the workspace file system. Its absence
        means the storage came back empty or a different volume was mounted."""
        if payload.get("storage_mounted") is False:
            raise WarmupError("workspace file system is not mounted in the new environment")
        if generation > 1 and payload.get("storage_marker_present") is False:
            self.metrics["storage_reset_detected"] += 1
            log.error(
                "session %s (generation %d) came up with an EMPTY workspace file system "
                "(first-warm-up marker missing); check the mount / access point",
                session_id,
                generation,
            )

    def _spawn_probe(self, session_id: str) -> None:
        if session_id in self._probing:
            return
        task = asyncio.create_task(self._probe(session_id))
        self._probing[session_id] = task
        task.add_done_callback(lambda _t: self._probing.pop(session_id, None))

    async def _probe(self, session_id: str) -> None:
        """Single warm-up call on an idle session so exactly one execution
        environment is (re)provisioned before concurrent requests land."""
        now = int(self.clock())
        claimed = await self._db(
            self.store.claim_probe,
            session_id,
            idle_before=now - self.settings.reprobe_idle_s,
            lock_s=self.settings.warmup_timeout_s,
        )
        if not claimed:
            return
        self.metrics["probes"] += 1
        try:
            payload = await asyncio.wait_for(
                self._db(self.invoker.warmup, session_id),
                timeout=self.settings.warmup_timeout_s,
            )
            instance = payload.get("instance") or {}
            changed, generation = await self._db(
                self.store.record_generation,
                session_id,
                instance.get("boot_id"),
                instance.get("server_run_id"),
                instance.get("started_at"),
            )
            if changed:
                self.metrics["generation_changes"] += 1
                log.warning(
                    "probe found a new execution environment for %s (generation %d)",
                    session_id,
                    generation,
                )
            self._check_storage_marker(session_id, payload, generation)
            await self._db(self.store.finish_probe, session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("probe failed for %s: %s: %s", session_id, type(exc).__name__, exc)
            try:
                strikes = await self._db(self.store.add_strike, session_id)
                status = (
                    STATUS_QUARANTINED
                    if strikes >= self.settings.quarantine_strikes
                    else STATUS_COLD
                )
                await self._db(self.store.set_session_status, session_id, status)
                await self._db(self.store.finish_probe, session_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to record probe failure for %s", session_id)

    # ------------------------------------------------------------ reservation
    async def reserve(self, request: RouteRequest) -> Reservation:
        """§8.1 steps 1-5. Raises RouteError with an HTTP status on failure."""
        self.metrics["requests"] += 1
        settings = self.settings
        existing = await self._db(
            self.store.claim_request, request.tenant_id, request.request_id, request.user_id
        )
        if existing is not None:
            if existing.get("status") == REQ_COMPLETED:
                return Reservation(
                    request=request,
                    session_id=str((existing.get("resultRef") or {}).get("runtimeSessionId", "")),
                    generation=0,
                    lease={},
                    user_lease_token="",
                    affinity_version=None,
                    affinity_hit=False,
                    remapped=False,
                    queue_wait_ms=0.0,
                    warmed_cold_session=False,
                    replay=existing.get("resultRef") or {},
                )
            raise RouteError(
                "DUPLICATE_REQUEST",
                409,
                f"request {request.request_id} is {existing.get('status')}",
            )

        user_token = await self._acquire_user_lease(request)

        self.waiters += 1
        try:
            return await self._find_slot(request, user_token)
        except BaseException:
            await self._db(self.store.release_user_lease, request.tenant_id, request.user_id, user_token)
            await self._db(self.store.release_claim, request.tenant_id, request.request_id)
            raise
        finally:
            self.waiters -= 1

    async def _find_slot(self, request: RouteRequest, user_token: str) -> Reservation:
        settings = self.settings
        started = self.clock()
        deadline = started + settings.queue_wait_s
        affinity_deadline = started + settings.affinity_wait_s
        affinity = await self._db(self.store.get_affinity, request.tenant_id, request.user_id)
        now = int(self.clock())
        valid = affinity_is_valid(affinity, settings, now)
        affinity_sid: str | None = (
            str(affinity.get("runtimeSessionId")) if (valid and affinity) else None
        )
        expected_version = int(affinity["affinityVersion"]) if affinity else None
        warmed_cold = False
        scale_requested = False

        while True:
            sessions = await self._db(self.store.list_sessions)
            now = int(self.clock())
            by_id = {s.get("runtimeSessionId"): s for s in sessions}
            affinity_session = by_id.get(affinity_sid) if affinity_sid else None

            # 0) Idle ACTIVE sessions get one probe before admission (§10).
            probing: set[str] = set()
            for s in sessions:
                if probe_needed(s, settings, now):
                    probing.add(s["runtimeSessionId"])
                    self._spawn_probe(s["runtimeSessionId"])

            # 1) Stick to the affinity session whenever it can serve us.
            if affinity_session is not None and affinity_sid is not None:
                status = affinity_session.get("schedulerStatus")
                if status == STATUS_ACTIVE and affinity_sid not in probing:
                    lease = await self._db(
                        self.store.try_acquire,
                        affinity_sid,
                        tenant_id=request.tenant_id,
                        user_id=request.user_id,
                        request_id=request.request_id,
                    )
                    if lease is not None:
                        self.metrics["affinity_hits"] += 1
                        if warmed_cold:
                            self.metrics["cold_resumes"] += 1
                        return Reservation(
                            request=request,
                            session_id=affinity_sid,
                            generation=int(affinity_session.get("generation", 0)),
                            lease=lease,
                            user_lease_token=user_token,
                            affinity_version=expected_version,
                            affinity_hit=True,
                            remapped=False,
                            queue_wait_ms=round((self.clock() - started) * 1000, 1),
                            warmed_cold_session=warmed_cold,
                        )
                elif status == STATUS_COLD:
                    # §11: prefer reviving the user's own session over remapping
                    # (keeps affinity stable; the workspace itself lives on the
                    # shared file system either way).
                    warmed_cold = True
                    self._spawn_warm(affinity_sid, create=False)
                elif status == STATUS_WARMING:
                    warmed_cold = warmed_cold or affinity_sid in self._warming

            # 2) Other candidates, only when migration is acceptable.
            can_remap = remap_allowed(
                request.reset, affinity_session, externalized=settings.context_externalized
            ) and (affinity_session is None or self.clock() >= affinity_deadline)
            if can_remap:
                exclude = ({affinity_sid} if affinity_sid else set()) | probing
                for candidate in order_candidates(sessions, exclude=exclude, settings=settings):
                    sid = candidate["runtimeSessionId"]
                    lease = await self._db(
                        self.store.try_acquire,
                        sid,
                        tenant_id=request.tenant_id,
                        user_id=request.user_id,
                        request_id=request.request_id,
                        affinity_write={
                            "generation": int(candidate.get("generation", 0)),
                            "expected_version": expected_version,
                        },
                        count_new_user=True,
                    )
                    if lease is None:
                        continue
                    remapped = affinity is not None
                    if remapped:
                        self.metrics["remaps"] += 1
                    return Reservation(
                        request=request,
                        session_id=sid,
                        generation=int(candidate.get("generation", 0)),
                        lease=lease,
                        user_lease_token=user_token,
                        affinity_version=lease.get("affinityVersion"),
                        affinity_hit=False,
                        remapped=remapped,
                        queue_wait_ms=round((self.clock() - started) * 1000, 1),
                        warmed_cold_session=False,
                    )

            # 3) §8.4 / §13.1: scale out once per request, bounded. Warm-ups in
            # flight (new ids or COLD ids being revived) count as pending so
            # concurrent waiters do not over-provision.
            if not scale_requested:
                live_ids = {
                    s.get("runtimeSessionId")
                    for s in sessions
                    if s.get("schedulerStatus") in (STATUS_ACTIVE, STATUS_WARMING)
                }
                pending = sum(1 for sid in self._warming if sid not in live_ids)
                count = scale_out_count(sessions, self.waiters, settings, pending=pending)
                if count > 0:
                    self.ensure_capacity(count, sessions)
                scale_requested = True

            if self.clock() >= deadline:
                self.metrics["backpressure"] += 1
                raise RouteError(
                    "BACKPRESSURE",
                    429,
                    "no session slot became available within the queue SLA",
                    {"retry_after_s": 5, "waited_ms": round((self.clock() - started) * 1000)},
                )
            await asyncio.sleep(settings.queue_poll_s)

    async def _acquire_user_lease(self, request: RouteRequest) -> str:
        deadline = self.clock() + self.settings.user_lease_wait_s
        while True:
            token = await self._db(
                self.store.acquire_user_lease,
                request.tenant_id,
                request.user_id,
                request.request_id,
            )
            if token:
                return token
            if self.clock() >= deadline:
                await self._db(self.store.release_claim, request.tenant_id, request.request_id)
                raise RouteError(
                    "USER_BUSY",
                    409,
                    "another request for this user is still executing",
                )
            await asyncio.sleep(0.25)

    # -------------------------------------------------------------- execution
    async def execute(self, reservation: Reservation) -> AsyncGenerator[dict[str, Any], None]:
        """§8.1 steps 6-9: invoke, validate `complete`, persist, release."""
        request = reservation.request
        if reservation.replay is not None:
            yield {
                "event": "routed",
                "request_id": request.request_id,
                "runtime_session_id": reservation.session_id,
                "replayed": True,
            }
            yield {"event": "complete", "request_id": request.request_id, "replayed": True, **reservation.replay}
            return

        session_id = reservation.session_id
        store = self.store
        yield {
            "event": "routed",
            "request_id": request.request_id,
            "runtime_session_id": session_id,
            "generation": reservation.generation,
            "affinity_hit": reservation.affinity_hit,
            "remapped": reservation.remapped,
            "cold_resume": reservation.warmed_cold_session,
            "queue_wait_ms": reservation.queue_wait_ms,
            "lease_until": reservation.lease.get("leaseUntil"),
        }
        await self._db(store.finish_request, request.tenant_id, request.request_id, REQ_RUNNING)

        heartbeat = asyncio.create_task(self._heartbeat(reservation))
        queue: asyncio.Queue[dict[str, Any] | None | BaseException] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump() -> None:
            try:
                for event in self.invoker.stream_invoke(
                    session_id,
                    user_id=request.user_id,
                    request_id=request.request_id,
                    prompt=request.prompt,
                    reset=request.reset,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except BaseException as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        pump_future = loop.run_in_executor(None, pump)
        complete: dict[str, Any] | None = None
        failure: str | None = None
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    failure = f"{type(item).__name__}: {item}"[:400]
                    # The environment may be gone: force a single probe before
                    # anyone else is admitted to this session.
                    await self._db(store.mark_probe_required, session_id)
                    yield {
                        "event": "router_error",
                        "code": "UPSTREAM",
                        "request_id": request.request_id,
                        "message": failure,
                    }
                    break
                item.setdefault("request_id", request.request_id)
                if item.get("event") == "complete":
                    complete = item
                    instance = item.get("instance") or {}
                    changed, generation = await self._db(
                        store.record_generation,
                        session_id,
                        instance.get("boot_id"),
                        instance.get("server_run_id"),
                        instance.get("started_at"),
                    )
                    if changed:
                        self.metrics["generation_changes"] += 1
                        yield {
                            "event": "generation_changed",
                            "request_id": request.request_id,
                            "runtime_session_id": session_id,
                            "previous_generation": reservation.generation,
                            "generation": generation,
                            "resumed_from": item.get("resumed_from"),
                        }
                    item["router"] = {
                        "runtime_session_id": session_id,
                        "generation": generation,
                        "affinity_hit": reservation.affinity_hit,
                        "remapped": reservation.remapped,
                        "cold_resume": reservation.warmed_cold_session,
                        "queue_wait_ms": reservation.queue_wait_ms,
                    }
                elif item.get("event") == "error":
                    failure = str(item.get("message") or "application error event")[:400]
                yield item
            await pump_future

            if complete is None and failure is None:
                self.metrics["incomplete"] += 1
                failure = "complete SSE event missing"
                await self._db(store.mark_probe_required, session_id)
                strikes = await self._db(store.add_strike, session_id)
                if strikes >= self.settings.quarantine_strikes:
                    await self._db(store.set_session_status, session_id, STATUS_QUARANTINED)
                yield {
                    "event": "router_error",
                    "code": "INCOMPLETE",
                    "request_id": request.request_id,
                    "message": failure,
                    "strikes": strikes,
                }
                raise IncompleteInvocationError(failure)
            if complete is not None and not complete.get("is_error") and failure is None:
                await self._db(store.reset_strikes, session_id)
                if reservation.affinity_hit and reservation.affinity_version is not None:
                    await self._db(
                        store.touch_affinity,
                        request.tenant_id,
                        request.user_id,
                        reservation.affinity_version,
                    )
                await self._db(
                    store.finish_request,
                    request.tenant_id,
                    request.request_id,
                    REQ_COMPLETED,
                    result_ref={
                        "runtimeSessionId": session_id,
                        "generation": complete.get("router", {}).get("generation"),
                        "claudeSessionId": complete.get("claude_session_id"),
                        "resultTail": (complete.get("result") or "")[-200:],
                    },
                )
            else:
                await self._db(
                    store.finish_request,
                    request.tenant_id,
                    request.request_id,
                    REQ_FAILED,
                    error_code=failure or "agent is_error",
                )
        except IncompleteInvocationError:
            await self._db(
                store.finish_request,
                request.tenant_id,
                request.request_id,
                REQ_FAILED,
                error_code="INCOMPLETE",
            )
        finally:
            heartbeat.cancel()
            # §8.3: each release is independent so one failure cannot leak the other.
            try:
                await self._db(
                    store.release,
                    session_id,
                    request.request_id,
                    reservation.lease.get("leaseToken"),
                )
            except Exception:  # noqa: BLE001
                log.exception("session lease release failed for %s", request.request_id)
            try:
                await self._db(
                    store.release_user_lease,
                    request.tenant_id,
                    request.user_id,
                    reservation.user_lease_token,
                )
            except Exception:  # noqa: BLE001
                log.exception("user lease release failed for %s", request.user_id)

    async def _heartbeat(self, reservation: Reservation) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_s)
            ok = await self._db(
                self.store.heartbeat,
                reservation.session_id,
                reservation.request.request_id,
                reservation.lease.get("leaseToken"),
            )
            if not ok:
                log.warning(
                    "lease %s on %s no longer ours; heartbeat stopped",
                    reservation.request.request_id,
                    reservation.session_id,
                )
                return

    # --------------------------------------------------------------- admin
    async def pool_snapshot(self) -> dict[str, Any]:
        sessions = await self._db(self.store.list_sessions)
        now = int(self.clock())
        out = []
        for s in sorted(sessions, key=lambda x: x.get("createdAt", 0)):
            leases = await self._db(self.store.list_leases, s["runtimeSessionId"])
            valid = [l for l in leases if int(l.get("leaseUntil", 0)) > now]
            out.append(
                {
                    "runtime_session_id": s["runtimeSessionId"],
                    "status": s.get("schedulerStatus"),
                    "inflight": int(s.get("inflight", 0)),
                    "valid_leases": len(valid),
                    "max_inflight": int(s.get("maxInflight", 0)),
                    "assigned_users": int(s.get("assignedUsers", 0)),
                    "generation": int(s.get("generation", 0)),
                    "strikes": int(s.get("strikes", 0)),
                    "boot_id": s.get("bootId"),
                    "created_at": s.get("createdAt"),
                    "last_active_at": s.get("lastActiveAt"),
                    "status_changed_at": s.get("statusChangedAt"),
                    "environment_started_at": s.get("environmentStartedAt"),
                    "warmup_ms": s.get("warmupMs"),
                    "lease_users": sorted({l.get("userId", "") for l in valid}),
                }
            )
        counts: dict[str, int] = {}
        for s in out:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        return {
            "now": now,
            "waiters": self.waiters,
            "warming_in_process": sorted(self._warming),
            "probing_in_process": sorted(self._probing),
            "status_counts": counts,
            "metrics": dict(self.metrics),
            "settings": {
                "max_inflight": self.settings.max_inflight,
                "target_inflight": self.settings.target_inflight,
                "max_sessions": self.settings.max_sessions,
                "min_warm_sessions": self.settings.min_warm_sessions,
            },
            "sessions": out,
        }
