"""Step Functions task Lambda (in the ops VPC, private subnets) -- one function, dispatched by `action`.

Batch flow: init -> per wave {send_patch (PingStatus + disk-headroom precheck) -> tick every 30 s} -> tick (drain) -> report.
Any state-machine error or the batch timeout -> abort (every non-terminal host to NEEDS_HUMAN, agents stopped) -> report.

Patch Manager does the bulk work; the agent only sees failures. State lives in DynamoDB so that any
instance can be re-driven on its own, and nothing here depends on a single long-lived agent session.
"""
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ["AWS_REGION"]
PARTITION = "aws-cn" if REGION.startswith("cn-") else "aws"
RUNTIME_ARN = os.environ["RUNTIME_ARN"]
SPOKE_ACCOUNTS = os.environ["SPOKE_ACCOUNTS"].split(",")
SPOKE_ROLE = os.environ.get("SPOKE_ROLE", "PatchAgentSpokeRole")
EXTERNAL_ID = os.environ["EXTERNAL_ID"]
PATCH_GROUP = os.environ.get("PATCH_GROUP", "patchdemo")
ONCALL_ROLE_ARN = os.environ["ONCALL_ROLE_ARN"]
table = boto3.resource("dynamodb", region_name=REGION).Table(os.environ["BATCH_TABLE"])
TERMINAL = {"PATCH_OK", "FIXED", "NEEDS_HUMAN", "SKIPPED_HALTED"}
_spokes: dict = {}
AWS_CFG = Config(retries={"max_attempts": 8, "mode": "adaptive"})  # SSM/EC2 throttle at fleet scale
SSM_MAX_IDS = 50       # SendCommand / InstanceIds filter limit
MAX_HOST_ERRORS = 5    # consecutive per-host API errors before a human takes over
MAX_DISPATCH = 3       # first dispatch + 2 retries


def now():
    return int(time.time())


def spoke(account: str) -> boto3.Session:
    if account not in _spokes or _spokes[account][1] < time.time():
        c = boto3.client("sts", region_name=REGION).assume_role(
            RoleArn=f"arn:{PARTITION}:iam::{account}:role/{SPOKE_ROLE}", RoleSessionName="patch-orchestrator",
            ExternalId=EXTERNAL_ID)["Credentials"]
        _spokes[account] = (boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                          aws_session_token=c["SessionToken"], region_name=REGION), time.time() + 3000)
    return _spokes[account][0]


def ssm_c(account):
    return spoke(account).client("ssm", config=AWS_CFG)


def chunks(xs, n=SSM_MAX_IDS):
    xs = list(xs)
    return [xs[k:k + n] for k in range(0, len(xs), n)]


def items(batch_id, prefix="INST#"):
    out, kw = [], dict(KeyConditionExpression=Key("pk").eq(batch_id) & Key("sk").begins_with(prefix))
    while True:
        r = table.query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def upd(batch_id, iid, **f):
    f["updated_at"] = now()
    table.update_item(Key={"pk": batch_id, "sk": f"INST#{iid}"}, UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in f),
                      ExpressionAttributeNames={f"#{k}": k for k in f}, ExpressionAttributeValues={f":{k}": v for k, v in f.items()})


def upd_if(batch_id, iid, statuses, **f):
    """Conditional write: only if the host is still in one of `statuses` (never overwrite FIXED written by the agent)."""
    f["updated_at"] = now()
    try:
        table.update_item(Key={"pk": batch_id, "sk": f"INST#{iid}"}, UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in f),
                          ConditionExpression="#status IN (" + ", ".join(f":s{n}" for n in range(len(statuses))) + ")",
                          ExpressionAttributeNames={**{f"#{k}": k for k in f}, "#status": "status"},
                          ExpressionAttributeValues={**{f":{k}": v for k, v in f.items()},
                                                     **{f":s{n}": v for n, v in enumerate(statuses)}})
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def session_id(batch_id, iid):
    sid = f"{batch_id}-{iid}"
    return sid if len(sid) >= 33 else sid.ljust(33, "0")  # runtimeSessionId must be >= 33 chars


# ------------------------------------------------------------------ batch phases
# Per-instance pipeline: one SendCommand per wave keeps Patch Manager's rate control (MaxConcurrency/MaxErrors),
# but every tick (30 s) dispatches an agent for each host that has just failed -- diagnosis no longer waits
# for the slowest host of the wave.
DIAGNOSABLE = {"PATCH_FAILED", "PATCH_TIMEOUT", "UNDELIVERABLE", "PRECHECK_FAILED"}
POOL = 16


