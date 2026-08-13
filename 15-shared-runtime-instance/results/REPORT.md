# 共享 Runtime Session 多用户短程与长程任务并发测试报告

- 报告日期：2026-08-13（UTC）
- 测试日期：2026-08-12 至 2026-08-13（UTC）
- 区域：us-west-2
- 项目：`15-shared-runtime-instance/`
- 模型：`us.anthropic.claude-sonnet-4-6`
- 镜像：`launchpad-agents:shared-runtime-v1`（Agent 实现基于 Claude Agent SDK）
- 计算资源：
  - `capacity_provider_arm_kb-FQtDNVGq1t`：`c7g.large`（2C / 4 GiB）
  - `capacity_provider_arm_m7g_large-1HB6aXJTVr`：`m7g.large`（2C / 8 GiB）
  - 两者均为 Amazon Linux ARM64，使用 50 GiB gp3 `scratch` 卷
- Runtime：
  - `shared_runtime_multiuser-X10bCH6p6u`：`c7g.large`，version 5，
    `MAX_PARALLEL_AGENTS=16`
  - `shared_runtime_multiuser_m7g-EZpQed4lPW`：`m7g.large`，version 1，
    `MAX_PARALLEL_AGENTS=40`
  - 两者状态均为 `READY`，`MAX_TURNS=64`

## TL;DR

多个真实用户可以通过同一个 `runtimeSessionId` 安全复用同一台 AgentCore EC2
Capacity Provider 实例上的 Claude Agent SDK 容器。安全并发取决于任务持续时间、
进程驻留方式和实例内存。

### 容量结论

| 实例与场景 | Runtime 主要瓶颈 | 建议活跃 Agent 并发 | 已验证边界 | 不可用边界 |
|---|---|---:|---:|---:|
| `c7g.large`（2C / 4 GiB）短程任务 | CPU 峰值、进程启动、并发内存和队列延迟 | **12～16** | 24 真并行可完成，但 CPU 平均 94.6%、最低可用内存 447 MB | 32 真并行：0/32 |
| `c7g.large`（2C / 4 GiB）长程任务 | 内存余量、子进程长时间驻留、模型执行长尾 | **8～12** | 16 可完成，但最低可用内存仅 573 MB | 24 真并行：两次 0/24 |
| `m7g.large`（2C / 8 GiB）长程任务 | 高并发内存余量、CPU 瞬时峰值、模型执行长尾 | **最多 32** | 40 在两台实例上均 40/40，复测最低可用内存 425 MB | 40 以内未失败；41+ 未测试 |

### 核心判断

1. **多用户隔离成立。** 18/18 检查通过，文件、工作区和 Claude session 无串扰。
2. **请求并发不等于 Agent 真并行。** 短程 40 请求在
   `MAX_PARALLEL_AGENTS=16` 下是 16 个执行、其余排队；164/164 请求全部成功。
3. **应用层排队是实例保护机制。** 放开到 40 槽后，短程 24 真并行虽可完成，
   但最低可用内存仅 447 MB；32 真并行变为 0/32，并导致 SSM 失联。
4. **长程任务首先受内存约束。** `c7g.large`（2C / 4 GiB）的 16 并发只剩
   573 MB，24 并发两次 0/24；保持 2C 不变、升级到 `m7g.large`
   （2C / 8 GiB）后，24、32、40 并发均 100% 完成。
5. **40 是 `m7g.large`（2C / 8 GiB）的已验证运行边界，不是建议常态值。**
   两台实例均
   40/40，但复测最低可用内存只剩 425 MB；本轮建议持续运行上限为 32。
6. **同一个 shared session 不能水平扩展。** ASG 扩容不会拆分同一个
   `runtimeSessionId`，更高总容量需要 session 分片。
7. **Runtime 配置按实例规格区分。** `c7g.large` 保持 16 槽；`m7g.large`
   测试 Runtime 配置 40 槽，但生产容量建议不超过 32。

