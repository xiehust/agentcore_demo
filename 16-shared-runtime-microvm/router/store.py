"""DynamoDB single-table store for UserAffinity, SessionPool, RequestLease and
Idempotency records (SESSION_POOL_ARCHITECTURE.zh.md §7-§8).

Key layout
    USER#<tenant>#<user>      AFFINITY          user -> runtimeSessionId mapping
    USER#<tenant>#<user>      ULEASE            per-user serialisation lease
    SESSION#<sid>             META              SessionPool record
    SESSION#<sid>             LEASE#<requestId> one executing request
    REQUEST#<tenant>#<rid>    IDEMPOTENCY       request claim / result

GSI1 (pool index)
    GSI1PK = POOL#<region>#<tenantClass>#<modelId>#<appVersion>#<shard>
    GSI1SK = <schedulerStatus>#<runtimeSessionId>

All time values are integer epoch seconds. `ttl` is only for asynchronous
cleanup; business decisions always compare `leaseUntil` with `now`.
"""

from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal
from typing import Any, Callable

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from config import Settings

STATUS_COLD = "COLD"
STATUS_WARMING = "WARMING"
STATUS_ACTIVE = "ACTIVE"
STATUS_DRAINING = "DRAINING"
STATUS_QUARANTINED = "QUARANTINED"
ALL_STATUSES = (
    STATUS_COLD,
    STATUS_WARMING,
    STATUS_ACTIVE,
    STATUS_DRAINING,
    STATUS_QUARANTINED,
)

REQ_CLAIMED = "CLAIMED"
REQ_RUNNING = "RUNNING"
REQ_COMPLETED = "COMPLETED"
REQ_FAILED = "FAILED"

_SER = TypeSerializer()
_DE = TypeDeserializer()


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def ser(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _SER.serialize(v) for k, v in item.items() if v is not None}


