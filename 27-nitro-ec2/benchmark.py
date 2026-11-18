#!/usr/bin/env python3
"""Sequential Nitro Sandbox cold-start benchmark. Python standard library only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid

GUEST_IP = "192.0.2.2"
HOST_IP = "192.0.2.1"
SUBNET = ipaddress.ip_network("192.0.2.0/30")
SYSFS = Path("/sys/class/misc/nitro_enclaves")
METRICS = ("sandbox_ready_ms", "request_ms", "launch_to_response_ms",
           "dsh_first_event_ms", "dsh_first_text_ms", "dsh_total_ms")

def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=15).stdout.strip()


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def digest(file: Path) -> dict:
    sha = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return {"path": str(file), "bytes": file.stat().st_size, "sha256": sha.hexdigest()}


def config(args, run_id: str, tap: str) -> dict:
    return {
        "boot-source": {"kernel_image_path": str(args.kernel.resolve()),
                        "initrd_path": str(args.initrd.resolve()),
                        "boot_args": f"panic=1 initcall_blacklist=i8042_init bench_id={run_id}"},
        "machine-config": {"vcpu_count": args.vcpus, "mem_size_mib": args.memory_mib},
        "drives": [],
        "network-interfaces": [{"iface_id": "eth0", "host_dev_name": tap}],
        "enclave": {"cpu_overcommit": True, "direct_mode": args.direct_mode,
                    "debug_mode": args.debug},
    }


def image_manifest(args) -> dict:
    data = json.loads(args.manifest.read_text())
    release = data.get("kernel_release", "")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.amzn2023\.(aarch64|x86_64)", release)
    if not match or tuple(map(int, match.groups()[:5])) < (6, 18, 51, 120, 162):
        raise ValueError("guest image kernel does not meet the Nitro Sandbox minimum")
    if match[6] != platform.machine():
        raise ValueError("guest kernel architecture differs from the parent")
    images = {"kernel_image": digest(args.kernel), "initrd": digest(args.initrd)}
    if images["kernel_image"]["sha256"] != data.get("kernel_sha256") or images["initrd"]["sha256"] != data.get("initrd_sha256"):
        raise ValueError("guest image hashes differ from the build manifest")
    return {**images, "build_manifest": data}


def preflight(args) -> dict:
    if os.geteuid() != 0:
        raise RuntimeError("run on the prepared parent as root (sudo python3 benchmark.py ...)")
    if not Path("/dev/nitro_enclaves").exists():
        raise RuntimeError("missing /dev/nitro_enclaves; this is not a prepared Nitro Sandbox parent")
    release = platform.release()
    version = re.match(r"(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.amzn2023", release)
    if not version or tuple(map(int, version.groups())) < (6, 18, 51, 120, 162):
        raise RuntimeError("requires AL2023 kernel >= 6.18.51-120.162.amzn2023")
    for tool in (args.firecracker, "ip", "nft"):
        if not shutil.which(tool):
            raise RuntimeError(f"missing executable: {tool}")
    fc_version = command(args.firecracker, "--version")
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", fc_version)
    if not match or tuple(map(int, match.groups())) < (1, 18, 0):
        raise RuntimeError("requires Firecracker >= 1.18.0")
    for file in (args.kernel, args.initrd):
        if not file.is_file() or not file.stat().st_size:
            raise RuntimeError(f"missing/empty image file: {file}")
    pool = {name: (SYSFS / name).read_text().strip() for name in
            ("pcie_slots", "pcie_slots_total", "pcie_slots_in_use", "mem_pool/free", "cpu_pool/free")}
    if int(pool["pcie_slots"]) < 1:
        raise RuntimeError("no Sandbox slots; reserve nitro_enclaves.mempool and reboot first")
    if int(pool["pcie_slots_in_use"]) != 0:
        raise RuntimeError("use a dedicated idle parent; another Sandbox is already running")
    routes = json.loads(command("ip", "-j", "route", "show", "table", "all"))
    for route in routes:
        dst = route.get("dst", "default")
        if dst == "default":
            continue
        if ipaddress.ip_network(dst, strict=False).overlaps(SUBNET):
            raise RuntimeError(f"benchmark subnet conflicts with existing route: {dst}")
    if args.workload == "agent" and not args.direct_mode:
        raise RuntimeError("agent credentials require --direct-mode: this demo does not implement attestation")
    return {"kernel": release, "arch": platform.machine(), "cpus": os.cpu_count(),
            "firecracker": fc_version, "pool_before": pool,
            "loadavg_start": os.getloadavg(), **image_manifest(args)}


def credentials(file: Path) -> dict:
    if file.stat().st_mode & 0o077:
        raise ValueError("credentials file must not be accessible by group/others; chmod 600 it")
    raw = json.loads(file.read_text())
    # AWS CLI `configure export-credentials --format process` field names.
    fields = {"AccessKeyId": "AWS_ACCESS_KEY_ID", "SecretAccessKey": "AWS_SECRET_ACCESS_KEY",
              "SessionToken": "AWS_SESSION_TOKEN"}
    if not all(isinstance(raw.get(k), str) and raw[k] for k in
               ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")):
        raise ValueError("use temporary credentials with AccessKeyId, SecretAccessKey, SessionToken and Expiration")
    expires = dt.datetime.fromisoformat(raw["Expiration"].replace("Z", "+00:00"))
    if expires.tzinfo is None or expires <= dt.datetime.now(dt.timezone.utc):
        raise ValueError("credentials expired or missing timezone; export fresh temporary credentials")
    return {env: raw[key] for key, env in fields.items() if raw.get(key)}


def completed(row: dict) -> bool:
    if row.get("workload") != "agent":
        return row.get("ok") is True
    return (row.get("exit_code") == 0 and not row.get("timed_out")
            and row.get("stop_reason") == "completed" and row.get("dsh_first_text_ms") is not None
            and row.get("request_ms") is not None)


def answer_mismatch(row: dict) -> bool:
    return completed(row) and row.get("answer_matches_expected") is False


def completion_summary(records: list) -> dict:
    return summarize([{**row, "ok": completed(row)} for row in records])


def percentile(values: list, p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def summarize(records: list) -> dict:
    groups = {}
    for phase in ("cold", "warm"):
        group = [r for r in records if r["phase"] == phase]
        metrics = {}
        for metric in METRICS:
            values = [r[metric] for r in group if r["ok"] and r.get(metric) is not None]
            metrics[metric] = ({"n": len(values), "min": min(values), "max": max(values),
                                "mean": statistics.fmean(values), "p50": percentile(values, .5),
                                "p90": percentile(values, .9), "p99": percentile(values, .99)}
                               if values else {"n": 0})
        groups[phase] = {"attempted": len(group), "failed": sum(not r["ok"] for r in group),
                         "metrics_ms": metrics}
    return groups

def firewall_rules(table: str, tap: str, online: bool, iface: str) -> str:
    # Input protection prevents the guest reaching services on the parent.
    # The established rule permits replies to the parent's benchmark requests.
    forward = "reject"
    nat = ""
    if online:
        forward = '''ip daddr 169.254.169.254 reject
            ip daddr 169.254.169.253 udp dport 53 accept
            ip daddr 169.254.169.253 tcp dport 53 accept
            ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
                       169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16,
                       192.0.2.0/24, 198.18.0.0/15, 224.0.0.0/4, 240.0.0.0/4 } reject
            tcp dport 443 accept
            reject'''
        nat = f'''table ip {table}_nat {{
            chain postrouting {{
                type nat hook postrouting priority srcnat; policy accept;
                iifname "{tap}" oifname "{iface}" masquerade
            }}
        }}'''
    return f'''table inet {table} {{
        chain input {{
            type filter hook input priority -10; policy accept;
            iifname "{tap}" ct state established,related accept
            iifname "{tap}" reject
        }}
        chain forward {{
            type filter hook forward priority -10; policy accept;
            iifname "{tap}" meta nfproto ipv6 reject
            iifname "{tap}" jump guest_egress
            oifname "{tap}" ct state established,related accept
            oifname "{tap}" reject
        }}
        chain guest_egress {{
            {forward}
        }}
    }}
    {nat}'''


@contextlib.contextmanager
def network(online: bool):
    token = uuid.uuid4().hex[:10]
    tap, table = f"dsh{token}", f"dshb_{token}"
    iface = ""
    if online:
        defaults = json.loads(command("ip", "-j", "route", "show", "default"))
        if not defaults:
            raise RuntimeError("agent test needs a default egress route")
        iface = defaults[0]["dev"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", iface):
            raise RuntimeError("unexpected egress interface name")
    old_forward = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    tap_created = False
    # Names are random, but still refuse collisions rather than deleting anyone's resources.
    tables = json.loads(command("nft", "-j", "list", "tables"))["nftables"]
    if any(item.get("table", {}).get("name") in (table, f"{table}_nat") for item in tables):
        raise RuntimeError("generated nft table name already exists")
    rules_attempted = False
    try:
        command("ip", "tuntap", "add", "dev", tap, "mode", "tap")
        tap_created = True
        rules_attempted = True
        subprocess.run(["nft", "-f", "-"], input=firewall_rules(table, tap, online, iface),
                       text=True, check=True, capture_output=True, timeout=15)
        command("ip", "addr", "add", f"{HOST_IP}/30", "dev", tap)
        command("ip", "link", "set", tap, "up")
        if online:
            Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
        yield tap
    finally:
        # Tear down connectivity BEFORE dropping the protective rules. If deletion
        # fails, leave the rules in place and surface the failure to the operator.
        try:
            if tap_created:
                command("ip", "link", "delete", tap)
        finally:
            if online:
                Path("/proc/sys/net/ipv4/ip_forward").write_text(old_forward + "\n")
        if rules_attempted:
            # Reconcile actual state, including a timed-out nft commit, before cleanup.
            actual = json.loads(command("nft", "-j", "list", "tables"))["nftables"]
            for family, name in (("inet", table), ("ip", f"{table}_nat")):
                if any(item.get("table", {}).get("name") == name for item in actual):
                    command("nft", "delete", "table", family, name)

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url: str, body=None, timeout=1.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    with HTTP.open(req, timeout=timeout) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("guest response too large")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("guest response must be an object")
    return parsed


def wait_ready(proc, run_id: str, start: float, timeout: float) -> dict:
    deadline = start + timeout
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Firecracker exited before readiness; inspect firecracker.log")
        try:
            health = request(f"http://{GUEST_IP}:8080/health",
                             timeout=min(.5, max(.001, deadline - time.perf_counter())))
            if health.get("run_id") != run_id or not health.get("boot_id"):
                raise RuntimeError("stale/wrong guest identity")
            if health.get("dsh_version") == "unknown":
                raise RuntimeError("guest does not contain an identifiable DSH installation")
            return health
        except (OSError, urllib.error.URLError):
            # Readiness probes are read-only; never replay an /invoke POST.
            time.sleep(.02)
    raise TimeoutError("Sandbox readiness deadline exceeded")


def validate_result(data: dict, run_id: str, boot_id: str, workload: str) -> dict:
    if data.get("run_id") != run_id or data.get("boot_id") != boot_id:
        raise ValueError("invocation returned a different guest identity")
    keys = ("dsh_first_event_ms", "dsh_first_text_ms", "dsh_total_ms")
    result = {}
    for key in keys:
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError(f"invalid guest duration: {key}")
        result[key] = value
    total = result["dsh_total_ms"]
    if total is None:
        raise ValueError("missing DSH total duration")
    first, text = result["dsh_first_event_ms"], result["dsh_first_text_ms"]
    if (first is not None and first > total) or (text is not None and (first is None or text < first or text > total)):
        raise ValueError("guest durations are out of order")
    result["ok"] = data.get("ok") is True and data.get("exit_code") == 0 and not data.get("timed_out")
    if workload == "agent":
        result["ok"] = result["ok"] and data.get("stop_reason") == "completed" and text is not None
        result["ok"] = result["ok"] and data.get("answer_matches_expected") is not False
        result["answer_matches_expected"] = data.get("answer_matches_expected")
    # No raw output, prompts, credential material or provider exception messages.
    result.update(exit_code=data.get("exit_code"), timed_out=bool(data.get("timed_out")),
                  stop_reason=data.get("stop_reason") if data.get("stop_reason") == "completed" else None)
    if not result["ok"]:
        result["error"] = "DSH invocation failed (timeout/exit/invalid completion)"
    return result


def stop(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def run_round(args, number: int, tap: str, out: Path, creds: dict) -> list:
    run_id = uuid.uuid4().hex
    directory = out / f"round-{number:04d}"
    directory.mkdir()
    cfg = directory / "sandbox.json"
    cfg.write_text(json.dumps(config(args, run_id, tap), indent=2))
    records = []
    base = {"round": number, "run_id": run_id, "workload": args.workload}
    # No AWS credentials are inherited by the parent VMM.
    env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")}
    with (directory / "firecracker.log").open("wb") as log:
        start = time.perf_counter()
        proc = subprocess.Popen([args.firecracker, "--no-api", "--config-file", str(cfg.resolve())],
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        try:
            health = wait_ready(proc, run_id, start, args.boot_timeout)
            ready = time.perf_counter()
            expected = getattr(args, "expected_guest", {})
            if any(health.get(key) != expected[key] for key in ("dsh_version", "node_version", "arch") if key in expected):
                raise RuntimeError("guest versions/architecture differ from the build manifest")
            slots = int((SYSFS / "pcie_slots_in_use").read_text().strip())
            if slots != 1:
                raise RuntimeError("expected exactly one hardware Sandbox after launch")
            base.update(boot_id=health["boot_id"], dsh_version=health.get("dsh_version"),
                        node_version=health.get("node_version"), guest_arch=health.get("arch"),
                        slots_in_use=slots)
            for phase in ("cold", "warm"):
                rec = {**base, "phase": phase, "ok": False,
                       "error": "request interrupted before a validated response"}
                records.append(rec)
                invoke_start = time.perf_counter()
                payload = {"mode": args.workload, "timeout_ms": args.agent_timeout * 1000}
                if args.workload == "agent":
                    payload.update(region=args.region, model=args.model, prompt=args.prompt,
                                   expected_text=args.expected_text, credentials=creds)
                try:
                    data = request(f"http://{GUEST_IP}:8080/invoke", payload,
                                   timeout=args.agent_timeout + 5)
                    end = time.perf_counter()
                    rec.update(validate_result(data, run_id, health["boot_id"], args.workload))
                    if rec["ok"]:
                        rec.pop("error", None)
                    rec.update(request_ms=(end - invoke_start) * 1000,
                               sandbox_ready_ms=(ready - start) * 1000 if phase == "cold" else None,
                               launch_to_response_ms=(end - start) * 1000 if phase == "cold" else None,
                               ready_to_invoke_ms=(invoke_start - ready) * 1000 if phase == "cold" else None,
                               request_non_dsh_ms=(end - invoke_start) * 1000 - rec["dsh_total_ms"])
                except (OSError, ValueError, urllib.error.URLError):
                    # The request may already have run. Record failure and stop the
                    # VM, never retry it or accidentally classify the next call as cold.
                    rec["error"] = "HTTP/response validation failed; request not retried"
                if not rec["ok"] and not (args.continue_on_answer_mismatch and answer_mismatch(rec)):
                    break
        except (OSError, ValueError, RuntimeError, TimeoutError) as err:
            records.append({**base, "phase": "cold", "ok": False,
                            "error": f"launch/readiness failed: {type(err).__name__}"})
        finally:
            if not records:
                records.append({**base, "phase": "cold", "ok": False,
                                "error": "launch interrupted before invocation"})
            # Persist BEFORE cleanup, so a stuck slot/interrupt cannot erase samples.
            try:
                (directory / "records.json").write_text(json.dumps(records, indent=2) + "\n")
            finally:
                stop(proc)
    # Do not launch another VM until hardware capacity has actually been released.
    deadline = time.perf_counter() + 10
    while int((SYSFS / "pcie_slots_in_use").read_text().strip()) != 0:
        if time.perf_counter() >= deadline:
            raise RuntimeError("Sandbox slot was not released; stop the benchmark and inspect this parent")
        time.sleep(.05)
    return records

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kernel", type=Path, default=Path("artifacts/vmlinuz"))
    p.add_argument("--initrd", type=Path, default=Path("artifacts/initrd.img"))
    p.add_argument("--manifest", type=Path, default=Path("artifacts/manifest.json"))
    p.add_argument("--firecracker", default="firecracker")
    p.add_argument("--rounds", type=positive_int, default=10)
    p.add_argument("--vcpus", type=positive_int, default=2)
    p.add_argument("--memory-mib", type=positive_int, default=4096)
    p.add_argument("--boot-timeout", type=positive_float, default=120)
    p.add_argument("--agent-timeout", type=positive_float, default=120)
    p.add_argument("--workload", choices=("startup", "agent"), default="startup")
    p.add_argument("--direct-mode", action="store_true")
    p.add_argument("--debug", action="store_true", help="guest console visible; invalidates attestation measurements")
    p.add_argument("--credentials-file", type=Path)
    p.add_argument("--region")
    p.add_argument("--model")
    p.add_argument("--prompt", default="Reply with exactly: pong. Do not use tools.")
    p.add_argument("--expected-text", default="pong", help="required final answer; empty disables answer checking")
    p.add_argument("--continue-on-answer-mismatch", action="store_true",
                   help="measure completed text turns even if answer differs; stop after 3 mismatching rounds")
    p.add_argument("--out", type=Path, default=Path("results") / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return p


def main() -> int:
    p = parser()
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error("run on the prepared parent as root (sudo python3 benchmark.py ...)")
    if args.memory_mib < 256 or args.agent_timeout > 300:
        p.error("memory must be >= 256 MiB; agent timeout must be <= 300 s")
    if args.workload == "agent" and not (args.credentials_file and args.region and args.model and args.direct_mode):
        p.error("agent mode needs --direct-mode, --credentials-file, --region and --model")
    if not args.prompt.strip() or len(args.prompt) > 4000:
        p.error("prompt must have 1-4000 non-whitespace characters")
    if len(args.expected_text) > 256:
        p.error("expected text must be at most 256 characters")
    # This lock serializes this demo, not other orchestrators; use an idle parent.
    records = []
    with open("/run/dsh-nitro-benchmark.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = preflight(args)
        args.expected_guest = host["build_manifest"]
        creds = credentials(args.credentials_file) if args.workload == "agent" else {}
        args.out.mkdir(parents=True, mode=0o700, exist_ok=False)
        document = {"schema_version": 1, "tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "requested_rounds": args.rounds, "host": host,
                    "configuration": {"workload": args.workload, "direct_mode": args.direct_mode,
                                      "debug_mode": args.debug, "cpu_overcommit": True,
                                      "vcpus": args.vcpus, "memory_mib": args.memory_mib,
                                      "region": args.region, "model": args.model,
                                      "continue_on_answer_mismatch": args.continue_on_answer_mismatch,
                                      "max_output_tokens": 512 if args.workload == "agent" else None,
                                      "expected_text": args.expected_text if args.workload == "agent" else None,
                                      "prompt_sha256": hashlib.sha256(args.prompt.encode()).hexdigest()},
                    "cache_policy": "new guest/initramfs every round; parent image cache NOT evicted",
                    "percentile_method": "nearest-rank", "records": records}
        try:
            with network(args.workload == "agent") as tap:
                document["network"] = {"tap": tap, "table": f"dshb_{tap[3:]}"}
                mismatch_streak = 0
                for number in range(1, args.rounds + 1):
                    group = run_round(args, number, tap, args.out, creds)
                    records.extend(group)
                    document["summary"] = summarize(records)
                    (args.out / "results.json").write_text(json.dumps(document, indent=2) + "\n")
                    print(f"round {number}: " + ", ".join(f"{r['phase']} ok={r['ok']}" for r in group), flush=True)
                    # Never retry execution/HTTP failures. A completed reply whose
                    # formatting differs is a quality outcome, not an API rejection.
                    bad = [row for row in group if not row["ok"]]
                    if bad:
                        if args.continue_on_answer_mismatch and all(answer_mismatch(row) for row in bad):
                            mismatch_streak += 1
                            if mismatch_streak < 3:
                                continue
                            document["stopped_early"] = "3 consecutive rounds with answer mismatch; inspect before continuing"
                        else:
                            document["stopped_early"] = "failed round; inspect evidence before starting a new run"
                        break
                    mismatch_streak = 0
        except KeyboardInterrupt:
            document["stopped_early"] = "interrupted by operator"
        except Exception as err:
            document["stopped_early"] = f"infrastructure/cleanup error: {type(err).__name__}"
            raise
        finally:
            saved = sorted(args.out.glob("round-*/records.json"))
            if saved:
                records[:] = [row for file in saved for row in json.loads(file.read_text())]
            document["summary"] = summarize(records)
            document["completion_summary"] = completion_summary(records)
            document["answer_mismatch_records"] = sum(answer_mismatch(row) for row in records)
            document["first_round_records"] = [r for r in records if r["round"] == 1]
            document["subsequent_summary"] = summarize([r for r in records if r["round"] > 1])
            document["executed_rounds"] = len({r["round"] for r in records})
            document["host"]["loadavg_end"] = os.getloadavg()
            document["host"]["slots_after"] = (SYSFS / "pcie_slots_in_use").read_text().strip()
            (args.out / "results.json").write_text(json.dumps(document, indent=2) + "\n")
            print(f"Evidence: {args.out / 'results.json'}", flush=True)
    print(json.dumps(document["summary"], indent=2))
    fatal = any(not row["ok"] and not (args.continue_on_answer_mismatch and answer_mismatch(row)) for row in records)
    return 1 if document.get("stopped_early") or fatal else 0

if __name__ == "__main__":
    raise SystemExit(main())
