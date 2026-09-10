# AgentCore Runtime 内存实验结果

## 结论

**能探测到 AWS Runtime 资源用量遥测与应用内存之间非常大的差异，但不能把遥测直接称为最终计费内存。**

本次小镜像应用仅约 22 MiB RSS，AWS 资源日志约 1.2 GB-equivalent；镜像多放 256 MiB 文件，即使应用不读取，RSS 仍约 22 MiB，AWS 遥测却约 2.3 GB-equivalent。缓存负载进一步显示：RSS 几乎不变，cgroup 和 AWS 遥测显著增长。

结论支持“进程以外的环境内存/缓存会造成巨大差额”，也支持控制镜像体积和初始化 I/O 的必要性。**尚不能证明全部差额都是 page cache，更不能用当前 RSS、guest MemAvailable 或一个固定倍数重建 AWS 最终账单。**

## 实验与证据

- 日期：2026-09-10，区域 `us-west-2`，AgentCore Runtime 默认 microVM 类型。
- 调用窗口：03:41–03:49 UTC 左右；两个 Runtime、五个独立会话、串行执行。
- 相同 Python 3.12 标准库探针，无模型、Agent SDK 或第三方应用依赖。基础镜像固定 digest。
- 小镜像 ECR 压缩体积 46,204,449 bytes（约 44.1 MiB）；填充镜像 314,721,975 bytes（约 300.1 MiB）。差额来自 256 MiB 随机文件；该组 baseline 不读取此文件。
- 应用每秒采样，默认每个阶段 30 秒。最终返回 465 个样本（62 + 93 + 124 + 62 + 124）。进程内 `smaps_rollup.Rss` 作为表中 RSS。
- 对齐方法：同一个 `session.id`，每阶段首尾各排除 3 秒，比较余下约 24 秒窗口内中位数。不使用全账号聚合，不用分钟总量冒充瞬时内存。
- `.state/resources.json` 保存实际 ARN、镜像 URI、session UUID 和调用/停止记录；每个 UUID JSON 保存全部应用原始样本；`.state/telemetry.json` 保存 AWS 原始日志和分钟指标；`.state/comparison.json` 保存完整统计。该目录不提交 git。

### 真实 AWS 日志结构

此次实际字段是 `attributes.time_elapsed_seconds`，**不是文档字段列表中的 `elapsed_time_seconds`**。内存值位于 `metrics`，session 位于 `attributes`：

```json
{
  "event_timestamp": 1789011626195,
  "attributes": {
    "session.id": "<session UUID>",
    "time_elapsed_seconds": 1.00
  },
  "metrics": {
    "agent.runtime.memory.gb_hours.used": 0.000199856996384
  }
}
```

实际 `event_timestamp` 是 epoch 毫秒，与 CloudWatch 外层事件时间一致。多数 duration 为 1.00 秒，少量边缘记录是 0.99、0.32 或 0.00。**0.00 秒仍可能有非零用量**，不能除以零，也不能丢弃其 GB-hours；分析器保留总量，但不生成这类记录的 gauge。

本报告的 AWS GB-equivalent 按下式折算：

```text
memory.gb_hours.used × 3600 ÷ time_elapsed_seconds
```

它是遥测给出的区间内存用量等效值。AWS 页面未明示此处 GB 的字节约定，所以表中不将其直接标成 GiB/MiB，也不把它当作最终账单。稳态样本均为约 1 秒间隔。

## 稳态对照

以下全部是对应稳态窗口中位数。应用/环境单位为 **MiB**；AWS 单位为日志换算的 **GB-equivalent**，不是直接测量的 RSS。15 个阶段的内部窗口均有约 100% 的 AWS 逐秒日志覆盖，无解析错误。