## 1. 方案架构与隔离模型

### 1.1 请求路由

所有用户请求通过 `InvokeAgentRuntime` 调用同一个 Runtime：

```text
user A ─┐
user B ─┼─ InvokeAgentRuntime
user C ─┘   runtimeSessionId = shared-session
            runtimeUserId    = real-user-id
                    │
                    ▼
        同一 EC2 / 同一 Agent 容器
```

相同 `runtimeSessionId` 的请求固定落在同一 Runtime 实例。ASG 可以预热更多 EC2，
但不会自动把同一个 shared session 的用户分摊到其他实例。

### 1.2 用户隔离

| 层 | 机制 |
|---|---|
| 身份 | `runtimeUserId` 注入可信用户 ID，payload 中保留 `user_id` 兜底 |
| ID 校验 | 仅允许 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |
| 工作区 | `/mnt/scratch/users/<readable-id>-<sha256前12位>` |
| 文件权限 | 每个用户工作区创建为 `0700` |
| 工具白名单 | `Read / Write / Edit / Glob / Grep / LS / TodoWrite` |
| 工具黑名单 | `Bash / WebFetch / WebSearch / Task / KillBash` |
| 路径守卫 | `PreToolUse` 对路径做 resolve，阻止绝对路径、`..` 和 symlink 逃逸 |
| 会话记忆 | 每用户独立 Claude session id，存入各自 `.session_meta.json` |
| 用户内串行 | 每个用户独立 `asyncio.Lock`，避免同用户 resume 冲突 |
| 跨用户并发 | 不同用户并行执行，由全局 semaphore 控制进程上限 |
| CLI 隔离 | 每个 Claude 子进程的 `HOME` 指向该用户工作区 |

这是应用层隔离，不是 OS 级租户隔离。安全性依赖工具白名单和路径守卫，不能向
终端用户开放 Bash 或直接授予 `InvokeAgentRuntime` 权限。

## 2. 测试范围与方法

### 2.1 Runtime 修订

测试过程中使用过多个 Runtime revision，但镜像、模型和 Capacity Provider 一致：

| 测试 | Runtime ID | 说明 |
|---|---|---|
| 隔离功能 | `shared_runtime_multiuser-efbkaT7141` | 3 用户文件与记忆隔离 |
| 短程压测 | `shared_runtime_multiuser-HNft74DB4i` | 2～40 并发 `pong` |
| `c7g.large`（2C / 4 GiB）长程压测 | `shared_runtime_multiuser-X10bCH6p6u` | 2～24 并发 Web 项目 |
| `m7g.large`（2C / 8 GiB）长程压测 | `shared_runtime_multiuser_m7g-EZpQed4lPW` | 16～40 并发 Web 项目 |

### 2.2 通用并发方法

- 每档使用 barrier 同时释放 N 个不同 `runtimeUserId`；
- 所有用户共用一个随机 `runtimeSessionId`；
- 每个请求记录开始时间、结束时间、延迟、错误和容器进程指纹；
- warmup 用于吸收扩容、镜像拉取和容器启动；
- 通过 SSM 在承载实例上每约 3 秒采集 CPU、内存、load 和 Agent 进程数；
- 每档比对 `server_run_id`，确认所有请求命中同一容器进程。

### 2.3 短程工作负载

提示词：

```text
Reply with exactly: pong
```

特点：

- 不读写文件；
- 不依赖上一轮对话；
- 单次仅启动一个短生命周期 Claude 请求；
- 服务端 `MAX_PARALLEL_AGENTS=16`；
- 20～40 并发中的超额请求由 semaphore 排队。

测试档位：

```text
2 -> 4 -> 6 -> 8 -> 12 -> 16 -> 20 -> 24 -> 32 -> 40
```

### 2.4 长程工作负载

每个用户完成一个离线任务看板项目，共两个连续阶段。

阶段一 `foundation`：

