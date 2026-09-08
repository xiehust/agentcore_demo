#!/usr/bin/env python3
"""Targeted experiment: what does managed session storage keep when several
execution environments serve one runtimeSessionId at the same time?

Steps (all against the pool's Runtime, on a throw-away session id, bypassing
the Router so the fan-out is deliberately provoked):

  A  warm up -> boot A; write baseline.txt via InvokeAgentRuntimeCommand
  B  StopRuntimeSession, wait for teardown
  C  fire N concurrent first-calls (warm-up invokes + shell commands); every
     command writes writer-<i>.txt containing its own boot id, then lists the
     directory it can see
  D  a few sequential commands: what does the "current" environment see now?
  E  StopRuntimeSession again, wait so every environment can flush
  F  fresh environment (boot F) restores from durable storage: list the dir
  G  StopRuntimeSession

Output: results/fanout_probe_<ts>.json and a console summary.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "router"))

from config import Settings  # noqa: E402
from invoker import AgentCoreInvoker  # noqa: E402
from runtime_session import RuntimeSession, atomic_write_json, create_agentcore_client  # noqa: E402

DIR = "/mnt/workspace/fanout"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def sh(session: RuntimeSession, script: str, timeout: int = 60) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = session.run_shell_script(script, timeout=timeout, require_success=False)
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300], "stdout": "", "stderr": ""}
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def boot_of(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("BOOT="):
            return line[5:].strip()[:8]
    return None


def listing_of(stdout: str) -> list[str]:
    files: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("FILE="):
            files.append(line[5:].strip())
    return sorted(files)


# boot_id is inherited from the microVM snapshot and is NOT unique per execution
# environment (observed: two values across dozens of microVMs). Each environment
# therefore also gets a private marker in /tmp (outside session storage) on first
# touch; ENV=<marker> identifies the environment reliably.
ENV_MARK = "[ -f /tmp/env-mark ] || cat /proc/sys/kernel/random/uuid > /tmp/env-mark; echo ENV=$(cut -c1-8 /tmp/env-mark)"

LIST_SCRIPT = f"""
echo BOOT=$(cat /proc/sys/kernel/random/boot_id)
{ENV_MARK}
mkdir -p {DIR}
for f in {DIR}/*; do [ -f "$f" ] && echo "FILE=$(basename "$f"):$(cat "$f" | head -c 8)"; done
true
"""


def env_of(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("ENV="):
            return line[4:].strip()
    return None


def writer_script(index: int, hold_s: int) -> str:
    return f"""
B=$(cat /proc/sys/kernel/random/boot_id)
{ENV_MARK}
E=$(cut -c1-8 /tmp/env-mark)
mkdir -p {DIR}
echo "$E" > {DIR}/writer-{index:02d}.txt
sync
sleep {hold_s}
echo BOOT=$B
for f in {DIR}/*; do [ -f "$f" ] && echo "FILE=$(basename "$f"):$(cat "$f" | head -c 8)"; done
true
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "pool.json"))
    parser.add_argument("--writers", type=int, default=8)
    parser.add_argument("--warmups", type=int, default=8, help="concurrent warm-up invokes fired alongside the writers")
    parser.add_argument("--hold-s", type=int, default=8, help="how long each writer keeps its environment busy")
    parser.add_argument("--teardown-wait-s", type=int, default=20)
    parser.add_argument("--flush-wait-s", type=int, default=45)
    parser.add_argument("--baseline-settle-s", type=int, default=30, help="seconds between writing baseline.txt and the first stop (flush-latency probe)")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    settings = Settings(region=config["region"], table_name=config["tableName"], runtime_arn=config["runtimeArn"])
    invoker = AgentCoreInvoker(settings)
    runtime = {"region": config["region"], "runtimeArn": config["runtimeArn"]}
    client = create_agentcore_client(runtime, read_timeout=300, max_connections=64)
    sid = f"fanout-{uuid.uuid4().hex}"
    session = RuntimeSession(runtime, sid, client)
    report: dict[str, Any] = {"session_id": sid, "runtime_arn": config["runtimeArn"], "args": vars(args), "steps": {}}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "results" / f"fanout_probe_{stamp}.json"

    def save() -> None:
        atomic_write_json(out, report)

    def fp(payload: dict[str, Any]) -> str:
        inst = payload.get("instance") or {}
        return f"{(inst.get('boot_id') or '')[:8]}/{(inst.get('server_run_id') or '')[:8]}"

    try:
        print(f"{now()} A: warm up {sid}, write baseline.txt, settle {args.baseline_settle_s}s")
        warm = invoker.warmup(sid)
        base = sh(session, f"mkdir -p {DIR}; echo A > {DIR}/baseline.txt; sync; " + LIST_SCRIPT)
        report["steps"]["A"] = {"warmup_fp": fp(warm), "env": env_of(base["stdout"]), "files": listing_of(base["stdout"]), "error": base.get("error")}
        print(f"   env {report['steps']['A']['env']} files {report['steps']['A']['files']}")
        save()
        time.sleep(args.baseline_settle_s)

        print(f"{now()} B: StopRuntimeSession, wait {args.teardown_wait_s}s")
        report["steps"]["B"] = invoker.stop_session(sid)
        time.sleep(args.teardown_wait_s)

        print(f"{now()} C: {args.warmups} concurrent warm-ups + {args.writers} concurrent writers")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.warmups + args.writers) as pool:
            warm_futs = [pool.submit(invoker.warmup, sid) for _ in range(args.warmups)]
            write_futs = [pool.submit(sh, session, writer_script(i, args.hold_s), 120) for i in range(args.writers)]
            warm_fps = []
            for f in warm_futs:
                try:
                    warm_fps.append(fp(f.result()))
                except Exception as exc:  # noqa: BLE001
                    warm_fps.append(f"ERR:{type(exc).__name__}")
            writers = []
            for i, f in enumerate(write_futs):
                r = f.result()
                writers.append({"writer": i, "env": env_of(r["stdout"]), "saw_files": listing_of(r["stdout"]), "error": r.get("error"), "latency_ms": r["latency_ms"]})
        envs_seen = sorted({w["env"] for w in writers if w["env"]})
        report["steps"]["C"] = {
            "warmup_fingerprints": warm_fps,
            "distinct_warmup_processes": len({x for x in warm_fps if not x.startswith("ERR")}),
            "writers": writers,
            "distinct_writer_envs": envs_seen,
        }
        print(f"   warm-up server_run_ids: {report['steps']['C']['distinct_warmup_processes']} distinct; writer environments: {len(envs_seen)}")
        for w in writers:
            print(f"   writer-{w['writer']:02d} env={w['env']} saw={w['saw_files']} err={w['error']}")
        save()

        print(f"{now()} D: 3 sequential listings on the live session")
        report["steps"]["D"] = []
        for _ in range(3):
            r = sh(session, LIST_SCRIPT)
            report["steps"]["D"].append({"env": env_of(r["stdout"]), "files": listing_of(r["stdout"]), "error": r.get("error")})
            print(f"   env={report['steps']['D'][-1]['env']} files={report['steps']['D'][-1]['files']}")
            time.sleep(2)
        save()

        print(f"{now()} E: StopRuntimeSession, wait {args.flush_wait_s}s for flush/teardown")
        report["steps"]["E"] = invoker.stop_session(sid)
        time.sleep(args.flush_wait_s)

        print(f"{now()} F: fresh environment restores from durable storage")
        warm_f = invoker.warmup(sid)
        r = sh(session, LIST_SCRIPT)
        report["steps"]["F"] = {"warmup_fp": fp(warm_f), "env": env_of(r["stdout"]), "files": listing_of(r["stdout"]), "error": r.get("error")}
        print(f"   env {report['steps']['F']['env']} durable files: {report['steps']['F']['files']}")
        save()
    finally:
        print(f"{now()} G: StopRuntimeSession")
        report["steps"]["G"] = invoker.stop_session(sid)
        save()

    # ------------------------------------------------------------ analysis
    c = report["steps"]["C"]
    f_files = report["steps"]["F"]["files"]
    written = {f"writer-{w['writer']:02d}.txt": w["env"] for w in c["writers"] if w["env"] and not w["error"]}
    survived = {name.split(":")[0]: name.split(":")[1] for name in f_files}
    lost = sorted(n for n in written if n not in survived)
    kept_by_env: dict[str, int] = {}
    for name, env in written.items():
        if name in survived:
            kept_by_env[env] = kept_by_env.get(env, 0) + 1
    d_envs = sorted({d["env"] for d in report["steps"]["D"] if d["env"]})
    report["analysis"] = {
        "distinct_environments_during_C": len(c["distinct_writer_envs"]),
        "environment_serving_after_C": d_envs,
        "files_written_in_C": len(written),
        "files_survived_in_F": len([n for n in written if n in survived]),
        "files_lost": lost,
        "survivors_by_environment": kept_by_env,
        "baseline_survived_after_settle_s": {"settle_s": args.baseline_settle_s, "survived": any(n.startswith("baseline.txt") for n in f_files)},
    }
    save()
    print("\n=== analysis ===")
    print(json.dumps(report["analysis"], indent=2))
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
