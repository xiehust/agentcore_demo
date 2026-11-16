# Lifecycle hook（build warmup）对 DSH 冷启动的影响

2026-11-14，us-east-1，平台 V2，5 个 runtime，同一镜像 `dsh-0.2.2-rc.1-pin`（差别只在环境变量和 hook 配置），
每个 runtime 3 个版本（3 次创建/更新）× 每版 smoke + 完整矩阵（并发 1 / 10 / 30），2,056 个请求，0 失败。
原始数据：`results/raw/*20261114T*.json`。文档：
[runtime-lifecycle-hooks](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-hooks.html)。

| key | runtime 名称 | 做法 |
|---|---|---|
| `dsh_pinctl_v2` | `dsh_coldstart_diag_v2` | 对照：不 pin、无 hook |
| `dsh_pin_v2` | `dsh_coldstart_deepwarm_v2` | page pin（监听前，`DSH_PREWARM=3`） |
| `dsh_hook_v2` | `dsh_coldstart_hook_v2` | 只用 hook：`disk` 调用逐个文件 `fadvise(DONTNEED)` 后冷读清单（`DSH_HOOK=read`） |
| `dsh_hookpin_v2` | `dsh_coldstart_hookpin_v2` | hook：`disk` 调用里冷读后 pin（`DSH_HOOK=pin`） |
| `dsh_pinhook_v2` | `dsh_coldstart_pinhook_v2` | 监听前 pin + `disk` 调用读清单（`DSH_PREWARM=3` + `DSH_HOOK=read`） |

## 结论

1. **hook 解决的是「新版本头几次使用」**：第 1 次使用的 DSH 启动 p50 从 47.6 秒降到 4.9 秒，比 page pin（7.3 秒）
   更好，且第 2–10 次也稳定在 3.5–4.0 秒，没有 pin 那样逐次下降的过程。
2. **hook 不改善稳定态**：同一快照用过 10 次之后，hook 的 DSH 启动 3.4 秒，与对照（3.3 秒）相同；
   只有 page pin 能到 2.0 秒（≈ 本地页缓存已热的 2.15 秒）。
3. **两者叠加拿不到两边的好处**：
   - `disk` 调用里 pin（`hookpin`）无效，与只用 hook 相同：服务 session 的快照取自 `memory` 调用之后，
     `disk` 调用约 90 秒后才在一个从快照恢复的副本上执行，它留在内存里的东西不会进入任何 session。
   - 监听前 pin + `disk` 读（`pinhook`）：`disk` 调用里的读全部命中被 pin 的页（3,629 个文件约 100 毫秒），
     第 1 次使用 6.7 秒，与 pin 相当；但稳定态 DSH 启动 3.4 秒，**pin 的稳定态收益消失了**，尽管恢复出来的
     session 里 pinner 仍持有 3,629 个文件的映射。原因未查明。
4. 选择：在意新版本上线后的头几个用户 → 用 hook（不锁定 147 MB 内存，也不用常驻 pinner 进程）；
   在意稳定态每条首消息再快约 1.4 秒 → 用 page pin。两者不要同时开。

## 数据

DSH 进程启动（spawn → 首个输出，不含模型），按「同一快照的第几次冷 agent 调用」分组，p50 毫秒（样本数）[最大值]：

| runtime | 第 1 次 | 第 2 次 | 第 3–5 次 | 第 6–10 次 | 第 11 次起 |
|---|---|---|---|---|---|
| 对照 | 47,582 (4) [48,388] | 32,200 (2) | 8,640 (6) [19,048] | 7,672 (10) | 3,316 (81) |
| pin | 7,330 (4) [8,502] | 6,240 (2) | 4,916 (6) | 2,268 (10) | **1,971** (81) |
| hook | **4,917** (4) [8,228] | 3,510 (2) | 3,685 (6) | 4,023 (10) | 3,398 (81) |
| hook 里 pin | 5,536 (4) [8,356] | 3,542 (2) | 4,391 (6) | 3,500 (10) | 3,670 (81) |
| pin + hook 读 | 6,655 (3) [6,924] | 4,236 (2) | 3,208 (6) | 4,050 (10) | 3,432 (81) |

第一条消息端到端（平台 + DSH 启动 + 一次模型调用），p50 毫秒：

| runtime | 第 1 次 | 第 2 次 | 第 3–5 次 | 第 6–10 次 | 第 11 次起（p50 / p90） |
|---|---|---|---|---|---|
| 对照 | 51,530 | 36,137 | 12,170 | 12,180 | 5,983 / 9,874 |
| pin | 11,275 | 9,052 | 8,440 | 5,147 | **4,594** / 5,785 |
| hook | **7,852** | 6,300 | 6,626 | 7,094 | 6,295 / 7,749 |
| hook 里 pin | 8,564 | 6,399 | 7,132 | 6,271 | 6,263 / 7,206 |
| pin + hook 读 | 9,780 | 6,905 | 6,006 | 7,621 | 5,746 / 6,354 |

冷启动 ping（只有平台）五组都在 1.8–1.9 秒，hook 不影响。

## 观察到的平台行为

- 每次创建/更新，平台起 **2 个**构建实例，各自走一遍 `/ping` → `memory` → `disk`，各有完整的 120 秒预算；
  session 也确实来自两个快照（`proc_start_ts` 相差约 5 秒）。表中的「第几次」按每个快照分别计数。
- 恢复出来的 session 里 `hook.memory_ts` 有值、`hook.disk_calls` 为 0：快照在 `memory` 之后、`disk` 之前。
- `disk` 调用在 `memory` 之后约 86–100 秒开始，读 147 MB 用 12–25 秒，说明它读的是冷的快照盘。
  AWS 的 [blog](https://aws.amazon.com/blogs/machine-learning/architecture-patterns-for-fast-session-startup-on-amazon-bedrock-agentcore-runtime/)（2026-11-13）证实了这两条：`memory` 调用在冻结之前，`disk` 调用在「a separate microVM
  that restores the snapshot to check it. Nothing from this call is snapshotted」，作用是决定恢复后的 session 先拉哪些块。
- 同一快照越用越快的原因，blog 的解释是镜像块第一次被读时从快照存储拉取，「then cached closer to the host」。

## 局限

- 第 1 次使用每组只有 3–4 个样本（3 个版本，部分版本的第二个快照的第 1 次落在矩阵里）。
- `disk` 调用读的是 strace 得到的清单（一次 `pong` 回合）；用到清单外文件的回合不受益。
  也可以在 `disk` 调用里直接跑一次真实回合，本次没测。
- pin + hook 下 pin 的稳定态收益为何消失，只观察到现象，没有定位原因。