1. 创建 `index.html`；
2. 创建带 480px / 768px 断点的 `styles.css`；
3. 创建使用 `localStorage` 和 `textContent` 的 `app.js`；
4. 创建带唯一 run token 的 `README.md`；
5. 回读并修复四个文件。

阶段二 `final-qa`：

1. 恢复上一阶段的 Claude session；
2. 增加 `about.html`；
3. 全量检查引用、可访问性、响应式规则、存储逻辑和 XSS 安全写法；
4. 创建 `loadtest.json`；
5. 回读关键文件后返回 `PROJECT-DONE 6`。

最终产物：

```text
webapp/
├── index.html
├── about.html
├── styles.css
├── app.js
├── README.md
└── loadtest.json
```

成功判定不只依赖 Agent 回复，还要求：

- 两个阶段 marker 正确；
- 第二阶段 `resumed_from` 等于第一阶段 `claude_session_id`；
- 两阶段工作区和 `server_run_id` 一致；
- SSM 在宿主机检查 6 个文件全部存在；
- 文件大小、HTML 引用、响应式断点、`localStorage`、`textContent` 和 run token
  全部通过。

测试档位：

```text
2 -> 8 -> 16 -> 24
```

24 并发容量测试期间临时将 `MAX_PARALLEL_AGENTS` 从 16 提高到 24。测试完成后
已恢复到 16。

## 3. 多用户隔离功能验证

数据文件：`results/multiuser_20260812T094127Z.json`

结果：18/18 检查通过。

| 验证项 | 方法 | 结果 |
|---|---|---|
| warmup | 首次调用返回 `ready` | 通过，76.6 s |
| 同实例复用 | 4 次调用比较 `server_run_id` | 仅 1 个指纹 |
| 并发写入 | alice / bob / carol 写入唯一 token | 全部写入并读回 |
| 工作区隔离 | 检查三个用户的 workspace | 路径完全独立 |
| 相对路径越权 | bob 尝试读取 `../alice-*/secret.txt` | 未泄露 |
| 绝对路径越权 | bob 尝试读取 alice 的绝对路径 | 未泄露 |
| 会话记忆 | 三个用户分别追问自己的 token | 全部召回正确 |
| 记忆无串扰 | 检查回复是否包含其他用户 token | 无串扰 |

两次越权尝试在系统提示词层已被模型拒绝，`denied_count=0`，因此未实际触发
PreToolUse deny。路径守卫本身由 12 个单元测试覆盖，包括绝对路径、`..`、tilde
和 symlink 逃逸。

## 4. 短程任务并发结果

原始数据：

- `results/load_test_20260812T112652Z.json`（2～20 并发）
- `results/load_test_20260812T113047Z.json`（24～40 并发）

### 4.1 延迟与成功率

| 并发 | 成功率 | p50 | p90 | 最大延迟 | distinct instances |
|---:|---:|---:|---:|---:|---:|
| 2 | 100% | 5.0 s | 6.0 s | 6.2 s | 1 |
| 4 | 100% | 4.2 s | 5.0 s | 5.3 s | 1 |
| 6 | 100% | 5.3 s | 5.9 s | 6.4 s | 1 |
| 8 | 100% | 6.7 s | 7.3 s | 7.7 s | 1 |
| 12 | 100% | 8.5 s | 8.9 s | 9.0 s | 1 |
| 16 | 100% | 10.7 s | 11.3 s | 11.5 s | 1 |
| 20 | 100% | 12.2 s | 15.7 s | 16.3 s | 1 |
| 24 | 100% | 10.8 s | 15.2 s | 15.3 s | 1 |
| 32 | 100% | 14.7 s | 21.0 s | 21.5 s | 1 |
| 40 | 100% | 18.6 s | 24.4 s | 25.8 s | 1 |

共 164 个短程请求，164/164 成功。40 并发内没有发现硬失败边界。

### 4.2 资源使用

