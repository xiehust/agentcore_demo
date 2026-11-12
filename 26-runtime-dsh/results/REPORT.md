# DeepSeek Harness (dsh) on AgentCore Runtime — cold-start benchmark results

- **Test date:** 2026-11-12 05:54–06:56 UTC · **Region:** us-east-1 · **Account:** …5045
- **Agent:** `dsh headless` (npm `@deepseek-ai/dsh` `0.2.2-rc.1`) behind a Node HTTP wrapper · **Model:** `deepseek.v3.2` (both from `deployments.json`)
- **Reported by the container during the test:** dsh `0.2.2-rc.1`, model `deepseek.v3.2`, image tag `dsh-0.2.2-rc.1`, `dsh-0.2.2-rc.1-pin`
- **Runtimes** (platform version · uncompressed docker / compressed ECR image · image · env):
  - `dsh_coldstart_base_v2` (`dsh_v2`) — platform `V2` — 1,054 MB / 279 MB — `agentcore-dsh-coldstart:dsh-0.2.2-rc.1` — env: `DSH_MODEL_ID=deepseek.v3.2`, `DSH_PREWARM=0`
  - `dsh_coldstart_prewarm_v2` (`dsh_prewarm_v2`) — platform `V2` — 1,054 MB / 279 MB — `agentcore-dsh-coldstart:dsh-0.2.2-rc.1` — env: `DSH_MODEL_ID=deepseek.v3.2`, `DSH_PREWARM=1`
  - `dsh_coldstart_diag_v2` (`dsh_pinctl_v2`) — platform `V2` — 1,089 MB / 291 MB — `agentcore-dsh-coldstart:dsh-0.2.2-rc.1-pin` — env: `DSH_MODEL_ID=deepseek.v3.2`, `DSH_PREWARM=0`
  - `dsh_coldstart_deepwarm_v2` (`dsh_pin_v2`) — platform `V2` — 1,089 MB / 291 MB — `agentcore-dsh-coldstart:dsh-0.2.2-rc.1-pin` — env: `DSH_MODEL_ID=deepseek.v3.2`, `DSH_PREWARM=3`
- **Requests:** 1600 sent on 800 sessions, 0 failed, 0 not sent (the second invoke of a session is skipped when its first one never reached the container; a first invoke is not sent when its client worker process failed before the release)
- **Scope:** platform V2 only. Also measured, and left out of this report on purpose: `dsh` (V1), `dsh_prewarm` (V1); their raw data is still in `results/raw/`.

## Summary

Two things are measured. **Cold start**: the first invoke of a new session only returns timestamps (`ping`), so it is the platform alone. **First message**: the first invoke of a new session is a real `dsh headless` turn, timed until the complete answer is back; `DSH boot` is the part of it from spawning `dsh` to its first output, before any model call. p50 in ms, number of samples in brackets, pooled over every concurrency level run (c=1, c=10, c=30). Later messages on the same session are not reported: they measure DSH and the model, not the start of a session.

| runtime | platform | variant | cold start (ping) | first message | of which DSH boot | cold requests ok / sent |
|---|---|---|---|---|---|---|
| `dsh_v2` | V2 | `DSH_PREWARM=0`, `dsh-0.2.2-rc.1` | snapshot restore 2,014 (100) | snapshot restore 6,437 (100) | snapshot restore 3,552 (100) | 200 / 200 |
| `dsh_prewarm_v2` | V2 | `DSH_PREWARM=1`, `dsh-0.2.2-rc.1` | snapshot restore 1,897 (100) | snapshot restore 6,441 (100) | snapshot restore 3,514 (100) | 200 / 200 |
| `dsh_pinctl_v2` | V2 | `DSH_PREWARM=0`, `dsh-0.2.2-rc.1-pin` | snapshot restore 2,050 (100) | snapshot restore 6,531 (100) | snapshot restore 3,691 (100) | 200 / 200 |
| `dsh_pin_v2` | V2 | `DSH_PREWARM=3`, `dsh-0.2.2-rc.1-pin` | snapshot restore 1,938 (100) | snapshot restore 4,678 (100) | snapshot restore 2,002 (100) | 200 / 200 |

- **`dsh_v2` (V2): the first message is slow until its snapshot has been used a few times.** Cold agent turn number 1 served by the same snapshot: p50 61,068 ms (n=2); turn 11+: p50 6,399 ms (n=86) (every raw file, including superseded runs and smoke probes).
- **`dsh_prewarm_v2` (V2): the first message is slow until its snapshot has been used a few times.** Cold agent turn number 1 served by the same snapshot: p50 65,246 ms (n=2); turn 11+: p50 6,313 ms (n=86) (every raw file, including superseded runs and smoke probes).
- **`dsh_pinctl_v2` (V2): the first message is slow until its snapshot has been used a few times.** Cold agent turn number 1 served by the same snapshot: p50 64,383 ms (n=2); turn 11+: p50 6,469 ms (n=80) (every raw file, including superseded runs and smoke probes).
- **`dsh_pin_v2` (V2): the first message is slow until its snapshot has been used a few times.** Cold agent turn number 1 served by the same snapshot: p50 15,202 ms (n=2); turn 11+: p50 4,645 ms (n=80) (every raw file, including superseded runs and smoke probes).
- **Page pin (`dsh_pin_v2` against its control `dsh_pinctl_v2`, same image):** first message p50 6,531 ms (n=100) without it and p50 4,678 ms (n=100) with it; DSH boot p50 3,691 ms (n=100) without it and p50 2,002 ms (n=100) with it. The sample code is in the page pin section.
- **Local Docker floor:** container start + first answer p50 4,413 ms (n=5), with a warm host page cache and no image pull.
- **Reliability:** 0 failed out of 1600 requests sent; sessions stopped after measurement: 800/800.

## Cold start and first message by concurrency (end-to-end, ms)

The first invoke of a new session, all successful samples. `ping` rows are the cold start (the platform alone); `agent` rows are the first message (a real `dsh headless` turn as that first invoke). Each cell is p50 / p90 / p99 / max (number of samples).

| runtime · sequence | c=1 | c=10 | c=30 |
|---|---|---|---|
| **dsh_v2 · ping** | 2,014 / 2,587 / 2,923 / 2,960 (10) | 1,995 / 2,769 / 3,171 / 3,181 (30) | 2,027 / 2,398 / 4,828 / 5,333 (60) |
| **dsh_v2 · agent** | 6,739 / 22,072 / 35,672 / 37,183 (10) | 6,535 / 10,115 / 15,760 / 16,783 (30) | 6,391 / 9,966 / 13,463 / 13,470 (60) |
| **dsh_prewarm_v2 · ping** | 1,998 / 2,503 / 3,130 / 3,199 (10) | 1,907 / 2,253 / 2,769 / 2,928 (30) | 1,889 / 2,185 / 3,831 / 4,030 (60) |
| **dsh_prewarm_v2 · agent** | 6,631 / 16,937 / 23,526 / 24,258 (10) | 6,208 / 7,070 / 7,781 / 7,803 (30) | 6,518 / 9,700 / 12,453 / 12,460 (60) |
| **dsh_pinctl_v2 · ping** | 2,003 / 2,819 / 3,597 / 3,684 (10) | 2,092 / 2,533 / 4,330 / 5,004 (30) | 2,034 / 2,408 / 5,808 / 5,866 (60) |
| **dsh_pinctl_v2 · agent** | 37,939 / 62,349 / 66,468 / 66,925 (10) | 6,832 / 13,846 / 20,239 / 21,080 (30) | 6,476 / 10,382 / 15,010 / 15,070 (60) |
| **dsh_pin_v2 · ping** | 2,011 / 2,477 / 2,666 / 2,687 (10) | 1,819 / 2,175 / 3,714 / 4,241 (30) | 1,953 / 2,567 / 3,869 / 4,077 (60) |
| **dsh_pin_v2 · agent** | 4,971 / 18,368 / 23,669 / 24,258 (10) | 4,799 / 7,935 / 11,462 / 12,512 (30) | 4,655 / 9,966 / 10,682 / 10,799 (60) |

## What the first message is made of (p50, ms)

`platform part` = end-to-end − time inside the wrapper's request handler: starting or restoring the session, routing and network. `DSH boot` = `dsh` spawn → first stdout byte, before any model call. The remainder of the turn is the model call and DSH finishing; it is not broken down here.

| cell | samples | first message end-to-end | platform part | DSH boot |
|---|---|---|---|---|
| dsh_v2 · c=1 · snapshot restore | 10 | 6,739 | 2,044 | 3,492 |
| dsh_v2 · c=10 · snapshot restore | 30 | 6,535 | 2,019 | 3,618 |
| dsh_v2 · c=30 · snapshot restore | 60 | 6,391 | 1,981 | 3,496 |
| dsh_prewarm_v2 · c=1 · snapshot restore | 10 | 6,631 | 1,963 | 3,539 |
| dsh_prewarm_v2 · c=10 · snapshot restore | 30 | 6,208 | 1,828 | 3,564 |
| dsh_prewarm_v2 · c=30 · snapshot restore | 60 | 6,518 | 1,869 | 3,510 |
| dsh_pinctl_v2 · c=1 · snapshot restore | 10 | 37,939 | 2,224 | 33,136 |
| dsh_pinctl_v2 · c=10 · snapshot restore | 30 | 6,832 | 1,962 | 3,834 |
| dsh_pinctl_v2 · c=30 · snapshot restore | 60 | 6,476 | 2,040 | 3,492 |
| dsh_pin_v2 · c=1 · snapshot restore | 10 | 4,971 | 1,898 | 1,968 |
| dsh_pin_v2 · c=10 · snapshot restore | 30 | 4,799 | 1,913 | 2,032 |
| dsh_pin_v2 · c=30 · snapshot restore | 60 | 4,655 | 1,909 | 1,999 |

