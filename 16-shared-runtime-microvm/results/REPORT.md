# AgentCore 默认 microVM 共享 Runtime Session 1–40 并发实测报告

- 报告日期：2026-08-15（UTC）
- 测试日期：2026-08-14 至 2026-08-15（UTC）
- AWS 账号：`434444145045`
- 区域：`us-west-2`
- 项目：`16-shared-runtime-microvm/`
- 模型：`us.anthropic.claude-sonnet-4-6`
- 测试对象：AgentCore 默认 Runtime 计算模式；未配置 Capacity Provider
- 云端部署和计费调用：**已实际执行**

## 结论摘要

本报告将全部有效数据按并发档统一呈现：短程覆盖 `2/4/8/12/16/24/32/40`，长程覆盖 `1/2/4/8/12/16/24/32/40`。所有数值均来自本目录中的真实 JSON，没有复用 `15-shared-runtime-instance/` 的 EC2 Capacity Provider 容量数据。

1. **应用层多用户隔离通过。** 三用户 smoke 共 26/26 项检查通过；alice、bob、carol 共用一个 `runtimeSessionId` 和一个完整服务进程指纹，但使用独立工作区与 Claude session。相对路径和绝对路径越权探测均实际触发 `PreToolUse` deny，`denied_count=1`，无 token 泄露。
2. **短程 2–40 全部成功。** 八个档位共 **138/138** 个请求通过精确 marker、独立工作区和单服务进程检查。40 并发 p50/p90/max 为 17.447/22.129/22.833 秒，CPU 平均/峰值 75.1%/100%，最低可用内存 2795 MB。
3. **长程 1–32 全部端到端成功。** 八个成功档共 **99/99** 个用户完成 198 个 Agent 阶段和 594 个最终文件验证。32 并发 p50/p90/max 为 324.4/556.5/633.0 秒，但最低可用内存只剩 1144 MB，cgroup current 峰值 6506 MB。
4. **长程 40 是明确失败档。** 40 个 foundation 请求都得到 API HTTP 200，但全部缺少最终 `complete` SSE event，只平均执行 7 次工具调用；没有请求返回 workspace、Claude session ID 或服务进程指纹，最终为 0/40。monitor 命令也退出 1，但 `StopRuntimeSession` 成功。
5. **40 的失败与内存压力导致进程丢失高度一致，但没有直接 OOM 证据。** 32 档只剩约 1.14 GB，若按并发趋势延伸到 40，会逼近或超过约 7.83 GiB 可见内存；然而 40 档失去 monitor，本目录没有可用于确认 OOM 或进程退出的 Runtime 日志证据，因此不能声称已确认内核 OOM kill。
6. **容量建议：短程常态 24–32，长程常态不超过 24。** 短程 40 已验证成功，但 CPU 饱和、load1 和尾延迟明显上升；长程 32 是低余量边界，不适合作为常态配置，40 不可用。
7. **同一 shared session 不适合互不信任的租户。** 用户共享 FastAPI 进程域、OS 用户和 Runtime 凭证。工作区、工具白名单与路径 hook 属于应用层控制，不是用户之间的 microVM/内核隔离。

## 1. 测试配置与证据映射

### 1.1 统一配置

| 项目 | 值 |
|---|---|
| Region | `us-west-2` |
| 模型 | `us.anthropic.claude-sonnet-4-6` |
| 网络 | `PUBLIC` |
| 可见逻辑 CPU | 2 |
| `/proc/meminfo` MemTotal | 8,209,720 kB，约 7.83 GiB |
| 最大测试槽位 | `MAX_PARALLEL_AGENTS=40` |
| `MAX_TURNS` | 64 |
| idle/max lifetime | 900 / 28800 秒 |
| 用户根目录 | `/tmp/agentcore-users` |
| Capacity Provider | 未配置 |
| 外部 filesystem | 未配置 |
| 镜像 tag | `launchpad-agents:shared-runtime-microvm-v1` |
| 最终保留镜像 digest | `sha256:ca626ff6df75493fbac6d1c7f47eaeca546051d55776fcfef22cc21251cb76c9` |

