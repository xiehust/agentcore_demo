# 共享 Runtime Session 短程任务无排队并发测试报告

- 测试日期：2026-08-13（UTC）
- 区域：us-west-2
- Runtime：`shared_runtime_multiuser-X10bCH6p6u`
- 测试版本：4
- 模型：`us.anthropic.claude-sonnet-4-6`
- 镜像：`launchpad-agents:shared-runtime-v1`（Agent 实现基于 Claude Agent SDK）
- 实例类型：`c7g.large`（2C / 4 GiB，无 swap）
- 测试配置：`MAX_PARALLEL_AGENTS=40`
- 测试后恢复配置：`MAX_PARALLEL_AGENTS=16`（Runtime version 5）
- 原始数据：`results/load_test_20260813T020930Z.json`

## 1. 测试目标

历史短程测试在 `MAX_PARALLEL_AGENTS=16` 下向 Runtime 同时发送最多 40 个请求。
40/40 虽然全部成功，但超过 16 的请求会在应用层 semaphore 排队，不能代表
40 个 Claude Agent 同时运行。

本轮临时将：

```text
MAX_PARALLEL_AGENTS: 16 -> 40
```

测试 16、24、32、40 个短任务真正同时运行时的成功率、延迟、CPU 和内存。

## 2. 测试方法

- 所有用户共用一个新的 `runtimeSessionId`；
- 每档用 barrier 同时释放 N 个请求；
- 每个用户 ID 包含唯一 run id；
- 每个请求显式 `reset=true`，不复用旧 Claude session；
- 提示词为 `Reply with exactly: pong`；
- 每档结束后立即写 checkpoint；
- SSM 每约 3 秒采集 CPU、内存、load 和 Claude 进程数；
- 成功率低于 80% 时停止后续档位。

计划档位：

```text
16 -> 24 -> 32 -> 40
```

32 并发成功率降到 0%，因此按停止条件没有继续执行 40。

## 3. 测试结果

### 3.1 总体结果

| 真并发 Agent | 成功率 | p50 | p90 | 最大延迟 | CPU 平均/峰值 | 内存已用峰值 | 内存最低可用 | 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 16/16 | 9.85 s | 10.63 s | 10.69 s | 68.5% / 100% | 2383 MB | 1258 MB | 16 |
| 24 | 24/24 | 14.63 s | 15.51 s | 16.16 s | 94.6% / 100% | 3192 MB | 447 MB | 25 |
| 32 | 0/32 | – | – | 全部约 829～830 s 后失败 | 无完整样本 | 无完整样本 | SSM 失联 | 无完整样本 |
| 40 | 未执行 | – | – | 32 档已触发停止条件 | – | – | – | – |

16 和 24 真并行均全部成功。24 并发时 CPU 平均值已达到 94.6%，最低可用内存
仅 447 MB，实例没有可接受的持续运行余量。

32 并发没有任何请求返回有效 complete event 或实例指纹。所有请求在约
829～830 秒后结束，SSM agent 同时失联。

### 3.2 与 16 槽排队测试对比

| 请求并发 | 模式 | 成功率 | p50 | p90 | CPU 平均 | 内存最低可用 |
|---:|---|---:|---:|---:|---:|---:|
| 16 | 16 槽，无排队 | 100% | 10.74 s | 11.31 s | 61.2% | 1270 MB |
| 16 | 40 槽，真并行 | 100% | 9.85 s | 10.63 s | 68.5% | 1258 MB |
| 24 | 16 槽，8 个排队 | 100% | 10.80 s | 15.22 s | 70.3% | 1448 MB |
| 24 | 40 槽，真并行 | 100% | 14.63 s | 15.51 s | 94.6% | 447 MB |
| 32 | 16 槽，16 个排队 | 100% | 14.75 s | 20.97 s | 78.1% | 1246 MB |
| 32 | 40 槽，真并行 | 0% | – | – | 无完整样本 | SSM 失联 |

对比说明：

