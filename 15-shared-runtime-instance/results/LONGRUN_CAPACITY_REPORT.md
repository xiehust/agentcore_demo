# 共享 Runtime Session 长程任务容量测试报告

- 日期：2026-08-12（UTC）
- 区域：us-west-2
- 项目：`15-shared-runtime-instance/`
- Runtime：`shared_runtime_multiuser-X10bCH6p6u`
- 模型：`us.anthropic.claude-sonnet-4-6`
- 实例类型：`c7g.large`（2C / 4 GiB，无 swap）
- 容量测试配置：`MAX_PARALLEL_AGENTS=24`
- 测试后恢复配置：`MAX_PARALLEL_AGENTS=16`
- 原始数据：
  - `results/load_test_longrun_20260812T151832Z.json`（8 / 16 并发）
  - `results/load_test_longrun_20260812T150257Z.json`（24 并发复测）

## 1. 测试目标

在已经验证 2 用户长程任务并发正确性的基础上，继续测量单个共享
`runtimeSessionId` 在 `c7g.large`（2C / 4 GiB）上承载长程 Web 项目任务的容量边界。

本轮重点回答：

1. 8、16、24 个用户同时执行 5～10 分钟级任务时的成功率；
2. 并发升高后的 p50、p90 和最大任务耗时；
3. CPU、内存和 Agent 子进程数量的变化；
4. `MAX_PARALLEL_AGENTS=24` 是否能在 4 GiB 实例上稳定运行；
5. 应用层并发上限应该保留在 16，还是可以提高到 24。

## 2. 测试方法

### 2.1 工作负载

每个用户连续完成两个阶段：

1. `foundation`：创建任务看板的 `index.html`、`styles.css`、`app.js` 和
   `README.md`；
2. `final-qa`：恢复上一阶段 Claude session，增加 `about.html`，完成全量检查并
   创建 `loadtest.json`。

每个用户最终必须生成 6 个文件。成功条件同时包括：

- 两个阶段 marker 正确；
- 第二阶段正确恢复第一阶段 session；
- 两阶段工作区和容器进程指纹一致；
- SSM 宿主机检查确认 6 个文件存在；
- HTML 引用、响应式断点、`localStorage`、`textContent` 和 run token 校验通过。

### 2.2 并发配置

容量测试期间临时将服务端：

```text
MAX_PARALLEL_AGENTS: 16 -> 24
```

然后测试：

```text
8 -> 16 -> 24
```

8/16 档使用同一共享 session 连续执行。24 档在两个不同共享 session、两台不同
EC2 实例上各执行一次，以排除单实例偶发故障。

### 2.3 资源采样

通过 SSM 在承载实例上每约 3 秒采集：

- CPU 平均值和峰值；
- 内存已用峰值和最低可用值；
- 1 分钟 load average；
- Claude / Node Agent 进程数。

测试脚本在每档结束后原子写入 checkpoint。即使宿主机因压力导致 SSM 失联，
请求级结果也不会丢失。

## 3. 测试结果

### 3.1 总体结果

| 并发用户 | Agent 成功 | 产物验证 | 成功率 | p50 | p90 | 最大耗时 | 平均 tool calls | distinct instances |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 8 | 8/8 | 8/8 | 100% | 441.8 s | 515.2 s | 546.8 s | 38.2 | 1 |
| 16 | 16/16 | 16/16 | 100% | 402.0 s | 553.4 s | 647.1 s | 27.6 | 1 |
| 24 | 0/24 | 0/24 | 0% | – | – | – | 0.5 | 0 |

8 和 16 并发均全部完成两个阶段，session resume、工作区一致性和宿主机产物校验
全部通过。24 并发没有任何用户完成第一阶段。

### 3.2 延迟

| 并发用户 | 最快任务 | p50 | p90 | 最慢任务 |
|---:|---:|---:|---:|---:|
| 8 | 342.7 s | 441.8 s | 515.2 s | 546.8 s |
| 16 | 277.4 s | 402.0 s | 553.4 s | 647.1 s |
| 24 | 480.1 s 后失败 | – | – | 492.4 s 后失败 |

16 并发的 p50 低于 8 并发，属于生成式模型执行波动，不能解读为并发越高越快。
更有代表性的指标是 p90 和最大耗时：16 并发下分别增长到约 9.2 分钟和
10.8 分钟。

### 3.3 资源使用

| 并发用户 | CPU 平均 | CPU 峰值 | 内存已用峰值 | 内存最低可用 | load1 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|
| 8 | 13.3% | 100% | 1764 MB | 1876 MB | 1.26 | 9 |
| 16 | 20.2% | 100% | 3067 MB | 573 MB | 2.58 | 16 |
| 24 | 无完整样本 | 无完整样本 | 无完整样本 | SSM 失联 | 无完整样本 | 无完整样本 |

16 并发已将可用内存压缩到 573 MB，仅约占物理内存的 15%。实例没有 swap，
继续增加 8 个 Claude 子进程缺少足够内存余量。

CPU 在 8 和 16 并发下都出现 100% 瞬时峰值，但平均值仍不高。24 并发失败时
EC2 CPU 监控峰值约 54%，说明 CPU 不是唯一或首要失败信号。

## 4. 24 并发失败分析

### 4.1 第一次失败

- 实例：`i-01aaff80816bca518`
- 24 个请求在 14:41:42 UTC 同时进入服务端；
- 24/24 均未完成 `foundation`；
- 所有请求约 718～726 秒后失败；
- 平均 tool calls 仅 0.6；
- SSM 在 14:41:21 后变为 `ConnectionLost`；
- Runtime log stream 只有 24 条 request 日志，没有后续 complete/error；
- EC2 system / instance status check 始终为 `ok`。