运行环境探测见 `results/runtime_probe_20260814.json`。`cpu.max` 为 `max 100000`，`memory.max` 和 `pids.max` 均为 `max`；因此 `/proc` 可见资源是实测环境观测值，不应解释为 AgentCore 服务承诺的固定配额。

### 1.2 Runtime revision 与数据文件

为保证原始数据可审计，以下列出并发档对应的 Runtime revision；正文中的延迟与资源表按并发档统一比较，不按执行时间拆表。

| Runtime ID | Version | `MAX_PARALLEL_AGENTS` | 对应数据 | 删除证据 |
|---|---:|---:|---|---|
| `shared_runtime_microvm-KkqELu3xji` | 3 | 8 | 隔离 smoke；短程 2/4/8；长程 1/2/4/8 | `runtime_cleanup_20260814.json` |
| `shared_runtime_microvm-oGddGiDWRD` | 1 | 40 | 短程 12/16/24/32/40；长程 12/16/24/32/40 | `runtime_cleanup_scale40_20260815.json` |

所有档位使用相同的应用代码、依赖、模型、prompt 和 verifier。重新推送相同镜像内容后，ECR tag 的 manifest digest 由 `sha256:64a5...96a4` 更新为 `sha256:ca62...76c9`；完整值保存在对应 metadata JSON 中。

## 2. 拓扑、隔离与测试方法

```text
user 1  ─┐  runtimeUserId=<user-1>                一个 active Runtime Session
user 2  ─┼─ InvokeAgentRuntime ─────────────────► 默认 AgentCore microVM
...      │  runtimeSessionId=<同一个随机 ID>       ├─ FastAPI PID 1
user N  ─┘                                         ├─ /tmp/agentcore-users/*
                                                   ├─ Claude 子进程 semaphore
operator ─ InvokeAgentRuntimeCommand ─────────────► ├─ /proc + cgroup v2 monitor
                                                   └─ 六文件 verifier
```

每个测试命令创建一个新的随机 shared session；同一命令可以包含一个或多个连续档位。每档通过 barrier 同时释放 N 个不同 `runtimeUserId`。服务端要求可信 header ID 与 payload `user_id` 一致，并把用户映射到哈希后缀的 `0700` 工作区。

应用层隔离包括：

- 每用户独立工作区、`HOME`、Claude session ID 和 `asyncio.Lock`；
- 不同用户并行执行，由全局 semaphore 控制 Claude 子进程上限；
- 只开放文件类工具，禁止 Bash、Web 和 Task 工具；
- `PreToolUse` hook 防止绝对路径、`..`、Glob 和 symlink 逃逸；
- Python function hooks 通过双向 `ClaudeSDKClient` 执行，不能退回不支持该控制协议的一次性 `query()`。

命令侧 monitor 每约 2 秒读取 `/proc/stat`、`/proc/meminfo`、`/proc/loadavg`、Agent 进程名和 cgroup v2。短程原始 session 共记录 56 个 monitor 样本，其中 52 个落入各档 level window；长程成功档原始 session 共记录 2064 个样本，其中 2058 个落入各档 level window。

命令 API 的 HTTP 200 本身不算成功。客户端还要求：返回同一个 session ID；存在有序且唯一的 `contentStart`/`contentDelta`/`contentStop`；没有 stream exception 或未知事件；`status=COMPLETED`；`exitCode=0`。所有 session 都在 `finally` 中调用 `StopRuntimeSession`。

## 3. 多用户隔离 smoke

原始数据：`results/multiuser_20260814T160656Z.json`

| 检查 | 结果 |
|---|---|
| 总检查数 | 26/26 通过 |
| warmup | 成功，14.216 秒 |
| command/app 同环境 | boot ID 和 hostname 一致 |
| 服务进程一致性 | 全程 9/9 指纹完整，distinct fingerprint=1 |
| 用户工作区 | 3 个用户，3 个独立工作区 |
| 初始写入/读回 | alice、bob、carol 全部成功 |
| Claude session | 三个初始 session ID 互不相同 |
| 相对路径越权 | 无泄露，`denied_count=1` |
| 绝对路径越权 | 无泄露，`denied_count=1` |
| 记忆召回 | 三个用户都只召回自己的 token |
| resume chain | 三个用户均恢复各自上一条 Claude session |
| 清理 | `StopRuntimeSession` HTTP 200，返回 ID 匹配 |

