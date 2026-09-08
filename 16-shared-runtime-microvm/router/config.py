"""Environment-driven settings shared by the Router service and the reconciler.

Every knob maps to a section of SESSION_POOL_ARCHITECTURE.zh.md; the defaults
implement the recommended values (hard cap 10, target 6-7, 30 minute affinity).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-west-2"))
    table_name: str = field(
        default_factory=lambda: os.environ.get("POOL_TABLE", "shared-runtime-session-pool")
    )
    runtime_arn: str = field(default_factory=lambda: os.environ.get("RUNTIME_ARN", ""))
    runtime_qualifier: str = field(
        default_factory=lambda: os.environ.get("RUNTIME_QUALIFIER", "DEFAULT")
    )
    tenant_class: str = field(
        default_factory=lambda: os.environ.get("TENANT_CLASS", "standard")
    )
    model_id: str = field(
        default_factory=lambda: os.environ.get(
            "MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
    )
    app_version: str = field(default_factory=lambda: os.environ.get("APP_VERSION", "v1"))
    scheduler_shards: int = field(default_factory=lambda: _int("SCHEDULER_SHARDS", 2))

    # §5 / §9: hard cap per session and the normal scheduling target.
    max_inflight: int = field(default_factory=lambda: _int("MAX_INFLIGHT", 10))
    target_inflight: int = field(default_factory=lambda: _int("TARGET_INFLIGHT", 7))
    # Cost guard for the demo; production would derive this from forecasts.
    max_sessions: int = field(default_factory=lambda: _int("MAX_SESSIONS", 6))
    min_warm_sessions: int = field(default_factory=lambda: _int("MIN_WARM_SESSIONS", 1))

    # §7.1 affinity lease and §7.3 request lease / heartbeat.
    affinity_ttl_s: int = field(default_factory=lambda: _int("AFFINITY_TTL_S", 1800))
    request_lease_ttl_s: int = field(default_factory=lambda: _int("REQUEST_LEASE_TTL_S", 120))
    heartbeat_interval_s: float = field(
        default_factory=lambda: _float("HEARTBEAT_INTERVAL_S", 30.0)
    )
    user_lease_ttl_s: int = field(default_factory=lambda: _int("USER_LEASE_TTL_S", 1800))
    idempotency_ttl_s: int = field(default_factory=lambda: _int("IDEMPOTENCY_TTL_S", 86400))

    # §9: wait briefly on the affinity session, then bounded queue, then 429.
    affinity_wait_s: float = field(default_factory=lambda: _float("AFFINITY_WAIT_S", 2.0))
    queue_wait_s: float = field(default_factory=lambda: _float("QUEUE_WAIT_S", 45.0))
    queue_poll_s: float = field(default_factory=lambda: _float("QUEUE_POLL_S", 0.5))
    user_lease_wait_s: float = field(default_factory=lambda: _float("USER_LEASE_WAIT_S", 5.0))

    # §11 lifecycle: idle stop, drain before maxLifetime, quarantine strikes.
    idle_stop_s: int = field(default_factory=lambda: _int("IDLE_STOP_S", 300))
    # §10/§15.2: before admitting requests to a session that is idle (inflight=0
    # for at least this long) or that just failed a request, exactly ONE probe
    # (warm-up call) runs first. Measured on AWS (scripts/fanout_probe.py): N
    # concurrent first calls on a terminated session start N microVMs. With
    # per-session managed storage that lost whole volumes; with the shared S3
    # Files workspace it still means duplicate environments and racing writes,
    # so the probe stays. Small values probe on almost every idle->busy
    # transition (one ~100 ms round trip on a live environment); a session
    # warmed/probed within the window is trusted.
    reprobe_idle_s: int = field(default_factory=lambda: _int("REPROBE_IDLE_S", 3))
    drain_age_s: int = field(default_factory=lambda: _int("DRAIN_AGE_S", 7 * 3600 + 15 * 60))
    warmup_timeout_s: int = field(default_factory=lambda: _int("WARMUP_TIMEOUT_S", 180))
    quarantine_strikes: int = field(default_factory=lambda: _int("QUARANTINE_STRIKES", 3))
    invoke_read_timeout_s: int = field(
        default_factory=lambda: _int("INVOKE_READ_TIMEOUT_S", 1800)
    )
    session_id_prefix: str = field(
        default_factory=lambda: os.environ.get("SESSION_ID_PREFIX", "pool")
    )
    # §10: when user workspaces and Claude transcripts live on storage shared by
    # every session (S3 Files / EFS), a resumed conversation may be moved to
    # another session; with per-session storage it must stay put.
    context_externalized: bool = field(
        default_factory=lambda: os.environ.get("CONTEXT_EXTERNALIZED", "1") not in ("0", "false", "")
    )

    @property
    def pool_key_base(self) -> str:
        return f"POOL#{self.region}#{self.tenant_class}#{self.model_id}#{self.app_version}"

    def pool_key(self, shard: int) -> str:
        return f"{self.pool_key_base}#{shard}"

    def validate(self) -> None:
        if not self.runtime_arn:
            raise ValueError("RUNTIME_ARN is required")
        if not 1 <= self.target_inflight <= self.max_inflight:
            raise ValueError("TARGET_INFLIGHT must be within 1..MAX_INFLIGHT")
        if self.max_sessions < 1 or self.min_warm_sessions < 0:
            raise ValueError("MAX_SESSIONS must be >=1 and MIN_WARM_SESSIONS >=0")
        if self.min_warm_sessions > self.max_sessions:
            raise ValueError("MIN_WARM_SESSIONS cannot exceed MAX_SESSIONS")
        if self.scheduler_shards < 1:
            raise ValueError("SCHEDULER_SHARDS must be >=1")


def load_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