def counters(batch_id):
    return table.get_item(Key={"pk": batch_id, "sk": "COUNTERS"}).get("Item", {})


# *Windows knobs: a Windows cumulative update (scan + install, and the agent's rerun) is much slower than dnf;
# every host is timed by its own platform.
KNOBS = {"patchTimeout": 600, "patchTimeoutWindows": 3600, "agentTimeout": 2400, "agentTimeoutWindows": 5400, "maxAgents": 10, "minFreePct": 15, "minFreeMB": 500,
         "waveHardTimeout": 0, "dispatchGrace": 180, "batchTimeout": 14400}


def init(ev):
    batch_id = ev.get("batchId") or time.strftime("b%Y%m%d-%H%M%S")
    found, warnings = [], []
    for acct in SPOKE_ACCOUNTS:
        pages = spoke(acct).client("ec2", config=AWS_CFG).get_paginator("describe_instances").paginate(Filters=[
            {"Name": "tag:PatchGroup", "Values": [PATCH_GROUP]}, {"Name": "instance-state-name", "Values": ["running"]}])
        for page in pages:
            for r in page["Reservations"]:
                for i in r["Instances"]:
                    t = {x["Key"]: x["Value"] for x in i.get("Tags", [])}
                    wave = t.get("PatchWave", "main")
                    if wave not in ("canary", "main"):  # an unknown wave would never be sent and never drain
                        warnings.append(f"{i['InstanceId']}: PatchWave={wave!r} unknown, treated as main")
                        wave = "main"
                    found.append({"account": acct, "instance_id": i["InstanceId"], "name": t.get("Name", ""), "wave": wave,
                                  # EC2 sets Platform only for Windows; the agent picks its tool set from this
                                  "platform": "windows" if i.get("Platform") == "windows" else "linux"})
    knobs = {**KNOBS, **{k: int(ev[k]) for k in KNOBS if ev.get(k) not in (None, "")}}
    if not knobs["waveHardTimeout"]:
        knobs["waveHardTimeout"] = 3 * knobs["patchTimeout"]
    with table.batch_writer() as w:
        for f in found:
            w.put_item(Item={"pk": batch_id, "sk": f"INST#{f['instance_id']}", **f, "status": "PENDING",
                             "session_id": session_id(batch_id, f["instance_id"]), "created_at": now()})
        w.put_item(Item={"pk": batch_id, "sk": "COUNTERS", "remediations": 0, "halted": False, "warnings": warnings,
                         "started_at": now(), "batch_deadline": now() + knobs["batchTimeout"],
                         "remediation_limit": int(ev.get("remediationLimit", max(2, len(found))))})
    waves = [w for w in ("canary", "main") if any(f["wave"] == w for f in found)]
    return {"batchId": batch_id, "total": len(found), "warnings": warnings, **knobs,
            "waves": [{"batchId": batch_id, "wave": w, "isCanary": w == "canary", **knobs} for w in waves]}


def _wait_invocations(ssm, cmd_id, ids, max_wait):
    """Poll a multi-instance command until every invocation is final or max_wait elapses (paginated)."""
    end, final = time.time() + max_wait, {}
    while time.time() < end and len(final) < len(ids):
        time.sleep(5)
        for page in ssm.get_paginator("list_command_invocations").paginate(CommandId=cmd_id, Details=True):
            for inv in page["CommandInvocations"]:
                if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
                    final[inv["InstanceId"]] = inv
    return final