这证明了当前工具集合和应用路径控制下的隔离行为，不证明同一 session 内存在 OS 级多租户隔离。若开放 Bash、自定义 MCP 或其他可能绕过路径 hook 的工具，必须重新做威胁建模和越权测试。

## 4. 短程并发完整结果

短程请求要求每个用户精确返回自己的唯一 marker。全部档位的请求并发都不高于对应 Runtime 的 semaphore，因此表中表示真并行，而不是应用层排队后的请求总量。

### 4.1 成功率与延迟

| 并发 | 成功 | p50 | p90 | 最大延迟 | 独立工作区 | 服务进程数 |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 2/2 | 4.147 s | 4.747 s | 4.897 s | 2 | 1 |
| 4 | 4/4 | 4.596 s | 5.855 s | 6.392 s | 4 | 1 |
| 8 | 8/8 | 6.790 s | 7.410 s | 7.885 s | 8 | 1 |
| 12 | 12/12 | 7.118 s | 7.605 s | 8.033 s | 12 | 1 |
| 16 | 16/16 | 9.132 s | 9.676 s | 9.800 s | 16 | 1 |
| 24 | 24/24 | 12.521 s | 13.520 s | 14.627 s | 24 | 1 |
| 32 | 32/32 | 15.035 s | 19.367 s | 19.533 s | 32 | 1 |
| 40 | 40/40 | 17.447 s | 22.129 s | 22.833 s | 40 | 1 |

总计：**138/138 成功**。

### 4.2 资源窗口

| 并发 | 样本 | CPU 平均/峰值 | 内存已用峰值 | 最低可用内存 | load1 峰值 | cgroup current 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 3 | 14.1% / 31.1% | 736 MB | 7281 MB | 0.39 | 349 MB | 2 |
| 4 | 4 | 21.9% / 63.4% | 1060 MB | 6958 MB | 0.33 | 675 MB | 4 |
| 8 | 4 | 45.8% / 100.0% | 1511 MB | 6507 MB | 0.98 | 1148 MB | 9 |
| 12 | 5 | 54.8% / 100.0% | 2138 MB | 5879 MB | 0.39 | 1761 MB | 12 |
| 16 | 6 | 61.8% / 100.0% | 2475 MB | 5542 MB | 1.61 | 2080 MB | 16 |
| 24 | 8 | 69.4% / 100.0% | 3472 MB | 4546 MB | 4.65 | 3105 MB | 24 |
| 32 | 10 | 70.6% / 100.0% | 4382 MB | 3635 MB | 4.93 | 4013 MB | 32 |
| 40 | 12 | 75.1% / 100.0% | 5223 MB | 2795 MB | 8.97 | 4853 MB | 41 |

8 并发起出现 100% CPU 峰值；12–40 的 CPU 平均值、load1 和延迟整体上升。40 仍保留约 2.8 GB 可用内存，因此短程任务的主要代价是 CPU 竞争、进程启动重叠和尾延迟，而不是立即耗尽内存。Agent 进程峰值 41 是旧进程退出与新进程启动在采样点重叠；应用 fingerprint 始终只有一个 FastAPI 服务进程。

## 5. 长程两阶段完整结果

每个用户连续执行：

1. foundation：创建四个离线 Web 项目文件；
2. final-QA：恢复同一 Claude session，补齐并修复六个文件。

最终 command-side verifier 要求工作区只包含：

```text
index.html  about.html  styles.css  app.js  README.md  loadtest.json
```

它还检查文件大小、引用关系、v2.0 footer、480/768px 断点、`localStorage`、`textContent`、`innerHTML` 禁令、唯一 run token 和 manifest 精确值。

### 5.1 Agent、产物与端到端结果

