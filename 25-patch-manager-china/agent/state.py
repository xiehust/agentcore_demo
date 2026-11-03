"""Batch state in DynamoDB -- the single source of truth (the microVM may vanish at any time)."""
import json
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key

REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
_ddb = boto3.resource("dynamodb", region_name=REGION)
batch_table = _ddb.Table(os.environ.get("BATCH_TABLE", "patchdemo-batch"))

TERMINAL = {"PATCH_OK", "FIXED", "NEEDS_HUMAN"}
DIAGNOSABLE = {"PATCH_FAILED", "PATCH_TIMEOUT", "UNDELIVERABLE", "PRECHECK_FAILED", "DIAGNOSING"}


def now() -> int:
    return int(time.time())


def get_instance(batch_id: str, instance_id: str) -> dict | None:
    return batch_table.get_item(Key={"pk": batch_id, "sk": f"INST#{instance_id}"}).get("Item")


def update_instance(batch_id: str, instance_id: str, condition=None, **fields):
    fields["updated_at"] = now()
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    kw = dict(Key={"pk": batch_id, "sk": f"INST#{instance_id}"},
              UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
              ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
    if condition is not None:
        kw["ConditionExpression"] = condition
    return batch_table.update_item(**kw)["Attributes"]


def claim_for_diagnosis(batch_id: str, instance_id: str, session_id: str) -> tuple[bool, dict]:
    """Idempotent start: only one diagnosis per instance, re-invocations with the same session are no-ops."""
    try:
        item = update_instance(batch_id, instance_id,
                               condition=Attr("status").is_in(["PATCH_FAILED", "PATCH_TIMEOUT", "UNDELIVERABLE", "PRECHECK_FAILED"])
                               & Attr("session_id").eq(session_id),
                               status="DIAGNOSING", diag_started_at=now())
        return True, item
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False, get_instance(batch_id, instance_id) or {}


def bump_counter(batch_id: str, instance_id: str, field: str, limit: int) -> bool:
    """Atomic per-instance quota (remediations / reruns). False when the quota is used up."""
    try:
        batch_table.update_item(Key={"pk": batch_id, "sk": f"INST#{instance_id}"},
                                UpdateExpression="ADD #f :one", ConditionExpression="attribute_not_exists(#f) OR #f < :lim",
                                ExpressionAttributeNames={"#f": field}, ExpressionAttributeValues={":one": 1, ":lim": limit})
        return True
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def bump_batch_remediations(batch_id: str) -> bool:
    """Batch-wide circuit breaker: total automatic remediations <= limit set by the orchestrator."""
    try:
        batch_table.update_item(Key={"pk": batch_id, "sk": "COUNTERS"},
                                UpdateExpression="ADD remediations :one",
                                ConditionExpression="attribute_not_exists(remediations) OR remediations < remediation_limit",
                                ExpressionAttributeValues={":one": 1})
        return True
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def audit(batch_id: str, instance_id: str, kind: str, **data):
    batch_table.put_item(Item={"pk": batch_id, "sk": f"AUDIT#{instance_id}#{time.time():.6f}", "kind": kind,
                               "data": json.dumps(data, ensure_ascii=False, default=str)[:20000], "ts": now()})


def request_approval(batch_id: str, instance_id: str, action: str, reason: str, evidence: str, session_id: str) -> str:
    aid = "APR-" + uuid.uuid4().hex[:8]
    batch_table.put_item(Item={"pk": batch_id, "sk": f"APPROVAL#{aid}", "instance_id": instance_id, "action": action,
                               "reason": reason, "evidence": evidence[:4000], "session_id": session_id,
                               "status": "PENDING_APPROVAL", "ts": now()})
    return aid


def list_prefix(batch_id: str, prefix: str) -> list[dict]:
    items, kw = [], dict(KeyConditionExpression=Key("pk").eq(batch_id) & Key("sk").begins_with(prefix))
    while True:
        r = batch_table.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
