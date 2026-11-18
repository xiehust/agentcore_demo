#!/usr/bin/env python3
"""Submit once, collect and wait for SSM commands without leaking credentials."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent

def aws(region, *args, timeout=60):
    proc = subprocess.run(["aws", "--region", region, "--output", "json", *args],
                          capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "1"})
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def send(region, instance, script, label, record_dir, execution_timeout):
    comment = f"dsh-nitro-{label}-{uuid.uuid4().hex[:12]}"
    # Save the identifying comment BEFORE submitting. If acknowledgement times
    # out, never replay; reconcile by this comment using ssm list-commands.
    record_dir.mkdir(parents=True, exist_ok=True)
    record = record_dir / f"{comment}.json"
    record.write_text(json.dumps({"comment": comment, "instance": instance, "state": "submitting"}, indent=2))
    result = aws(region, "ssm", "send-command", "--instance-ids", instance,
                 "--document-name", "AWS-RunShellScript", "--comment", comment,
                 "--parameters", json.dumps({"commands": [script], "executionTimeout": [str(execution_timeout)]}))
    cid = result["Command"]["CommandId"]
    record.write_text(json.dumps({"comment": comment, "instance": instance, "command_id": cid, "state": "submitted"}, indent=2))
    print(f"Submitted {cid} ({comment})", flush=True)
    return cid


def wait(region, instance, cid, record_dir, timeout, quiet=False):
    deadline = time.monotonic() + timeout
    missing = 0
    while time.monotonic() < deadline:
        try:
            result = aws(region, "ssm", "get-command-invocation", "--command-id", cid, "--instance-id", instance)
        except RuntimeError as err:
            if "InvocationDoesNotExist" not in str(err) or missing >= 5:
                raise
            missing += 1
            time.sleep(2)
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed", "Cancelling"):
            record_dir.mkdir(parents=True, exist_ok=True)
            (record_dir / f"{cid}.json").write_text(json.dumps(result, indent=2))
            print(f"Command {cid}: {result['Status']} (exit {result.get('ResponseCode')})", flush=True)
            if not quiet:
                print(result.get("StandardOutputContent", ""), flush=True)
                print(result.get("StandardErrorContent", ""), flush=True)
            if result["Status"] != "Success":
                raise RuntimeError(f"SSM command {cid} failed; inspect saved invocation")
            return result
        time.sleep(3)
    raise TimeoutError(f"SSM command still running at deadline: {cid}; inspect, do not resubmit")

def upload(region, instance, record_dir):
    import io
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative in ("benchmark.py", "guest", "scripts/build_guest.sh", "scripts/prepare_parent.sh",
                         "scripts/smoke_sandbox.sh", "scripts/run_startup_benchmark.sh", "scripts/run_agent_benchmark.sh",
                         "scripts/native_addons.py", "disk_isolation.py", "scripts/build_disk_guest.sh",
                         "scripts/run_disk_isolation.sh"):
            tar.add(ROOT / relative, arcname=relative)
    content = base64.b64encode(buffer.getvalue()).decode()
    script = ("set -eu\nmkdir -p /opt/dsh-nitro-benchmark\n"
              + "printf '%s' '" + content + "' | base64 -d | tar -xz -C /opt/dsh-nitro-benchmark\n"
              + "find /opt/dsh-nitro-benchmark -maxdepth 2 -type f -print\n")
    if len(script) > 60000:
        raise ValueError("upload too large for the bounded SSM transfer")
    cid = send(region, instance, script, "upload", record_dir, 120)
    wait(region, instance, cid, record_dir, 180)


def fetch(region, instance, remote, destination, record_dir):
    import shlex
    if not remote.startswith("/opt/dsh-nitro-benchmark/results/") or ".." in remote.split("/"):
        raise ValueError("only benchmark results may be fetched")
    if destination.exists():
        raise ValueError("refusing to overwrite download")
    cmd = f"stat -c %s -- {shlex.quote(remote)}"
    cid = send(region, instance, cmd, "download-size", record_dir, 60)
    result = wait(region, instance, cid, record_dir, 120)
    size = int(result["StandardOutputContent"].strip())
    if size < 1 or size > 5 * 1024 * 1024:
        raise ValueError("invalid/oversized evidence archive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as out:
        for offset in range(0, size, 12000):
            # At most 16 KB base64 per invocation, below the SSM stdout cap.
            code = ("import base64; f=open(" + repr(remote) + ", 'rb'); "
                    + f"f.seek({offset}); print(base64.b64encode(f.read(12000)).decode())")
            cid = send(region, instance, "python3 -c " + shlex.quote(code), "download-part", record_dir, 60)
            part = wait(region, instance, cid, record_dir, 120, quiet=True)
            decoded = base64.b64decode(part["StandardOutputContent"].strip(), validate=True)
            if len(decoded) != min(12000, size - offset):
                raise ValueError("truncated evidence part")
            out.write(decoded)
    print(f"Downloaded {size} bytes to {destination}", flush=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("send", "wait", "upload", "fetch"))
    p.add_argument("--instance", required=True)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--script", type=Path)
    p.add_argument("--command-id")
    p.add_argument("--label", default="command")
    p.add_argument("--record-dir", type=Path, default=ROOT / "results/ssm")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--remote")
    p.add_argument("--destination", type=Path)
    args = p.parse_args()
    if args.action == "send":
        if not args.script:
            p.error("send requires --script")
        cid = send(args.region, args.instance, args.script.read_text(), args.label, args.record_dir, args.timeout)
        wait(args.region, args.instance, cid, args.record_dir, args.timeout + 60)
    elif args.action == "wait":
        if not args.command_id:
            p.error("wait requires --command-id")
        wait(args.region, args.instance, args.command_id, args.record_dir, args.timeout)
    elif args.action == "upload":
        upload(args.region, args.instance, args.record_dir)
    else:
        if not args.remote or not args.destination:
            p.error("fetch requires --remote and --destination")
        fetch(args.region, args.instance, args.remote, args.destination, args.record_dir)

if __name__ == "__main__":
    main()