1. 16 并发下两种配置接近，说明 semaphore 尚未形成队列；
2. 24 真并行没有降低延迟，反而使 p50 增加约 3.8 秒；
3. 24 真并行的最低可用内存比排队模式少约 1 GiB；
4. 32 请求在 16 槽排队模式下可 100% 完成，放开 40 槽后变为 0%；
5. 对 `c7g.large`，应用层排队是保护机制，不是单纯的性能限制。

## 4. CPU 与内存分析

### 4.1 16 真并行

- CPU 平均 68.5%，峰值 100%；
- 内存已用峰值 2383 MB；
- 最低可用内存 1258 MB；
- 16 个请求全部在约 7.0～10.7 秒内完成。

该档位有明确 CPU 压力，但内存仍保留约 1.2 GiB，可作为短任务的安全上限。

### 4.2 24 真并行

- CPU 平均 94.6%，峰值 100%；
- 内存已用峰值 3192 MB；
- 最低可用内存仅 447 MB；
- Agent 进程采样峰值 25；
- 24 个请求全部成功，但 p50 已达到 14.6 秒。

该档位可以完成一次突发测试，但 CPU 和内存都接近实例极限，不适合作为持续配置。

### 4.3 32 真并行

- 32/32 请求失败；
- 没有请求返回实例指纹；
- SSM 从测试开始附近停止心跳，最终为 `ConnectionLost`；
- EC2 system / instance status check 仍为 `ok`；
- EC2 CPU 指标没有显示持续 100%，但 SSM 失联后无法取得最后内存数据。

表现与长程任务 24 并发失败相似，强烈符合用户态内存或进程资源耗尽。由于无法读取
最后的 `dmesg`，不能将内核 OOM kill 写成已确认事实。

## 5. 容量结论

| 运行目标 | 建议 |
|---|---:|
| 短任务稳定运行 | 16 个活跃 Agent |
| 可接受高风险突发 | 24 个活跃 Agent |
| 不可用 | 32 个活跃 Agent |
| 40 并发请求 | 保持 16 槽并允许排队，不要放开 40 个活跃 Agent |

结论：

1. **已验证的稳定真并行上限为 16。**
2. **24 真并行可以完成，但资源余量过低。**
3. **32 真并行不可用，40 无需继续测试。**
4. **短任务 40 请求成功依赖 16 槽 semaphore 的排队保护。**
5. **当前 Runtime 应保持 `MAX_PARALLEL_AGENTS=16`。**

测试结束后已将线上 Runtime 恢复为 version 5、`MAX_PARALLEL_AGENTS=16`。

## 6. 测试脚本改进

`load_test.py` 已增加：

- 每次测试使用唯一 run id；
- 每个请求显式 `reset=true`；
- JSON 记录声明的服务端并发上限；
- 每档结束后原子写 checkpoint；
- 每档读取一次监控快照；
- 最终 SSM 失败时保留最后可用样本并继续写结果；
- 缺失 complete event 时记录明确错误。

32 并发导致 SSM 失联后，脚本仍成功保存 16、24 和 32 的请求结果。

## 7. 附录

### 原始数据

以下 JSON 是本地测试产物，已由 `.gitignore` 排除提交。

- `results/load_test_20260813T020930Z.json`：40 槽短程测试；
- `results/load_test_20260812T112652Z.json`：16 槽 2～20 并发；
- `results/load_test_20260812T113047Z.json`：16 槽 24～40 并发；
- `results/REPORT.md`：短程与长程统一报告。

### 复测命令

```bash
cd 15-shared-runtime-instance

# 临时放开到 40 个活跃 Agent
aws bedrock-agentcore-control update-agent-runtime ...

LEVELS='[16,24,32,40]' \
  SERVER_MAX_PARALLEL_AGENTS=40 \
  MONITOR_DURATION_S=2400 \
  TASK_READ_TIMEOUT_S=900 \
  python3 scripts/load_test.py

# 测试后恢复
MAX_PARALLEL_AGENTS=16 bash scripts/deploy.sh
```
