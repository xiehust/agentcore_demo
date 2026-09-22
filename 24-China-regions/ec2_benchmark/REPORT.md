# 宁夏 EC2 同区域冷启动与并发复测

日期：2026-09-22。两组件均直接从宁夏区 EC2 发起调用，
本地 us-west-2 工作站只负责 SSM 调度与结果下载，不在计时链路内。

## 实例与实测环境

| 项目 | 实测值 |
| --- | --- |
| AWS 账号 / profile | 447150580482 / agentcore_cn |
| EC2 | `i-0a685957c9b9355d7` |
| 区域 / 可用区 | `cn-northwest-1` / `cn-northwest-1a` |
| 实例规格 | `t3.small`，2 vCPU / 2 GiB，x86_64 |
| AMI | `ami-0cd8b949613a8418b` |
| Python / boto3 / botocore | 3.12.14 / 1.43.87 / 1.43.87 |
| 目标 endpoint | `https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn` |
| 最终实例状态 | **stopped** |

区域与实例 ID 由实例内 IMDSv2 Identity Document 验证，并与 EC2 DescribeInstances、
STS 实例角色身份交叉检查。安全组没有入站规则，通过 SSM 执行；
API 使用实例角色，没有复制本地静态密钥。两组件串行测试，SDK 自动重试均关闭。

## Runtime：相同 100 / 500 / 50 测试

| 验证项 | 目标 | 本轮宁夏 EC2 | 上轮宁夏 Code Interpreter 客户端 | 结果 |
| --- | --- | --- | --- | --- |
| 7.1 冷请求 P50，100 个样本 | < 3,000 ms | 1623.420 ms | 1685.153 ms | **PASS** |
| 7.2 冷请求 P99，100 个样本 | < 5,000 ms | 2008.168 ms | 1988.700 ms | **PASS** |
| 7.3 暖请求 P99，500 个样本 | < 200 ms | 154.920 ms | 151.128 ms | **PASS** |
| 7.4 从 0 个用户会话到 50 并发 | 错误率 0% | 0/50 失败 | 0/50 失败 | **PASS** |

651 次请求包括 100 次串行新会话请求、1 次暖会话准备请求、500 次暖请求、50 次扩容请求。
冷请求的首次调用标记各不相同，业务序号均为 1；暖请求维持同一标记，序号连续。
50 并发的客户端区间峰值重叠 50，
应用处理区间峰值重叠 50；
扩容阶段沿用 5 秒处理停留，仅检查错误率和重叠，不混入空载冷/暖时延。

Runtime 应用和基础镜像 digest 沿用上轮；新镜像与资源标识记录在本轮 resources.json。
独立比较确认根文件系统 layer 列表、容器 Config 和 app 源码均相同；
镜像 manifest digest 有变化，因此原始 digest 也一并记录，不把它们写成同一个 digest。
分位数为 nearest-rank，目标严格小于。

## Code Interpreter：相同 1 / 10 / 50 并发

下表单位为秒。每档使用新会话，全部首次执行完成后才统一停止该批会话。
统计沿用原脚本的线性插值，1 样本档位的分位数只代表一个观测值。

| 并发 | 成功 | Start P50 | Start P95 | 端到端 P50 | 端到端 P95 | 端到端 max | 上轮 us-west-2 客户端端到端 P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1/1 | 0.702 | 0.702 | 0.822 | 0.822 | 0.822 | 1.588 |
| 10 | 10/10 | 0.849 | 0.902 | 0.995 | 1.039 | 1.047 | 2.926 |
| 50 | 50/50 | 0.832 | 0.988 | 1.123 | 1.310 | 1.801 | 3.732 |

### 补充：100 个串行新会话

成功 100/100，失败 0，唯一 session 数 100。
此表单位为毫秒，使用 nearest-rank。

| 指标 | P50 | P95 | P99 | max |
| --- | --- | --- | --- | --- |
| 创建会话 | 776.137 | 929.526 | 1326.779 | 1339.233 |
| 首次执行 | 135.706 | 149.298 | 160.562 | 162.111 |
| 创建到首次执行完成 | 917.005 | 1071.575 | 1443.306 | 1485.827 |

Code Interpreter 的端到端包含 StartSession 和首次 Invoke 两次调用；
Runtime 在首次 Invoke 内完成用户会话分配，不能把两者数值直接当作完全相同 API 的性能比较。

