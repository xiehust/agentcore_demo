# Lambda MicroVMs cold-start analysis notes

[中文版 / Chinese version](MICROVM_NOTES.zh.md)

Hand-written interpretation of [COMPARE.md](COMPARE.md) (which is script-generated and
therefore stays numerically consistent with the data). Three topics: the default Lambda
MicroVMs quotas, why its cold start does not depend on image size, and the "fast / slow"
bimodal pattern in the data. Data: 300 `RunMicrovm` launches on 2026-09-04 in us-west-2
(3 image sizes × concurrency 1/5/10/50), client on an EC2 instance in the same region.
Inferences that are not documented facts are marked as such.

---

## 1. Default quotas

Source: [Lambda quotas → Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html#microvms-quotas),
cross-checked with `aws service-quotas list-aws-default-service-quotas --service-code lambda --region us-west-2` (identical).

### Compute and storage

| Resource | Default | Adjustable |
|---|---|---|
| Memory across all MicroVMs (per account, per Region) | **400 GB**; **1,024 GB** in us-east-1 / us-east-2 / us-west-2 / ap-northeast-1 (512 VMs at 2 GB). Burstable to 4× | Yes |
| Maximum execution duration per MicroVM | 8 h (28,800 s) | No |

### Images and versions

| Resource | Default | Adjustable |
|---|---|---|
| MicroVM images per account per Region | 100 | Yes |
| Versions per MicroVM image | 50 | Yes |
| Concurrent image builds | 5; 10 in the four regions above | Yes |

### Per-MicroVM throughput (not adjustable)

- Concurrent connections: 8 (1 vCPU) / 16 (2) / 32 (4) / 64 (8) / 128 (16 vCPU)
- Requests per second: 40 (4 vCPU / 8 GB), 160 (16 vCPU / 32 GB)

### API rate limits (per account, per Region, all adjustable)

| API | Rate (TPS) | Burst |
|---|---|---|
| `RunMicrovm` | 5 | 5 |
| `ResumeMicrovm` | 5 | 5 |
| `SuspendMicrovm` | 2 | 2 |
| `TerminateMicrovm` | 10 | 10 |
| `GetMicrovm` | 100 | 100 |
| `CreateMicrovmAuthToken` | 50 | 50 |
| `CreateMicrovmShellAuthToken` | 5 | 5 |

### Where observation differs from the documentation

- 10 simultaneous `RunMicrovm` calls (60 per size × 3 sizes = 150) were **never throttled**.
- 50 simultaneous calls let **37–45 of 50** through, the rest `ThrottlingException`; the accepted calls' API latency rose from ~120 ms to p50 ~0.9 s (server-side queuing).
- Two c=50 bursts only 5 s apart: the second was throttled 44/50, the third 49/50 — the **5 TPS sustained rate is real**; the token bucket is simply much larger than the documented "burst 5".
- The docs add that new accounts get lower Lambda function / MicroVM concurrency and memory quotas, raised automatically with usage.

Impact on the benchmark: c≤10 cells are quota-free; c=50 `cold_ms` includes API queuing, and each size must be run separately with enough time for the bucket to refill.

---

## 2. Why cold start does not depend on image size

### 2.1 The two platforms do different work at "cold start"

| | AgentCore Runtime | Lambda MicroVMs |
|---|---|---|
| What happens | **Pull the whole container image from ECR** (download compressed layers + extract + write) → start container → Python imports the SDK → uvicorn listens | Restore the VM from the **memory + disk Firecracker snapshot** taken at build time; the process is already listening on 8080 |
| What image size affects | Bytes that must be pulled/extracted, linear: **≈ +2–4 s per +500 MB** | Only the snapshot's footprint in storage, not what must be read at start |
| Data | genuine boot p50 **8.1 / 11.4 / 13.5 s** (500 MB / 1 GB / 2 GB) | **2.2 / 2.1 / 2.3 s** |

The OCI image model forces AgentCore to move the whole image to the host before a container can start (layers are tar.gz; no filesystem until extracted).
A MicroVM "image" is the memory pages + block device Lambda snapshotted after running the Dockerfile, starting the app and receiving 200 from `/ready` —
every import, pip package and uvicorn's listening socket is already in memory. `proc_start_ts` predating the VM by 78 s (= the image build time) is direct evidence.

### 2.2 The snapshot is loaded lazily, page by page; untouched bytes cost nothing at start

The docs describe the `/validate` hook as letting Lambda "sample the portions of the snapshot that are used when your application runs, allowing Lambda to **prefetch** those portions to reduce latency"
([MicroVM images → build hooks](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)).
Being able to prefetch *a part* implies the rest is fetched on page fault — the Firecracker snapshot + lazy-load approach Lambda SnapStart also uses.

Our pad files `/opt/pad_1.bin … pad_4.bin` (257 MB → 1,707 MB) are **never read** while the app runs; they are cold data on the block device and generate no I/O at start. In the data:

- guest-side `/run` hook → first request arrival is **tens of ms** for all three sizes (fast-mode p50 66 ms);
- nothing in the cold-start decomposition trends with size; the slow-mode share is 41% / 24% / 32% with no monotonic pattern.

### 2.3 So where do the 1.5 s / 3.5 s go?

Into the **control plane and the ingress proxy**: `RunMicrovm` ~120 ms, `CreateMicrovmAuthToken` ~150 ms, then the proxy holds the first HTTPS request ~1.0 s before delivering it (fast mode),
or holds ~3 s, answers 502, and the retry succeeds (slow mode). These are platform scheduling/routing behaviours, independent of how many GB the image holds. See section 3.

### 2.4 When image size *does* matter again

The conclusion has preconditions:

- **The working set of the start path** is the real variable. If the first request reads a 1.5 GB model file, those pages are faulted in on that request and cold start grows with the working set —
  measured in "bytes touched at runtime", not "image size". Running a realistic payload inside `/validate` so Lambda prefetches those pages is the documented mitigation (this repo's `/validate` does exactly that).
- **Memory snapshot size** is whatever is resident at baseline. This process keeps a few tens of MB resident; loading large data into memory at build time enlarges the memory snapshot and the restore prefetch.
- **Build time** scales with image size (writing 1.7 GB of urandom), but that is one-off and not on the cold-start path.

In one sentence: AgentCore's cold start is "move the image here, then start" — cost ∝ image bytes; Lambda MicroVMs' cold start is "wake an already-running process from a snapshot on demand" —
cost ∝ pages actually touched by the start path, which for most agents is tens of MB, so 500 MB and 2 GB images are equally fast.

---

## 3. Fast mode and slow mode

A **bimodal distribution** in the 300 cold starts; "fast / slow" are labels coined here, not platform terms. Roughly 68% / 32%, independent of image size and concurrency.

### 3.1 Measurement flow

```
t0  RunMicrovm call                       (~120 ms; returns microvmId + endpoint, state=PENDING)
    CreateMicrovmAuthToken                (~150 ms)
    POST https://<endpoint>/invocations    ← attempt 1
    …if not 200, sleep 100 ms and retry…
    first 200 fully read                  → cold_ms = now − t0
```

The client records every HTTP attempt's duration (`attempt_ms`), non-200 statuses (`non_ok_statuses`) and two in-VM timestamps:
`run_hook_ts` (Lambda calls `/run` right after restoring the snapshot) and `request_ts` (the request reaches the app).

### 3.2 Fast mode (68%, cold_ms ≈ 1.3–1.5 s)

```
attempt_ms      = [~1,050]           one attempt, success
non_ok_statuses = {}
guest: request_ts − run_hook_ts ≈ 10–100 ms
```

The **ingress proxy holds the first connection for ~1 s**, then returns 200. Inside the VM, the request arrives only tens of ms after `/run`.
The VM is ready soon after the API returns; the ~1 s is spent in the proxy path (routing to the new VM / waiting for its network), not in the app.

### 3.3 Slow mode (32%, cold_ms ≈ 3.4–3.6 s)

```
attempt_ms      = [~3,020, ~25]      attempt 1 fails, attempt 2 succeeds at once
non_ok_statuses = {"502": 1}
guest: request_ts − run_hook_ts ≈ 1,800 ms
```

The proxy **holds the first request ~3 s and returns 502 Bad Gateway** (documented meaning: "application not responding"). The client retries 100 ms later and gets **200 in ~25 ms**.
Inside the VM, `/run` fired 1.8 s before the request arrived — the app had been listening the whole time; the proxy simply never delivered that first request and gave up at its timeout.

### 3.4 Side by side

| | Fast mode | Slow mode |
|---|---|---|
| Share | 68% | 32% (500 MB 41% / 1 GB 24% / 2 GB 32%, no size trend) |
| cold_ms p50 | ~1,540 ms | ~3,520 ms |
| HTTP attempts | 1, held ~1.07 s → 200 | #1 held ~3.02 s → 502; #2 ~25 ms → 200 |
| In-VM `/run` → request | ~66 ms | ~1,760 ms (VM idle-waiting) |
| Difference | — | ≈ 2 s = the extra proxy hold |

Raw records of the c=50 cells carry `attempt_ms` (the field was added to the client after the c≤10 runs), showing per-attempt durations for both modes.

### 3.5 Interpretation (inference, not an official explanation)

VM readiness time is about the same in both modes (within ~1 s of `RunMicrovm` returning); the difference is **whether the proxy's first connection to the freshly started VM succeeds**.
In the fast mode the proxy waits until the VM's network is up and forwards. In the slow mode it seems to forward before the VM is reachable, sits on a ~3 s timeout, returns 502 —
by which time the VM has long been ready, so the retry is instant. This matches the docs' "`GetMicrovm.state` is eventually consistent — determine readiness by connecting": a failed first connection is allowed by design.

The same pattern shows up on suspend → auto-resume: the 30 resume timings cluster at ~6.2 s and ~8.2 s, again ~2 s apart.

### 3.6 What it means for users

- Clients **must retry the first request on 502**, or about one launch in three fails outright; the retry costs ~25 ms.
- p50 reads 1.5 s, but p90 sits at ~3.4 s in every cell — budget for the slow mode.
- The 2 s gap is entirely in the platform's proxy layer; the application cannot optimise it. What you can do: set the first-request timeout above 3 s and retry fast,
  or have the `/run` hook report readiness outbound (a message / callback) instead of probing with the first request.
- If quota allows, pre-`RunMicrovm` VMs into your own pool — which is exactly what AgentCore's pre-warmed pool does for you; the difference between the platforms is essentially who maintains that pool.

---

## Reproduce

```bash
bash scripts/deploy_microvm.sh
uv run python microvm_coldstart_test.py --full --resume            # c=1,5,10
uv run python microvm_coldstart_test.py --full --concurrency 50 --sizes 500mb   # one size per run
python3 scripts/gen_compare_report.py
```

Per-attempt data: `attempt_ms` in `results/microvm/raw/*_c50.json`; in-VM timestamps: `run_hook_ts` / `request_ts` in every record.