| 并发 | CPU 平均 | CPU 峰值 | 内存已用峰值 | 内存最低可用 | load1 峰值 | 进程采样峰值 |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 1.0% | 2% | 607 MB | 3034 MB | 0.40 | 未正确采集 |
| 4 | 41.0% | 69% | 839 MB | 2802 MB | 0.77 | 未正确采集 |
| 6 | 62.0% | 100% | 1154 MB | 2487 MB | 1.08 | 未正确采集 |
| 8 | 53.3% | 91% | 1303 MB | 2337 MB | 1.17 | 未正确采集 |
| 12 | 68.2% | 100% | 1969 MB | 1671 MB | 2.33 | 未正确采集 |
| 16 | 61.2% | 100% | 2370 MB | 1270 MB | 4.66 | 未正确采集 |
| 20 | 66.4% | 100% | 2200 MB | 1439 MB | 5.72 | 未正确采集 |
| 24 | 70.3% | 100% | 2192 MB | 1448 MB | 2.95 | 16 |
| 32 | 78.1% | 100% | 2394 MB | 1246 MB | 5.70 | 20 |
| 40 | 86.0% | 100% | 2222 MB | 1417 MB | 7.26 | 18 |

前 7 档的进程数为空，是因为采样器最初只匹配 `node`，而 bundled CLI 的进程名为
`claude`。24 并发起已修正。进程采样峰值可能短暂高于 semaphore 数值，因为旧进程
退出与新进程启动存在采样重叠。

### 4.3 短程任务分析

- **主要瓶颈是 CPU 和排队延迟。** 6 并发起 CPU 峰值达到 100%；
- **内存未成为硬约束。** 全程最低可用内存仍约 1.2 GiB；
- **16 并发是实际执行槽位上限。** 20～40 的额外请求在应用层等待；
- **请求越多，成功率未下降，但长尾增加。** 40 并发 p90 为 24.4 秒；
- **同一 shared session 不能水平扩展。** ASG 扩容不会拆分该 session。

短程容量建议：

| 目标 | 建议 |
|---|---:|
| p50 < 10 s | 不超过 12 并发 |
| p50 约 10～12 s | 16 并发 |
| 可接受排队和 20～25 s 长尾 | 最多约 40 个同时请求 |

这里的“40 个同时请求”不等于 40 个 Agent 同时运行，实际运行槽位仍为 16。

### 4.4 临时放开 40 槽的无排队测试

为了测量真正同时运行大量短程 Agent 的资源边界，Runtime 临时设置：

```text
MAX_PARALLEL_AGENTS=40
```

每个请求使用唯一用户 ID 和全新 Claude session，测试档位为
`16 -> 24 -> 32 -> 40`。32 并发成功率降到 0%，因此按 80% 停止阈值未继续执行 40。

原始数据：`results/load_test_20260813T020930Z.json`

专项报告：`results/SHORT_NOQUEUE_REPORT.md`

| 真并发 Agent | 成功率 | p50 | p90 | CPU 平均/峰值 | 内存已用峰值 | 内存最低可用 | 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 16/16 | 9.85 s | 10.63 s | 68.5% / 100% | 2383 MB | 1258 MB | 16 |
| 24 | 24/24 | 14.63 s | 15.51 s | 94.6% / 100% | 3192 MB | 447 MB | 25 |
| 32 | 0/32 | – | – | 无完整样本 | 无完整样本 | SSM 失联 | 无完整样本 |
| 40 | 未执行 | – | – | 32 档已触发停止条件 | – | – | – |

关键对比：

- 24 请求在 16 槽排队模式下最低可用内存为 1448 MB，真并行时只剩 447 MB；
- 32 请求在 16 槽排队模式下 100% 成功，真并行时 0/32；
- 24 真并行 p50 为 14.63 秒，高于排队模式的 10.80 秒；
- 32 真并行持续约 13.8 分钟无有效 complete event，并导致 SSM 失联；
- 短任务的安全活跃进程上限仍应保持 16，24 只适合作为高风险突发边界。