| 并发 | Agent | 产物 | 端到端 | p50 | p90 | 最大延迟 | 平均工具调用 | 工作区/服务进程 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1/1 | 1/1 | 1/1 | 273.9 s | 273.9 s | 273.9 s | 25.0 | 1 / 1 |
| 2 | 2/2 | 2/2 | 2/2 | 343.7 s | 376.8 s | 385.1 s | 25.5 | 2 / 1 |
| 4 | 4/4 | 4/4 | 4/4 | 344.6 s | 370.6 s | 378.2 s | 31.0 | 4 / 1 |
| 8 | 8/8 | 8/8 | 8/8 | 368.6 s | 557.4 s | 587.8 s | 35.1 | 8 / 1 |
| 12 | 12/12 | 12/12 | 12/12 | 337.4 s | 385.4 s | 459.5 s | 31.5 | 12 / 1 |
| 16 | 16/16 | 16/16 | 16/16 | 341.1 s | 439.8 s | 751.1 s | 33.5 | 16 / 1 |
| 24 | 24/24 | 24/24 | 24/24 | 356.2 s | 449.5 s | 634.5 s | 31.4 | 24 / 1 |
| 32 | 32/32 | 32/32 | 32/32 | 324.4 s | 556.5 s | 633.0 s | 29.9 | 32 / 1 |
| 40 | 0/40 | 0/40 | 0/40 | — | — | — | 7.0 | 0 / 0 |

1–32 共 **99/99** 个用户完成 Agent marker、resume chain、工作区/进程一致性和六文件验证，即 198 个 Agent 阶段、594 个最终文件全部通过。1 并发只有一个样本，其 p50/p90/max 相同，不代表延迟分布。32 的 p50 低于 24 是 Agent/模型路径随机性，不能解释为更高并发改善性能；p90、内存和 load 更能反映容量压力。

### 5.2 资源窗口

| 并发 | 样本 | CPU 平均/峰值 | 内存已用峰值 | 最低可用内存 | load1 峰值 | cgroup current 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 138 | 1.3% / 18.1% | 658 MB | 7360 MB | 0.53 | 297 MB | 1 |
| 2 | 194 | 2.0% / 29.7% | 889 MB | 7128 MB | 0.34 | 500 MB | 2 |
| 4 | 190 | 3.9% / 49.0% | 1284 MB | 6733 MB | 0.15 | 923 MB | 5 |
| 8 | 295 | 5.4% / 86.4% | 2046 MB | 5971 MB | 0.75 | 1662 MB | 8 |
| 12 | 230 | 9.2% / 100.0% | 2840 MB | 5177 MB | 0.75 | 2464 MB | 12 |
| 16 | 376 | 7.8% / 100.0% | 3580 MB | 4437 MB | 1.46 | 3205 MB | 16 |
| 24 | 318 | 13.4% / 100.0% | 5074 MB | 2944 MB | 3.97 | 4704 MB | 24 |
| 32 | 317 | 18.4% / 100.0% | 6873 MB | **1144 MB** | 5.53 | **6506 MB** | 33 |
| 40 | — | — | — | — | — | — | — |

长程 CPU 平均值较低，是因为大量时间用于等待模型和工具往返，不代表 CPU 没有压力。内存趋势更明确：cgroup current 峰值由 1 并发约 297 MB 增至 32 并发约 6506 MB；32 只保留 1144 MB，已处于低余量边界。

## 6. 长程 40 失败分析

原始数据：`results/load_test_scale40_long_l40_20260815.json`

- 40 个 foundation 请求的 API status 全部为 200；
- 40/40 都缺少最终 `complete` SSE event；
- 每个请求中断前产生 2–19 次工具事件，平均 7.0 次；
- 所有请求都没有 workspace、Claude session ID 或服务进程 fingerprint，因此未进入 final-QA；
- level monitor 和 final collection command 均以 exit code 1 失败，资源窗口不可用；
- `StopRuntimeSession` 返回 HTTP 200 且 session ID 匹配；
- 失败 session 停止后，Runtime 和 DEFAULT endpoint 控制面仍为 `READY`；
- 本目录没有保存可用于确认 OOM 或进程退出的 Runtime 日志证据。

从 24 到 32，cgroup current 峰值由 4704 MB 增至 6506 MB，最低可用内存由 2944 MB 降至 1144 MB。按趋势延伸到 40 会逼近或超过可见内存，因此资源压力是最合理解释。但证据只能支持：**长程 40 已确认不可用，具体终止机制未被直接证明。**

