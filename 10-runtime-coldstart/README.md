# AgentCore Runtime Cold-Start Benchmark (ping-pong)

[中文版 / Chinese version](README.zh.md)

Measures **end-to-end cold-start latency** of Amazon Bedrock AgentCore Runtime as a
function of **container image size** (≈500 MB / 1 GB / ~1.95 GB) and **invocation
concurrency** (1 / 5 / 10 / 50). The agent is a minimal ping-pong on the real
`bedrock-agentcore` SDK (`BedrockAgentCoreApp`) with no LLM calls, so the numbers are
pure infrastructure latency.

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
```
