# AgentCore Runtime 中国区实测报告

2026-09-21，账号 `447150580482`，区域 `cn-northwest-1`。
**最终一轮四项验收全部通过**。
这里的冷启动和从 0 扩容采用“全新用户会话”的可观测口径，不能据此声称已清空平台内部预热池。

## 验收结果

| 编号 | 优先级 | 验证项 | 目标 | 实测 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 7.1 | P0 | 单并发首次请求 P50 | < 3,000 ms | 1685.153 ms | **PASS** |
| 7.2 | P0 | 单并发首次请求 P99 | < 5,000 ms | 1988.700 ms | **PASS** |
| 7.3 | P0 | 同一热实例 P99 | < 200 ms | 151.128 ms | **PASS** |
| 7.4 | P0 | 0 → 50 并发请求错误率 | 0% | 0/50，0% | **PASS** |

主测试共有 **651 次请求，151 个 Runtime 会话**，包含 1 次不计入热请求统计的准备调用。
样本窗口：`2026-09-21T16:39:30.768014+00:00` 至 `2026-09-21T16:43:41.798767+00:00`。
P50/P99 使用 nearest-rank；SDK 自动重试关闭；所有请求均保留。

## 测量环境

| 项目 | 配置 |
| --- | --- |
| 管理端 profile | `agentcore_cn` |
| 压测客户端 | 宁夏 `cn-northwest-1` 的 PUBLIC Code Interpreter，专用 IAM 执行角色 |
| 客户端 boto3 / botocore | 1.40.30 / 1.40.76 |
| 本地管理端 | us-west-2 工作站；不使用该工作站的网络延迟进行验收 |
| 被测服务 | Python 标准库 HTTP echo，无 LLM、无外部业务依赖 |
| 镜像架构 | linux/arm64 |
| ECR 压缩镜像大小 | 46,202,168 bytes，44.062 MiB |
| 镜像 digest | `sha256:98f8aed17302b93d66d5594402fbb44692d734a553f0dd8ad3834b714ed84460` |
| Runtime 网络 | PUBLIC，默认 IAM 签名认证 |
| 生命周期 | idle 60 秒，max lifetime 1,800 秒；测试结束主动 Stop |
| baseline Runtime | `cn_runtime24_9e54b7531d_baseline-Of9YWeDVCC`，版本 1 |
| scale Runtime | `cn_runtime24_9e54b7531d_scale-dKJ1ObA5TB`，版本 1 |

完整响应延迟从 SDK Invoke 调用前开始，到响应体完整读取结束，包含区内网络、服务转发、
实例准备及应用执行开销；不包含本地结果文件写入和 Stop 调用。
压测脚本经 Code Interpreter 在中国区内执行，管理工作站只负责部署、发起任务与下载结果。
没有向沙箱复制本地静态凭证。

## 延迟明细

单位均为毫秒。扩容阶段有意运行 5 秒工作负载，不能拿其延迟与空载冷/热请求直接比较。

| 阶段 | 成功 / 总数 | P50 | P95 | P99 | max |
| --- | --- | --- | --- | --- | --- |
| 新会话首次请求 | 100/100 | 1685.153 | 1936.903 | 1988.700 | 2137.438 |
| 同实例热请求 | 500/500 | 87.566 | 129.692 | 151.128 | 369.638 |
| 50 并发，含 5 秒处理 | 50/50 | 7091.433 | 7459.968 | 7517.941 | 7517.941 |

冷请求是串行 100 个新 session，全部请求序号为 1，并获得
100 个不同的首次调用实例标记。
热请求是同一 session 的 500 次后续调用，实例标记保持不变，请求序号从 2 连续至 501。
有 1 次热请求达到或超过 200 ms；验收对象为 P99，而不是最大值。

镜像构建、上传和 Runtime 的 CREATING → READY 等待单独发生在压测前，
没有混入首次 Invoke 的延迟。镜像规模、应用依赖、客户端位置和采样数量均会影响结果，
本次数据不代表任意业务 Agent 的性能，也不是长期 P99 保证。