## 5. 长程任务并发结果

以下 5.1～5.4 为 `c7g.large`（2C / 4 GiB）基线，5.5 为
`m7g.large`（2C / 8 GiB）纵向扩容复测。

原始数据：

- `results/load_test_longrun_20260812T134627Z.json`（2 并发）
- `results/load_test_longrun_20260812T151832Z.json`（8 / 16 并发）
- `results/load_test_longrun_20260812T150257Z.json`（24 并发失败复测）

详细容量分析：`results/LONGRUN_CAPACITY_REPORT.md`

### 5.1 成功率、延迟与产物

| 并发 | Agent 成功 | 产物验证 | 成功率 | p50 | p90 | 最大耗时 | 平均 tool calls |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 2/2 | 2/2 | 100% | 386.4 s | 461.8 s | 480.7 s | 24.0 |
| 8 | 8/8 | 8/8 | 100% | 441.8 s | 515.2 s | 546.8 s | 38.2 |
| 16 | 16/16 | 16/16 | 100% | 402.0 s | 553.4 s | 647.1 s | 27.6 |
| 24 | 0/24 | 0/24 | 0% | 无有效样本 | 无有效样本 | 全部约 480～492 s 后失败 | 0.5 |

2、8、16 并发中的每个成功用户都满足：

- 两阶段 marker 正确；
- session resume 链正确；
- workspace 和容器指纹一致；
- 6 个文件通过宿主机校验；
- run token、HTML 引用、响应式断点和安全写法全部通过。

### 5.2 资源使用

| 并发 | CPU 平均 | CPU 峰值 | 内存已用峰值 | 内存最低可用 | load1 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 3.6% | 25% | 727 MB | 2913 MB | 0.64 | 2 |
| 8 | 13.3% | 100% | 1764 MB | 1876 MB | 1.26 | 9 |
| 16 | 20.2% | 100% | 3067 MB | 573 MB | 2.58 | 16 |
| 24 | 无完整样本 | 无完整样本 | 无完整样本 | SSM 失联 | 无完整样本 | 无完整样本 |

长程任务的 CPU 平均值不高，但 Agent 子进程会在整个任务期间驻留。16 并发时最低
可用内存已降到 573 MB，明显比短程 16 并发的 1270 MB 更接近实例边界。

### 5.3 24 并发失败

容量测试期间临时把 `MAX_PARALLEL_AGENTS` 提高到 24，让 24 个长任务真正同时
运行。结果在两台不同 `c7g.large` 上均可复现：

| 轮次 | 实例 | 结果 | 失败时间 | SSM |
|---|---|---|---|---|
| 第一次 | `i-01aaff80816bca518` | 0/24 | 约 718～726 s | `ConnectionLost` |
| 独立复测 | `i-0f97367334b1d6102` | 0/24 | 约 480～492 s | `ConnectionLost` |

共同特征：

- 没有用户完成第一阶段；
- 平均只有约 0.5～0.6 次 tool call；
- 没有返回 workspace 或实例指纹；
- Runtime log 只有 request，没有后续 complete/error；
- EC2 system / instance status check 仍为 `ok`；
- SSM 到测试结束仍未恢复。

现有证据强烈符合用户态内存或进程资源耗尽。由于实例失联后无法读取 `dmesg`，
不能把内核 OOM kill 作为已确认事实。

### 5.4 `c7g.large`（2C / 4 GiB）长程容量建议

| 目标 | 建议并发 |
|---|---:|
| 保守运行，保留内存余量 | 8～12 |
| 已验证可完成的硬上限 | 16 |
| 不可用配置 | 24 |
| 尚未细分的临界区间 | 17～23 |

当前已确认的最大可用长程并发为 16，但 16 并发内存余量仅 573 MB，不适合作为
长期稳定运行目标。生产或持续压测建议控制在 8～12。

