# 共享 Runtime Session `m7g.large`（2C / 8 GiB）长程任务容量测试报告

- 报告日期：2026-08-13（UTC）
- 区域：us-west-2
- 模型：`us.anthropic.claude-sonnet-4-6`
- 镜像：`launchpad-agents:shared-runtime-v1`（基于 Claude Agent SDK）
- Capacity Provider：`capacity_provider_arm_m7g_large-1HB6aXJTVr`
- Runtime：`shared_runtime_multiuser_m7g-EZpQed4lPW`（version 1）
- 实例类型：`m7g.large`（2C / 8 GiB，ARM64）
- Runtime 配置：`MAX_PARALLEL_AGENTS=40`、`MAX_TURNS=64`
- 持久卷：50 GiB gp3 `scratch`

## TL;DR

`m7g.large` 在长程 Web 项目 workload 下完成了 16、24、32、40 四档真实并发，
所有档位的 Agent 成功率和宿主机产物验证成功率均为 100%。

40 并发又在第二台独立实例上复测，结果仍为 40/40，但最低可用内存只有 425 MB。
因此：

- **本次已验证可运行上限：40**
- **建议的持续运行上限：32**
- **40 的定位：峰值边界，不建议作为长期稳定配置**
- **真正失败点：尚未确定，41+ 未测试**

CPU 平均值从 16 并发的 18.6% 增长到 40 并发的 37.9%，各档均出现 100% 瞬时
峰值；容量边界仍主要由内存决定，而不是 CPU 平均利用率。

## 1. 测试目标

此前 `c7g.large`（2C / 4 GiB）长程测试在 16 并发时最低可用内存只剩
573 MB，24 并发在两台不同实例上均为 0/24，并导致 SSM 失联。

本轮保持模型、镜像、任务、网络、存储和 Agent 配置不变，仅将 Capacity Provider
实例类型提升为 `m7g.large`，验证增加 4 GiB 内存后长程任务的容量变化。

## 2. 测试配置

### 2.1 Capacity Provider

新 Provider 复用原 Provider 的以下配置：

- Amazon Linux ARM64；
- 相同 IAM operator role 和 instance profile；
- 相同两个 VPC 子网和 security group；
- 50 GiB gp3 加密 EBS 卷；
- `idleInstanceTimeout=7200`；
- `maxLifetime=86400`。

唯一的计算规格差异：

```text
c7g.large: 2C / 4 GiB
m7g.large: 2C / 8 GiB
```

### 2.2 长程 workload

每个用户连续完成两个阶段：

1. 创建 4 文件的离线项目看板；
2. resume 同一 Claude session，增加 about 页面、执行 QA，并生成
   `loadtest.json`。

每个成功用户必须同时满足：

- 两阶段 marker 正确；
- session resume 链正确；
- workspace 和实例指纹一致；
- 6 个文件通过宿主机逐项校验；
- run token、HTML 引用、响应式断点和安全写法全部通过。

所有用户共用一个 `runtimeSessionId`，通过 barrier 同时释放。应用层
`MAX_PARALLEL_AGENTS=40`，因此 16、24、32、40 四档均为真实执行并发，不存在
16 槽 semaphore 排队。

## 3. 测试结果

### 3.1 并发爬坡

| 并发 | Agent 成功 | 产物验证 | p50 | p90 | 最大耗时 | CPU 平均/峰值 | 最低可用内存 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 16/16 | 16/16 | 398.7 s | 691.8 s | 788.0 s | 18.6% / 100% | 4507 MB |
| 24 | 24/24 | 24/24 | 442.5 s | 613.3 s | 675.7 s | 31.3% / 100% | 3123 MB |
| 32 | 32/32 | 32/32 | 429.4 s | 606.4 s | 755.7 s | 34.2% / 100% | 1738 MB |
| 40 | 40/40 | 40/40 | 451.4 s | 662.3 s | 830.8 s | 37.9% / 100% | 425 MB |

40 并发行使用第二台独立实例复测的数据，因为该轮使用改进后的单进程采样器，
获得了完整 CPU 和内存窗口。

### 3.2 40 并发重复性

| 轮次 | 实例 | Agent 成功 | 产物验证 | p50 | p90 | 最大耗时 |
|---|---|---:|---:|---:|---:|---:|
| 爬坡轮次 | `i-03a23f8802bd4be44` | 40/40 | 40/40 | 457.9 s | 700.9 s | 765.9 s |
| 独立复测 | `i-0f7dd10c1eaeb25e1` | 40/40 | 40/40 | 451.4 s | 662.3 s | 830.8 s |

