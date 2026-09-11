# Runtime V2 内存用量同期对照实测

日期：2026-09-11；区域：`us-west-2`。

## 结论

**Runtime V2 有明显改善，但没有消除“应用 RSS 很小、AgentCore 内存用量遥测仍很高”的现象。** 本次 V2 小镜像空载 RSS 约 22 MiB，AgentCore 内存仍约 1.06 GB-equivalent；同期 V1 约 2.19。不能把这个值叫做最终计费内存。

| 原实验中的问题 | 此次 V2 结果 |
|---|---|
| RSS 严重低估 AgentCore 内存用量遥测 | **仍存在。** 小镜像空载约 22 MiB RSS 对应 1.06 GB-equivalent；相对同期对照下降约 52%，不是消除差额。 |
| 镜像加 256 MiB、应用不读取，guest 缓存大增 | **本次未复现原来的巨大 guest 缓存增量。** small/padded 的 V2 Cached 均约 190–191 MiB；V1 为 662/1431 MiB。 |
| 未读大镜像提高 AgentCore 内存遥测 | **仍有较小跨会话差额。** V2 baseline 1.056→1.284，约 +0.228；V1 2.187→3.019，约 +0.832。单次跨会话差额不能全部归因于镜像。 |
| 临时文件形成约两份缓存、fadvise 后仍残留 | **明显改善。** V2 的 cgroup file 增量约 256 MiB，而非 V1 约 513 MiB；fadvise 后接近 baseline。 |
| 释放后的观测阶段 AgentCore 内存遥测仍高于 baseline | **这次没有原样复现。** V2 匿名组释放后低于原 baseline；临时文件关闭后只比 baseline 高约 0.042 GB-equivalent。但存在后台漂移，不能据此重建计费规则。 |

另外，V2 首次读取镜像内 256 MiB 文件所在的阶段间隔达 **64.920 秒**，V1 为 0.016 秒。它与按需读取的解释相容，但本次没有 I/O tracing，也没有重复样本，不能把具体存储机制或普遍性能回退写成定论。

## 实验设计与证据

- 对照来源：`../23-runtime-memory-usage/` 的原始 Python 标准库探针、两个镜像和执行角色，全部复用，不重建镜像、不扩 IAM、不更改原 Runtime。
- 本报告将原默认平台控制组统一标记为 **V1**，作为与 V2 对照的版本标签。原创建请求未指定 `platformVersion`，本次 GetAgentRuntime 也没有返回该字段，因此该标签不代表接口已明确回显 V1；原始证据中的组名保持不变。新建组两份配置均明确返回 `platformVersion=V2` 和 READY。
- small 镜像压缩大小 46,204,449 bytes；padded 为 314,721,975 bytes，增加一个 256 MiB 随机文件层。每组使用相同 URI、digest、HTTP 协议、PUBLIC 网络、执行角色、idle 60 秒和 maxLifetime 600 秒。
- 每个平台 5 个独立 UUID 会话：small 空载、匿名内存、临时文件，padded 不读文件、读取文件。共 10 个会话，按负载配对串行执行并交替平台顺序，没有额外 smoke 或自动重试调用。
- 每阶段 30 秒，负载 256 MiB；排除每阶段首尾 3 秒，在同一 session 的内部窗口比较中位数。使用 930 个应用样本，30 个内部窗口各有 24 条逐秒用量日志，共 720 条。
- 部署开始 03:16:10 UTC；调用窗口 03:19:17–03:35:40 UTC；临时 Runtime 删除完成于 03:35:51；03:53:21 遥测覆盖全部阶段。实际日志投递延迟约 16.7–18.5 分钟。
- 日志字段为 `attributes.time_elapsed_seconds`、`attributes.session.id` 和 `metrics.agent.runtime.memory.gb_hours.used`；换算公式为 `GB-hours × 3600 ÷ interval_seconds`。零时长用量保留在总量中，不生成 gauge。
- 原始证据：`.state-memory-v2/2026-09-11/`，目录 0700、JSON 0600，并被 git 忽略。包含资源/请求/响应、镜像 digest、源文件 hash、10 份完整应用响应、原始遥测与统计结果。原实验 `.state/resources.json` hash 核验未变。

## 完整阶段对照

应用 RSS 列为 MiB；AgentCore 内存列为遥测换算的 **GB-equivalent**，不是应用 GiB，也不是最终账单。每行均为同负载、同阶段配对后的内部窗口中位数。