def disk_precheck(acct, ids, min_pct, min_mb):
    """Root-disk headroom via the read-only PatchDiagnose-Collect doc. Returns {iid: reason} for hosts that must not be patched.
    A host so full that SSM cannot run any document fails here too -- caught before the patch, not after a 10-minute wave."""
    ssm, bad = ssm_c(acct), {}
    for part in chunks(ids):  # SendCommand takes <= 50 InstanceIds
        cmd = ssm.send_command(InstanceIds=part, DocumentName="PatchDiagnose-Collect", Parameters={"Check": ["disk"]},
                               TimeoutSeconds=60, Comment="precheck disk headroom")["Command"]["CommandId"]
        final = _wait_invocations(ssm, cmd, part, 90)
        for iid in part:
            inv = final.get(iid)
            if not inv:
                try:
                    ssm.cancel_command(CommandId=cmd, InstanceIds=[iid])
                except ClientError as e:
                    print("precheck cancel:", iid, e)
                bad[iid] = "precheck: disk check did not finish in 90s (SSM agent unable to run documents?), patch not sent"
                continue
            out = " ".join(p.get("Output", "") for p in inv.get("CommandPlugins", []))
            m = re.search(r"ROOT_FREE_PCT=(\d+)\s+ROOT_FREE_MB=(\d+)", out)
            if inv["Status"] != "Success" or not m:
                bad[iid] = f"precheck: disk check {inv['Status']}/{inv.get('StatusDetails')} without output (root disk full?), patch not sent"
                continue
            pct, mb = int(m.group(1)), int(m.group(2))
            if pct < min_pct or mb < min_mb:
                bad[iid] = f"precheck: root free {pct}% ({mb} MB) below threshold ({min_pct}% / {min_mb} MB), patch not sent"
    return bad


def ping_statuses(acct, ids):
    """PingStatus for every id (InstanceIds filter <= 50 values; the API pages at 10 by default)."""
    ssm, out = ssm_c(acct), {}
    for part in chunks(ids):
        for page in ssm.get_paginator("describe_instance_information").paginate(
                Filters=[{"Key": "InstanceIds", "Values": part}], PaginationConfig={"PageSize": 50}):
            out.update({x["InstanceId"]: x["PingStatus"] for x in page["InstanceInformationList"]})
    return out


def send_patch(ev):
    batch_id, wave = ev["batchId"], ev["wave"]
    todo = [i for i in items(batch_id) if i["wave"] == wave and i["status"] == "PENDING"]
    if counters(batch_id).get("halted"):  # canary breaker tripped: do not touch the main fleet
        for i in todo:
            upd(batch_id, i["instance_id"], status="SKIPPED_HALTED", patch_status="canary circuit breaker; not patched")
        return {**ev, "sent": 0, "halted": True}
    sent = 0
    timeout_of = {"linux": int(ev.get("patchTimeout", 600)), "windows": int(ev.get("patchTimeoutWindows", 1800))}
    hard = int(ev.get("waveHardTimeout") or 3 * timeout_of["linux"])
    platform = {i["instance_id"]: i.get("platform", "linux") for i in todo}
    for acct in {i["account"] for i in todo}:
        ids = [i["instance_id"] for i in todo if i["account"] == acct]
        # Precheck 1: an offline SSM agent would sit in Pending until the wave deadline.
        ping = ping_statuses(acct, ids)
        for iid in [x for x in ids if ping.get(x) != "Online"]:
            upd(batch_id, iid, status="UNDELIVERABLE", patch_status=f"precheck: PingStatus={ping.get(iid, 'NotRegistered')}, not sent",
                failed_at=now())
        ids = [x for x in ids if ping.get(x) == "Online"]
        # Precheck 2: root-disk headroom, / on Linux and C: on Windows (the package manager / Windows Update needs space;
        # at 100% SSM itself cannot run). Same document, the platform's step answers.
        if ids:
            for iid, reason in disk_precheck(acct, ids, int(ev.get("minFreePct", 15)), int(ev.get("minFreeMB", 500))).items():
                upd(batch_id, iid, status="PRECHECK_FAILED", patch_status=reason, failed_at=now())
                ids.remove(iid)
        if not ids or ev.get("dryRun"):  # dryRun: prechecks only (smoke tests), leave the rest PENDING
            continue
        for part in chunks(ids):  # one command per <= 50 hosts; rate control applies per command
            cmd = ssm_c(acct).send_command(
                InstanceIds=part, DocumentName="AWS-RunPatchBaseline", Parameters={"Operation": ["Install"], "RebootOption": ["NoReboot"]},
                MaxConcurrency="50%", MaxErrors="100%",  # demo: 4/5 hosts are broken on purpose; prod: 10% + canary gate
                TimeoutSeconds=120, Comment=f"{batch_id} wave={wave}")["Command"]["CommandId"]
            t = now()
            for iid in part:
                # per-host clock starts when the host is first seen InProgress (queued hosts are not penalised);
                # wave_hard_deadline bounds hosts that never leave Pending
                pt = timeout_of.get(platform[iid], timeout_of["linux"])
                upd(batch_id, iid, status="PATCHING", patch_command_id=cmd, patch_started_at=t, patch_timeout=pt,
                    wave_hard_deadline=t + max(hard, pt))
            sent += len(part)
    return {**ev, "sent": sent, "halted": False}


