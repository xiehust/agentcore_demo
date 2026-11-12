#!/usr/bin/env python3
"""Local Docker baseline for the DSH cold-start benchmark.

Runs the same image the AgentCore runtimes use, on this machine, and records
the same metrics so the report can put the two side by side:

  ping sequence   new container -> first healthy /ping -> ping invocation,
                  then a second (warm) ping invocation
  agent sequence  new container -> first healthy /ping -> one DSH turn,
                  then a second (warm) DSH turn in the same container

"Cold" here means a brand-new container. The host page cache is warm and the
image is already on disk, so these numbers are a floor for the platform, not a
like-for-like cold start. The port is published on loopback only.

Usage: python3 scripts/local_baseline.py [--rounds 5] [--prewarm]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "dsh-coldstart-baseline"
PROMPT = "Reply with exactly: pong"


def sh(*cmd: str, env: dict | None = None) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def aws_env() -> dict:
    """Credentials for the container, exported from the active AWS profile."""
    out = sh("aws", "configure", "export-credentials", "--format", "env-no-export")
    creds = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {**os.environ, **creds}


def post(port: int, body: dict, timeout: float = 180.0) -> tuple[float, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/invocations",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return (time.perf_counter() - t0) * 1000, data


def start_container(image: str, port: int, region: str, prewarm: str, env: dict) -> float:
    """Start a new container; return ms from `docker run` to first healthy /ping."""
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    t0 = time.perf_counter()
    sh(
        "docker", "run", "-d", "--name", NAME, "--platform", "linux/arm64",
        "-p", f"127.0.0.1:{port}:8080",
        "-e", "AWS_ACCESS_KEY_ID", "-e", "AWS_SECRET_ACCESS_KEY", "-e", "AWS_SESSION_TOKEN",
        "-e", f"AWS_REGION={region}", "-e", f"DSH_PREWARM={prewarm}",
        image, env=env,
    )
    deadline = t0 + 60
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as resp:
                if resp.status == 200:
                    return (time.perf_counter() - t0) * 1000
        except OSError:
            time.sleep(0.02)
    raise RuntimeError("container did not become healthy within 60 s")


def evict_page_cache() -> int:
    """Drop the host page cache for the files one DSH turn reads (the pin
    manifest), from inside the container: `dd iflag=nocache count=0` advises the
    kernel to drop a file's cached pages. Only these files are affected, nothing
    else on the machine, and no root on the host is needed. Pages mapped by a
    running process (the wrapper's own node binary) stay, as they would anywhere."""
    paths = [l for l in (ROOT / "app" / "pin-manifest.txt").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    subprocess.run(
        ["docker", "exec", "-i", "-u", "root", NAME, "sh", "-c",
         'while IFS= read -r f; do dd if="$f" iflag=nocache count=0 status=none 2>/dev/null; done; sync'],
        input="\n".join(paths) + "\n", text=True, check=True)
    return len(paths)


def record(rnd: int, sequence: str, phase: str, start_ms: float | None, total_ms: float, server: dict) -> dict:
    agent = server.get("agent")

    def since_spawn(field: str) -> float | None:
        if not agent or agent.get(field) is None or agent.get("spawn_ts") is None:
            return None
        return round((agent[field] - agent["spawn_ts"]) * 1000, 1)

    return {
        "round": rnd,
        "sequence": sequence,
        "phase": phase,
        "ok": bool(agent["ok"]) if agent else True,
        "container_start_ms": None if start_ms is None else round(start_ms, 1),
        "client_total_ms": round(total_ms, 1),
        "dsh_boot_ms": since_spawn("first_output_ts"),
        "first_text_ms": since_spawn("first_text_ts"),
        "dsh_total_ms": since_spawn("exit_ts"),
        "server": server,
    }


def stats(values: list[float], failures: int) -> dict:
    if not values:
        return {"n": 0, "failures": failures, "min": None, "p50": None, "p90": None, "max": None, "mean": None}
    ordered = sorted(values)

    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))]

    return {
        "n": len(ordered), "failures": failures, "min": ordered[0], "p50": pct(0.5), "p90": pct(0.9),
        "max": ordered[-1], "mean": round(statistics.fmean(ordered), 1),
    }


