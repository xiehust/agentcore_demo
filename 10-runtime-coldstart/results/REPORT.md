# AgentCore Runtime cold-start benchmark — results

- **Date:** 2026-07-09 (UTC) · **Region:** us-west-2 · **Account:** …5045
- **Agent:** ping-pong on the `bedrock-agentcore` SDK (`BedrockAgentCoreApp`), no LLM calls — pure infrastructure latency.
- **Images** (uncompressed docker / compressed ECR):
  - `coldstart_ping_500mb` — 501 MB / 383 MB
  - `coldstart_ping_1gb` — 1,025 MB / 907 MB
  - `coldstart_ping_2gb` — 1,951 MB / 1,833 MB

## Cold-start latency (end-to-end, ms) — all successful first-invokes per new session

| image | c=1 p50/p90/max | c=5 p50/p90/max | c=10 p50/p90/max | c=50 p50/p90/max |
|---|---|---|---|---|
| **500mb** | 412 / 436 / 503 | 667 / 7,339 / 7,432 | 7,320 / 7,591 / 7,723 | 8,149 / 8,348 / 10,285 |
| **1gb** | 732 / 1,170 / 1,208 | 763 / 11,438 / 15,409 | 11,401 / 11,470 / 11,503 | 11,422 / 11,501 / 39,581 |
| **2gb** | 1,176 / 1,811 / 2,090 | 1,155 / 13,478 / 13,619 | 13,458 / 13,610 / 15,620 | 13,493 / 13,634 / 15,778 |

## The two populations: genuine microVM boots vs pre-warmed instances

A “cold” invoke (fresh 33+ char `runtimeSessionId`) does **not** always boot a microVM: AgentCore keeps a small pre-warmed pool per runtime (created/replenished after Create/UpdateAgentRuntime and over time). The agent's in-VM `proc_start_ts` lets us split the populations — a probe is a *fresh boot* when the agent process started during the request.

| cell | fresh boots | fresh p50 ms | fresh max ms | pre-warmed hits | pre-warmed p50 ms |
|---|---|---|---|---|---|
| 500mb c=1 | 0 | — | — | 10 | 412 |
| 500mb c=5 | 5 | 7,330 | 7,432 | 15 | 630 |
| 500mb c=10 | 11 | 7,429 | 7,723 | 9 | 576 |
| 500mb c=50 | 41 | 8,174 | 10,285 | 8 | 397 |
| 1gb c=1 | 0 | — | — | 10 | 732 |
| 1gb c=5 | 5 | 11,434 | 15,409 | 15 | 731 |
| 1gb c=10 | 11 | 11,420 | 11,503 | 9 | 421 |
| 1gb c=50 | 41 | 11,434 | 39,581 | 9 | 422 |
| 2gb c=1 | 0 | — | — | 10 | 1,176 |
| 2gb c=5 | 6 | 13,471 | 13,619 | 14 | 907 |
| 2gb c=10 | 12 | 13,497 | 15,620 | 8 | 430 |
| 2gb c=50 | 43 | 13,517 | 15,778 | 6 | 436 |

**Genuine cold boot p50 by image size: 8,121 ms (500mb) → 11,428 ms (1gb) → 13,503 ms (2gb).** Image size is the dominant factor; concurrency mainly determines *how many* requests miss the pre-warmed pool (c=1: 0%; c=5: 25–30%; c=10: 55–60%; c=50: 82–86%).

## Warm baseline (2nd invoke, same session)

| image | warm p50 (ms) |
|---|---|
| 500mb | c=1: 101.5, c=5: 83.3, c=10: 95.8, c=50: 76.2 |
| 1gb | c=1: 68.6, c=5: 75.7, c=10: 72.5, c=50: 73.8 |
| 2gb | c=1: 75.7, c=5: 84.2, c=10: 70.3, c=50: 68.9 |

Warm latency is ~70–100 ms regardless of image size — image size only matters for boots.

## Success / errors