## 7. 容量建议

| 工作负载 | 建议常态活跃 Agent | 已验证成功边界 | 已观察失败边界 | 主要依据 |
|---|---:|---:|---:|---|
| 短程精确回复 | 24–32 | 40 | 40 以内未失败 | 40 成功但 CPU 峰值 100%、load1 8.97、p90 22.1 秒 |
| 长程两阶段项目 | **不超过 24** | 32 | **40** | 24 尚余 2944 MB；32 仅余 1144 MB；40 为 0/40 |

若目标是稳定尾延迟而不是最大吞吐，长程可把常态槽位进一步降到 16。应用层必须保留 semaphore，不能允许请求无限制地产生 Claude 子进程。

`MAX_PARALLEL_AGENTS=40` 只提高同时驻留上限，不增加 CPU 或内存。它适合作为容量边界测试配置，不适合作为当前长程工作负载的默认生产配置。

## 8. 实测中发现并修复的集成问题

| 问题 | 真实表现 | 修复和回归保护 |
|---|---|---|
| 创建 Runtime 缺网络配置 | `CreateAgentRuntime` 被控制面拒绝，没有半成品 Runtime | create/update 显式传入 `networkMode=PUBLIC`；加入部署契约测试 |
| command 不隐式解释 shell | base64 pipeline 被当普通 argv，stdout 只是编码文本 | `run_shell_script()` 显式使用 `/bin/bash -c`；加入运输测试 |
| 一次性 `query()` 不执行 Python hooks | 越权请求无泄露但 `denied_count=0` | 改用双向 `ClaudeSDKClient`，确定性 guard probe 最终 `denied_count=1` |
| 长程 marker 判定过严 | marker 前摘要导致 Agent 成功被误判 | 长程要求包含 marker；短程继续要求精确相等 |
| prompt 与 verifier 不一致 | `app.js` 注释出现 `innerHTML` 字面量 | prompt 要求代码和注释完全不出现该字面量 |

`app/server.py` 中的 `ClaudeSDKClient` 和 `scripts/runtime_session.py` 中显式 `/bin/bash -c` 都是实测所需行为，不能退回旧实现。

## 9. 与 EC2 Capacity Provider 测试的方法差异

`15-shared-runtime-instance/` 测试显式 EC2 Capacity Provider，本目录测试未配置 Capacity Provider 的默认 Runtime。两者工作负载形状可以对照，容量数值不能混用。

| 维度 | 默认 microVM Runtime | EC2 Capacity Provider |
|---|---|---|
| 计算拓扑 | AgentCore 默认 Runtime | 显式 c7g/m7g、ASG/Capacity Provider |
| session 内诊断 | `InvokeAgentRuntimeCommand` | SSM 进入承载 EC2 |
| 资源视角 | session 容器内 `/proc`、cgroup v2 | EC2 宿主机级 CPU、内存、进程 |
| 文件系统 | `/tmp/agentcore-users` 临时数据 | `/mnt/scratch/users` 实例挂载卷 |
| 产物验证 | command API 在同一 session 内执行 | SSM 从实例侧执行 |
| 横向容量 | 一个 shared session 未自动拆分 | 实例池可扩容，但一个 session 仍固定到一个实例 |
| 清理 | stop session、删除 Runtime | 还涉及 Capacity Provider、ASG/EC2 生命周期 |

即使当前容器也看到 2 个逻辑 CPU 和接近 8 GiB 内存，也不能把它等同于 `m7g.large`；管理边界、监控范围、调度、文件系统和生命周期均不同。

## 10. 证据边界与限制

- 每个最终并发档只执行一次，没有在 32 成功边界或 40 失败边界做重复试验。
- 未执行小时级 soak、反复突发、故障注入或 33–39 精确转折点测试。
- 未测试不同模型、prompt、区域或 Runtime 平台版本。
- 40 档没有资源窗口或 OOM 日志，不能确认内核 OOM kill。
- `/proc` 和 cgroup 数据是 session 内视角，不是宿主机级监控；cgroup 上限显示为 `max`。
- 隔离测试适用于当前受限工具集合和弱威胁模型，不适用于互不信任租户。
- 所有容量建议只适用于当前镜像、Sonnet 4.6、当前 prompt/verifier 和 2026-08-14/15 的平台状态。