def _poll_one(ev, i):
    batch_id, iid = ev["batchId"], i["instance_id"]
    ssm = ssm_c(i["account"])
    inv = ssm.list_command_invocations(CommandId=i["patch_command_id"], InstanceId=iid)["CommandInvocations"]
    s = inv[0]["Status"] if inv else "Pending"
    detail = inv[0].get("StatusDetails", "") if inv else ""
    if s == "Success":
        upd_if(batch_id, iid, ["PATCHING"], status="PATCH_OK", patch_status=s, patch_done_at=now())
    elif s in ("Failed", "TimedOut", "Cancelled"):
        upd_if(batch_id, iid, ["PATCHING"], status="PATCH_FAILED", patch_status=f"{s}/{detail}", failed_at=now())
    elif detail in ("DeliveryTimedOut", "Undeliverable") or s == "Undeliverable":
        upd_if(batch_id, iid, ["PATCHING"], status="UNDELIVERABLE", patch_status=f"{s}/{detail}", failed_at=now())
    elif s == "InProgress":
        started = int(i.get("inprogress_at", 0))
        if not started:
            upd(batch_id, iid, inprogress_at=now())
        elif now() > started + int(i.get("patch_timeout", ev.get("patchTimeout", 600))):  # hung patch: cancel this host only
            ssm.cancel_command(CommandId=i["patch_command_id"], InstanceIds=[iid])
            upd_if(batch_id, iid, ["PATCHING"], status="PATCH_TIMEOUT", failed_at=now(),
                   patch_status=f"cancelled: running longer than patchTimeout ({s})")
    elif int(i.get("wave_hard_deadline") or i.get("patch_deadline") or 0) and \
            now() > int(i.get("wave_hard_deadline") or i.get("patch_deadline")):  # never started (rate-control queue / delivery)
        ssm.cancel_command(CommandId=i["patch_command_id"], InstanceIds=[iid])
        upd_if(batch_id, iid, ["PATCHING"], status="PATCH_TIMEOUT", failed_at=now(),
               patch_status=f"cancelled: queued too long, never started ({s}/{detail})")


def _check_agent(ev, i):
    limit = int(ev.get("agentTimeoutWindows", 5400)) if i.get("platform") == "windows" else int(ev.get("agentTimeout", 2400))
    if now() - int(i.get("diag_started_at", i.get("dispatched_at", now()))) <= limit:
        return
    # terminal state FIRST: the guardrail's terminal-status check blocks any tool call the agent still makes while
    # its microVM is shutting down; then stop the session (only if we won the conditional write)
    if upd_if(ev["batchId"], i["instance_id"], ["DIAGNOSING"], status="NEEDS_HUMAN", root_cause="agent timeout; session stopped by orchestrator"):
        try:
            AC.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=i["session_id"])
        except ClientError as e:
            print("stop session:", e)


def _release_dispatch(batch_id, i, why):
    """Give the slot back (re-queue) or, after MAX_DISPATCH attempts, hand the host to a human. Conditional on the
    host still being undispatched-diagnosable, so a late runtime claim (DIAGNOSING) or FIXED always wins."""
    iid, attempts = i["instance_id"], int(i.get("dispatch_attempts", 1))
    names = {"#s": "status", "#d": "dispatched_at"}
    vals = {f":s{n}": v for n, v in enumerate(sorted(DIAGNOSABLE))}
    cond = "#s IN (" + ", ".join(vals) + ") AND #d = :old"
    vals[":old"] = i["dispatched_at"]
    try:
        if attempts >= MAX_DISPATCH:
            table.update_item(Key={"pk": batch_id, "sk": f"INST#{iid}"}, ConditionExpression=cond,
                              UpdateExpression="SET #s=:nh, root_cause=:rc, updated_at=:t", ExpressionAttributeNames=names,
                              ExpressionAttributeValues={**vals, ":nh": "NEEDS_HUMAN", ":t": now(),
                                                         ":rc": f"agent could not be started after {attempts} attempts: {why}"[:500]})
            return "needs_human"
        table.update_item(Key={"pk": batch_id, "sk": f"INST#{iid}"}, ConditionExpression=cond,
                          UpdateExpression="REMOVE #d SET last_dispatch_error=:e, updated_at=:t", ExpressionAttributeNames=names,
                          ExpressionAttributeValues={**vals, ":e": str(why)[:300], ":t": now()})
        return "requeued"
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return "claimed-meanwhile"