## Variant comparison

Runtime variants side by side, only within one platform version (their `env` is listed at the top). Cells are p50 ms (number of samples), pooled over every concurrency level run (c=1, c=10, c=30).

### Platform V2

| metric | `dsh_v2` | `dsh_prewarm_v2` | `dsh_pinctl_v2` | `dsh_pin_v2` |
|---|---|---|---|---|
| cold start (ping) · snapshot restore | 2,014 (100) | 1,897 (100) | 2,050 (100) | 1,938 (100) |
| first message · snapshot restore | 6,437 (100) | 6,441 (100) | 6,531 (100) | 4,678 (100) |
| first message · DSH boot · snapshot restore | 3,552 (100) | 3,514 (100) | 3,691 (100) | 2,002 (100) |

## Platform V2: every new session is a snapshot restore

On `dsh_pin_v2`, `dsh_pinctl_v2`, `dsh_prewarm_v2`, `dsh_v2` a new session does not start the container. Sessions report a wrapper process that started long before the request, and many sessions share the same process start while each still sees its first invocation (`invocation_seq` = 1), so they are copies restored from a snapshot: every cold probe here is a *snapshot restore*. The table shows the evidence: a handful of process starts answered all the cold probes.

| runtime | platform | cold probes answered | distinct wrapper process starts | wrapper process age at the request, p50 (s) |
|---|---|---|---|---|
| `dsh_v2` | V2 | 200 | 2 | 2,301 |
| `dsh_prewarm_v2` | V2 | 200 | 2 | 2,339 |
| `dsh_pinctl_v2` | V2 | 200 | 2 | 1,838 |
| `dsh_pin_v2` | V2 | 200 | 2 | 1,883 |

### `dsh_v2` (V2)

All concurrency levels pooled (c=1, c=10, c=30). Each cell is p50 / p90 / max (number of samples), end-to-end ms unless the row names a part. `DSH boot` = `dsh` spawn → first stdout byte; `platform part` = end-to-end − time inside the wrapper's request handler.

| metric | `dsh_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) | 2,014 / 2,536 / 5,333 (100) |
| first message | 6,437 / 10,277 / 37,183 (100) |
| first message · DSH boot | 3,552 / 4,039 / 33,186 (100) |
| first message · platform part | 1,991 / 2,508 / 5,709 (100) |

The same figures by concurrency level.

| metric · concurrency | `dsh_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) · c=1 | 2,014 / 2,587 / 2,960 (10) |
| cold start (ping) · c=10 | 1,995 / 2,769 / 3,181 (30) |
| cold start (ping) · c=30 | 2,027 / 2,398 / 5,333 (60) |
| first message · c=1 | 6,739 / 22,072 / 37,183 (10) |
| first message · c=10 | 6,535 / 10,115 / 16,783 (30) |
| first message · c=30 | 6,391 / 9,966 / 13,470 (60) |
| first message · DSH boot · c=1 | 3,492 / 18,458 / 33,186 (10) |
| first message · DSH boot · c=10 | 3,618 / 3,953 / 4,307 (30) |
| first message · DSH boot · c=30 | 3,496 / 3,938 / 4,160 (60) |

### `dsh_prewarm_v2` (V2)

All concurrency levels pooled (c=1, c=10, c=30). Each cell is p50 / p90 / max (number of samples), end-to-end ms unless the row names a part. `DSH boot` = `dsh` spawn → first stdout byte; `platform part` = end-to-end − time inside the wrapper's request handler.

| metric | `dsh_prewarm_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) | 1,897 / 2,260 / 4,030 (100) |
| first message | 6,441 / 8,937 / 24,258 (100) |
| first message · DSH boot | 3,514 / 4,371 / 21,040 (100) |
| first message · platform part | 1,868 / 2,123 / 3,231 (100) |

The same figures by concurrency level.

| metric · concurrency | `dsh_prewarm_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) · c=1 | 1,998 / 2,503 / 3,199 (10) |
| cold start (ping) · c=10 | 1,907 / 2,253 / 2,928 (30) |
| cold start (ping) · c=30 | 1,889 / 2,185 / 4,030 (60) |
| first message · c=1 | 6,631 / 16,937 / 24,258 (10) |
| first message · c=10 | 6,208 / 7,070 / 7,803 (30) |
| first message · c=30 | 6,518 / 9,700 / 12,460 (60) |
| first message · DSH boot · c=1 | 3,539 / 12,996 / 21,040 (10) |
| first message · DSH boot · c=10 | 3,564 / 3,962 / 4,540 (30) |
| first message · DSH boot · c=30 | 3,510 / 4,160 / 4,525 (60) |

### `dsh_pinctl_v2` (V2)

All concurrency levels pooled (c=1, c=10, c=30). Each cell is p50 / p90 / max (number of samples), end-to-end ms unless the row names a part. `DSH boot` = `dsh` spawn → first stdout byte; `platform part` = end-to-end − time inside the wrapper's request handler.

| metric | `dsh_pinctl_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) | 2,050 / 2,533 / 5,866 (100) |
| first message | 6,531 / 17,398 / 66,925 (100) |
| first message · DSH boot | 3,691 / 13,043 / 61,413 (100) |
| first message · platform part | 1,994 / 2,421 / 5,634 (100) |

The same figures by concurrency level.

| metric · concurrency | `dsh_pinctl_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) · c=1 | 2,003 / 2,819 / 3,684 (10) |
| cold start (ping) · c=10 | 2,092 / 2,533 / 5,004 (30) |
| cold start (ping) · c=30 | 2,034 / 2,408 / 5,866 (60) |
| first message · c=1 | 37,939 / 62,349 / 66,925 (10) |
| first message · c=10 | 6,832 / 13,846 / 21,080 (30) |
| first message · c=30 | 6,476 / 10,382 / 15,070 (60) |
| first message · DSH boot · c=1 | 33,136 / 57,558 / 61,413 (10) |
| first message · DSH boot · c=10 | 3,834 / 6,603 / 13,406 (30) |
| first message · DSH boot · c=30 | 3,492 / 4,316 / 4,721 (60) |

### `dsh_pin_v2` (V2)

All concurrency levels pooled (c=1, c=10, c=30). Each cell is p50 / p90 / max (number of samples), end-to-end ms unless the row names a part. `DSH boot` = `dsh` spawn → first stdout byte; `platform part` = end-to-end − time inside the wrapper's request handler.

