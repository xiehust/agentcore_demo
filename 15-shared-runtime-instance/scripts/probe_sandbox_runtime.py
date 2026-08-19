"""Probe whether @anthropic-ai/sandbox-runtime (bubblewrap-based) can run
inside the shared-runtime Instances session container.

Follows the supported re-verification pattern from ISOLATION_OPTIONS.md §5:
one fresh session per run, warmup via InvokeAgentRuntime, probe via
InvokeAgentRuntimeCommand, StopRuntimeSession in finally.

sandbox-runtime hard dependencies on Linux:
  - bubblewrap (bwrap) + socat + ripgrep binaries
  - unprivileged user namespace creation (CLONE_NEWUSER)
  - mount namespace + tmpfs/bind mounts inside that userns

No escape testing, no credential/env reads.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config

runtime = json.loads(
    (Path(__file__).parent.parent / "runtime.json").read_text()
)
session_id = (
    f"srt-probe-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
)
client = boto3.client(
    "bedrock-agentcore",
    region_name=runtime["region"],
    config=Config(connect_timeout=30, read_timeout=900,
                  retries={"total_max_attempts": 1}),
)

probe = r"""exec 2>&1
echo '--- binaries ---'
for b in bwrap socat rg node npx; do
  p=$(command -v "$b" 2>/dev/null) && echo "$b=$p" || echo "$b=MISSING"
done
echo '--- userns: unshare -Ur true ---'
unshare -Ur true; echo "unshare_userns_exit=$?"
echo '--- userns+mountns+pidns: unshare -UrmpF ---'
unshare -UrmpF true; echo "unshare_full_exit=$?"
echo '--- tmpfs mount inside userns ---'
unshare -Urm sh -c 'mount -t tmpfs tmpfs /mnt && echo tmpfs_ok'
echo "tmpfs_exit=$?"
echo '--- bwrap minimal (if present) ---'
if command -v bwrap >/dev/null 2>&1; then
  bwrap --unshare-user --uid 0 --gid 0 --ro-bind / / /bin/true
  echo "bwrap_exit=$?"
else
  echo "bwrap_exit=SKIPPED_MISSING"
fi
echo '--- kernel knobs (context only) ---'
cat /proc/sys/user/max_user_namespaces 2>/dev/null; echo "userns_quota_exit=$?"
cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null; echo "userns_clone_exit=$?"
uname -m
"""

print(f"session_id={session_id}")
print(f"utc={datetime.now(timezone.utc).isoformat()}")
print(f"runtime={runtime['runtimeArn']}")

try:
    warmup = client.invoke_agent_runtime(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
        runtimeUserId="srt-probe",
        payload=json.dumps({
            "prompt": "Reply with exactly: ready",
            "user_id": "srt-probe",
            "reset": True,
        }).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    body = warmup["response"].read().decode(errors="replace")
    print(f"warmup_http={warmup['ResponseMetadata']['HTTPStatusCode']}")
    print(f"warmup_tail={body[-200:]!r}")

    response = client.invoke_agent_runtime_command(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
        body={"command": probe, "timeout": 60},
    )
    for event in response["stream"]:
        if "contentDelta" in event:
            d = event["contentDelta"]
            sys.stdout.write(d.get("stdout", "") + d.get("stderr", ""))
        elif "contentStop" in event:
            s = event["contentStop"]
            print(f"\n[contentStop] exitCode={s.get('exitCode')} "
                  f"status={s.get('status')}")
        else:
            print(f"[event] {event}")
finally:
    stopped = client.stop_runtime_session(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
    )
    code = stopped["ResponseMetadata"]["HTTPStatusCode"]
    match = stopped.get("runtimeSessionId") == session_id
    print(f"stop_http={code} session_match={match}")
    if code != 200 or not match:
        raise RuntimeError("StopRuntimeSession verification failed")