### 5.5 `m7g.large`（2C / 8 GiB）纵向扩容复测

新建独立 Capacity Provider
`capacity_provider_arm_m7g_large-1HB6aXJTVr`，仅将实例规格从
`c7g.large`（2C / 4 GiB）改为 `m7g.large`（2C / 8 GiB），其余 IAM、
VPC、EBS、镜像、模型和 workload 保持一致。

独立 Runtime `shared_runtime_multiuser_m7g-EZpQed4lPW` 配置：

```text
MAX_PARALLEL_AGENTS=40
MAX_TURNS=64
```

因此 16、24、32、40 四档均为真实执行并发，不存在应用层 16 槽排队。

| 并发 | Agent 成功 | 产物验证 | p50 | p90 | 最大耗时 | CPU 平均/峰值 | 最低可用内存 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 16/16 | 16/16 | 398.7 s | 691.8 s | 788.0 s | 18.6% / 100% | 4507 MB |
| 24 | 24/24 | 24/24 | 442.5 s | 613.3 s | 675.7 s | 31.3% / 100% | 3123 MB |
| 32 | 32/32 | 32/32 | 429.4 s | 606.4 s | 755.7 s | 34.2% / 100% | 1738 MB |
| 40 | 40/40 | 40/40 | 451.4 s | 662.3 s | 830.8 s | 37.9% / 100% | 425 MB |

40 并发在两台独立 `m7g.large` 上重复成功：

| 实例 | Agent 成功 | 产物验证 | p50 | p90 | 最大耗时 |
|---|---:|---:|---:|---:|---:|
| `i-03a23f8802bd4be44` | 40/40 | 40/40 | 457.9 s | 700.9 s | 765.9 s |
| `i-0f7dd10c1eaeb25e1` | 40/40 | 40/40 | 451.4 s | 662.3 s | 830.8 s |

该规格的容量结论：

| 目标 | 建议并发 |
|---|---:|
| 保守持续运行 | 24 |
| 本轮建议容量上限 | 32 |
| 已验证峰值边界 | 40 |
| 41+ | 未测试 |

40 并发虽然功能和产物均 100% 成功，但最低可用内存只有 425 MB，不具备足够的
生产余量。详细分析见 `results/LONGRUN_M7G_REPORT.md`。

## 6. 短程与长程任务对比

| 维度 | `c7g.large`（2C / 4 GiB）短程 | `c7g.large`（2C / 4 GiB）长程 | `m7g.large`（2C / 8 GiB）长程 |
|---|---|---|---|
| 单次时长 | 约 4～26 秒 | 约 5～11 分钟 | 约 7～14 分钟 |
| 文件工具调用 | 无 | 每用户约 24～38 次 | 每用户约 34～39 次 |
| 会话阶段 | 单阶段 | 两阶段 resume | 两阶段 resume |
| 进程驻留 | 短 | 长 | 长 |
| 最高成功真实运行槽位 | 24，资源余量极低 | 16 | 40，资源余量极低 |
| 主要瓶颈 | CPU 峰值和队列延迟 | 内存余量和进程驻留 | 高并发内存余量和模型长尾 |
| 失败边界 | 32 真并行时 0/32 | 24 真并行时两次 0/24 | 40 以内未失败，41+ 未测试 |
| 建议并发 | 12～16 | 8～12 | 不超过 32 |

关键差异：

1. 短程请求完成快，进程释放后 semaphore 可以继续服务队列；
2. 长程任务让 Claude 子进程持续驻留数分钟，内存不能快速回收；
3. 短程 40 并发测试实际最多 16 个槽位同时运行；
4. 长程 24 并发测试放开了 24 个槽位，因此直接触发实例资源边界；
5. 轻量任务的并发数字不能用于推算长程 Agent 容量；
6. vCPU 数不变、内存翻倍后，长程可运行并发从 16 提升到至少 40，进一步证明
   该 workload 的首要实例瓶颈是内存。