| cell | samples | success | throttles | other errors |
|---|---|---|---|---|
| 500mb c=1 | 10 | 10 | 0 | 0 |
| 500mb c=5 | 20 | 20 | 0 | 0 |
| 500mb c=10 | 20 | 20 | 0 | 0 |
| 500mb c=50 | 50 | 49 | 0 | 1 |
| 1gb c=1 | 10 | 10 | 0 | 0 |
| 1gb c=5 | 20 | 20 | 0 | 0 |
| 1gb c=10 | 20 | 20 | 0 | 0 |
| 1gb c=50 | 50 | 50 | 0 | 0 |
| 2gb c=1 | 10 | 10 | 0 | 0 |
| 2gb c=5 | 20 | 20 | 0 | 0 |
| 2gb c=10 | 20 | 20 | 0 | 0 |
| 2gb c=50 | 50 | 49 | 1 | 0 |

2/300 probes failed (other, throttle); all cells ≥90% success. Sessions stopped after measurement: 298/300 (the unstopped ones are failed probes that never created a session).

## Post-deploy first invoke

The very first invoke after an UpdateAgentRuntime (500mb smoke, 06:04 UTC) was a genuine boot at 7,404 ms. By the time the matrix ran (~3 min later) every c=1 probe landed on pre-warmed instances — AgentCore replenishes the warm pool within a few minutes of deployment. Plan for genuine-boot latency (table above) whenever traffic exceeds the pool.

## Methodology

- Cold start = client-side wall time of `InvokeAgentRuntime` with a **fresh 40-char `runtimeSessionId`** (new microVM per AgentCore's session model), from API call to full response-body read. boto3, SigV4, us-west-2, from an EC2 instance in-region.
- botocore retries **disabled** (`total_max_attempts=1`) — throttles/errors are counted, never silently retried into the latency distribution. Read timeout 300 s.
- Concurrency-N: N threads released simultaneously by a `threading.Barrier`.
- Rounds per concurrency: c=1: 10 samples, c=5: 20 samples, c=10: 20 samples, c=50: 50 samples per size; 300 probes total. 5 s pause between rounds. Cells added in later runs are merged by taking the latest raw file per (size, concurrency).
- After each cold probe: one warm re-invoke (same session), then `StopRuntimeSession` (best-effort). Runtimes use `idleRuntimeSessionTimeout=60`.
- Pad layers are `/dev/urandom` (incompressible) in ≤500 MB chunks, so ECR compressed size ≈ uncompressed and pull cost matches the nominal size.

## Caveats

- **2048 MB platform cap:** AgentCore rejects images ≥2048 MB (Service Quotas: "Maximum size for a Docker image in an AgentCore Runtime"), so the "2GB" variant is 1,950 MB.
- **Pre-warmed pool size is opaque** and may vary by runtime, account, region, and time; the fresh-boot share at a given concurrency is not a contract. Cells were also measured at different times (c=5 was added in a later run), so pool state differs between cells.
- Fleet-side image caching may make steady-state boots faster than the very first boots after a deploy; boot times here were stable across rounds, but this is a single-day, single-region snapshot, not an SLA.
- Outlier: one 1gb c=50 boot took 39.6 s (single occurrence in the matrix).

## Findings

1. **Image size drives genuine cold-boot latency**: p50 8,121 ms (500mb) → 11,428 ms (1gb) → 13,503 ms (2gb) — consistent with image pull dominating boot time (adding ~0.5 GB compressed costs roughly 2–4 s).
2. **Concurrency does not slow individual boots much** (fresh-boot p50 is stable across c levels per size); it determines how many requests miss the pre-warmed pool: c=1: 0%; c=5: 25–30%; c=10: 55–60%; c=50: 82–86%.
3. **Throttling was a non-issue** (1 throttle(s) in 300 probes; quota is 200 req/s per agent).
4. **Warm requests are flat ~70–100 ms** across all sizes — once a microVM is up, image size is irrelevant.
5. Practical guidance: keep production images small (every ~500 MB compressed ≈ +2–4 s cold boot), and expect the pre-warmed pool to hide cold starts only for low-concurrency bursts (≲5 simultaneous new sessions per runtime).
