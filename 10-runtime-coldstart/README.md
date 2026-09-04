# AgentCore Runtime Cold-Start Benchmark (ping-pong) — now vs AWS Lambda MicroVMs

[中文版 / Chinese version](README.zh.md)

Measures **end-to-end cold-start latency** of Amazon Bedrock AgentCore Runtime as a
function of **container image size** (≈500 MB / 1 GB / ~1.95 GB) and **invocation
concurrency** (1 / 5 / 10 / 50). The agent is a minimal ping-pong on the real
`bedrock-agentcore` SDK (`BedrockAgentCoreApp`) with no LLM calls, so the numbers are
pure infrastructure latency.

A second track runs the **same agent, image ladder and concurrency ladder on
[AWS Lambda MicroVMs](https://aws.amazon.com/lambda/lambda-microvms/)** (GA June 2026:
Firecracker VMs resumed from a memory+disk snapshot of your built image) — see
[Lambda MicroVMs track](#lambda-microvms-track) and the side-by-side report
[results/COMPARE.md](results/COMPARE.md) · [中文](results/COMPARE.zh.md).

## Headline results (2026-07-09, us-west-2)

| image | c=1 p50 | c=5 p50 | c=10 p50 | c=50 p50 | genuine boot p50 | warm p50 |
|---|---|---|---|---|---|---|
| 500 MB | 412 ms | 667 ms | 7,320 ms | 8,149 ms | ~7.4–8.2 s | ~100 ms |
| 1 GB | 732 ms | 763 ms | 11,401 ms | 11,422 ms | ~11.4 s | ~70 ms |
| 2 GB (1,950 MB) | 1,176 ms | 1,155 ms | 13,458 ms | 13,493 ms | ~13.5 s | ~70 ms |

Key insight: a fresh `runtimeSessionId` does **not** always boot a microVM. AgentCore
keeps a small **pre-warmed pool** per runtime (~5 instances observed, replenished within
minutes of deployment). The share of requests that missed the pool and paid the genuine
boot cost: c=1: 0%, c=5: 25–30% (median still sub-second), c=10: 55–60%, c=50: 82–86%.
The boot cost itself scales with image size (**≈ +2–4 s per +500 MB compressed**). Warm
requests are flat ~70–100 ms regardless of size. Full analysis:
[results/REPORT.md](results/REPORT.md) · [中文报告](results/REPORT.zh.md).

## Architecture

```
coldstart_test.py ──InvokeAgentRuntime(fresh 40-char sessionId)──▶ AgentCore Runtime
  (boto3, retries off,                                             ├─ microVM per session
   threading.Barrier for N                                         │  (pre-warmed pool or
   simultaneous requests)                                          │   genuine boot: ECR pull + start)
                                                                   └─ ping-pong container
      3 runtimes, one per image size:                                 (BedrockAgentCoreApp,
      coldstart_ping_500mb / _1gb / _2gb                               returns pong + proc_start_ts)
```

The agent returns `proc_start_ts` (process start) and `request_ts`, so each probe is
classified: **genuine boot** if the process started during the request, else
**pre-warmed hit**. Image size is calibrated with incompressible `/dev/urandom` pad
layers (≤500 MB chunks), so ECR compressed size ≈ uncompressed size.

## Prerequisites

- Docker with an ARM64-capable builder (AgentCore requires linux/arm64 images)
- AWS credentials with ECR, IAM and `bedrock-agentcore*` permissions; region us-west-2
- [uv](https://docs.astral.sh/uv/) and Python ≥3.11

## Quick start

```bash
bash scripts/build_images.sh        # build 3 size-calibrated ARM64 images (~2 min)
bash scripts/test_local.sh          # local contract test: /ping + /invocations, 3x PASS
bash scripts/deploy.sh              # ECR push + IAM role + 3 runtimes -> deployments.json (idempotent)
uv sync
uv run python coldstart_test.py --smoke   # single cold+warm probe vs the 500mb runtime
uv run python coldstart_test.py --full    # full matrix (~15 min, ~300 sessions)
python3 scripts/gen_report.py             # regenerate REPORT.md + REPORT.zh.md from the data
```

Useful flags: `--sizes 500mb,1gb --concurrency 1,10 --rounds-c1 5 --out results/` —
see `--help`. Raw per-request data lands in `results/raw/*.json`, aggregates in
`results/summary.json`.

## Platform facts worth knowing

- **Max image size: 2048 MB** (Service Quotas: "Maximum size for a Docker image in an
  AgentCore Runtime") — hence the "2GB" variant targets 1,950 MB.
- Cold start = first `InvokeAgentRuntime` with a fresh **33+ char** `runtimeSessionId`
  (each session gets its own microVM). Reusing the session ID within the idle timeout
  hits the same warm microVM.
- `InvokeAgentRuntime` quota: 200 req/s per agent — c=50 bursts are fine (we saw 1
  throttle in 240 probes).
- `UpdateAgentRuntime` wipes the pre-warmed pool; it replenishes within a few minutes.

## Cost notes

- Runtimes bill per active microVM-second. Every probe stops its session
  (`StopRuntimeSession`) right after the warm follow-up, and the runtimes are created
  with `idleRuntimeSessionTimeout=60` as a safety net — a full matrix run costs well
  under a dollar of compute.
- ECR storage for the three images ≈ 3.1 GB compressed ≈ $0.31/month.
- The three runtimes cost nothing while idle (no active sessions).

## Cleanup

```bash
bash scripts/cleanup.sh --dry-run   # list the 3 runtimes + ECR repo + IAM role
bash scripts/cleanup.sh --yes      # delete them (runtimes -> ECR -> role)
```

## Lambda MicroVMs track

### Headline results (2026-09-04, us-west-2, client in-region)

| image | c=1 p50 | c=5 p50 | c=10 p50 | c=50 p50 | fast / slow mode | warm p50 | suspend→resume p50 |
|---|---|---|---|---|---|---|---|
| 500 MB | 1,520 ms | 1,460 ms | 1,453 ms | 4,030 ms* | ~1.5 s (68%) / ~3.5 s (32%) | 4 ms | 8.1 s |
| 1 GB | 1,510 ms | 1,500 ms | 1,502 ms | 2,224 ms* | same | 4 ms | 8.1 s |
| 2 GB (1,950 MB) | 3,419 ms† | 1,372 ms | 1,398 ms | 2,396 ms* | same | 4 ms | 6.2 s |

\* c=50 hits the `RunMicrovm` quota (5 TPS / burst 5): 5–13 of 50 launches throttled,
the rest queued ~0.9 s inside the API. † n=10, all landed in the slow mode; the 2 GB
c=5/c=10 cells are indistinguishable from the smaller images.

Key insight: **image size does not matter** on Lambda MicroVMs — every launch resumes
a snapshot and the pad layers are lazily loaded — but the cold start is **bimodal**:
~68% of launches answer in ~1.5 s (the ingress proxy holds the first request ~1 s
until the app is reachable; the guest sees `/run`→request in tens of ms), ~32% take
~3.5 s (the proxy holds ~3 s, returns **502**, and the immediate retry succeeds even
though the guest had been ready for ~1.8 s). Like-for-like, a genuine AgentCore boot
(8.1 / 11.4 / 13.5 s) is 3.7–6× a MicroVM boot (~2.2 s all-cells p50). Warm requests
are ~4 ms (direct same-region HTTPS to the VM's own endpoint) vs 70–100 ms via
`InvokeAgentRuntime`. Full analysis: [results/COMPARE.md](results/COMPARE.md) (generated) and
[results/MICROVM_NOTES.md](results/MICROVM_NOTES.md) (default quotas, why size doesn't matter,
fast/slow mode explained).

### Architecture

```
microvm_coldstart_test.py ──RunMicrovm(image vN)──▶ Lambda MicroVMs control plane
  (boto3 lambda-microvms,      ◀── microvmId + https endpoint      │ resume Firecracker snapshot
   retries off, Barrier)       ──CreateMicrovmAuthToken──▶         │ (memory+disk, taken at build
                               ──POST https://<endpoint>/invocations ──▶ VM ──▶ :8080 BedrockAgentCoreApp
                                 X-aws-proxy-auth, retry on 502        │      :9000 lifecycle hooks
                               ──TerminateMicrovm──▶                    └ /run hook -> run_hook_ts
      3 images, one per size: coldstart-ping-microvm-500mb / -1gb / -2gb
      (built BY Lambda from an S3 zip: microvm/Dockerfile + microvm/app.py)
```

`cold_ms` = `RunMicrovm` call → full body of the first HTTP 200. The agent returns
`proc_start_ts` (image build time — the process was snapshotted after `/ready`),
`run_hook_ts` (Lambda's `/run` hook on this VM) and `request_ts`, so the guest-side
share of the cold start is measurable. `/validate` runs a real `/invocations` on the
test VM so Lambda can prefetch the snapshot pages the hot path touches.

### Quick start

```bash
bash scripts/test_local_microvm.sh                 # docker contract test: agent + 6 hooks, PASS
bash scripts/deploy_microvm.sh                     # S3 bucket + build/exec roles + 3 images (~3 min) -> deployments_microvm.json
uv run python microvm_coldstart_test.py --smoke --resume
uv run python microvm_coldstart_test.py --full --resume            # c=1,5,10 x 3 sizes, ~10 min, 150 VMs
uv run python microvm_coldstart_test.py --full --concurrency 50 --sizes 500mb   # one size per run (quota bucket refill)
python3 scripts/gen_compare_report.py              # -> results/COMPARE.md + COMPARE.zh.md
bash scripts/cleanup_microvm.sh --dry-run | --yes  # VMs -> images -> bucket -> roles
```

Raw per-launch data lands in `results/microvm/raw/*.json`, aggregates in
`results/microvm/summary.json` (schema in the module docstring).

### Platform facts worth knowing

- **Two resources**: `MicrovmImage` (Lambda builds your Dockerfile on the managed
  `al2023-1` base, starts `CMD`, waits for the `/ready` hook, snapshots) and `Microvm`
  (`RunMicrovm` → dedicated HTTPS endpoint; token via `CreateMicrovmAuthToken`, header
  `X-aws-proxy-auth`, default target port 8080). `CreateMicrovmImage` has no build
  args, so the deploy script rewrites the `PADn_MB` defaults per size variant.
- **Readiness is "it answers"**: `GetMicrovm.state` is eventually consistent; the docs
  say to connect. The proxy returns 502 until the app is reachable.
- **Quotas (this account)**: `RunMicrovm` 5 TPS / burst 5 (adjustable), `SuspendMicrovm`
  2 TPS, `TerminateMicrovm` 10 TPS, `CreateMicrovmAuthToken` 50 TPS, 1,024 GB total
  MicroVM memory, 10 concurrent image builds. ARM64 only. Baseline 0.5–8 GB (2 GB /
  1 vCPU default, 4× vertical burst, 8 GB disk).
- **Snapshot semantics**: everything generated at build time (random seeds, IDs,
  `proc_start_ts`) is shared by every VM of that image; use `/run` to re-seed.
- **Idle policy** is a cost safety net: `maxIdleDurationSeconds=60,
  suspendedDurationSeconds=0` terminates a VM a minute after its last request even if
  the client dies. `--resume` probes use `autoResumeEnabled=true` instead.

### Cost notes

- Compute is billed per second on the 2 GB baseline while a VM is RUNNING; every
  probe terminates its VM right after the warm (and resume) follow-up, so a full matrix
  is a few VM-minutes. Suspended VMs are storage-only.
- Image versions (3 snapshots ≈ 0.5 + 1 + 2 GB disk) incur snapshot storage until
  deleted — run `cleanup_microvm.sh --yes` when done.

## Layout

```
app/main.py           ping-pong agent (BedrockAgentCoreApp)
docker/Dockerfile     base + PAD1..4_MB urandom pad layers
scripts/build_images.sh  builds and size-verifies the 3 images
scripts/test_local.sh    local /ping + /invocations contract test
scripts/deploy.sh        ECR + IAM + 3 runtimes (idempotent), writes deployments.json
scripts/gen_report.py    regenerates REPORT.md + REPORT.zh.md from recorded data
scripts/cleanup.sh       tears everything down (--dry-run | --yes)
coldstart_test.py        benchmark client (--smoke | --full)
deployments.json         generated: ARNs + image sizes
results/                 raw probes, summary.json, REPORT.md, REPORT.zh.md

microvm/app.py               same agent + lifecycle hooks server (:9000) for Lambda MicroVMs
microvm/Dockerfile           al2023-minimal base + python3.12 + SDK + pad layers (+ EXPOSE 8080 9000)
scripts/test_local_microvm.sh  docker contract test incl. the 6 hooks
scripts/deploy_microvm.sh      S3 + IAM + 3 MicroVM images (idempotent), writes deployments_microvm.json
scripts/cleanup_microvm.sh     VMs -> images -> bucket -> roles (--dry-run | --yes)
scripts/gen_compare_report.py  regenerates COMPARE.md + COMPARE.zh.md from both summaries
microvm_coldstart_test.py      MicroVM benchmark client (--smoke | --full [--resume] [--cw-logs])
deployments_microvm.json       generated: image ARNs/versions, pad sizes, roles, bucket
results/microvm/               raw launches, summary.json, run logs
results/COMPARE.md(.zh.md)     AgentCore vs Lambda MicroVMs side-by-side report (generated)
results/MICROVM_NOTES.md(.zh.md)  hand-written notes: default quotas, size independence, fast/slow mode
```