| 镜像 / 负载 | 阶段 | RSS MiB | cgroup current MiB | guest Cached MiB | AWS GB-equivalent |
|---|---|---:|---:|---:|---:|
| small / 空载 | baseline | 22.22 | 17.40 | 662.93 | 1.222 |
| small / 空载 | idle_control | 22.50 | 18.87 | 664.58 | 1.197 |
| small / 匿名内存 | baseline | 22.21 | 17.12 | 662.14 | 1.206 |
| small / 匿名内存 | 分配 256 MiB | 278.50 | 275.38 | 664.69 | 1.458 |
| small / 匿名内存 | 释放后 | 22.95 | 19.33 | 665.05 | 1.411 |
| small / 临时文件 | baseline | 22.22 | 16.98 | 661.62 | 1.184 |
| small / 临时文件 | 写入并读回 256 MiB | 23.45 | 547.07 | 1175.54 | 1.964 |
| small / 临时文件 | fadvise 后 | 23.79 | 283.82 | 920.78 | 1.965 |
| small / 临时文件 | 关闭后 | 24.16 | 284.19 | 921.17 | 1.965 |
| padded / 不读填充文件 | baseline | 22.22 | 16.99 | 1429.83 | 2.322 |
| padded / 不读填充文件 | idle_control | 22.50 | 18.89 | 1432.64 | 2.327 |
| padded / 读镜像文件 | baseline | 22.22 | 17.00 | 1430.09 | 2.500 |
| padded / 读镜像文件 | 读取 256 MiB 后 | 23.46 | 19.85 | 1433.02 | 2.505 |
| padded / 读镜像文件 | fadvise 后 | 23.80 | 20.20 | 1177.26 | 2.479 |
| padded / 读镜像文件 | 关闭后 | 24.17 | 20.57 | 1177.75 | 2.479 |

注意 padded 的两个独立会话在读取前就存在约 0.178 GB-equivalent 的 baseline 差异，所以不能把跨会话的这段差额说成应用读取的成本。读取时 guest 缓存几乎不再增长，说明对应页面可能早已驻留；fadvise 后 Cached 下降约 256 MiB，AWS 等效值只下降约 0.026。缓存和遥测之间不是一一对应的简单公式。

## 如何解释

### 1. 已证实：应用 RSS 严重低估此次 Runtime 资源遥测

小镜像空载，应用 RSS 约 22 MiB，cgroup 约 17–19 MiB，而 AWS 侧约 1.2 GB-equivalent。即便考虑 GB/GiB 单位差异，差距仍然在数十倍量级。这里不是 Python 堆突然变大，而是观察范围不同。

cgroup 甚至小于进程 RSS，并不矛盾：文件页可能早已由平台加载，内存记账归属在父级/其他 cgroup，但映射进了应用地址空间。仅凭这个容器可读的 cgroup，不能重建整个 VM 或 AWS host 的内存。

### 2. 已证实：未读取的大镜像也会提高本次环境缓存和 AWS 遥测

两个 baseline 组应用 RSS 基本相同；镜像额外增加 256 MiB 文件后，guest `Cached` 从约 663 MiB 增至约 1430 MiB，差约 767 MiB，而 AWS 遥测从约 1.2 增至约 2.3 GB-equivalent。**镜像影响不只表现为应用进程 RSS。**

可读挂载信息显示 containerd overlayfs 快照位于 `/dev/loop0` 的 ext4 文件系统上。压缩层、解压文件、loopback 后端文件可能产生多层缓存，这与观测相符；但本实验没有 AWS host 级归属数据，**不能证明每一份缓存具体对应哪一层，更不能宣称镜像每增 1 MiB 必然增加固定倍数的计费内存**。每组只有一个会话，也未区分所有预热/冷启动路径。

### 3. 已证实：文件缓存可能远大于应用内统计

小镜像写入并读取 256 MiB 临时文件，RSS 只增加约 1.2 MiB，cgroup `memory.stat.file` 却从约 3.7 MiB 增到约 517 MiB；guest `Cached` 增加约 514 MiB。这与 loopback 文件系统多层缓存的解释一致，但不是缓存层归属的严格证明。

按文件调用 `FADV_DONTNEED` 后，cgroup file 降到约 261 MiB，仍比 baseline 多约 258 MiB。建议清缓存并没有释放所有环境缓存，关闭临时文件后仍未立即回到 baseline。AWS 等效内存仍保持在约 1.965，而 RSS 只有约 24 MiB。

### 4. 已证实：释放应用内存不等于 AWS 遥测回到原点