def _dispatch(batch_id, i):
    t = now()
    try:  # claim the dispatch slot first so overlapping ticks never double-invoke
        table.update_item(Key={"pk": batch_id, "sk": f"INST#{i['instance_id']}"},
                          UpdateExpression="SET dispatched_at=:t ADD dispatch_attempts :one",
                          ConditionExpression="attribute_not_exists(dispatched_at)", ExpressionAttributeValues={":t": t, ":one": 1})
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return "skip"
    i = {**i, "dispatched_at": t, "dispatch_attempts": int(i.get("dispatch_attempts", 0)) + 1}
    try:
        resp, retries = invoke_runtime(i["session_id"], {"mode": "diagnose", "batchId": batch_id, "instanceId": i["instance_id"]})
    except Exception as e:  # noqa: BLE001  never fail the batch for one host; a late claim by the runtime still wins
        return _release_dispatch(batch_id, i, f"invoke failed: {e}")
    st_ = str(resp.get("status", ""))
    if st_ == "rejected" or (st_.startswith("already_") and st_.removeprefix("already_").upper() in DIAGNOSABLE):
        # runtime refused to claim (e.g. an older runtime build that does not know this status): hand to a human
        upd_if(batch_id, i["instance_id"], sorted(DIAGNOSABLE), status="NEEDS_HUMAN",
               root_cause=f"runtime did not start diagnosis: {resp}"[:500])
    elif retries:
        upd(batch_id, i["instance_id"], invoke_retries=retries)
    return st_ or "?"


def _reap_stale_dispatch(ev, i):
    """dispatched_at set but the runtime never claimed it (Lambda died mid-dispatch, unexpected response, ...)."""
    if now() - int(i["dispatched_at"]) > int(ev.get("dispatchGrace", 180)):
        return _release_dispatch(ev["batchId"], i, f"not claimed by the runtime within {ev.get('dispatchGrace', 180)}s")


def _safe(ev, fn, i):
    """Per-host isolation: one host's API error never fails the tick. MAX_HOST_ERRORS in a row -> NEEDS_HUMAN."""
    try:
        r = fn(ev, i)
        if int(i.get("error_count", 0)):
            upd(ev["batchId"], i["instance_id"], error_count=0)
        return r
    except Exception as e:  # noqa: BLE001
        n = int(i.get("error_count", 0)) + 1
        print(f"host error {i['instance_id']} ({fn.__name__}) #{n}: {e}")
        try:
            if n >= MAX_HOST_ERRORS and i["status"] not in TERMINAL:
                won = upd_if(ev["batchId"], i["instance_id"], [i["status"]], status="NEEDS_HUMAN", error_count=n,
                             last_error=f"{fn.__name__}: {e}"[:500], root_cause=f"orchestrator gave up after {n} consecutive API errors: {e}"[:500])
                if won and i["status"] == "DIAGNOSING":  # terminal state first, then stop the session
                    try:
                        AC.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=i["session_id"])
                    except ClientError:
                        pass
            else:
                upd(ev["batchId"], i["instance_id"], error_count=n, last_error=f"{fn.__name__}: {e}"[:500])
        except Exception as e2:  # noqa: BLE001
            print("error bookkeeping failed:", e2)
        return "error"


POSTPROC_GRACE = 300


