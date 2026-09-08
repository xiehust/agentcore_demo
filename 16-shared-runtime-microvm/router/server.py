"""FastAPI entrypoint for the stateless Session Router (runs on ECS Fargate).

POST /v1/invoke                          route + stream one agent request (SSE)
GET  /v1/pool                            SessionPool snapshot for dashboards/tests
POST /v1/admin/warm?count=N              force warm-ups (demo/ops)
POST /v1/admin/sessions/{sid}/chaos-stop StopRuntimeSession WITHOUT touching
                                         DynamoDB: simulates an unnoticed microVM
                                         replacement (§10 generation handling)
POST /v1/admin/sessions/{sid}/status     manual DRAINING/COLD/... transitions
GET  /healthz                            ALB target health
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Settings, load_settings  # noqa: E402
from invoker import AgentCoreInvoker  # noqa: E402
from scheduler import RouteError, RouteRequest, SessionRouter  # noqa: E402
from store import ALL_STATUSES, PoolStore  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("router")

USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEEPALIVE_S = float(os.environ.get("SSE_KEEPALIVE_S", "15"))
ROUTER_RUN_ID = uuid.uuid4().hex[:12]


def build_router(settings: Settings | None = None) -> SessionRouter:
    settings = settings or load_settings()
    return SessionRouter(settings, PoolStore(settings), AgentCoreInvoker(settings))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=96, thread_name_prefix="router-io"))
    router = build_router()
    app.state.router = router
    log.info(
        "router %s up: table=%s runtime=%s max_inflight=%d target=%d max_sessions=%d",
        ROUTER_RUN_ID,
        router.settings.table_name,
        router.settings.runtime_arn,
        router.settings.max_inflight,
        router.settings.target_inflight,
        router.settings.max_sessions,
    )
    warm_task = asyncio.create_task(router.ensure_min_warm())
    try:
        yield
    finally:
        warm_task.cancel()


app = FastAPI(lifespan=lifespan)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"error": code, "message": message, **extra}
    headers = {}
    if "retry_after_s" in extra:
        headers["Retry-After"] = str(extra["retry_after_s"])
    return JSONResponse(body, status_code=status, headers=headers)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "router_run_id": ROUTER_RUN_ID})


@app.get("/v1/pool")
async def pool(request: Request) -> JSONResponse:
    router: SessionRouter = request.app.state.router
    snapshot = await router.pool_snapshot()
    snapshot["router_run_id"] = ROUTER_RUN_ID
    return JSONResponse(snapshot)


@app.post("/v1/invoke")
async def invoke(request: Request):
    router: SessionRouter = request.app.state.router
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _error(400, "BAD_REQUEST", "payload must be JSON")
    if not isinstance(payload, dict):
        return _error(400, "BAD_REQUEST", "payload must be an object")

    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        return _error(400, "BAD_REQUEST", "user_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    tenant_id = payload.get("tenant_id", "default")
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
        return _error(400, "BAD_REQUEST", "tenant_id is invalid")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error(400, "BAD_REQUEST", "prompt is required")
    reset = payload.get("reset", False)
    if not isinstance(reset, bool):
        return _error(400, "BAD_REQUEST", "reset must be a boolean")
    request_id = payload.get("request_id") or uuid.uuid4().hex
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        return _error(400, "BAD_REQUEST", "request_id must be 1..128 characters")

    route_request = RouteRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
        prompt=prompt,
        reset=reset,
    )
    try:
        reservation = await router.reserve(route_request)
    except RouteError as exc:
        log.info("request %s rejected: %s", request_id, exc.code)
        return _error(exc.http_status, exc.code, exc.message, request_id=request_id, **exc.extra)

    async def stream() -> AsyncIterator[str]:
        events = router.execute(reservation)
        pending: asyncio.Task[dict[str, Any]] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(events.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=KEEPALIVE_S)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield _sse(event)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            await events.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/admin/warm")
async def admin_warm(request: Request, count: int = 1) -> JSONResponse:
    router: SessionRouter = request.app.state.router
    if not 1 <= count <= router.settings.max_sessions:
        return _error(400, "BAD_REQUEST", f"count must be 1..{router.settings.max_sessions}")
    sessions = await router._db(router.store.list_sessions)  # noqa: SLF001
    router.ensure_capacity(count, sessions)
    return JSONResponse({"scheduled": count, "warming": sorted(router._warming)})  # noqa: SLF001


@app.post("/v1/admin/sessions/{session_id}/chaos-stop")
async def admin_chaos_stop(request: Request, session_id: str) -> JSONResponse:
    """Fault injection: terminate the microVM behind a session but leave the
    SessionPool record untouched so the next request must detect the new
    generation on its own."""
    router: SessionRouter = request.app.state.router
    meta = await router._db(router.store.get_session, session_id)  # noqa: SLF001
    if meta is None:
        return _error(404, "NOT_FOUND", "unknown session")
    result = await router._db(router.invoker.stop_session, session_id)  # noqa: SLF001
    log.warning("chaos-stop %s -> %s", session_id, result)
    return JSONResponse(
        {
            "runtime_session_id": session_id,
            "stop": result,
            "pool_status_left_as": meta.get("schedulerStatus"),
            "generation_before": meta.get("generation"),
        }
    )


@app.post("/v1/admin/sessions/{session_id}/status")
async def admin_set_status(request: Request, session_id: str) -> JSONResponse:
    router: SessionRouter = request.app.state.router
    payload = await request.json()
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in ALL_STATUSES:
        return _error(400, "BAD_REQUEST", f"status must be one of {list(ALL_STATUSES)}")
    ok = await router._db(router.store.set_session_status, session_id, status)  # noqa: SLF001
    return JSONResponse({"runtime_session_id": session_id, "status": status, "updated": ok})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