## 7. 统一容量与部署建议

### 7.1 `c7g.large`（2C / 4 GiB）

| 场景 | 建议 |
|---|---|
| 仅短程交互请求 | `MAX_PARALLEL_AGENTS=12～16`，不建议持续设置 24 |
| 仅长程文件任务 | `MAX_PARALLEL_AGENTS=8～12` |
| 可接受短程排队 | 接收超过槽位的请求，但必须设置队列和超时 SLO |
| 不建议 | 在 4 GiB 实例上配置 24 个长程 Agent 槽位 |

`c7g.large` Runtime 和部署脚本默认值保持：

```text
MAX_PARALLEL_AGENTS=16
MAX_TURNS=64
```

如果业务以长程任务为主，建议进一步将默认上限调整到 8～12。

### 7.2 `m7g.large`（2C / 8 GiB）

| 场景 | 建议 |
|---|---|
| 长程任务保守持续运行 | `MAX_PARALLEL_AGENTS=24` |
| 本轮建议容量上限 | `MAX_PARALLEL_AGENTS=32` |
| 峰值或隔离压测 | 40，必须监控内存低水位 |
| 不建议 | 将 40 作为无准入控制的长期稳定配置 |

独立测试 Runtime 当前配置：

```text
MAX_PARALLEL_AGENTS=40
MAX_TURNS=64
```

该配置用于保留 40 真并行复测能力，不代表生产推荐值。40 并发最低可用内存仅
425 MB，生产配置建议不超过 32。

### 7.3 扩容路径

1. **纵向扩容**：`m7g.large` 已验证 40 可运行；若需要稳定承载 40，应继续增加
   内存并重新验证；
2. **session 分片**：网关按 `hash(user_id) % N` 分配多个 shared session；
3. **工作负载拆分**：短任务和长任务使用不同 Runtime / semaphore；
4. **动态准入**：结合可用内存、活跃进程数和队列长度决定是否接收新长任务；
5. **保护机制**：对长任务设置队列上限、取消机制、每用户配额和最大执行时间；
6. **监控告警**：关注 SSM `ConnectionLost`、内存低水位、无 complete 事件和
   Runtime 容器重启。

## 8. 故障发现与测试工具改进

24 并发第一次失败时，宿主机 SSM 同时失联，旧版测试脚本在最终资源采集阶段抛错，
导致已完成的 8 / 16 档结果没有写入 JSON。

`load_test_longrun.py` 已增加：

1. 每档 `run_level` 返回后立即写 agent checkpoint；
2. 宿主机产物校验后再次写 checkpoint；
3. 监控快照成功后再次写 checkpoint；
4. 临时文件加原子 replace；
5. 监控失败时保留最后可用样本并照常写结果；
6. 没有 workspace 时跳过无意义的 SSM 产物命令。

独立 24 并发复测证明修复有效，即使 SSM 再次失联，请求级失败结果仍成功落盘。

`load_test.py` 同步增加了唯一 run id、`reset=true`、逐档 checkpoint、监控降级和
服务端并发配置记录。32 真并行导致 SSM 失联后，16/24/32 的结果仍成功落盘。

`m7g.large` 第一轮 40 并发功能测试为 40/40，但原 shell 采样器在 40 档开始时
提前终止。长程脚本已将采样器改为单个 Python 进程直接读取 `/proc`，不再反复
启动 `vmstat`、`free` 和 `ps`；第二台实例的 40 并发复测取得 415 个有效窗口
样本，并确认最低可用内存为 425 MB。

## 9. 已知边界