## 11. 原始证据清单

### 11.1 主要成功数据

- 隔离：`results/multiuser_20260814T160656Z.json`
- 短程 2/4/8：`results/load_test_20260814T160811Z.json`
- 短程 12：`results/load_test_scale40_short_l12_20260815.json`
- 短程 16：`results/load_test_scale40_short_l16_20260815.json`
- 短程 24：`results/load_test_scale40_short_l24_20260815.json`
- 短程 32：`results/load_test_scale40_short_l32_20260815.json`
- 短程 40：`results/load_test_scale40_short_l40_20260815.json`
- 长程 1：`results/load_test_longrun_20260814T162033Z.json`
- 长程 2/4：`results/load_test_longrun_20260814T162540Z.json`
- 长程 8：`results/load_test_longrun_20260814T163857Z.json`
- 长程 12：`results/load_test_scale40_long_l12_20260815.json`
- 长程 16：`results/load_test_scale40_long_l16_20260815.json`
- 长程 24：`results/load_test_scale40_long_l24_20260815.json`
- 长程 32：`results/load_test_scale40_long_l32_20260815.json`

### 11.2 失败边界与诊断数据

- 长程 40：`results/load_test_scale40_long_l40_20260815.json`
- command shell 诊断：`results/multiuser_20260814T160000Z.json`
- hook 诊断：`results/multiuser_20260814T160116Z.json`
- hook 诊断：`results/multiuser_20260814T160338Z.json`
- marker 诊断：`results/load_test_longrun_20260814T160911Z.json`
- verifier 契约诊断：`results/load_test_longrun_20260814T161339Z.json`

### 11.3 Runtime、环境与清理证据

- `results/runtime_probe_20260814.json`
- `results/runtime_metadata_before_cleanup_20260814.json`
- `results/runtime_cleanup_20260814.json`
- `results/runtime_metadata_scale40_before_cleanup_20260815.json`
- `results/runtime_cleanup_scale40_20260815.json`

### 11.4 控制台日志

- `results/deploy_20260814.log`
- `results/smoke_console_20260814.log`
- `results/short_console_20260814.log`
- `results/long1_console_20260814.log`
- `results/long24_console_20260814.log`
- `results/long8_console_20260814.log`
- `results/deploy_scale40_20260815.log`
- `results/short_scale40_l{12,16,24,32,40}_20260815.log`
- `results/long_scale40_l{12,16,24,32,40}_20260815.log`

## 12. 质量验证与资源清理

最终本地质量门：

- `uv lock --check`、`uv sync --frozen`、compileall：通过；
- `python -m unittest discover -s tests -v`：45/45 通过；
- Ruff check/format：通过；
- Pyright：0 errors、0 warnings；
- shell syntax、三个 CLI help、server import、JSON 解析和 diff check：通过；
- 独立 Trellis 检查重新计算全部并发档，结果与报告一致。

清理终态：

- 全部 21 个带 session 的 JSON 均记录 `StopRuntimeSession` 成功、HTTP 200；
- 长程 40 的 monitor cleanup 失败被保留为容量失败证据，其 session stop 仍成功；
- Runtime `shared_runtime_microvm-KkqELu3xji` 已删除，`GetAgentRuntime` 返回 `ResourceNotFoundException`，列表过滤为空；
- Runtime `shared_runtime_microvm-oGddGiDWRD` 已删除，`GetAgentRuntime` 返回 `ResourceNotFoundException`，列表过滤为空；
- 本地 `runtime.json` 已删除；
- ECR `launchpad-agents:shared-runtime-microvm-v1` 保留为 `ACTIVE`，最终 digest 为 `sha256:ca626ff6df75493fbac6d1c7f47eaeca546051d55776fcfef22cc21251cb76c9`；
- 未修改现有 IAM、VPC、Capacity Provider、ASG、目录 15 Runtime 或其他 Runtime。