| metric | `dsh_pin_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) | 1,938 / 2,426 / 4,241 (100) |
| first message | 4,678 / 9,966 / 24,258 (100) |
| first message · DSH boot | 2,002 / 2,645 / 9,539 (100) |
| first message · platform part | 1,913 / 2,275 / 7,900 (100) |

The same figures by concurrency level.

| metric · concurrency | `dsh_pin_v2` (V2) · snapshot restore |
|---|---|
| cold start (ping) · c=1 | 2,011 / 2,477 / 2,687 (10) |
| cold start (ping) · c=10 | 1,819 / 2,175 / 4,241 (30) |
| cold start (ping) · c=30 | 1,953 / 2,567 / 4,077 (60) |
| first message · c=1 | 4,971 / 18,368 / 24,258 (10) |
| first message · c=10 | 4,799 / 7,935 / 12,512 (30) |
| first message · c=30 | 4,655 / 9,966 / 10,799 (60) |
| first message · DSH boot · c=1 | 1,968 / 9,426 / 9,539 (10) |
| first message · DSH boot · c=10 | 2,032 / 2,656 / 2,697 (30) |
| first message · DSH boot · c=30 | 1,999 / 2,561 / 2,718 (60) |

## Platform V2: cold agent turns by order of use of the same snapshot

Sessions that report the same wrapper process start were restored from the same snapshot. `turn` numbers the cold agent turns one snapshot has answered, in send order, counted over every raw file on disk (superseded runs and smoke probes included; only probes in these files are known). Each cell is p50 / p90 / max (number of samples), ms.

| runtime | turn on the same snapshot | first message end-to-end | DSH boot | platform part |
|---|---|---|---|---|
| `dsh_v2` | 1 | 61,068 / 63,470 / 64,070 (2) | 52,434 / 54,273 / 54,733 (2) | 2,088 / 2,155 / 2,172 (2) |
| `dsh_v2` | 2 | 43,518 / 45,876 / 46,465 (2) | 39,806 / 41,939 / 42,472 (2) | 2,006 / 2,185 / 2,230 (2) |
| `dsh_v2` | 3 | 28,008 / 35,348 / 37,183 (2) | 24,317 / 31,412 / 33,186 (2) | 2,232 / 2,316 / 2,337 (2) |
| `dsh_v2` | 4 | 14,496 / 19,214 / 20,393 (2) | 10,919 / 15,641 / 16,822 (2) | 2,023 / 2,100 / 2,119 (2) |
| `dsh_v2` | 5 | 11,885 / 12,441 / 12,580 (2) | 8,307 / 8,971 / 9,137 (2) | 2,044 / 2,053 / 2,056 (2) |
| `dsh_v2` | 6–10 | 6,426 / 7,535 / 8,087 (10) | 3,219 / 3,780 / 3,832 (10) | 2,140 / 2,445 / 3,752 (10) |
| `dsh_v2` | 11+ | 6,399 / 10,022 / 16,783 (86) | 3,552 / 3,948 / 4,307 (86) | 1,977 / 2,528 / 5,709 (86) |
| `dsh_prewarm_v2` | 1 | 65,246 / 67,959 / 68,637 (2) | 53,061 / 53,598 / 53,732 (2) | 1,992 / 2,078 / 2,099 (2) |
| `dsh_prewarm_v2` | 2 | 48,069 / 51,636 / 52,528 (2) | 39,828 / 39,909 / 39,929 (2) | 1,870 / 1,889 / 1,894 (2) |
| `dsh_prewarm_v2` | 3 | 43,428 / 45,746 / 46,325 (2) | 23,336 / 26,966 / 27,874 (2) | 1,880 / 1,888 / 1,890 (2) |
| `dsh_prewarm_v2` | 4 | 20,191 / 23,444 / 24,258 (2) | 16,571 / 20,146 / 21,040 (2) | 1,938 / 2,025 / 2,047 (2) |
| `dsh_prewarm_v2` | 5 | 8,189 / 9,271 / 9,541 (2) | 4,396 / 4,970 / 5,113 (2) | 1,925 / 2,084 / 2,123 (2) |
| `dsh_prewarm_v2` | 6–10 | 6,631 / 7,139 / 7,803 (10) | 3,539 / 4,436 / 4,540 (10) | 1,952 / 2,031 / 2,088 (10) |
| `dsh_prewarm_v2` | 11+ | 6,313 / 8,532 / 12,460 (86) | 3,478 / 4,038 / 4,525 (86) | 1,866 / 2,128 / 3,231 (86) |
| `dsh_pinctl_v2` | 1 | 64,383 / 66,417 / 66,925 (2) | 59,272 / 60,985 / 61,413 (2) | 2,489 / 2,549 / 2,563 (2) |
| `dsh_pinctl_v2` | 2 | 50,750 / 54,562 / 55,515 (2) | 46,277 / 50,100 / 51,056 (2) | 2,171 / 2,193 / 2,199 (2) |
| `dsh_pinctl_v2` | 3 | 34,993 / 41,424 / 43,032 (2) | 31,023 / 37,214 / 38,762 (2) | 2,031 / 2,206 / 2,250 (2) |
| `dsh_pinctl_v2` | 4 | 27,594 / 31,795 / 32,845 (2) | 22,985 / 26,604 / 27,509 (2) | 2,075 / 2,237 / 2,277 (2) |
| `dsh_pinctl_v2` | 5 | 16,670 / 20,198 / 21,080 (2) | 9,470 / 12,619 / 13,406 (2) | 2,286 / 2,436 / 2,473 (2) |
| `dsh_pinctl_v2` | 6–10 | 10,044 / 17,398 / 18,181 (10) | 5,810 / 13,043 / 13,167 (10) | 2,024 / 2,643 / 4,126 (10) |
| `dsh_pinctl_v2` | 11+ | 6,469 / 10,184 / 14,968 (80) | 3,533 / 4,349 / 5,852 (80) | 1,973 / 2,336 / 5,634 (80) |
| `dsh_pin_v2` | 1 | 15,202 / 17,211 / 17,713 (2) | 9,339 / 9,499 / 9,539 (2) | 2,235 / 2,256 / 2,261 (2) |
| `dsh_pin_v2` | 2 | 14,485 / 22,303 / 24,258 (2) | 5,683 / 8,668 / 9,414 (2) | 2,064 / 2,284 / 2,339 (2) |
| `dsh_pin_v2` | 3 | 4,867 / 5,434 / 5,575 (2) | 2,300 / 2,610 / 2,687 (2) | 1,889 / 2,080 / 2,127 (2) |
| `dsh_pin_v2` | 4 | 5,123 / 5,579 / 5,693 (2) | 2,287 / 2,583 / 2,657 (2) | 2,193 / 2,342 / 2,379 (2) |
| `dsh_pin_v2` | 5 | 4,877 / 5,433 / 5,573 (2) | 2,266 / 2,566 / 2,641 (2) | 1,941 / 2,178 / 2,237 (2) |
| `dsh_pin_v2` | 6–10 | 5,542 / 8,577 / 12,512 (10) | 2,302 / 2,639 / 2,644 (10) | 2,034 / 2,456 / 2,574 (10) |
| `dsh_pin_v2` | 11+ | 4,645 / 8,317 / 10,799 (80) | 1,999 / 2,561 / 2,718 (80) | 1,880 / 2,239 / 7,900 (80) |

## Page pin on a snapshot-restore platform: effect and sample code

On a snapshot-restore platform a new session is a copy of the container as it was when it first reported healthy. Files that were only read into the page cache before that moment were not served from memory afterwards in this test: `dsh` started slowly until the same snapshot had been used several times (see the turn-order table). Page pin keeps those files resident: before the wrapper starts listening, a helper process maps every file a first turn reads and holds the mappings, so the pages belong to a live process when the snapshot is taken. That pages mapped this way survive into the snapshot is the working explanation behind this recipe, not something the platform documents; the measured effect below is consistent with it.

| metric | `dsh_pinctl_v2` (control) | `dsh_pin_v2` (page pin) |
|---|---|---|
| files pinned | — | 3,629 |
| memory pinned, MB | — | 147 |
| time to pin before listening, ms | — | 82–113 |
| cold start (ping), p50 / p90 / max | 2,050 / 2,533 / 5,866 (100) | 1,938 / 2,426 / 4,241 (100) |
| first message, p50 / p90 / max | 6,531 / 17,398 / 66,925 (100) | 4,678 / 9,966 / 24,258 (100) |
| first message · DSH boot, p50 / p90 / max | 3,691 / 13,043 / 61,413 (100) | 2,002 / 2,645 / 9,539 (100) |
| first message, turn 1 on the same snapshot, p50 / p90 / max | 64,383 / 66,417 / 66,925 (2) | 15,202 / 17,211 / 17,713 (2) |
| first message, turn 2 on the same snapshot, p50 / p90 / max | 50,750 / 54,562 / 55,515 (2) | 14,485 / 22,303 / 24,258 (2) |
| first message, turn 3 on the same snapshot, p50 / p90 / max | 34,993 / 41,424 / 43,032 (2) | 4,867 / 5,434 / 5,575 (2) |
| first message, turn 4 on the same snapshot, p50 / p90 / max | 27,594 / 31,795 / 32,845 (2) | 5,123 / 5,579 / 5,693 (2) |
| first message, turn 5 on the same snapshot, p50 / p90 / max | 16,670 / 20,198 / 21,080 (2) | 4,877 / 5,433 / 5,573 (2) |
| first message, turn 6–10 on the same snapshot, p50 / p90 / max | 10,044 / 17,398 / 18,181 (10) | 5,542 / 8,577 / 12,512 (10) |
| first message, turn 11+ on the same snapshot, p50 / p90 / max | 6,469 / 10,184 / 14,968 (80) | 4,645 / 8,317 / 10,799 (80) |

### Sample code: the pinner (`app/pinner.py`)

Copied from the repository when this report was generated. The manifest it reads (`app/pin-manifest.txt`, 3,630 files) is produced by `scripts/gen_pin_manifest.sh`, which traces one real first turn with strace inside the built image and lists the files in the order they are first opened; it depends on the base image and the DSH version, so regenerate it when either changes.

```python
"""Page pinner for AgentCore Runtime V2 (snapshot start).

V2 snapshots the container after its first healthy /ping, and the page cache is
dropped before the snapshot is taken. Files the first request needs are then
read cold from the on-demand snapshot disk. Pages that a live process has
mapped cannot be dropped, so this process maps every file in the manifest and
blocks; the pages go into the snapshot with it.

Protocol with the wrapper: print exactly one JSON line on stdout, then hold the
mappings until stdin closes or the process is killed.

Adapted from the page-pin recipe in agentcore-e-layer-best-practices (§6.4);
the byte cap is raised because DSH reads far more than that recipe's example.
"""
import json
import mmap
import os
import sys
import time

MANIFEST = sys.argv[1] if len(sys.argv) > 1 else "/opt/dsh/pin-manifest.txt"
DEADLINE_S = float(os.environ.get("PIN_DEADLINE_S", "60"))
MAX_BYTES = int(os.environ.get("PIN_MAX_BYTES", str(1024 << 20)))
CHUNK = 1 << 20


def pin(paths, deadline):
    maps, total, skipped, buf = [], 0, 0, bytearray(CHUNK)  # one reused buffer
    seen = set()
    for path in paths:
        if time.monotonic() >= deadline:
            skipped += 1
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            skipped += 1
            continue
        try:
            st = os.fstat(fd)
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            if st.st_size == 0 or total + st.st_size > MAX_BYTES:  # never pin part of a file
                skipped += 1
                continue
            off = 0
            while off < st.st_size:
                n = os.preadv(fd, [buf], off)  # read into the page cache first
                if n == 0:
                    break
                off += n
            maps.append(mmap.mmap(fd, off, prot=mmap.PROT_READ,
                                  flags=mmap.MAP_SHARED | mmap.MAP_POPULATE))
            seen.add(key)
            total += off
        except (OSError, ValueError):
            skipped += 1
        finally:
            os.close(fd)
    return maps, total, skipped


def main():
    started = time.monotonic()
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    except OSError:
        paths = []
    maps, total, skipped = pin(paths, started + DEADLINE_S)
    print(json.dumps({"files": len(maps), "bytes": total, "skipped": skipped, "listed": len(paths),
                      "ms": round((time.monotonic() - started) * 1000), "max_bytes": MAX_BYTES}), flush=True)
    sys.stdin.buffer.read()  # hold the mappings until the wrapper lets go