两轮均满足：

- 40 个用户全部完成两个阶段；
- 40 个工作区均通过 6/6 文件校验；
- 所有用户命中各自轮次中的同一实例和容器；
- 未出现 SSE error、Runtime 重启或 SSM 失联。

## 4. 资源分析

| 并发 | 内存已用峰值 | 最低可用内存 | load1 峰值 | `node`/`claude` 进程采样峰值 |
|---:|---:|---:|---:|---:|
| 16 | 3073 MB | 4507 MB | 1.85 | 16 |
| 24 | 4456 MB | 3123 MB | 6.26 | 28 |
| 32 | 5840 MB | 1738 MB | 7.20 | 64 |
| 40 | 7341 MB | 425 MB | 12.27 | 50 |

结论：

1. 16 到 40 并发的内存峰值近似随活跃 Agent 数增加；
2. 32 并发仍保留约 1.7 GiB 可用内存，可吸收 Agent 行为波动；
3. 40 并发只剩 425 MB，已经接近无 swap 实例的危险区；
4. CPU 平均值并未饱和，但瞬时峰值始终可达 100%，load1 随并发明显上升；
5. 40 并发成功不等于具备足够生产余量。

## 5. 与 `c7g.large`（2C / 4 GiB）对比

| 并发 | `c7g.large`（2C / 4 GiB）结果 | `m7g.large`（2C / 8 GiB）结果 | 关键差异 |
|---:|---|---|---|
| 16 | 16/16，最低可用内存 573 MB | 16/16，最低可用内存 4507 MB | m7g 多约 3.9 GiB 余量 |
| 24 | 两次 0/24，SSM 失联 | 24/24，最低可用内存 3123 MB | 8 GiB 规格跨过原失败点 |
| 32 | 未做长程测试 | 32/32，最低可用内存 1738 MB | 可作为本轮建议配置上限 |
| 40 | 未做长程测试 | 两次 40/40，复测最低可用内存 425 MB | 功能可用但余量不足 |

本轮结果直接支持“长程任务的首要实例瓶颈是内存”这一判断。CPU 规格保持 2C
不变，仅将内存从 4 GiB 提升到 8 GiB，就把可运行并发从 16 提升到至少 40。

## 6. 容量结论

| 使用方式 | 建议 |
|---|---|
| 保守持续运行 | `MAX_PARALLEL_AGENTS=24` |
| 本轮建议容量上限 | `MAX_PARALLEL_AGENTS=32` |
| 峰值或隔离压测 | 40，可运行但必须监控内存低水位 |
| 不建议 | 将 40 作为无准入控制的长期稳定配置 |
| 未验证 | 41+ 的真正失败边界 |

若生产必须接近 40，应至少增加以下保护：

- 可用内存低水位准入；
- 队列上限和请求超时；
- 任务取消与每用户配额；
- SSM `ConnectionLost`、Runtime 无 complete 事件和容器重启告警。

## 7. 测试工具改进

第一轮 40 并发功能测试为 40/40，但原 shell 采样器在 40 档开始时提前终止，导致
该档资源窗口为空。采样器已改为单个 Python 进程直接读取 `/proc`：

- 每 2 秒采集 CPU、内存、load1 和 Agent 进程数；
- 不再反复启动 `vmstat`、`free` 和 `ps`；
- 降低采样器成为 OOM 优先终止对象的概率；
- 40 并发独立复测获得 415 个有效窗口样本。

## 8. 原始数据与复测命令

原始 JSON 是本地测试产物，已由 `.gitignore` 排除提交：

- `results/load_test_longrun_m7g_20260813T025207Z.json`
- `results/load_test_longrun_m7g40_20260813T034606Z.json`

复测：

```bash
cd 15-shared-runtime-instance

CAPACITY_PROVIDER_ARN="$(scripts/create_capacity_provider.sh)"

CAPACITY_PROVIDER_ARN="${CAPACITY_PROVIDER_ARN}" \
  RUNTIME_NAME=shared_runtime_multiuser_m7g \
  RUNTIME_CONFIG=runtime.m7g.json \
  MAX_PARALLEL_AGENTS=40 \
  MAX_TURNS=64 \
  SKIP_IMAGE_BUILD=1 \
  bash scripts/deploy.sh

RUNTIME_CONFIG=runtime.m7g.json \
  RESULT_TAG=m7g \
  LEVELS='[16,24,32,40]' \
  SUCCESS_FLOOR=0.75 \
  MONITOR_DURATION_S=14400 \
  TASK_READ_TIMEOUT_S=3600 \
  python3 scripts/load_test_longrun.py
```