| 镜像 / 负载 | 阶段 | V1 RSS | V2 RSS | V1 AgentCore 内存 | V2 AgentCore 内存 |
|---|---|---:|---:|---:|---:|
| small / 空载 | baseline | 22.205 | 22.158 | 2.187076 | 1.055970 |
| small / 空载 | idle_control | 22.486 | 22.459 | 2.036240 | 1.004227 |
| small / 匿名内存 | baseline | 22.221 | 22.158 | 2.154141 | 1.177086 |
| small / 匿名内存 | allocated | 278.510 | 278.459 | 2.368085 | 1.243476 |
| small / 匿名内存 | released | 22.955 | 22.887 | 2.164120 | 1.056634 |
| small / 临时文件 | baseline | 22.248 | 22.156 | 2.131678 | 1.211941 |
| small / 临时文件 | file_cached | 23.484 | 23.393 | 2.916186 | 1.857753 |
| small / 临时文件 | after_fadvise | 23.824 | 23.729 | 2.917762 | 1.346551 |
| small / 临时文件 | file_closed | 24.197 | 24.102 | 2.883684 | 1.253591 |
| padded / 不读文件 | baseline | 22.213 | 22.146 | 3.019310 | 1.284206 |
| padded / 不读文件 | idle_control | 22.496 | 22.445 | 2.777714 | 1.284929 |
| padded / 读镜像文件 | baseline | 22.215 | 22.146 | 3.508928 | 1.290774 |
| padded / 读镜像文件 | file_cached | 23.441 | 23.379 | 3.392087 | 1.753392 |
| padded / 读镜像文件 | after_fadvise | 23.777 | 23.717 | 3.258117 | 1.613416 |
| padded / 读镜像文件 | file_closed | 24.148 | 24.084 | 3.096725 | 1.241896 |

本次 15 个配对阶段的 V2 AgentCore 内存中位数均低于 V1，降幅约 36%–63%。这只是本次遥测观测，不代表账单降幅或具有统计显著性的普遍结论。

## AgentCore 内存与应用 RSS 的差额如何理解

**AgentCore 内存遥测覆盖的范围比应用 RSS 更广，差额可能包含 microVM 内的系统、运行时和文件缓存，以及其他平台开销；目前无法将其全部归因于 Firecracker VMM 自身。**

需要区分 Firecracker 虚拟机管理器（VMM）本身与整个 microVM 运行环境：

| 层次 | 内存范围与本次可见性 |
|---|---|
| 应用进程 | Python 堆、栈、加载的库等；应用 RSS 反映该进程的驻留页，不代表整个运行环境。 |
| guest 系统 | Linux 内核、页表、slab、系统进程等；本次可通过 guest `/proc/meminfo` 观察部分统计。 |
| 容器运行时与文件系统 | 容器运行时、镜像文件页、读写缓存等；本次能看到部分 cgroup 与 guest 缓存数据。 |
| 宿主机与虚拟化层 | VMM、guest 内存映射及其他平台开销；本次没有宿主机级分解，具体计量归属尚不明确。 |