if __name__ == "__main__":
    main()
```

### Sample code: how the wrapper uses it (`app/server.mjs`, excerpt)

The pinner is started and awaited before the server listens, so the files are mapped before the container can report healthy, and it is released after the first agent turn. A failure to pin is logged and never stops the server.

```javascript
// Page pin (level 3). pinner.py reads and mmaps every file in the manifest,
// prints one JSON line, then blocks holding the mappings. Pages mapped by a
// live process survive the page-cache drop that precedes the V2 snapshot, so a
// restored session finds DSH's files in memory instead of reading them cold.
// Best effort: any failure is logged and the server starts anyway.
let pinner = null;
function startPinner() {
  prewarm.start_ts = now();
  return new Promise((resolve) => {
    let settled = false;
    let out = '';
    const finish = (detail) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      prewarm.detail = detail;
      prewarm.end_ts = now();
      resolve();
    };
    const timer = setTimeout(() => finish({ error: `no result within ${PIN_WAIT_MS} ms` }), PIN_WAIT_MS);
    try {
      pinner = spawn('python3', [path.join(APP_DIR, 'pinner.py'), PIN_MANIFEST], { stdio: ['pipe', 'pipe', 'inherit'] });
    } catch (err) {
      return finish({ error: `spawn failed: ${err.code || err.name}` });
    }
    pinner.on('error', (err) => { pinner = null; finish({ error: `spawn failed: ${err.code || err.name}` }); });
    pinner.on('close', (code) => { pinner = null; prewarm.exit_code = code; finish({ error: `pinner exited with ${code}` }); });
    pinner.stdout.on('data', (chunk) => {
      out += chunk.toString('utf8');
      const idx = out.indexOf('\n');
      if (idx < 0) return;
      try { finish(JSON.parse(out.slice(0, idx))); } catch { finish({ error: 'pinner printed something other than its result line' }); }
    });
  });
}
// Called after the first turn: the files are in the page cache now, so the
// mappings are no longer needed.
function releasePinner() {
  if (!pinner) return;
  pinner.kill('SIGKILL');
  pinner = null;
  prewarm.released_ts = now();
}
```

Where the two functions are called:

```javascript
releasePinner();
if (PREWARM_LEVEL === 3) await startPinner();
```

## Local Docker vs AgentCore Runtime

- **Local host:** aarch64, 8 CPUs, 61.7 GB RAM, kernel `6.12.113-138.209.amzn2023.aarch64`, Docker `25.0.18`
- **Local run:** image `agentcore-dsh-coldstart:dsh-0.2.2-rc.1` (1,054 MB), model `deepseek.v3.2`, dsh `0.2.2-rc.1`, tested 2026-11-12T04:48:03.745683+00:00, 5 rounds, host page cache: warm, DSH pre-warm enabled: false
- Locally, every round starts a brand-new container and sends it its first request. AgentCore figures pool every concurrency level run (c=1, c=10, c=30). Cells are p50 ms (number of samples).

| metric | local Docker | `dsh_v2` (V2) · snapshot restore | `dsh_prewarm_v2` (V2) · snapshot restore | `dsh_pinctl_v2` (V2) · snapshot restore | `dsh_pin_v2` (V2) · snapshot restore |
|---|---|---|---|---|---|
| cold start (local: `docker run` → first healthy `/ping`; AgentCore: cold ping end-to-end) | 598 (5) | 2,014 (100) | 1,897 (100) | 2,050 (100) | 1,938 (100) |
| first message · DSH boot | 2,588 (5) | 3,552 (100) | 3,514 (100) | 3,691 (100) | 2,002 (100) |
| first message (local: container start + first agent request, per round; AgentCore: first message end-to-end) | 4,413 (5) | 6,437 (100) | 6,441 (100) | 6,531 (100) | 4,678 (100) |

Local Docker runs with a warm host page cache and no image pull, so it is a floor, not a like-for-like cold start.

## Success / errors

| cell | phase | sent | ok | failed | not sent | error types |
|---|---|---|---|---|---|---|
| dsh_v2 · ping · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_v2 · ping · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_v2 · ping · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_v2 · ping · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_v2 · ping · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_v2 · ping · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_v2 · agent · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_v2 · agent · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_v2 · agent · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_v2 · agent · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_v2 · agent · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_v2 · agent · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_prewarm_v2 · ping · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_prewarm_v2 · agent · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_pinctl_v2 · ping · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_pinctl_v2 · agent · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_pin_v2 · ping · c=30 | warm | 60 | 60 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=1 | cold | 10 | 10 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=1 | warm | 10 | 10 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=10 | cold | 30 | 30 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=10 | warm | 30 | 30 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=30 | cold | 60 | 60 | 0 | 0 | — |
| dsh_pin_v2 · agent · c=30 | warm | 60 | 60 | 0 | 0 | — |

0 of 1600 requests failed (non-200 response, unparseable body, or `agent.ok` not true); failed requests are counted here and excluded from every latency statistic. Sessions stopped after measurement: 800/800. Requests flagged as anomalous (first invoke not the first invocation of its microVM, or second invoke served by a different process): 0.

## Client-side overhead

How much of a number is the benchmark client. All probes of a round are released together, and each request's clock starts at its own `t0`, just before the SDK call. `t0 spread` is how far apart the cold requests of one round started (from their send timestamps) and `release → t0` is each probe's delay after the release: both are stagger before the clock starts, so they are **not** in any latency, but they mean the requests did not all arrive at the same instant. `t0 → before-send` (request serialized and signed, handed to the HTTP layer) and `new connection` (TCP + TLS setup) **are** inside the measured latency. Cold requests only, ms; a dash means the raw file predates these measurements.

| concurrency | client | cells | cold requests | t0 spread per round p50 / max | release → t0 p50 / p90 / max | t0 → before-send p50 / p90 / max | new connection p50 / p90 / max |
|---|---|---|---|---|---|---|---|
| c=1 | single process, 1 thread | 8 | 80 | — | — | — | — |
| c=10 | single process, 10 threads | 8 | 240 | 8.4 / 12.1 | — | — | — |
| c=30 | single process, 30 threads | 8 | 480 | 26.9 / 33.5 | — | — | — |

## Smoke probes (single invokes outside the matrix)

The first invoke of each smoke session, and any smoke request that failed.

| sent (UTC) | runtime | sequence | phase | ok | end-to-end ms | boot kind | DSH boot ms | error |
|---|---|---|---|---|---|---|---|---|
| 2026-11-12T05:42:17 | dsh_v2 | ping | cold | yes | 1,974 | snapshot_restore | — | — |
| 2026-11-12T05:42:19 | dsh_v2 | agent | cold | yes | 58,065 | snapshot_restore | 50,135 | — |
| 2026-11-12T05:43:21 | dsh_prewarm_v2 | ping | cold | yes | 1,795 | snapshot_restore | — | — |
| 2026-11-12T05:43:23 | dsh_prewarm_v2 | agent | cold | yes | 68,637 | snapshot_restore | 52,390 | — |
| 2026-11-12T05:44:32 | dsh_prewarm_v2 | agent | warm | no | 10,616 | snapshot_restore | — | http_error 424 |

## Methodology

- Cold start = client-side wall time of `InvokeAgentRuntime` with a **fresh 40-char `runtimeSessionId`** (a new session in AgentCore's session model), from the API call to the full response-body read; time to response headers is recorded separately. boto3, SigV4, us-east-1.
- botocore retries **disabled** (`total_max_attempts=1`): throttles and errors are counted, never retried into the latency distribution. Read timeout 300 s.
- Concurrency-N: N probes released together. In one process, N threads wait on a `threading.Barrier`. With worker processes (spawn start method), each process builds its own client and resolves its credentials before the release, runs its share of the probes in its own thread pool, and one cross-process gate releases them all; no connection to the runtime is opened before the release. Client per concurrency level (number of cells): c=1: single process, 1 thread (8); c=10: single process, 10 threads (8); c=30: single process, 30 threads (8).
- Rounds completed/planned per cell: dsh_v2 ping c=1: 10/10; dsh_v2 ping c=10: 3/3; dsh_v2 ping c=30: 2/2; dsh_v2 agent c=1: 10/10; dsh_v2 agent c=10: 3/3; dsh_v2 agent c=30: 2/2; dsh_prewarm_v2 ping c=1: 10/10; dsh_prewarm_v2 ping c=10: 3/3; dsh_prewarm_v2 ping c=30: 2/2; dsh_prewarm_v2 agent c=1: 10/10; dsh_prewarm_v2 agent c=10: 3/3; dsh_prewarm_v2 agent c=30: 2/2; dsh_pinctl_v2 ping c=1: 10/10; dsh_pinctl_v2 ping c=10: 3/3; dsh_pinctl_v2 ping c=30: 2/2; dsh_pinctl_v2 agent c=1: 10/10; dsh_pinctl_v2 agent c=10: 3/3; dsh_pinctl_v2 agent c=30: 2/2; dsh_pin_v2 ping c=1: 10/10; dsh_pin_v2 ping c=10: 3/3; dsh_pin_v2 ping c=30: 2/2; dsh_pin_v2 agent c=1: 10/10; dsh_pin_v2 agent c=10: 3/3; dsh_pin_v2 agent c=30: 2/2. Pause between rounds and between cells (seconds, every value used): 0, 5, 60. A later run replaces a cell with its latest raw file.
- After each first invoke: one more invoke on the same session, then `StopRuntimeSession` (best effort). The second invoke only confirms that the session stays on the same process (it is in the success and error counts); its latency is not reported, because it measures DSH and the model, not a session start.
- Boot kind comes from the timestamps the container returns and from each runtime's `platform_version` in `deployments.json`, joined when this report is generated.
- Agent prompt: `Reply with exactly: pong`.

## Caveats

- **Snapshot-restore latency may depend on how often the same snapshot was already used**: read the turn-order and time-ordered tables before quoting a single figure. The other tables use only the latest raw file per cell, so they describe the snapshots as they were during that run, and those runtimes were tested at a different time than the others.
- **The first message includes one model call**, which varies with model load; the benchmark prompt is very short, so this is a lower bound for real turns. Cold start (ping), the platform part and DSH boot contain no model time.
- Boot-kind classification trusts the microVM wall clock; a clock step during boot would distort it.
- With few samples per cell, p90/p99 are interpolated and sit close to the max. These are measurements from one region at one point in time, not an SLA.

## Detailed findings

- **`dsh_v2` (V2) · cold start (ping, c=1):** snapshot restore p50 2,014 ms (n=10).
- **`dsh_v2` (V2) · first message (c=1):** snapshot restore p50 6,739 ms (n=10).
- **`dsh_v2` (V2) · inside the first message (snapshot restore, c=1):** platform part p50 2,044 ms (n=10); DSH boot p50 3,492 ms (n=10).
- **`dsh_v2` (V2) · snapshot restore across every concurrency level (c=1, c=10, c=30), end-to-end:** ping p50 2,014 ms, max 5,333 ms (n=100); agent p50 6,437 ms, max 37,183 ms (n=100).
- **`dsh_prewarm_v2` (V2) · cold start (ping, c=1):** snapshot restore p50 1,998 ms (n=10).
- **`dsh_prewarm_v2` (V2) · first message (c=1):** snapshot restore p50 6,631 ms (n=10).
- **`dsh_prewarm_v2` (V2) · inside the first message (snapshot restore, c=1):** platform part p50 1,963 ms (n=10); DSH boot p50 3,539 ms (n=10).
- **`dsh_prewarm_v2` (V2) · snapshot restore across every concurrency level (c=1, c=10, c=30), end-to-end:** ping p50 1,897 ms, max 4,030 ms (n=100); agent p50 6,441 ms, max 24,258 ms (n=100).
- **`dsh_pinctl_v2` (V2) · cold start (ping, c=1):** snapshot restore p50 2,003 ms (n=10).
- **`dsh_pinctl_v2` (V2) · first message (c=1):** snapshot restore p50 37,939 ms (n=10).
- **`dsh_pinctl_v2` (V2) · inside the first message (snapshot restore, c=1):** platform part p50 2,224 ms (n=10); DSH boot p50 33,136 ms (n=10).
- **`dsh_pinctl_v2` (V2) · snapshot restore across every concurrency level (c=1, c=10, c=30), end-to-end:** ping p50 2,050 ms, max 5,866 ms (n=100); agent p50 6,531 ms, max 66,925 ms (n=100).
- **`dsh_pin_v2` (V2) · cold start (ping, c=1):** snapshot restore p50 2,011 ms (n=10).
- **`dsh_pin_v2` (V2) · first message (c=1):** snapshot restore p50 4,971 ms (n=10).
- **`dsh_pin_v2` (V2) · inside the first message (snapshot restore, c=1):** platform part p50 1,898 ms (n=10); DSH boot p50 1,968 ms (n=10).
- **`dsh_pin_v2` (V2) · snapshot restore across every concurrency level (c=1, c=10, c=30), end-to-end:** ping p50 1,938 ms, max 4,241 ms (n=100); agent p50 4,678 ms, max 24,258 ms (n=100).
- **`dsh_v2` (V2) · first message by order of use of the same snapshot (every raw file):** turn 1 p50 61,068 ms (n=2); turn 2 p50 43,518 ms (n=2); turn 3 p50 28,008 ms (n=2); turn 4 p50 14,496 ms (n=2); turn 5 p50 11,885 ms (n=2); turn 6–10 p50 6,426 ms (n=10); turn 11+ p50 6,399 ms (n=86).
- **`dsh_prewarm_v2` (V2) · first message by order of use of the same snapshot (every raw file):** turn 1 p50 65,246 ms (n=2); turn 2 p50 48,069 ms (n=2); turn 3 p50 43,428 ms (n=2); turn 4 p50 20,191 ms (n=2); turn 5 p50 8,189 ms (n=2); turn 6–10 p50 6,631 ms (n=10); turn 11+ p50 6,313 ms (n=86).
- **`dsh_pinctl_v2` (V2) · first message by order of use of the same snapshot (every raw file):** turn 1 p50 64,383 ms (n=2); turn 2 p50 50,750 ms (n=2); turn 3 p50 34,993 ms (n=2); turn 4 p50 27,594 ms (n=2); turn 5 p50 16,670 ms (n=2); turn 6–10 p50 10,044 ms (n=10); turn 11+ p50 6,469 ms (n=80).
- **`dsh_pin_v2` (V2) · first message by order of use of the same snapshot (every raw file):** turn 1 p50 15,202 ms (n=2); turn 2 p50 14,485 ms (n=2); turn 3 p50 4,867 ms (n=2); turn 4 p50 5,123 ms (n=2); turn 5 p50 4,877 ms (n=2); turn 6–10 p50 5,542 ms (n=10); turn 11+ p50 4,645 ms (n=80).
- **Client side at c=30 (single process, 30 threads):** t0 spread per round p50 / max 26.9 / 33.5 ms.
- **Reliability:** 0 failed out of 1600 requests sent.

## Appendix · Platform V2: every cold agent probe in time order

Every cold agent probe sent to a snapshot-restore runtime, oldest first, so a trend with runtime age is visible. Unlike the tables above (latest raw file per cell), this list also includes raw files that a later run superseded and the smoke probes. `wrapper start` is the process start time the container reported (it identifies the snapshot) and `turn` is the order defined in the turn-order section; the last column is the time between `deployed_at` in `deployments.json` and the probe.

| sent (UTC) | runtime | c | source | ok | end-to-end ms | DSH boot ms | wrapper start (UTC) | turn | minutes since deployed_at |
|---|---|---|---|---|---|---|---|---|---|
| 2026-11-12T05:42:19 | dsh_v2 | 1 | smoke | yes | 58,065 | 50,135 | 05:40:10 | 1 | 0.5 |
| 2026-11-12T05:43:23 | dsh_prewarm_v2 | 1 | smoke | yes | 68,637 | 52,390 | 05:40:09 | 1 | 1.5 |
| 2026-11-12T05:45:02 | dsh_v2 | 1 | superseded | yes | 46,465 | 42,472 | 05:40:10 | 2 | 3.2 |
| 2026-11-12T05:45:58 | dsh_v2 | 1 | superseded | yes | 64,070 | 54,733 | 05:40:06 | 1 | 4.1 |
| 2026-11-12T05:47:10 | dsh_v2 | 1 | superseded | yes | 40,570 | 37,141 | 05:40:06 | 2 | 5.3 |
| 2026-11-12T05:47:58 | dsh_v2 | 1 | superseded | yes | 18,833 | 15,448 | 05:40:06 | 3 | 6.1 |
| 2026-11-12T05:48:26 | dsh_v2 | 1 | superseded | yes | 8,600 | 5,016 | 05:40:06 | 4 | 6.6 |
| 2026-11-12T05:48:43 | dsh_prewarm_v2 | 1 | superseded | yes | 61,854 | 53,732 | 05:40:14 | 1 | 6.8 |
| 2026-11-12T05:49:54 | dsh_prewarm_v2 | 1 | superseded | yes | 52,528 | 39,728 | 05:40:09 | 2 | 8.0 |
| 2026-11-12T05:51:02 | dsh_prewarm_v2 | 1 | superseded | yes | 40,531 | 27,874 | 05:40:09 | 3 | 9.1 |
| 2026-11-12T05:51:57 | dsh_prewarm_v2 | 1 | superseded | yes | 43,609 | 39,929 | 05:40:14 | 2 | 10.0 |
| 2026-11-12T05:52:49 | dsh_prewarm_v2 | 1 | superseded | yes | 46,325 | 18,798 | 05:40:14 | 3 | 10.9 |
| 2026-11-12T05:55:45 | dsh_v2 | 1 | matrix | yes | 11,191 | 7,477 | 05:40:06 | 5 | 13.9 |
| 2026-11-12T05:56:15 | dsh_v2 | 1 | matrix | yes | 5,527 | 2,805 | 05:40:06 | 6 | 14.4 |
| 2026-11-12T05:56:29 | dsh_v2 | 1 | matrix | yes | 37,183 | 33,186 | 05:40:10 | 3 | 14.6 |
| 2026-11-12T05:57:16 | dsh_v2 | 1 | matrix | yes | 5,604 | 2,928 | 05:40:06 | 7 | 15.4 |
| 2026-11-12T05:57:33 | dsh_v2 | 1 | matrix | yes | 20,393 | 16,822 | 05:40:10 | 4 | 15.7 |
| 2026-11-12T05:58:03 | dsh_v2 | 1 | matrix | yes | 7,201 | 2,713 | 05:40:06 | 8 | 16.2 |
| 2026-11-12T05:58:18 | dsh_v2 | 1 | matrix | yes | 5,895 | 3,203 | 05:40:06 | 9 | 16.4 |
| 2026-11-12T05:58:32 | dsh_v2 | 1 | matrix | yes | 6,086 | 3,053 | 05:40:06 | 10 | 16.7 |
| 2026-11-12T05:58:46 | dsh_v2 | 1 | matrix | yes | 12,580 | 9,137 | 05:40:10 | 5 | 16.9 |
| 2026-11-12T05:59:10 | dsh_v2 | 1 | matrix | yes | 6,276 | 3,780 | 05:40:06 | 11 | 17.3 |
| 2026-11-12T06:00:41 | dsh_prewarm_v2 | 1 | matrix | yes | 16,124 | 12,102 | 05:40:14 | 4 | 18.7 |
| 2026-11-12T06:01:05 | dsh_prewarm_v2 | 1 | matrix | yes | 24,258 | 21,040 | 05:40:09 | 4 | 19.1 |
| 2026-11-12T06:01:38 | dsh_prewarm_v2 | 1 | matrix | yes | 9,541 | 5,113 | 05:40:09 | 5 | 19.7 |
| 2026-11-12T06:01:58 | dsh_prewarm_v2 | 1 | matrix | yes | 6,758 | 3,777 | 05:40:09 | 6 | 20.0 |
| 2026-11-12T06:02:13 | dsh_prewarm_v2 | 1 | matrix | yes | 5,470 | 2,895 | 05:40:09 | 7 | 20.3 |
| 2026-11-12T06:02:29 | dsh_prewarm_v2 | 1 | matrix | yes | 6,504 | 3,577 | 05:40:09 | 8 | 20.5 |
| 2026-11-12T06:02:44 | dsh_prewarm_v2 | 1 | matrix | yes | 7,066 | 3,501 | 05:40:09 | 9 | 20.8 |
| 2026-11-12T06:02:59 | dsh_prewarm_v2 | 1 | matrix | yes | 6,018 | 3,444 | 05:40:09 | 10 | 21.0 |
| 2026-11-12T06:03:13 | dsh_prewarm_v2 | 1 | matrix | yes | 5,335 | 2,600 | 05:40:09 | 11 | 21.3 |
| 2026-11-12T06:03:30 | dsh_prewarm_v2 | 1 | matrix | yes | 5,661 | 2,748 | 05:40:09 | 12 | 21.6 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,251 | 3,525 | 05:40:06 | 12 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 10,636 | 3,619 | 05:40:06 | 13 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 7,474 | 3,752 | 05:40:10 | 6 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,336 | 3,838 | 05:40:06 | 14 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,354 | 3,615 | 05:40:06 | 15 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,748 | 3,791 | 05:40:06 | 16 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 9,093 | 4,307 | 05:40:06 | 17 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,236 | 3,316 | 05:40:06 | 18 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 6,246 | 3,235 | 05:40:10 | 7 | 26.0 |
| 2026-11-12T06:07:49 | dsh_v2 | 10 | matrix | yes | 7,045 | 3,832 | 05:40:10 | 8 | 26.0 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,678 | 3,617 | 05:40:06 | 19 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 7,827 | 3,915 | 05:40:06 | 20 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,388 | 3,594 | 05:40:06 | 21 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,392 | 3,191 | 05:40:06 | 22 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,658 | 3,557 | 05:40:06 | 23 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,362 | 3,874 | 05:40:06 | 24 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 5,501 | 2,758 | 05:40:06 | 25 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,502 | 3,474 | 05:40:06 | 26 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 16,783 | 3,944 | 05:40:06 | 27 | 27.2 |
| 2026-11-12T06:09:03 | dsh_v2 | 10 | matrix | yes | 6,400 | 3,907 | 05:40:06 | 28 | 27.2 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 5,397 | 2,618 | 05:40:06 | 29 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 8,087 | 3,774 | 05:40:10 | 9 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 6,606 | 3,437 | 05:40:10 | 10 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 6,568 | 3,728 | 05:40:06 | 30 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 8,407 | 4,203 | 05:40:06 | 31 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 10,057 | 4,034 | 05:40:06 | 32 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 13,256 | 2,823 | 05:40:06 | 33 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 5,364 | 2,590 | 05:40:06 | 34 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 6,038 | 3,389 | 05:40:06 | 35 | 28.5 |
| 2026-11-12T06:10:23 | dsh_v2 | 10 | matrix | yes | 6,499 | 3,717 | 05:40:06 | 36 | 28.5 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,457 | 3,905 | 05:40:09 | 13 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,024 | 3,422 | 05:40:09 | 14 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,300 | 3,767 | 05:40:09 | 15 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 5,340 | 2,753 | 05:40:09 | 16 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,513 | 3,912 | 05:40:09 | 17 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,456 | 3,801 | 05:40:09 | 18 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,837 | 3,678 | 05:40:14 | 5 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 5,848 | 2,801 | 05:40:09 | 19 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 5,962 | 2,586 | 05:40:09 | 20 | 32.9 |
| 2026-11-12T06:14:48 | dsh_prewarm_v2 | 10 | matrix | yes | 6,454 | 3,798 | 05:40:09 | 21 | 32.9 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 5,865 | 2,585 | 05:40:09 | 22 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 7,031 | 4,425 | 05:40:14 | 6 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 7,803 | 4,540 | 05:40:14 | 7 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 7,727 | 3,434 | 05:40:09 | 23 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 5,386 | 3,008 | 05:40:09 | 24 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 6,079 | 3,552 | 05:40:09 | 25 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 5,653 | 2,757 | 05:40:09 | 26 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 6,227 | 3,575 | 05:40:09 | 27 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 5,982 | 3,180 | 05:40:14 | 8 | 34.0 |
| 2026-11-12T06:15:58 | dsh_prewarm_v2 | 10 | matrix | yes | 6,180 | 3,234 | 05:40:09 | 28 | 34.0 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 7,421 | 3,662 | 05:40:09 | 29 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 6,189 | 3,483 | 05:40:14 | 9 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 7,010 | 3,897 | 05:40:14 | 10 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 6,980 | 4,417 | 05:40:14 | 11 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 6,294 | 3,668 | 05:40:09 | 30 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 6,874 | 3,712 | 05:40:09 | 31 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 6,117 | 3,608 | 05:40:09 | 32 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 5,569 | 2,519 | 05:40:09 | 33 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 5,496 | 2,776 | 05:40:09 | 34 | 35.3 |
| 2026-11-12T06:17:13 | dsh_prewarm_v2 | 10 | matrix | yes | 5,889 | 2,862 | 05:40:09 | 35 | 35.3 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,180 | 2,759 | 05:40:06 | 37 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 7,866 | 3,595 | 05:40:06 | 38 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,277 | 2,833 | 05:40:06 | 39 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 7,991 | 3,535 | 05:40:06 | 40 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,630 | 3,304 | 05:40:10 | 11 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,354 | 3,901 | 05:40:06 | 41 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 7,046 | 3,840 | 05:40:10 | 12 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 10,238 | 3,773 | 05:40:06 | 42 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,606 | 2,996 | 05:40:06 | 43 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,398 | 3,307 | 05:40:06 | 44 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,874 | 3,678 | 05:40:10 | 13 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,528 | 3,041 | 05:40:06 | 45 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 9,961 | 3,836 | 05:40:06 | 46 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,663 | 3,575 | 05:40:10 | 14 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,004 | 2,797 | 05:40:06 | 47 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,731 | 3,799 | 05:40:10 | 15 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,242 | 2,535 | 05:40:06 | 48 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,134 | 3,194 | 05:40:10 | 16 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 8,254 | 4,120 | 05:40:06 | 49 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,305 | 3,868 | 05:40:06 | 50 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 7,558 | 2,596 | 05:40:06 | 51 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,499 | 3,067 | 05:40:06 | 52 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,247 | 2,678 | 05:40:06 | 53 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,318 | 3,775 | 05:40:06 | 54 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,211 | 2,635 | 05:40:06 | 55 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 8,012 | 3,580 | 05:40:06 | 56 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 6,472 | 3,952 | 05:40:06 | 57 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,240 | 2,629 | 05:40:06 | 58 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 7,840 | 3,526 | 05:40:06 | 59 | 36.7 |
| 2026-11-12T06:18:35 | dsh_v2 | 30 | matrix | yes | 5,226 | 2,847 | 05:40:06 | 60 | 36.7 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,673 | 3,729 | 05:40:06 | 61 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,924 | 3,899 | 05:40:10 | 17 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 13,470 | 3,817 | 05:40:06 | 62 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,418 | 2,840 | 05:40:06 | 63 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,117 | 3,356 | 05:40:10 | 18 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,928 | 4,097 | 05:40:10 | 19 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,413 | 2,625 | 05:40:06 | 64 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 7,057 | 4,160 | 05:40:10 | 20 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,654 | 2,984 | 05:40:06 | 65 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,945 | 3,893 | 05:40:10 | 21 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,417 | 2,760 | 05:40:06 | 66 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,792 | 3,170 | 05:40:06 | 67 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 13,222 | 4,084 | 05:40:06 | 68 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,785 | 3,680 | 05:40:10 | 22 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,558 | 2,874 | 05:40:06 | 69 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,792 | 3,485 | 05:40:06 | 70 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,291 | 2,870 | 05:40:06 | 71 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 10,028 | 3,936 | 05:40:06 | 72 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,258 | 3,741 | 05:40:06 | 73 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,733 | 3,210 | 05:40:06 | 74 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,572 | 3,054 | 05:40:06 | 75 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 7,024 | 3,765 | 05:40:10 | 23 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 7,542 | 3,508 | 05:40:10 | 24 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,385 | 3,919 | 05:40:06 | 76 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,260 | 2,604 | 05:40:06 | 77 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 5,427 | 3,032 | 05:40:06 | 78 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 13,458 | 3,973 | 05:40:10 | 25 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 10,015 | 3,181 | 05:40:06 | 79 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,710 | 3,548 | 05:40:06 | 80 | 36.9 |
| 2026-11-12T06:18:48 | dsh_v2 | 30 | matrix | yes | 6,402 | 3,459 | 05:40:06 | 81 | 36.9 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,079 | 3,398 | 05:40:14 | 12 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 12,448 | 3,530 | 05:40:09 | 36 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,188 | 3,700 | 05:40:09 | 37 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,596 | 3,357 | 05:40:14 | 13 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 7,875 | 3,428 | 05:40:14 | 14 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,214 | 3,657 | 05:40:09 | 38 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,001 | 3,537 | 05:40:09 | 39 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,859 | 3,411 | 05:40:09 | 40 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 10,207 | 3,902 | 05:40:14 | 15 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,343 | 2,740 | 05:40:09 | 41 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,697 | 3,288 | 05:40:09 | 42 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,492 | 3,964 | 05:40:09 | 43 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 10,128 | 4,394 | 05:40:14 | 16 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,096 | 3,060 | 05:40:09 | 44 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 7,543 | 3,515 | 05:40:09 | 45 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,991 | 3,448 | 05:40:09 | 46 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 9,656 | 2,512 | 05:40:09 | 47 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,382 | 2,673 | 05:40:09 | 48 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 7,771 | 3,214 | 05:40:09 | 49 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,757 | 3,622 | 05:40:09 | 50 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,920 | 3,179 | 05:40:09 | 51 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 7,376 | 3,839 | 05:40:14 | 17 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,499 | 3,837 | 05:40:09 | 52 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 10,827 | 3,412 | 05:40:09 | 53 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 5,943 | 3,571 | 05:40:09 | 54 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 8,870 | 3,697 | 05:40:09 | 55 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 6,886 | 4,007 | 05:40:14 | 18 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 12,460 | 3,284 | 05:40:14 | 19 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 10,089 | 3,256 | 05:40:09 | 56 | 37.3 |
| 2026-11-12T06:19:16 | dsh_prewarm_v2 | 30 | matrix | yes | 7,440 | 3,292 | 05:40:09 | 57 | 37.3 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,851 | 3,377 | 05:40:09 | 58 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 7,422 | 4,458 | 05:40:14 | 20 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 7,898 | 2,574 | 05:40:09 | 59 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,792 | 3,276 | 05:40:09 | 60 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,910 | 4,368 | 05:40:14 | 21 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,013 | 3,307 | 05:40:09 | 61 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,999 | 4,069 | 05:40:14 | 22 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,328 | 2,795 | 05:40:09 | 62 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,474 | 2,827 | 05:40:09 | 63 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,068 | 3,688 | 05:40:14 | 23 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,764 | 4,144 | 05:40:14 | 24 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,172 | 3,313 | 05:40:09 | 64 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 7,142 | 4,525 | 05:40:14 | 25 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,257 | 3,508 | 05:40:09 | 65 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,135 | 3,420 | 05:40:14 | 26 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,361 | 3,512 | 05:40:09 | 66 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,134 | 2,676 | 05:40:09 | 67 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,904 | 3,528 | 05:40:09 | 68 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 7,146 | 4,483 | 05:40:14 | 27 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,536 | 3,927 | 05:40:09 | 69 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,427 | 3,763 | 05:40:09 | 70 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 8,696 | 2,536 | 05:40:09 | 71 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,246 | 3,352 | 05:40:09 | 72 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 4,897 | 2,603 | 05:40:09 | 73 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 8,369 | 3,874 | 05:40:14 | 28 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,232 | 3,541 | 05:40:09 | 74 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,754 | 4,304 | 05:40:14 | 29 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 7,452 | 2,823 | 05:40:09 | 75 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 5,974 | 3,698 | 05:40:09 | 76 | 37.7 |
| 2026-11-12T06:19:37 | dsh_prewarm_v2 | 30 | matrix | yes | 6,326 | 3,938 | 05:40:09 | 77 | 37.7 |
| 2026-11-12T06:27:12 | dsh_pinctl_v2 | 1 | matrix | yes | 61,840 | 57,130 | 06:24:32 | 1 | 0.6 |
| 2026-11-12T06:28:22 | dsh_pinctl_v2 | 1 | matrix | yes | 66,925 | 61,413 | 06:24:36 | 1 | 1.8 |
| 2026-11-12T06:29:37 | dsh_pinctl_v2 | 1 | matrix | yes | 45,986 | 41,498 | 06:24:32 | 2 | 3.1 |
| 2026-11-12T06:30:32 | dsh_pinctl_v2 | 1 | matrix | yes | 26,954 | 23,284 | 06:24:32 | 3 | 4.0 |
| 2026-11-12T06:31:07 | dsh_pinctl_v2 | 1 | matrix | yes | 55,515 | 51,056 | 06:24:36 | 2 | 4.6 |
| 2026-11-12T06:32:11 | dsh_pinctl_v2 | 1 | matrix | yes | 43,032 | 38,762 | 06:24:36 | 3 | 5.6 |
| 2026-11-12T06:33:03 | dsh_pinctl_v2 | 1 | matrix | yes | 22,342 | 18,461 | 06:24:32 | 4 | 6.5 |
| 2026-11-12T06:33:33 | dsh_pinctl_v2 | 1 | matrix | yes | 12,261 | 5,534 | 06:24:32 | 5 | 7.0 |
| 2026-11-12T06:34:00 | dsh_pinctl_v2 | 1 | matrix | yes | 6,990 | 4,149 | 06:24:32 | 6 | 7.4 |
| 2026-11-12T06:34:14 | dsh_pinctl_v2 | 1 | matrix | yes | 32,845 | 27,509 | 06:24:36 | 4 | 7.7 |
| 2026-11-12T06:34:56 | dsh_pin_v2 | 1 | matrix | yes | 17,713 | 9,139 | 06:24:36 | 1 | 8.3 |
| 2026-11-12T06:35:29 | dsh_pin_v2 | 1 | matrix | yes | 4,712 | 1,952 | 06:24:36 | 2 | 8.8 |
| 2026-11-12T06:35:46 | dsh_pin_v2 | 1 | matrix | yes | 4,159 | 1,914 | 06:24:36 | 3 | 9.1 |
| 2026-11-12T06:35:58 | dsh_pin_v2 | 1 | matrix | yes | 4,553 | 1,917 | 06:24:36 | 4 | 9.3 |
| 2026-11-12T06:36:11 | dsh_pin_v2 | 1 | matrix | yes | 4,181 | 1,892 | 06:24:36 | 5 | 9.5 |
| 2026-11-12T06:36:23 | dsh_pin_v2 | 1 | matrix | yes | 12,692 | 9,539 | 06:24:41 | 1 | 9.7 |
| 2026-11-12T06:36:44 | dsh_pin_v2 | 1 | matrix | yes | 4,190 | 1,963 | 06:24:36 | 6 | 10.1 |
| 2026-11-12T06:36:57 | dsh_pin_v2 | 1 | matrix | yes | 7,971 | 1,972 | 06:24:36 | 7 | 10.3 |
| 2026-11-12T06:37:13 | dsh_pin_v2 | 1 | matrix | yes | 5,230 | 2,076 | 06:24:36 | 8 | 10.6 |
| 2026-11-12T06:37:26 | dsh_pin_v2 | 1 | matrix | yes | 24,258 | 9,414 | 06:24:41 | 2 | 10.8 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 9,771 | 5,767 | 06:24:32 | 7 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 9,755 | 5,889 | 06:24:32 | 8 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 21,080 | 13,406 | 06:24:36 | 5 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 17,311 | 13,029 | 06:24:36 | 6 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 10,317 | 5,853 | 06:24:32 | 9 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 10,346 | 5,867 | 06:24:32 | 10 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 9,652 | 5,815 | 06:24:32 | 11 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 18,181 | 13,167 | 06:24:36 | 7 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 10,782 | 5,852 | 06:24:32 | 12 | 17.9 |
| 2026-11-12T06:44:27 | dsh_pinctl_v2 | 10 | matrix | yes | 13,461 | 5,676 | 06:24:32 | 13 | 17.9 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 9,919 | 3,701 | 06:24:32 | 14 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 5,850 | 3,272 | 06:24:32 | 15 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,519 | 3,843 | 06:24:32 | 16 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 9,584 | 4,235 | 06:24:32 | 17 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,521 | 3,642 | 06:24:32 | 18 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,078 | 3,825 | 06:24:32 | 19 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,384 | 3,376 | 06:24:32 | 20 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,097 | 3,528 | 06:24:32 | 21 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,033 | 3,523 | 06:24:32 | 22 | 19.3 |
| 2026-11-12T06:45:52 | dsh_pinctl_v2 | 10 | matrix | yes | 6,312 | 3,614 | 06:24:32 | 23 | 19.3 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 6,548 | 3,745 | 06:24:32 | 24 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 6,269 | 3,560 | 06:24:32 | 25 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 6,341 | 3,894 | 06:24:32 | 26 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 7,116 | 4,292 | 06:24:36 | 8 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 9,721 | 3,312 | 06:24:32 | 27 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 5,624 | 2,730 | 06:24:32 | 28 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 5,528 | 2,679 | 06:24:32 | 29 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 6,520 | 3,460 | 06:24:32 | 30 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 9,729 | 4,538 | 06:24:36 | 9 | 20.5 |
| 2026-11-12T06:47:05 | dsh_pinctl_v2 | 10 | matrix | yes | 5,229 | 2,862 | 06:24:32 | 31 | 20.5 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,392 | 1,978 | 06:24:36 | 9 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,875 | 1,940 | 06:24:36 | 10 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 5,575 | 2,687 | 06:24:41 | 3 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 5,693 | 2,657 | 06:24:41 | 4 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 5,573 | 2,641 | 06:24:41 | 5 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,825 | 2,054 | 06:24:36 | 11 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,512 | 2,031 | 06:24:36 | 12 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,676 | 2,034 | 06:24:36 | 13 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,801 | 2,044 | 06:24:36 | 14 | 24.8 |
| 2026-11-12T06:51:29 | dsh_pin_v2 | 10 | matrix | yes | 4,797 | 2,076 | 06:24:36 | 15 | 24.8 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,646 | 2,013 | 06:24:36 | 16 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 5,567 | 2,644 | 06:24:41 | 6 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,085 | 1,923 | 06:24:36 | 17 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,499 | 1,956 | 06:24:36 | 18 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,615 | 1,970 | 06:24:36 | 19 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,604 | 1,963 | 06:24:36 | 20 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,231 | 1,967 | 06:24:36 | 21 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 5,691 | 2,626 | 06:24:41 | 7 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,576 | 1,915 | 06:24:36 | 22 | 26.0 |
| 2026-11-12T06:52:38 | dsh_pin_v2 | 10 | matrix | yes | 4,777 | 1,929 | 06:24:36 | 23 | 26.0 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 5,517 | 2,638 | 06:24:41 | 8 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 4,438 | 2,020 | 06:24:36 | 24 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 12,512 | 2,527 | 06:24:41 | 9 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 4,601 | 1,965 | 06:24:36 | 25 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 8,139 | 2,556 | 06:24:41 | 10 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 7,912 | 1,931 | 06:24:36 | 26 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 4,393 | 1,926 | 06:24:36 | 27 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 5,290 | 2,489 | 06:24:41 | 11 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 8,889 | 2,697 | 06:24:41 | 12 | 27.1 |
| 2026-11-12T06:53:47 | dsh_pin_v2 | 10 | matrix | yes | 7,464 | 2,656 | 06:24:41 | 13 | 27.1 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 14,968 | 3,477 | 06:24:32 | 32 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 15,070 | 4,183 | 06:24:36 | 10 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 14,140 | 2,841 | 06:24:32 | 33 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 4,957 | 2,763 | 06:24:32 | 34 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 10,254 | 4,366 | 06:24:36 | 11 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 7,480 | 2,589 | 06:24:32 | 35 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 7,087 | 3,821 | 06:24:36 | 12 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 6,511 | 3,886 | 06:24:32 | 36 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,238 | 2,611 | 06:24:32 | 37 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 6,398 | 3,766 | 06:24:32 | 38 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 10,175 | 2,516 | 06:24:32 | 39 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 6,105 | 3,772 | 06:24:32 | 40 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,307 | 2,552 | 06:24:32 | 41 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 6,478 | 3,954 | 06:24:32 | 42 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 10,176 | 3,662 | 06:24:32 | 43 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,700 | 3,289 | 06:24:32 | 44 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 6,084 | 3,538 | 06:24:32 | 45 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,304 | 2,840 | 06:24:32 | 46 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 7,371 | 4,347 | 06:24:36 | 13 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 11,531 | 3,645 | 06:24:32 | 47 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 4,693 | 2,598 | 06:24:32 | 48 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 9,837 | 2,768 | 06:24:32 | 49 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 9,180 | 4,551 | 06:24:36 | 14 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 9,260 | 4,488 | 06:24:36 | 15 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,491 | 2,547 | 06:24:32 | 50 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,352 | 2,792 | 06:24:32 | 51 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 7,580 | 4,560 | 06:24:36 | 16 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 7,634 | 4,219 | 06:24:36 | 17 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 5,249 | 2,884 | 06:24:32 | 52 | 28.8 |
| 2026-11-12T06:55:20 | dsh_pinctl_v2 | 30 | matrix | yes | 14,934 | 2,898 | 06:24:32 | 53 | 28.8 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 7,005 | 3,613 | 06:24:36 | 18 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,439 | 3,763 | 06:24:32 | 54 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 5,039 | 2,519 | 06:24:32 | 55 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 5,898 | 3,304 | 06:24:32 | 56 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,346 | 3,247 | 06:24:36 | 19 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 7,020 | 4,081 | 06:24:36 | 20 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 5,355 | 2,713 | 06:24:32 | 57 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,473 | 3,750 | 06:24:32 | 58 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,524 | 3,640 | 06:24:36 | 21 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 5,268 | 2,747 | 06:24:32 | 59 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,302 | 3,407 | 06:24:32 | 60 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,402 | 4,045 | 06:24:32 | 61 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,504 | 3,960 | 06:24:32 | 62 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 7,566 | 4,313 | 06:24:36 | 22 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,520 | 3,714 | 06:24:32 | 63 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 5,127 | 2,897 | 06:24:32 | 64 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 8,013 | 3,443 | 06:24:32 | 65 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,457 | 3,925 | 06:24:32 | 66 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,558 | 3,508 | 06:24:36 | 23 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,290 | 3,188 | 06:24:32 | 67 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,538 | 3,385 | 06:24:32 | 68 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,352 | 3,681 | 06:24:32 | 69 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,510 | 3,936 | 06:24:32 | 70 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 8,977 | 2,598 | 06:24:32 | 71 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,381 | 3,396 | 06:24:32 | 72 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 11,784 | 4,721 | 06:24:36 | 24 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,080 | 3,351 | 06:24:36 | 25 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,464 | 3,671 | 06:24:32 | 73 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,301 | 3,369 | 06:24:32 | 74 | 29.1 |
| 2026-11-12T06:55:42 | dsh_pinctl_v2 | 30 | matrix | yes | 6,357 | 2,645 | 06:24:32 | 75 | 29.1 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,594 | 2,026 | 06:24:36 | 28 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 5,670 | 2,619 | 06:24:41 | 14 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,392 | 1,918 | 06:24:36 | 29 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,683 | 2,015 | 06:24:36 | 30 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 5,382 | 2,560 | 06:24:41 | 15 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,549 | 2,004 | 06:24:36 | 31 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 10,799 | 2,718 | 06:24:41 | 16 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,724 | 1,988 | 06:24:36 | 32 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,425 | 2,001 | 06:24:36 | 33 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,527 | 1,908 | 06:24:36 | 34 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,377 | 1,937 | 06:24:36 | 35 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,062 | 1,884 | 06:24:36 | 36 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,527 | 2,019 | 06:24:36 | 37 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,682 | 2,002 | 06:24:36 | 38 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 10,579 | 1,983 | 06:24:36 | 39 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 5,505 | 2,559 | 06:24:41 | 17 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,687 | 2,066 | 06:24:36 | 40 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 10,572 | 2,172 | 06:24:36 | 41 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 6,814 | 2,665 | 06:24:41 | 18 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,132 | 1,950 | 06:24:36 | 42 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,917 | 2,035 | 06:24:36 | 43 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,360 | 1,980 | 06:24:36 | 44 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,665 | 2,052 | 06:24:36 | 45 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,389 | 2,005 | 06:24:36 | 46 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,642 | 1,932 | 06:24:36 | 47 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,385 | 1,910 | 06:24:36 | 48 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 10,601 | 1,923 | 06:24:36 | 49 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,678 | 2,044 | 06:24:36 | 50 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,263 | 1,983 | 06:24:36 | 51 | 29.5 |
| 2026-11-12T06:56:07 | dsh_pin_v2 | 30 | matrix | yes | 4,446 | 2,038 | 06:24:36 | 52 | 29.5 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 5,672 | 2,674 | 06:24:41 | 19 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,681 | 1,886 | 06:24:36 | 53 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 5,216 | 2,574 | 06:24:41 | 20 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,439 | 1,892 | 06:24:36 | 54 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,836 | 2,150 | 06:24:36 | 55 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,530 | 1,930 | 06:24:36 | 56 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,638 | 2,034 | 06:24:36 | 57 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,465 | 1,934 | 06:24:36 | 58 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 8,254 | 1,967 | 06:24:36 | 59 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,342 | 1,965 | 06:24:36 | 60 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 7,354 | 2,593 | 06:24:41 | 21 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,968 | 2,113 | 06:24:36 | 61 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,445 | 2,022 | 06:24:36 | 62 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,550 | 2,094 | 06:24:36 | 63 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,679 | 2,105 | 06:24:36 | 64 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,158 | 1,964 | 06:24:36 | 65 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,318 | 1,928 | 06:24:36 | 66 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 10,592 | 1,984 | 06:24:36 | 67 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,256 | 1,902 | 06:24:36 | 68 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,695 | 2,002 | 06:24:36 | 69 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,452 | 1,940 | 06:24:36 | 70 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,517 | 2,011 | 06:24:36 | 71 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,445 | 1,978 | 06:24:36 | 72 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,645 | 1,995 | 06:24:36 | 73 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 9,976 | 1,980 | 06:24:36 | 74 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,675 | 1,924 | 06:24:36 | 75 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,862 | 1,956 | 06:24:36 | 76 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 9,964 | 1,997 | 06:24:36 | 77 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,640 | 1,925 | 06:24:36 | 78 | 29.7 |
| 2026-11-12T06:56:21 | dsh_pin_v2 | 30 | matrix | yes | 4,738 | 2,005 | 06:24:36 | 79 | 29.7 |