def summarize(records: list[dict]) -> dict:
    summary = {}
    metrics = ("container_start_ms", "client_total_ms", "dsh_boot_ms", "first_text_ms", "dsh_total_ms")
    for sequence in ("ping", "agent"):
        for phase in ("cold", "warm"):
            group = [r for r in records if r["sequence"] == sequence and r["phase"] == phase]
            failures = sum(1 for r in group if not r["ok"])
            for metric in metrics:
                values = [r[metric] for r in group if r["ok"] and r[metric] is not None]
                if values:
                    summary[f"{sequence}.{phase}.{metric}"] = stats(values, failures)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--prewarm", action="store_true", help="shorthand for --runtime dsh_prewarm_v2")
    parser.add_argument("--runtime", default=None,
                        help="deployments.json key whose image and DSH_PREWARM value to run locally "
                             "(default dsh_v2)")
    parser.add_argument("--prewarm-wait", type=float, default=6.0,
                        help="seconds to let the pre-warm finish before the first agent turn")
    parser.add_argument("--evict-page-cache", action="store_true",
                        help="before each cold agent turn, drop the page cache of the files a DSH turn "
                             "reads, so DSH starts from disk instead of from memory")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    deploy = json.loads((ROOT / "deployments.json").read_text())
    key = args.runtime or ("dsh_prewarm_v2" if args.prewarm else "dsh_v2")
    image = deploy["runtimes"][key]["image_uri"]      # same image as the deployed runtime
    prewarm = str(deploy["runtimes"][key].get("env", {}).get("DSH_PREWARM", "0"))
    region = deploy["region"]
    out = Path(args.out) if args.out else ROOT / "results" / (
        ("local_baseline" if key == "dsh_v2" else f"local_baseline_{key}")
        + ("_coldcache" if args.evict_page_cache else "") + ".json")
    env = aws_env()
    load_start = os.getloadavg()

    records = []
    try:
        for rnd in range(1, args.rounds + 1):
            for sequence, body in (("ping", {"mode": "ping"}), ("agent", {"mode": "agent", "prompt": PROMPT})):
                start_ms = start_container(image, args.port, region, prewarm, env)
                if prewarm == "1" and sequence == "agent":
                    time.sleep(args.prewarm_wait)
                if args.evict_page_cache and sequence == "agent":
                    evict_page_cache()
                for phase in ("cold", "warm"):
                    total_ms, server = post(args.port, body)
                    rec = record(rnd, sequence, phase, start_ms if phase == "cold" else None, total_ms, server)
                    records.append(rec)
                    print(f"round {rnd} {sequence:5s} {phase:4s} ok={rec['ok']} start={rec['container_start_ms']} "
                          f"total={rec['client_total_ms']} dsh_boot={rec['dsh_boot_ms']} "
                          f"first_text={rec['first_text_ms']} dsh_total={rec['dsh_total_ms']}")
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)

    mem_kb = int(next(l for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal")).split()[1])
    doc = {
        "host": {
            "machine": platform.machine(), "cpus": os.cpu_count(), "mem_gb": round(mem_kb / 1024 / 1024, 1),
            "kernel": platform.release(), "docker": sh("docker", "version", "--format", "{{.Server.Version}}"),
            "cpu_part": next((l.split(":")[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines()
                              if l.startswith("CPU part")), None),
            # Other work on this machine slows DSH's start, so record how busy it was.
            "loadavg_start": [round(x, 2) for x in load_start],
            "loadavg_end": [round(x, 2) for x in os.getloadavg()],
        },
        "image": image,
        "image_size_bytes": int(sh("docker", "image", "inspect", "--format", "{{.Size}}", image)),
        "region": region,
        "model_id": deploy["model_id"],
        "dsh_version": deploy["dsh_version"],
        "variant": key,
        "dsh_prewarm": prewarm,
        "prewarm_wait_s": args.prewarm_wait if prewarm == "1" else None,
        "tested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rounds": args.rounds,
        "page_cache": "evicted before each cold agent turn" if args.evict_page_cache else "warm",
        "records": records,
        "summary": summarize(records),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    print(f"Wrote {out}")
    for name, s in doc["summary"].items():
        print(f"  {name:36s} n={s['n']} p50={s['p50']} min={s['min']} max={s['max']}")


if __name__ == "__main__":
    main()