def de(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {k: _plain(_DE.deserialize(v)) for k, v in item.items()}


def new_session_id(prefix: str) -> str:
    """AgentCore requires runtimeSessionId to be at least 33 characters."""
    return f"{prefix}-{uuid.uuid4().hex}"


def user_pk(tenant_id: str, user_id: str) -> str:
    return f"USER#{tenant_id}#{user_id}"


def session_pk(session_id: str) -> str:
    return f"SESSION#{session_id}"


def request_pk(tenant_id: str, request_id: str) -> str:
    return f"REQUEST#{tenant_id}#{request_id}"


def is_conditional_failure(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        return True
    if code == "TransactionCanceledException":
        reasons = exc.response.get("CancellationReasons") or []
        return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons) and not any(
            r.get("Code") == "TransactionConflict" for r in reasons
        )
    return False


def is_transaction_conflict(exc: ClientError) -> bool:
    """Concurrent writers on the same item; safe to retry after backoff."""
    code = exc.response.get("Error", {}).get("Code", "")
    if code in ("TransactionConflictException", "TransactionInProgressException"):
        return True
    if code == "TransactionCanceledException":
        reasons = exc.response.get("CancellationReasons") or []
        return any(r.get("Code") == "TransactionConflict" for r in reasons)
    return False


def with_conflict_retry(
    operation: Callable[[], Any],
    *,
    attempts: int = 8,
    base_delay: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
    swallow_conditional: bool = False,
) -> Any:
    """Retry DynamoDB item-level transaction conflicts with jittered backoff."""
    for index in range(attempts):
        try:
            return operation()
        except ClientError as exc:
            if swallow_conditional and is_conditional_failure(exc):
                return None
            if not is_transaction_conflict(exc) or index + 1 >= attempts:
                raise
            sleep(min(base_delay * (2**index), 1.0) * (0.5 + random.random()))
    raise AssertionError("unreachable")


class PoolStore:
    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.table = settings.table_name
        self.clock = clock
        self.client = client or boto3.client(
            "dynamodb",
            region_name=settings.region,
            config=Config(
                retries={"mode": "standard", "max_attempts": 5},
                max_pool_connections=64,
            ),
        )

    def now(self) -> int:
        return int(self.clock())

    # ------------------------------------------------------------------ helpers
    def _get(self, pk: str, sk: str, consistent: bool = True) -> dict[str, Any] | None:
        response = self.client.get_item(
            TableName=self.table,
            Key=ser({"PK": pk, "SK": sk}),
            ConsistentRead=consistent,
        )
        return de(response.get("Item"))

    def _query_pk(self, pk: str, sk_prefix: str | None = None) -> list[dict[str, Any]]:
        expression = "PK = :pk"
        values: dict[str, Any] = {":pk": pk}
        if sk_prefix:
            expression += " AND begins_with(SK, :sk)"
            values[":sk"] = sk_prefix
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": expression,
            "ExpressionAttributeValues": ser(values),
            "ConsistentRead": True,
        }
        while True:
            response = self.client.query(**kwargs)
            items.extend(de(item) or {} for item in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    # -------------------------------------------------------------- idempotency
    def claim_request(
        self, tenant_id: str, request_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Claim the request; return None when new, or the existing record."""
        now = self.now()
        item = {
            "PK": request_pk(tenant_id, request_id),
            "SK": "IDEMPOTENCY",
            "tenantId": tenant_id,
            "requestId": request_id,
            "userId": user_id,
            "status": REQ_CLAIMED,
            "createdAt": now,
            "expiresAt": now + self.settings.idempotency_ttl_s,
            "ttl": now + self.settings.idempotency_ttl_s,
        }
        try:
            self.client.put_item(
                TableName=self.table,
                Item=ser(item),
                ConditionExpression="attribute_not_exists(PK)",
            )
            return None
        except ClientError as exc:
            if not is_conditional_failure(exc):
                raise
            return self._get(item["PK"], "IDEMPOTENCY")

    def finish_request(
        self,
        tenant_id: str,
        request_id: str,
        status: str,
        *,
        result_ref: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        names = {"#s": "status", "#f": "finishedAt"}
        values: dict[str, Any] = {":s": status, ":f": self.now()}
        expression = "SET #s = :s, #f = :f"
        if result_ref is not None:
            names["#r"] = "resultRef"
            values[":r"] = result_ref
            expression += ", #r = :r"
        if error_code is not None:
            names["#e"] = "errorCode"
            values[":e"] = error_code[:400]
            expression += ", #e = :e"
        self.client.update_item(
            TableName=self.table,
            Key=ser({"PK": request_pk(tenant_id, request_id), "SK": "IDEMPOTENCY"}),
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=ser(values),
        )

    def release_claim(self, tenant_id: str, request_id: str) -> None:
        """Drop a claim that never started executing so the client may retry."""
        self.client.delete_item(
            TableName=self.table,
            Key=ser({"PK": request_pk(tenant_id, request_id), "SK": "IDEMPOTENCY"}),
        )

    # --------------------------------------------------------------- user lease
    def acquire_user_lease(
        self, tenant_id: str, user_id: str, request_id: str
    ) -> str | None:
        now = self.now()
        token = uuid.uuid4().hex
        try:
            self.client.put_item(
                TableName=self.table,
                Item=ser(
                    {
                        "PK": user_pk(tenant_id, user_id),
                        "SK": "ULEASE",
                        "leaseToken": token,
                        "requestId": request_id,
                        "leaseUntil": now + self.settings.user_lease_ttl_s,
                        "ttl": now + self.settings.user_lease_ttl_s + 3600,
                    }
                ),
                ConditionExpression="attribute_not_exists(PK) OR leaseUntil < :now",
                ExpressionAttributeValues=ser({":now": now}),
            )
            return token
        except ClientError as exc:
            if is_conditional_failure(exc):
                return None
            raise

    def release_user_lease(self, tenant_id: str, user_id: str, token: str) -> bool:
        try:
            self.client.delete_item(
                TableName=self.table,
                Key=ser({"PK": user_pk(tenant_id, user_id), "SK": "ULEASE"}),
                ConditionExpression="leaseToken = :t",
                ExpressionAttributeValues=ser({":t": token}),
            )
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise

    # ----------------------------------------------------------------- affinity
    def get_affinity(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        return self._get(user_pk(tenant_id, user_id), "AFFINITY")

    def _affinity_put(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        generation: int,
        expected_version: int | None,
    ) -> dict[str, Any]:
        now = self.now()
        settings = self.settings
        item = {
            "PK": user_pk(tenant_id, user_id),
            "SK": "AFFINITY",
            "tenantId": tenant_id,
            "userId": user_id,
            "runtimeSessionId": session_id,
            "runtimeArn": settings.runtime_arn,
            "sessionGeneration": generation,
            "affinityVersion": (expected_version or 0) + 1,
            "leaseUntil": now + settings.affinity_ttl_s,
            "lastActiveAt": now,
            "status": "ACTIVE",
            "region": settings.region,
            "tenantClass": settings.tenant_class,
            "modelId": settings.model_id,
            "appVersion": settings.app_version,
            "ttl": now + settings.affinity_ttl_s + 7 * 86400,
        }
        put: dict[str, Any] = {"TableName": self.table, "Item": ser(item)}
        if expected_version is None:
            put["ConditionExpression"] = "attribute_not_exists(PK)"
        else:
            put["ConditionExpression"] = "affinityVersion = :v"
            put["ExpressionAttributeValues"] = ser({":v": expected_version})
        return put

    def touch_affinity(self, tenant_id: str, user_id: str, expected_version: int) -> bool:
        """Extend the affinity lease after a request on the mapped session."""
        now = self.now()
        try:
            self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": user_pk(tenant_id, user_id), "SK": "AFFINITY"}),
                UpdateExpression="SET leaseUntil = :lu, lastActiveAt = :now, #ttl = :ttl",
                ConditionExpression="affinityVersion = :v",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues=ser(
                    {
                        ":lu": now + self.settings.affinity_ttl_s,
                        ":now": now,
                        ":ttl": now + self.settings.affinity_ttl_s + 7 * 86400,
                        ":v": expected_version,
                    }
                ),
            )
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise

    def clear_affinity(self, tenant_id: str, user_id: str) -> None:
        self.client.delete_item(
            TableName=self.table,
            Key=ser({"PK": user_pk(tenant_id, user_id), "SK": "AFFINITY"}),
        )

    # ----------------------------------------------------------------- sessions
    def _gsi_sk(self, status: str, session_id: str) -> str:
        return f"{status}#{session_id}"

    def create_session(self, session_id: str, shard: int) -> dict[str, Any]:
        now = self.now()
        settings = self.settings
        item = {
            "PK": session_pk(session_id),
            "SK": "META",
            "runtimeSessionId": session_id,
            "runtimeArn": settings.runtime_arn,
            "endpoint": settings.runtime_qualifier,
            "region": settings.region,
            "schedulerStatus": STATUS_WARMING,
            "inflight": 0,
            "maxInflight": settings.max_inflight,
            "targetInflight": settings.target_inflight,
            "assignedUsers": 0,
            "generation": 0,
            "strikes": 0,
            "createdAt": now,
            "lastActiveAt": now,
            "statusChangedAt": now,
            "tenantClass": settings.tenant_class,
            "modelId": settings.model_id,
            "appVersion": settings.app_version,
            "schedulerShard": shard,
            "GSI1PK": settings.pool_key(shard),
            "GSI1SK": self._gsi_sk(STATUS_WARMING, session_id),
        }
        self.client.put_item(
            TableName=self.table,
            Item=ser(item),
            ConditionExpression="attribute_not_exists(PK)",
        )
        return item

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._get(session_pk(session_id), "META")

    def list_sessions(
        self, statuses: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Return SessionPool records across all scheduler shards."""
        items: list[dict[str, Any]] = []
        for shard in range(self.settings.scheduler_shards):
            kwargs: dict[str, Any] = {
                "TableName": self.table,
                "IndexName": "GSI1",
                "KeyConditionExpression": "GSI1PK = :pk",
                "ExpressionAttributeValues": ser({":pk": self.settings.pool_key(shard)}),
            }
            while True:
                response = self.client.query(**kwargs)
                items.extend(de(item) or {} for item in response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        if statuses is not None:
            items = [i for i in items if i.get("schedulerStatus") in statuses]
        return items

    def set_session_status(
        self,
        session_id: str,
        status: str,
        *,
        expected: tuple[str, ...] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        now = self.now()
        names = {"#st": "schedulerStatus", "#sk": "GSI1SK", "#ch": "statusChangedAt"}
        values: dict[str, Any] = {
            ":st": status,
            ":sk": self._gsi_sk(status, session_id),
            ":ch": now,
        }
        expression = "SET #st = :st, #sk = :sk, #ch = :ch"
        for index, (key, value) in enumerate(sorted((extra or {}).items())):
            names[f"#e{index}"] = key
            values[f":e{index}"] = value
            expression += f", #e{index} = :e{index}"
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "Key": ser({"PK": session_pk(session_id), "SK": "META"}),
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
        }
        if expected:
            placeholders = []
            for index, value in enumerate(expected):
                values[f":x{index}"] = value
                placeholders.append(f":x{index}")
            kwargs["ConditionExpression"] = f"#st IN ({', '.join(placeholders)})"
        kwargs["ExpressionAttributeValues"] = ser(values)
        try:
            with_conflict_retry(lambda: self.client.update_item(**kwargs))
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise

    def record_generation(
        self,
        session_id: str,
        boot_id: str | None,
        server_run_id: str | None,
        started_at: str | None,
    ) -> tuple[bool, int]:
        """Bump `generation` when the microVM fingerprint changed (§10)."""
        marker = f"{boot_id}:{server_run_id}"
        current = self.get_session(session_id) or {}
        if current.get("bootId") == marker:
            return False, int(current.get("generation", 0))
        try:
            response = with_conflict_retry(
                lambda: self.client.update_item(
                    TableName=self.table,
                    Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                    UpdateExpression=(
                        "SET bootId = :b, environmentStartedAt = :s, "
                        "generation = if_not_exists(generation, :zero) + :one"
                    ),
                    ConditionExpression="attribute_not_exists(bootId) OR bootId <> :b",
                    ExpressionAttributeValues=ser(
                        {":b": marker, ":s": started_at or "", ":zero": 0, ":one": 1}
                    ),
                    ReturnValues="ALL_NEW",
                )
            )
        except ClientError as exc:
            if is_conditional_failure(exc):
                refreshed = self.get_session(session_id) or {}
                return False, int(refreshed.get("generation", 0))
            raise
        updated = de(response.get("Attributes")) or {}
        return True, int(updated.get("generation", 0))

    def add_strike(self, session_id: str) -> int:
        response = with_conflict_retry(
            lambda: self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                UpdateExpression="SET strikes = if_not_exists(strikes, :zero) + :one",
                ExpressionAttributeValues=ser({":zero": 0, ":one": 1}),
                ReturnValues="ALL_NEW",
            )
        )
        return int((de(response.get("Attributes")) or {}).get("strikes", 0))

    def reset_strikes(self, session_id: str) -> None:
        with_conflict_retry(
            lambda: self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                UpdateExpression="SET strikes = :zero",
                ConditionExpression="strikes <> :zero",
                ExpressionAttributeValues=ser({":zero": 0}),
            ),
            swallow_conditional=True,
        )

    def set_inflight(self, session_id: str, value: int) -> None:
        self.client.update_item(
            TableName=self.table,
            Key=ser({"PK": session_pk(session_id), "SK": "META"}),
            UpdateExpression="SET inflight = :v",
            ExpressionAttributeValues=ser({":v": value}),
        )

    def claim_probe(self, session_id: str, *, idle_before: int, lock_s: int) -> bool:
        """Take the single-prober lock (see Settings.reprobe_idle_s). Allowed
        when the session is idle, or when a failed request flagged it."""
        now = self.now()
        try:
            with_conflict_retry(
                lambda: self.client.update_item(
                    TableName=self.table,
                    Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                    UpdateExpression="SET probeLockUntil = :until",
                    ConditionExpression=(
                        "schedulerStatus = :active "
                        "AND (probeRequired = :true OR (inflight = :zero AND lastActiveAt <= :idle)) "
                        "AND (attribute_not_exists(probeLockUntil) OR probeLockUntil < :now)"
                    ),
                    ExpressionAttributeValues=ser(
                        {
                            ":until": now + lock_s,
                            ":active": STATUS_ACTIVE,
                            ":true": True,
                            ":zero": 0,
                            ":idle": idle_before,
                            ":now": now,
                        }
                    ),
                )
            )
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise

    def mark_probe_required(self, session_id: str) -> None:
        """A request on this session failed upstream: force a probe before the
        next admission because its execution environment may be gone."""
        with_conflict_retry(
            lambda: self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                UpdateExpression="SET probeRequired = :true",
                ExpressionAttributeValues=ser({":true": True}),
            )
        )

    def finish_probe(self, session_id: str) -> None:
        with_conflict_retry(
            lambda: self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": session_pk(session_id), "SK": "META"}),
                UpdateExpression=(
                    "SET lastActiveAt = :now, lastProbedAt = :now "
                    "REMOVE probeLockUntil, probeRequired"
                ),
                ExpressionAttributeValues=ser({":now": self.now()}),
            )
        )

    def delete_session(self, session_id: str) -> None:
        self.client.delete_item(
            TableName=self.table, Key=ser({"PK": session_pk(session_id), "SK": "META"})
        )

    # --------------------------------------------------------- request leases
    def list_leases(self, session_id: str) -> list[dict[str, Any]]:
        return self._query_pk(session_pk(session_id), "LEASE#")

    def delete_lease(self, session_id: str, request_id: str) -> None:
        self.client.delete_item(
            TableName=self.table,
            Key=ser({"PK": session_pk(session_id), "SK": f"LEASE#{request_id}"}),
        )

    def try_acquire(
        self,
        session_id: str,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        affinity_write: dict[str, Any] | None = None,
        count_new_user: bool = False,
    ) -> dict[str, Any] | None:
        """§8.2 acquire transaction. Returns the lease or None on contention.

        affinity_write: {"generation": int, "expected_version": int | None}
        when the user is being mapped or remapped to this session.
        """
        now = self.now()
        token = uuid.uuid4().hex
        lease_until = now + self.settings.request_lease_ttl_s
        update_expr = "SET inflight = inflight + :one, lastActiveAt = :now"
        update_values: dict[str, Any] = {":one": 1, ":now": now, ":active": STATUS_ACTIVE}
        if count_new_user:
            update_expr += ", assignedUsers = if_not_exists(assignedUsers, :zero) + :one"
            update_values[":zero"] = 0
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table,
                    "Key": ser({"PK": session_pk(session_id), "SK": "META"}),
                    "UpdateExpression": update_expr,
                    "ConditionExpression": (
                        "schedulerStatus = :active AND inflight < maxInflight "
                        "AND (attribute_not_exists(drainAt) OR drainAt > :now) "
                        "AND (attribute_not_exists(probeLockUntil) OR probeLockUntil < :now)"
                    ),
                    "ExpressionAttributeValues": ser(update_values),
                }
            },
            {
                "Put": {
                    "TableName": self.table,
                    "Item": ser(
                        {
                            "PK": session_pk(session_id),
                            "SK": f"LEASE#{request_id}",
                            "runtimeSessionId": session_id,
                            "tenantId": tenant_id,
                            "userId": user_id,
                            "requestId": request_id,
                            "leaseToken": token,
                            "leaseUntil": lease_until,
                            "heartbeatAt": now,
                            "createdAt": now,
                            "ttl": lease_until + 3600,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        if affinity_write is not None:
            items.append(
                {
                    "Put": self._affinity_put(
                        tenant_id,
                        user_id,
                        session_id,
                        int(affinity_write.get("generation", 0)),
                        affinity_write.get("expected_version"),
                    )
                }
            )
        try:
            with_conflict_retry(lambda: self.client.transact_write_items(TransactItems=items))
        except ClientError as exc:
            if is_conditional_failure(exc):
                return None
            raise
        return {
            "runtimeSessionId": session_id,
            "requestId": request_id,
            "leaseToken": token,
            "leaseUntil": lease_until,
            "affinityVersion": (
                (affinity_write.get("expected_version") or 0) + 1
                if affinity_write is not None
                else None
            ),
        }

    def heartbeat(self, session_id: str, request_id: str, token: str) -> bool:
        now = self.now()
        try:
            self.client.update_item(
                TableName=self.table,
                Key=ser({"PK": session_pk(session_id), "SK": f"LEASE#{request_id}"}),
                UpdateExpression="SET leaseUntil = :lu, heartbeatAt = :now, #ttl = :ttl",
                ConditionExpression="leaseToken = :t",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues=ser(
                    {
                        ":lu": now + self.settings.request_lease_ttl_s,
                        ":now": now,
                        ":ttl": now + self.settings.request_lease_ttl_s + 3600,
                        ":t": token,
                    }
                ),
            )
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise

    def release(self, session_id: str, request_id: str, token: str | None) -> bool:
        """§8.3 release transaction; idempotent when the lease is already gone."""
        now = self.now()
        delete: dict[str, Any] = {
            "TableName": self.table,
            "Key": ser({"PK": session_pk(session_id), "SK": f"LEASE#{request_id}"}),
        }
        if token is not None:
            delete["ConditionExpression"] = "leaseToken = :t"
            delete["ExpressionAttributeValues"] = ser({":t": token})
        else:
            delete["ConditionExpression"] = "attribute_exists(PK)"
        release_items = [
            {"Delete": delete},
            {
                "Update": {
                    "TableName": self.table,
                    "Key": ser({"PK": session_pk(session_id), "SK": "META"}),
                    "UpdateExpression": "SET inflight = inflight - :one, lastActiveAt = :now",
                    "ConditionExpression": "inflight > :zero",
                    "ExpressionAttributeValues": ser({":one": 1, ":zero": 0, ":now": now}),
                }
            },
        ]
        try:
            with_conflict_retry(
                lambda: self.client.transact_write_items(TransactItems=release_items)
            )
            return True
        except ClientError as exc:
            if is_conditional_failure(exc):
                return False
            raise