匿名分配 256 MiB 时，RSS 增量约 256.28 MiB；AWS 遥测增量约 0.252 GB-equivalent。这个量级与二进制 0.25 GiB 很接近，但系统噪声和 telemetry 精度使本次数据不足以把单位约定当成规范。

释放后 RSS 从约 278.5 降到约 23.0 MiB，cgroup 也降至约 19.3 MiB；AWS 仍约 1.411，相较分配前约 1.206 保留明显差额。官方当前定价采用截至每秒的峰值并包含系统开销，这能解释为何不能用当前 RSS 估算账单。

**但本次 AWS 遥测本身不是严格的单调历史峰值。** 空载约 1.222 降为 1.197；匿名组分配期间约 1.458，释放后约 1.411。因此不能直接断言 `USAGE_LOGS` 就是内部计费峰值，也不能把差额全部归因为某一个缓存指标。平台的测量、计量与最终对账仍有区别。

### 5. 仍然不能从本实验得到什么

- AWS 物理 host 内存、hypervisor RSS，以及 cache 的逐层归属。
- 精确的最终计费内存和账单金额：AWS 明确声明遥测不等于权威计费数据。
- 完整会话 GB-hours 除以应用执行秒数不是公平比较：日志中存在早于首次 Invoke 的启动记录，也存在停止请求后的收尾记录。本报告只做对齐的阶段比较，不把启动、暂停间隙或结束窗口误算为应用稳态开销。
- 不能把一次大镜像实验推广到所有镜像尺寸、基础镜像、区域或 Runtime 实现版本。独立复核发现四个会话记录了相同 boot ID，可能与快照来源有关；独立 UUID 不证明独立冷启动，也不能据此认定共享实时内存。
- “稳态”是排除边界后的比较窗口，不代表完全平衡。部分启动缓存变化延续到约第 3 秒；匿名组的 AWS 遥测在释放前就开始下降，因此不能把全部下降因果归于 `mmap.close()`。
- 独立复核确认本次每个窗口是连续的 24 条完整秒记录，间隔约 0.999–1.001 秒。分析器的通用 coverage 指标使用 duration 求和，不是 interval union；对未来存在重叠/缺口的日志仍应额外检查时间间隔。

生产建议：同时跟踪进程 RSS/PSS、cgroup anon/file、guest Cached 和官方 GB-hours；缩减镜像与初始化读写；显式停止不再使用的会话。不要通过 RSS 很小或已经 `free()` 就认定资源计费也很小。

## 验证与资源状态

- `python3 -m unittest -v`：11 项通过，包含真实驻留内存分配/释放、文件负载、HTTP 健康状态/输入限制、调用失败仍停止会话、采集中断恢复、CloudWatch 查询点数限制、遥测 schema 和零间隔处理。
- Docker ARM64 构建和容器内采样通过；两个 Runtime 均达到 READY。
- 五个真实 Invoke 全部完成；五次 `StopRuntimeSession` 全部返回 HTTP 200。生命周期限制为 idle 60 秒、maxLifetime 600 秒；未创建长期实例或调用大模型。
- 截至 04:06:23 UTC 的采集包含 759 条用量日志（small 491，padded 268），覆盖全部 15 个稳态阶段。实际日志入库延迟约 16.9–18.6 分钟。
- 同一查询时间窗内，small 原始日志 GB-hours 之和为 `0.200013768884223`，CloudWatch Runtime 层 Sum 为 `0.200013768884232`；padded 对应 `0.169900611842584` 与 `0.169900611842590`。两条独立采集路径在浮点精度范围内一致，验证了 GB-hours 的聚合方式。
- endpoint 层与 Runtime 层提供相同用量的不同维度视图，**不能将两者相加**。以上总量只是当次已投递遥测，不是最终账单；session 收尾记录仍可能晚到。
- 应用 stdout 和 usage 四个日志组均已确认 7 天保留期。
- 为保留可复现条件，两个 Runtime 定义、一个 ECR 仓库、执行角色和日志投递资源暂未删除。ECR 镜像与日志仍有少量存储费用；原始证据保留在 `.state/`。没有更改相邻实验的资源或代码。

实现、指标限制及文档来源见 [README.md](README.md)。