- `c7g.large` 的 17～23 个长任务未逐档测试，精确临界点仍未知；
- `m7g.large` 的 41+ 长任务未测试，40 不是已确认硬失败点；
- 生成式模型执行时间存在方差，长程延迟需要更多重复轮次；
- 24 并发时 SSM 失联，最后时刻的内存和内核 OOM 日志不可获取；
- 两台失联实例因 IAM 显式 deny，当前测试身份不能执行 `RebootInstances`；
- 短程低档位资源采样点较少，CPU 平均值只用于趋势判断；
- 隔离属于应用层实现，容器内所有用户仍共享同一 OS 用户；
- 结论仅适用于当前模型、Agent 实现、镜像以及本报告中的 `c7g.large` /
  `m7g.large` 配置。

## 10. 原始数据与复测命令

### 10.1 原始数据

以下 JSON 是本地测试产物，已由 `.gitignore` 排除提交；仓库保留汇总报告、测试脚本
和复测命令。

| 文件 | 内容 |
|---|---|
| `results/multiuser_20260812T094127Z.json` | 18 项隔离功能验证 |
| `results/load_test_20260812T112652Z.json` | 短程 2～20 并发 |
| `results/load_test_20260812T113047Z.json` | 短程 24～40 并发 |
| `results/load_test_20260813T020930Z.json` | 短程 40 槽无排队测试 |
| `results/load_test_longrun_20260812T134627Z.json` | 长程 2 并发基线 |
| `results/load_test_longrun_20260812T151832Z.json` | 长程 8 / 16 并发 |
| `results/load_test_longrun_20260812T150257Z.json` | 长程 24 并发失败复测 |
| `results/load_test_longrun_m7g_20260813T025207Z.json` | `m7g.large` 16 / 24 / 32 / 40 并发 |
| `results/load_test_longrun_m7g40_20260813T034606Z.json` | `m7g.large` 40 并发资源复测 |
| `results/LONGRUN_CAPACITY_REPORT.md` | `c7g.large` 长程任务容量专项分析 |
| `results/LONGRUN_M7G_REPORT.md` | `m7g.large` 长程任务容量专项分析 |
| `results/SHORT_NOQUEUE_REPORT.md` | 短程任务无排队容量专项分析 |

### 10.2 复测命令

```bash
cd 15-shared-runtime-instance

# 隔离功能
python3 scripts/invoke_multiuser.py

# 短程并发
LEVELS='[2,4,6,8,12,16,20]' python3 scripts/load_test.py
LEVELS='[24,32,40]' python3 scripts/load_test.py

# 长程安全档位
LEVELS='[2,8,16]' \
  MONITOR_DURATION_S=7200 \
  TASK_READ_TIMEOUT_S=2400 \
  python3 scripts/load_test_longrun.py

# 24 长程并发仅限隔离测试环境，测试后必须恢复
MAX_PARALLEL_AGENTS=24 bash scripts/deploy.sh
LEVELS='[24]' \
  MONITOR_DURATION_S=5400 \
  TASK_READ_TIMEOUT_S=2400 \
  python3 scripts/load_test_longrun.py
MAX_PARALLEL_AGENTS=16 bash scripts/deploy.sh

# 创建 m7g.large Provider 和独立 40 槽 Runtime
CAPACITY_PROVIDER_ARN="$(scripts/create_capacity_provider.sh)"
CAPACITY_PROVIDER_ARN="${CAPACITY_PROVIDER_ARN}" \
  RUNTIME_NAME=shared_runtime_multiuser_m7g \
  RUNTIME_CONFIG=runtime.m7g.json \
  MAX_PARALLEL_AGENTS=40 \
  MAX_TURNS=64 \
  SKIP_IMAGE_BUILD=1 \
  bash scripts/deploy.sh

# m7g.large 长程容量爬坡
RUNTIME_CONFIG=runtime.m7g.json \
  RESULT_TAG=m7g \
  LEVELS='[16,24,32,40]' \
  SUCCESS_FLOOR=0.75 \
  MONITOR_DURATION_S=14400 \
  TASK_READ_TIMEOUT_S=3600 \
  python3 scripts/load_test_longrun.py
```