[Firecracker 官方规格](https://github.com/firecracker-microvm/firecracker/blob/main/SPECIFICATION.md)给出的参考是：在 **1 vCPU、128 MiB guest RAM、Firecracker 调优内核**及其指定测试条件下，VMM 线程额外内存开销 **≤5 MiB**。该值随负载和配置变化，且不包含 MMDS 数据存储。**这不是整台 guest 的内存，也不是 AgentCore 的实际测量值，不能用它推断本次 VMM 的大小；同样不能声称约 1 GB 的差额就是 Firecracker 自身开销。**

[AgentCore 定价说明](https://aws.amazon.com/bedrock/agentcore/pricing/)明确指出，计费包含应用资源之外的系统开销。但这不提供各层内存的逐项分解，也不意味着本报告的遥测等于最终计费内存。

本次 V2 small 空载 baseline 内部窗口的中位数为：

| 指标 | 观测值 |
|---|---:|
| 应用 RSS | 22.16 MiB |
| guest `Cached` | 190.05 MiB |
| guest `AnonPages` | 115.40 MiB |
| guest `Slab` | 25.29 MiB |
| AgentCore 内存遥测 | 1.056 GB-equivalent |

**这些行不能直接相加。** 应用匿名页已包含在 guest 匿名内存统计中；宿主机的 guest 内存映射也可能描述同一批物理页，不能跨层重复记账。数据确认了应用进程之外存在系统和缓存内存，但可见的 guest 指标不足以完整解释约 1 GB 的遥测。缺少宿主机 VMM 内存分解和 AgentCore 内部计量明细，无法准确划分剩余差额。

因此，这部分应描述为**应用进程之外的运行环境内存与计量口径差异**，而不是固定的“Firecracker 内存”。它也不是可以直接扣除的固定平台开销：启动过程、缓存状态、后台活动和计量时间窗口都可能使差额变化。

## 缓存、释放与读取时间

### 临时文件：两份缓存现象在此次 V2 中未复现

| 指标 / MiB | V1 baseline | V1 写读后 | V1 fadvise 后 | V2 baseline | V2 写读后 | V2 fadvise 后 |
|---|---:|---:|---:|---:|---:|---:|
| cgroup current | 17.375 | 547.066 | 283.818 | 13.031 | 278.047 | 14.793 |
| cgroup file | 4.035 | 517.273 | 261.273 | 0.000 | 256.168 | 0.168 |
| guest Cached | 663.018 | 1177.564 | 921.803 | 190.564 | 446.717 | 190.826 |

V2 写入并读回临时文件时，RSS 仍只有约 23 MiB，但 cgroup 和 guest 缓存仍增加约 256 MiB。**文件缓存仍是进程 RSS 之外的重要用量；改变的是倍增及回收表现，不是缓存从此不计入环境内存。** V2 AgentCore 内存遥测从 baseline 1.212 升至 1.858，fadvise 后降至 1.347，关闭后为 1.254；V1 则从 2.132 升至 2.916，关闭后仍为 2.884。

容器可见挂载也不同：V1 的 containerd snapshots 位于 `/dev/loop0` ext4；V2 的 containerd 位于 `/dev/vdc` ext4，应用根目录仍是 overlay。guest 内核分别为 `6.1.161-18.298.amzn2023.aarch64` 和 `6.1.166-24.303.amzn2023.aarch64`。这些证据支持平台存储/缓存路径发生变化，但没有 AWS host 级数据，不能证明每份内存的具体归属。

### 镜像文件：空载缓存少了，读取阶段出现长间隔

V2 padded baseline 的 guest Cached 约 191 MiB，读取完整镜像文件后约 448 MiB；cgroup file 从 0 增至约 256 MiB，再在 fadvise 后回到接近 0。V1 padded 在读取前 Cached 就约 1430 MiB，读取后仅增约 3 MiB，说明两种平台的预驻留情况明显不同。

探针在 baseline 阶段结束后执行 `open` 和完整文件读取，再进入 file_cached 阶段。该间隔 V2 为 **64.920 秒**，V1 为 **0.016 秒**；对应调用 E2E 分别为 **187.070 秒**和 **120.733 秒**。V2 从客户端发起到 probe started 仅约 2.080 秒，不能把额外 65 秒归为 Runtime 首次启动延迟。

这个间隔主要覆盖文件打开/读取，但包含调度等因素，不是专门的 I/O profiler。应用在读取循环内不采样，因此 30 个阶段的内存表**不包含这 65 秒的应用内存变化**。AgentCore 原始日志保留了这段时间的 65 条内存遥测，合计约 `0.02709224 GB-hours`，占该会话已观测用量约三分之一；不能用阶段表推断整次任务成本。后续若要定位，需要另行批准重复、冷热读对照或 I/O tracing，不在本次 10 次调用范围内。

### 匿名内存：应用回收明确，AgentCore 内存不能用线性增量解释

两平台分配时 RSS 都增加约 256.3 MiB，释放后回到约 23 MiB。V2 AgentCore 内存中位数为 1.177→1.243→1.057，分配增量仅 0.066，不能解释成“只计量了部分匿名内存”。baseline 和后台用量持续变化，AgentCore 内存遥测也不是单调历史峰值。原实验里释放后明显不回落的现象在此次 V2 匿名组未复现，甚至 V1 此次释放后也已接近其 baseline（2.154→2.164）。

## 验证、资源状态与复现

- 9 项新增离线测试、原实验 11 项测试全部通过；独立 `verify_memory_v2.py` 通过，检查精确负载矩阵、10 个唯一 UUID、同镜像配置、源 hash、930 个样本、30 个阶段、成功停止和 V2 删除。
- 独立只读复核从 1,374 条原始遥测记录重算全部应用及 AgentCore 内存统计，与保存结果一致；没有重复身份、未知 session 或资源 ARN 错配。原始日志无 schema 解析错误。所有内部窗口 duration 覆盖率约 99.992%–99.996%；另行检查每秒时间戳连续性，间隔约 0.993–1.001 秒，不仅依赖 duration 求和。
- 四个 Runtime 的原始日志 GB-hours 总量分别为 V1_small `0.297558904395815`、v2_small `0.106831448043914`、V1_padded `0.259428656532452`、v2_padded `0.107330550904692`，均与同查询窗口 CloudWatch Runtime 层 Sum 在浮点精度范围一致。endpoint 层是同用量的另一视图，未相加。总量包含不同启动/读取/尾部时长，不能直接当成公平的阶段内存或最终费用比较。已观测日志时间戳在 V1 停止请求后还延续约 66 秒，V2 约 12 秒；这也是不能直接拿全会话总量代表阶段内存差额的原因。
- 10 次 Invoke 均完成且响应通过验证；10 次 StopRuntimeSession 均返回 HTTP 200。两个临时 V2 Runtime 已删除，03:53 UTC 再次只读查询确认 ResourceNotFound；两个原 Runtime 仍 READY，镜像、角色、网络、协议和生命周期配置未变。
- V1/V2 共 8 个 stdout/usage 日志组均再次只读确认保留 7 天。新日志投递资源和日志留存，未删除原资源、角色、仓库或任何原始证据。可能继续产生日志存储费用。
- 本实验是 Runtime 内存测试，不创建 AgentCore Memory、不上传 Darwin 对话，也不调用大模型。

```bash
# 本地验证，无 AWS 请求
.venv/bin/python -B -m unittest test_memory_v2 -v
.venv/bin/python -B -m unittest discover -s ../23-runtime-memory-usage -p 'test_*.py' -v
.venv/bin/python -B verify_memory_v2.py .state-memory-v2/2026-09-11

# 以下仅供另一次获准的实测；会创建两个 Runtime、调用十次并删除新 Runtime
# 新输出目录必须不存在；不要在未确认时直接运行
.venv/bin/python -B -u memory_v2.py run --out .state-memory-v2/NEW_RUN
.venv/bin/python -B -u memory_v2.py collect --out .state-memory-v2/NEW_RUN --wait-seconds 3900
.venv/bin/python -B verify_memory_v2.py .state-memory-v2/NEW_RUN
```

`collect` 的 `complete` 只用于遥测窗口采集停止，不独立保证实验矩阵、响应验证或停止成功；必须通过上述独立 verifier 才接受实验。若执行中断，先读取保存状态再恢复清理；独立 `cleanup` 仅删除 owned Runtime，不重试日志保留设置，需另行检查保留期。创建请求若结果不明，保留原 name/clientToken 并只读核对，不自动重复创建。

## 解释边界

1. 每个负载每个平台只有一个会话，无法提供置信区间、统计显著性或跨区域结论。同一探针可用于发现现象，不能证明 V2 对所有镜像都修复了同类问题。
2. 独立 UUID 不等于独立冷启动；五个 V2 会话返回相同 boot ID，V1 也有重复值。这可能与快照有关，既不能证明共享实时内存，也不能证明每次都是冷启动。
3. 不同会话、甚至同会话的 AgentCore 内存 baseline 都存在漂移。2026-09-10 原报告 small 空载约 1.22，本次 V1 约 2.19，而应用 RSS 和 guest 缓存相近，说明不能仅靠历史值或 guest 指标重建 AgentCore 内存口径。**本报告的主比较是同期 V1 与 V2，不是跨日 V1 变化的根因分析。**
4. cgroup 的记账归属、guest Cached、RSS/PSS 与平台遥测观察范围不同；仍不能证明全部差额都是 page cache，更不能拿固定倍数估计最终账单。
5. “稳态”仅表示排除了阶段边缘，不保证 AgentCore 内存遥测已平衡。读取循环、响应序列化和停止尾部不在应用阶段采样表内；收齐阶段不代表所有最终尾部记录都已到达。
6. 官方明确区分遥测与权威账单；GB 的字节约定没有在该遥测页面明确，所以保持 GB-equivalent 标记。公开 microVM CPU/内存费率分别为 `$0.0895/vCPU-hour`、`$0.00945/GB-hour`，本报告不将其与 beta 遥测相乘后宣称最终费用。

参考：[原实验结果](../../23-runtime-memory-usage/RESULTS.md)、[AWS Runtime 遥测文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)、[AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)。