## 0 → 50 并发扩容

scale 使用独立、从未调用过的 Runtime。测试前该 Runtime 没有本实验创建的用户 session。
50 个工作线程通过屏障同时发出不同 session 的首次调用，客户端发起时间跨度约 165.983 ms。
SDK 无自动重试，50/50 首次调用成功。

返回 50 个不同的首次调用实例标记，业务请求序号均为 1。
客户端请求区间峰值重叠 **50**，
应用处理区间峰值重叠 **50**，证明这批请求确实并发处理；
应用区间计算依赖各 guest 时钟基本同步。

“0”指用户会话数，不是服务内部预热实例数。没有公开 API 让客户端清空预热池，
本次也没有取得底层物理实例计数，所以不将这项扩大为“确认从 0 个物理实例启动”。

## 首轮发现的实例识别问题

首轮目录 `results/20260921-runtime/` 保留完整原始数据。
651 次调用均成功，冷请求 P50 1,709.498 ms、P99 2,354.795 ms，
热请求 P99 157.937 ms，50 并发错误率 0%，处理区间峰值重叠 50。
但 100 个新会话只返回两组启动时 UUID / guest boot ID，且业务请求序号全部为 1。

这与预初始化状态被复用的现象一致，启动时随机 ID 和 guest boot ID
不能可靠标识恢复后的独立会话。首轮脚本因此将 7.1、7.2、7.4 的样本完整性判为失败；
这些失败是测量假设不成立，不能解释为服务请求失败。

最终探针在首次业务请求时生成并保留 `instance_id`，核验跨会话唯一、
同会话保持不变及请求计数连续。启动时字段仍保留：
本轮冷请求的启动 UUID 只有 2 种，
首次请求实例标记却有 100 种。
该标记用于识别会话执行状态，不用于识别 AWS 物理宿主。
使用新镜像和新的 baseline / scale Runtime 完整复测，没有改变阈值、样本量、统计方法或计时范围，
也没有覆盖首轮的失败标记。

## 清理与复核

本轮 151 个 Runtime 会话的 Stop 均成功；压测 Code Interpreter 会话已停止，2 个 Runtime、1 个自定义 Code Interpreter、2 个专用 IAM 角色、1 个 ECR 仓库和本次 Runtime 日志组已删除。
首轮资源也已清理。两轮合计 302 个 Runtime 会话、2 个压测 Code Interpreter 会话，
4 个 Runtime、2 个自定义解释器、4 个 IAM 角色、2 个 ECR 仓库。
`StopRuntimeSession` 成功是 API 确认；本报告未将它描述为逐实例操作系统层的退出观测。
删除 Runtime 后再次查询得到不存在响应。

原账号已有的 Runtime 不属于本实验，未修改或删除。
结果文件保留资源标识、请求 ID、完整响应和清理响应；没有保存 AWS 密钥。

## 证据与复现

- [计划](PLAN.md)与[运行说明](README.md)。
- [最终汇总](results/20260921-runtime-v2/benchmark_summary.json)。
- [完整请求、响应与会话台账](results/20260921-runtime-v2/benchmark_results.json)。
- [逐请求 JSONL](results/20260921-runtime-v2/requests.jsonl)。
- [压测环境与执行身份](results/20260921-runtime-v2/benchmark_environment.json)。
- [镜像与 Runtime 配置](results/20260921-runtime-v2/resources.json)。
- [独立重算与完整性检查](results/20260921-runtime-v2/evidence_audit.json)。
- [清理响应](results/20260921-runtime-v2/cleanup.json)。
- [两轮资源独立清理复核](results/final_cleanup_audit.json)。
- [首轮原始汇总](results/20260921-runtime/benchmark_summary.json)。

在仓库根目录执行以下命令可从保存的数据重新计算并生成本报告：

```bash
python3 24-China-regions/runtime/analyze.py --output 24-China-regions/runtime/results/20260921-runtime-v2
```

协议背景参考：[AgentCore HTTP contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)。
中国区可用性和性能结论以本次真实调用为依据。