## CPU 与测量边界

每秒读取 `/proc/stat` 和内存信息，busy 包含非 idle/iowait CPU tick，
steal 单列。以下为两组件测试期间采样，不含安装 Python / SDK 的启动准备。

| 阶段 | 样本数 | busy 平均 | busy P95 | busy max | steal max |
| --- | --- | --- | --- | --- | --- |
| code_interpreter_concurrency | 5 | 18.06% | 46.08% | 46.08% | 0.50% |
| code_interpreter_serial | 103 | 1.14% | 2.97% | 11.76% | 3.90% |
| runtime | 244 | 2.32% | 4.33% | 57.51% | 34.40% |

实例使用 T3 unlimited，CPU 积分模式与 CloudWatch 指标另存证据；
单次高 CPU 峰值不自动等于持续瓶颈，低 CPU 也不代表没有任何客户端开销。
本轮最初有 4 个约一秒采样区间的 steal 超过 10%，最高 34.40%，
与前三个 Runtime 冷请求重叠；这些请求全部保留在分位数计算中，没有剔除。
因此虽然已排除跨区域客户端链路，也不能把测试描述为完全没有客户端调度噪声。
本轮消除了“客户端与被测区域不同”的跨区链路，仍包含同区域网络、SDK、平台与应用时间。

上一轮 Runtime 已经由宁夏区 Code Interpreter 客户端运行；
这次替换为独立 EC2。上一轮 Code Interpreter 则由 us-west-2 工作站运行，
这次才改为同区域 EC2。跨天、CPU 架构、SDK 版本、客户端配置等也不同，
不能把数值变化全部归因于网络。此次“冷”仍指新用户会话首次响应，
不能观察或强制清空平台内部预热池。

## 清理与后续使用

用户选择停止并保留 EC2。当前状态为 `stopped`，保留资源如下：

```json
{
  "instance": "i-0a685957c9b9355d7",
  "instance_state": "stopped",
  "role": "cn-ec2-bench-8bbcd08d59",
  "profile": "cn-ec2-bench-8bbcd08d59",
  "security_group": "sg-0f7df7fe207ea4e73",
  "volumes": [
    "vol-06ff283c67f900d5b"
  ]
}
```

151 个 Runtime 会话、161 个 Code Interpreter 会话均取得成功 Stop 响应。
测试 Runtime、ECR 镜像/仓库、测试日志组和临时传输 S3 bucket 的删除结果见清理证据。
保留 EC2 的 EBS、角色、instance profile 和安全组；测试临时数据面授权已删除，
角色保留 SSM 托管权限。原有业务资源未修改。

重新启动实例：

```bash
aws ec2 start-instances --instance-ids i-0a685957c9b9355d7 \
  --profile agentcore_cn --region cn-northwest-1
```

再次压测需要重新部署被测 Runtime 并更新临时调用权限。
EC2 上 `/opt/cn-agentcore-benchmark/` 保留代码和结果，避免重跑时覆盖本轮目录。

## 证据与代码

- [执行计划](PLAN.md)、[运行说明](README.md)。
- [EC2 身份文档和客户端环境](results/20260922/collected/results/ec2_environment.json)。
- [EC2 配置与保留资源](results/20260922/resources.json)。
- [Runtime 原始结果](../runtime/results/20260922-ec2/benchmark_results.json)。
- [Code Interpreter 并发明细](../code_interpreter/results/20260922-ec2/concurrency.json)。
- [Code Interpreter 串行明细](../code_interpreter/results/20260922-ec2/serial_rows.json)。
- [CPU 原始采样](results/20260922/collected/results/cpu_samples.jsonl)。
- [CPU 积分模式与 CloudWatch 数据](results/20260922/ec2_cpu_metrics.json)。
- [短时 CPU steal 与请求重叠](results/20260922/cpu_spike_analysis.json)。
- [镜像文件系统与配置对照](results/20260922/image_comparison.json)。
- [独立重算与完整性审计](results/20260922/evidence_audit.json)。
- [停止与删除资源的独立复核](results/20260922/final_cleanup_audit.json)。

准备阶段首次安装因默认 Python 3.9 不满足固定 SDK 要求而失败，没有产生测量样本；
改用 Python 3.12 后执行正式测试。SSM 首次失败及后续执行响应均保留。