def tick(ev):
    """One 30-second heartbeat: poll patching hosts, time out stuck agents, re-queue unclaimed dispatches,
    dispatch agents for new failures (<= maxAgents). Never raises for a single host."""
    batch_id, wave = ev["batchId"], ev.get("wave")
    with ThreadPoolExecutor(POOL) as ex:
        inst = items(batch_id)
        list(ex.map(lambda i: _safe(ev, _poll_one, i), [i for i in inst if i["status"] == "PATCHING"]))
        inst = items(batch_id)
        list(ex.map(lambda i: _safe(ev, _check_agent, i), [i for i in inst if i["status"] == "DIAGNOSING"]))
        list(ex.map(lambda i: _safe(ev, _reap_stale_dispatch, i),
                    [i for i in inst if i["status"] in DIAGNOSABLE and "dispatched_at" in i]))
        inst = items(batch_id)
        active = [i for i in inst if i["status"] == "DIAGNOSING" or (i["status"] in DIAGNOSABLE and "dispatched_at" in i)]
        queue = sorted((i for i in inst if i["status"] in DIAGNOSABLE and "dispatched_at" not in i), key=lambda i: int(i.get("failed_at", 0)))
        slots = max(0, int(ev.get("maxAgents", 10)) - len(active))
        dispatched = list(ex.map(lambda i: _safe(ev, lambda e, x: _dispatch(batch_id, x), i), queue[:slots]))
    inst = items(batch_id)
    scope = [i for i in inst if wave is None or i["wave"] == wave]
    failed = sum(1 for i in scope if i["status"] in DIAGNOSABLE | {"DIAGNOSING", "NEEDS_HUMAN", "FIXED"} and "failed_at" in i)
    busy = sum(1 for i in inst if i["status"] == "DIAGNOSING" or i["status"] in DIAGNOSABLE)
    # report_result makes a host terminal before the agent writes its metrics and the memory extraction result
    # (memory_write). Wait for that post-processing too (max POSTPROC_GRACE s), so REPORT is complete.
    busy += sum(1 for i in inst if i["status"] in TERMINAL and "diag_started_at" in i and "memory_write" not in i
                and now() - int(i.get("updated_at", 0)) < POSTPROC_GRACE)
    timed_out = now() > int(counters(batch_id).get("batch_deadline", 1 << 62))
    return {**ev, "waveDone": all(i["status"] not in ("PENDING", "PATCHING") for i in scope),
            "drained": busy == 0 and all(i["status"] not in ("PENDING", "PATCHING") for i in inst),
            "batchTimedOut": timed_out, "failed": failed, "total": len(scope), "failureRate": round(failed / max(1, len(scope)), 3),
            "dispatchedNow": sum(1 for d in dispatched if d not in ("skip", "error", "requeued", "needs_human")),
            "queued": max(0, len(queue) - slots), "busy": busy}


def abort(ev):
    """Fail-safe: the state machine hit an error or the batch timeout. Every non-terminal host goes to a human;
    running agents are stopped and pending patches cancelled, so nothing keeps acting unsupervised."""
    batch_id = ev["batchId"]
    err = ev.get("error") or {}
    why = f"batch aborted: {err.get('Error', 'error')} {str(err.get('Cause', ''))[:300]}".strip()
    out = {"stopped": 0, "cancelled": 0, "marked": 0}
    for i in items(batch_id):
        if i["status"] in TERMINAL:
            continue
        # terminal state first (blocks further agent tool calls via the guardrail), then stop / cancel
        if not upd_if(batch_id, i["instance_id"], [i["status"]], status="NEEDS_HUMAN", root_cause=why[:500]):
            continue  # someone else (the agent's own report) got there first
        out["marked"] += 1
        try:
            if i["status"] == "DIAGNOSING" or "dispatched_at" in i:
                AC.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=i["session_id"])
                out["stopped"] += 1
            elif i["status"] == "PATCHING" and i.get("patch_command_id"):
                ssm_c(i["account"]).cancel_command(CommandId=i["patch_command_id"], InstanceIds=[i["instance_id"]])
                out["cancelled"] += 1
        except Exception as e:  # noqa: BLE001
            print("abort cleanup:", i["instance_id"], e)
    table.update_item(Key={"pk": batch_id, "sk": "COUNTERS"}, UpdateExpression="SET aborted=:t, abort_reason=:r",
                      ExpressionAttributeValues={":t": True, ":r": why[:500]})
    return {"batchId": batch_id, **out}


def halt(ev):
    table.update_item(Key={"pk": ev["batchId"], "sk": "COUNTERS"}, UpdateExpression="SET halted=:t, halted_reason=:r",
                      ExpressionAttributeValues={":t": True, ":r": f"canary wave failureRate={ev.get('failureRate')}"})
    return {**ev, "halted": True}


AC = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(read_timeout=120, retries={"max_attempts": 1}))