该轮使用旧版结果脚本，最终 SSM 采集失败导致 JSON 未落盘，但 CloudWatch、SSM
命令历史和客户端输出保留了失败证据。

### 4.2 独立复测

- 实例：`i-0f97367334b1d6102`
- 新共享 session、新 run id；
- 24/24 均未完成 `foundation`；
- 所有请求约 480～492 秒后失败；
- 平均 tool calls 仅 0.5；
- 0 个 workspace、0 个实例指纹、0 个有效产物；
- SSM 在 15:01:38 后变为 `ConnectionLost`；
- EC2 system / instance status check 仍为 `ok`；
- 修复后的 checkpoint 成功保存完整请求结果。

两台不同实例重复出现相同行为，排除了单台 EC2 偶发故障。

### 4.3 失败性质

现有证据强烈符合用户态内存或进程资源耗尽：

1. 16 并发时最低可用内存已降到 573 MB；
2. 24 并发增加 8 个 Claude 子进程后，所有任务几乎停止推进；
3. SSM agent 同时失联，但 EC2 内核和基础健康检查仍正常；
4. CPU 没有持续打满；
5. 两台独立 `c7g.large` 都能复现。

由于实例失联后无法读取 `dmesg`，本次不能把内核 OOM kill 作为已确认事实。
准确表述应为：**24 并发触发了可重复的用户态资源耗尽或失活，内存压力是最强嫌疑。**

## 5. 容量结论

| 运行目标 | 建议并发上限 |
|---|---:|
| 保守运行，保留内存余量 | 8～12 |
| 已验证可完成的硬上限 | 16 |
| 不可用配置 | 24 |
| 尚未细分的边界 | 17～23 |

结论：

1. **8 并发稳定。** 成功率 100%，最低可用内存约 1.8 GiB。
2. **16 并发可完成，但已接近资源边界。** 成功率 100%，最低可用内存仅 573 MB，
   p90 约 9.2 分钟，最大约 10.8 分钟。
3. **24 并发不可用。** 两次跨实例复测均为 0% 成功，且 SSM 到测试结束仍未恢复。
4. **当前已确认的最大可用并发是 16。** 生产或持续测试建议设置为 8～12。
5. **不能把轻量 `pong` 的 40 并发结果用于长程任务。** 两种 workload 的进程
   生命周期、内存驻留和工具调用量不同。

测试后已将 runtime 和 `scripts/deploy.sh` 的默认值恢复为：

```text
MAX_PARALLEL_AGENTS=16
```

## 6. 测试脚本改进

24 并发首次失败时，宿主机 SSM 采集失败导致结果脚本在写 JSON 前退出。已修复：

1. 每档 `run_level` 返回后立即写 agent checkpoint；
2. 宿主机产物校验后再次写 checkpoint；
3. 监控快照成功后再次写 checkpoint；
4. JSON 使用临时文件加原子 replace；
5. 最终监控采集失败时保留最后可用样本并照常写完整结果；
6. 没有 workspace 时跳过无意义的 SSM 产物命令。

独立 24 并发复测证明该修复有效：即使 SSM 再次失联，完整请求结果仍成功落盘。

## 7. 后续建议

1. 如需精确定位边界，可在隔离测试环境继续测 18、20、22；
2. `m7g.large`（2C / 8 GiB）复测已完成，24、32、40 并发均 100% 成功；40 并发最低
   可用内存仅 425 MB，建议持续运行不超过 32，详见
   `results/LONGRUN_M7G_REPORT.md`；
3. 为实例配置 swap 只能作为缓冲，不应替代合理的并发上限；
4. 服务端可增加基于可用内存的动态 admission control，而不是只依赖固定 semaphore；
5. 对 SSM `ConnectionLost`、可用内存低水位和 runtime 无 complete 事件增加告警；
6. 容量报告应始终区分轻量短请求和长程文件任务。

## 8. 已知边界

- 17～23 并发未逐档测试，因此真实临界点位于该区间但尚未精确定位；
- 生成式模型执行时间有较大方差，延迟比较需要更多重复样本；
- 24 并发时 SSM 失联，无法获得最后时刻的内存和内核日志；
- 两台失联实例因 IAM 显式 deny 无法由当前测试身份执行 `RebootInstances`；
- 测试结论仅适用于当前模型、Agent 实现、文件 workload 和
  `c7g.large`（2C / 4 GiB）。

## 9. 附录

### 原始数据

以下 JSON 是本地测试产物，已由 `.gitignore` 排除提交。

- `results/load_test_longrun_20260812T151832Z.json`：8 / 16 并发完整结果；
- `results/load_test_longrun_20260812T150257Z.json`：24 并发完整失败结果；
- `results/load_test_longrun_20260812T134627Z.json`：2 并发基线；
- `results/LONGRUN_M7G_REPORT.md`：`m7g.large`（2C / 8 GiB）纵向扩容复测；
- `results/REPORT.md`：短程与长程任务统一测试报告。

### 复测命令

```bash
cd 15-shared-runtime-instance

# 安全基线
LEVELS='[8,16]' \
  MONITOR_DURATION_S=7200 \
  TASK_READ_TIMEOUT_S=2400 \
  python3 scripts/load_test_longrun.py

# 24 并发边界复测，仅限隔离测试环境
MAX_PARALLEL_AGENTS=24 bash scripts/deploy.sh
LEVELS='[24]' \
  MONITOR_DURATION_S=5400 \
  TASK_READ_TIMEOUT_S=2400 \
  python3 scripts/load_test_longrun.py

# 测试后恢复安全上限
MAX_PARALLEL_AGENTS=16 bash scripts/deploy.sh
```