def invoke_runtime(session_id_, payload, client=None, user_id="patch-orchestrator", attempts=6):
    """InvokeAgentRuntime with exponential backoff + jitter on session-creation throttling.
    runtimeUserId is required for the runtime to receive a workload access token (-> API key from the token vault):
    the runtime's own workload identity is service-linked and cannot mint tokens for itself."""
    client = client or AC
    kw = dict(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=session_id_, payload=json.dumps(payload).encode(),
              contentType="application/json", accept="application/json")
    if user_id:
        kw["runtimeUserId"] = user_id
    for n in range(attempts):
        try:
            r = client.invoke_agent_runtime(**kw)
            return json.loads(r["response"].read() or b"{}"), n
        except ClientError as e:
            code, msg = e.response["Error"]["Code"], e.response["Error"].get("Message", "")
            if code in ("ThrottlingException", "ServiceQuotaExceededException") or "rate exceeded" in msg.lower():
                time.sleep(min(30, 2 ** n) + random.random() * 2)
                continue
            raise
    raise RuntimeError("throttled after retries")


def report(ev):
    inst = items(ev["batchId"])
    approvals = items(ev["batchId"], "APPROVAL#")

    def lat(i, a, b):
        return int(i[b]) - int(i[a]) if a in i and b in i else None
    rows = [{"name": i.get("name"), "instance": i["instance_id"], "account": i["account"], "status": i["status"],
             "patch": i.get("patch_status"), "signature": i.get("signature"), "root_cause": i.get("root_cause"),
             "tool_calls": int(i.get("tool_calls", 0)), "diag_seconds": int(i.get("diag_seconds", 0)),
             "failed_to_dispatch_s": lat(i, "failed_at", "dispatched_at"),  # pipelining: how long a failed host waited
             "patch_start_to_dispatch_s": lat(i, "patch_started_at", "dispatched_at"),
             "dispatch_attempts": int(i.get("dispatch_attempts", 0)), "last_error": i.get("last_error"),
             "recall": i.get("recall"), "session": i["session_id"]} for i in inst]
    summary = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    c = counters(ev["batchId"])
    rep = {"batchId": ev["batchId"], "halted": bool(c.get("halted")), "halted_reason": c.get("halted_reason"),
           "aborted": bool(c.get("aborted")), "abort_reason": c.get("abort_reason"), "warnings": c.get("warnings", []),
           "summary": summary, "instances": rows,
           "approvals": [{"id": a["sk"], "instance": a["instance_id"], "action": a["action"], "reason": a["reason"]} for a in approvals]}
    table.put_item(Item={"pk": ev["batchId"], "sk": "REPORT", "report": json.dumps(rep, ensure_ascii=False, default=str), "ts": now()})
    return json.loads(json.dumps(rep, default=str))


# ------------------------------------------------------------------ human take-over / experiments
def portal_chat(ev):
    """What the ops portal backend does after the engineer signs in via IAM Identity Center:
    act as the engineer's permission-set role and continue the SAME runtime session."""
    c = boto3.client("sts", region_name=REGION).assume_role(RoleArn=ONCALL_ROLE_ARN, RoleSessionName=ev["operator"][:64])["Credentials"]
    client = boto3.client("bedrock-agentcore", region_name=REGION, aws_access_key_id=c["AccessKeyId"],
                          aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"],
                          config=Config(read_timeout=600, retries={"max_attempts": 1}))
    it = table.get_item(Key={"pk": ev["batchId"], "sk": f"INST#{ev['instanceId']}"})["Item"]
    resp, _ = invoke_runtime(it["session_id"], {"mode": "chat", "batchId": ev["batchId"], "instanceId": ev["instanceId"],
                                                "message": ev["message"], "operator": ev["operator"]},
                             client=client, user_id=ev["operator"])
    return resp


def selftest(ev):
    """N parallel new sessions (async no-op tasks) to measure session-creation throttling + retry behaviour."""
    n, conc, secs = int(ev.get("count", 20)), int(ev.get("concurrency", 10)), int(ev.get("seconds", 60))
    tag = time.strftime("st%H%M%S")

    def one(k):
        t0 = time.time()
        try:
            r, retries = invoke_runtime(f"{tag}-{k:04d}".ljust(40, "x"), {"mode": "selftest", "seconds": secs})
            return {"ok": True, "retries": retries, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)[:120], "ms": int((time.time() - t0) * 1000)}
    with ThreadPoolExecutor(conc) as ex:
        res = list(ex.map(one, range(n)))
    lat = sorted(r["ms"] for r in res if r["ok"])
    return {"count": n, "concurrency": conc, "ok": sum(r["ok"] for r in res), "retried": sum(1 for r in res if r.get("retries")),
            "max_retries": max([r.get("retries", 0) for r in res] or [0]), "p50_ms": lat[len(lat) // 2] if lat else None,
            "max_ms": lat[-1] if lat else None, "errors": [r["err"] for r in res if not r["ok"]][:5]}


def probe(ev):
    """Inside-VPC positive checks: this Lambda reaches the runtime and the spokes only via VPCEs."""
    out = {}
    # Negative, with a TRUSTED principal: same role, same ExternalId, but the call leaves via NAT to a public
    # endpoint (cn-north-1 has no VPCE here) -> no aws:SourceVpce in the request -> must be denied.
    acct = SPOKE_ACCOUNTS[-1]
    try:
        boto3.client("sts", region_name="cn-north-1").assume_role(
            RoleArn=f"arn:{PARTITION}:iam::{acct}:role/{SPOKE_ROLE}", RoleSessionName="probe-public", ExternalId=EXTERNAL_ID)
        out["assume_spoke_via_public_sts"] = "UNEXPECTED ALLOW"
    except ClientError as e:
        out["assume_spoke_via_public_sts"] = f"denied ({e.response['Error']['Code']})"
    try:  # credentials obtained legitimately via the VPCE, then used outside it
        spoke(acct).client("ec2", region_name="cn-north-1").describe_instances(MaxResults=5)
        out["spoke_creds_used_via_public_endpoint"] = "UNEXPECTED ALLOW"
    except ClientError as e:
        out["spoke_creds_used_via_public_endpoint"] = f"denied ({e.response['Error']['Code']})"
    for acct in SPOKE_ACCOUNTS:
        try:
            spoke(acct).client("ssm").describe_instance_information(MaxResults=5)
            out[f"spoke_{acct}"] = "ok via sts+ssm VPCE"
        except Exception as e:  # noqa: BLE001
            out[f"spoke_{acct}"] = f"ERR {e}"[:200]
    try:
        out["runtime_probe"] = invoke_runtime(("probe-" + time.strftime("%H%M%S")).ljust(40, "p"), {"mode": "probe", "targets": ev["targets"], "models": ev.get("models", [])})[0]
    except Exception as e:  # noqa: BLE001
        out["runtime_probe"] = f"ERR {e}"[:300]
    return out


def memory_admin(ev):
    """LTM lives in a VPC-only OpenSearch domain: seed / reset / list go Lambda -> Runtime (mode=memory_admin)."""
    sid = ("memadmin-" + time.strftime("%H%M%S")).ljust(40, "m")
    return invoke_runtime(sid, {"mode": "memory_admin", **ev})[0]


def isolation_test(ev):
    """Two sessions A/B on the same runtime: filesystem + memory state must not cross; StopRuntimeSession wipes A's
    microVM while its conversation (STM in DynamoDB) survives. Inside-VPC only, demo runtime has ENABLE_PROBE=1."""
    tag = time.strftime("iso%H%M%S")
    a, b = f"{tag}-session-A".ljust(40, "a"), f"{tag}-session-B".ljust(40, "b")
    steps = []

    def call(name, sid, **p):
        r, _ = invoke_runtime(sid, {"mode": "isolation", **p})
        steps.append({"step": name, **r})
    call("A1 write secret-A", a, secret="secret-A")
    call("A2 same session again", a)
    call("B1 other session", b, secret="secret-B")
    AC.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=a)
    time.sleep(5)
    call("A3 after StopRuntimeSession", a)
    for sid in (a, b):
        try:
            AC.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=sid)
        except ClientError:
            pass
    return {"steps": steps}


ACTIONS = {f.__name__: f for f in (isolation_test, init, send_patch, tick, halt, abort, report, portal_chat, selftest, probe,
                                     memory_admin)}


def lambda_handler(ev, _ctx):
    # Step Functions passes the whole state as {"action": ..., "in": {...}}; direct invokes pass flat fields.
    body = {**(ev.get("in") or {}), **{k: v for k, v in ev.items() if k not in ("action", "in")}}
    return ACTIONS[ev["action"]](body)
