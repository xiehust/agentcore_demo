# AgentCore Runtime 冷启动基准测试（ping-pong）—— 并与 AWS Lambda MicroVMs 对比

[English version / 英文版](README.md)

测量 Amazon Bedrock AgentCore Runtime 的**端到端冷启动延迟**随**容器镜像大小**
（≈500 MB / 1 GB / ~1.95 GB）与**调用并发度**（1 / 5 / 10 / 50）的变化。Agent 是运行在真实
`bedrock-agentcore` SDK（`BedrockAgentCoreApp`）上的最小 ping-pong 服务，不调用任何
LLM，因此测得的数字是纯基础设施延迟。

第二条测试线把**同一个 agent、同一套镜像梯度与并发梯度搬到
[AWS Lambda MicroVMs](https://aws.amazon.com/lambda/lambda-microvms/)** 上（2026 年 6 月 GA：
从已构建镜像的内存+磁盘快照恢复出 Firecracker VM）—— 见下文
[Lambda MicroVMs 测试线](#lambda-microvms-测试线) 与并排对比报告
[results/COMPARE.zh.md](results/COMPARE.zh.md) · [English](results/COMPARE.md)。

## 核心结果（2026-07-09，us-west-2）

| 镜像 | c=1 p50 | c=5 p50 | c=10 p50 | c=50 p50 | 真实微VM启动 p50 | warm p50 |
|---|---|---|---|---|---|---|
| 500 MB | 412 ms | 667 ms | 7,320 ms | 8,149 ms | ~7.4–8.2 秒 | ~100 ms |
| 1 GB | 732 ms | 763 ms | 11,401 ms | 11,422 ms | ~11.4 秒 | ~70 ms |
| 2 GB（实际 1,950 MB） | 1,176 ms | 1,155 ms | 13,458 ms | 13,493 ms | ~13.5 秒 | ~70 ms |

关键发现：使用全新 `runtimeSessionId` 调用**不一定**触发微VM启动。AgentCore 为每个
runtime 维护一个小型**预热池**（观测约 5 个实例，部署后几分钟内补充完成）。未命中
预热池、需要支付完整微VM启动成本的请求占比：并发 1 为 0%，并发 5 约 25–30%（中位数
仍是亚秒级），并发 10 约 55–60%，并发 50 约 82–86%。启动成本本身随镜像大小增长
（**压缩后每 +500 MB 约 +2–4 秒**）。Warm 请求（同 session 第二次调用）稳定在
~70–100 ms，与镜像大小无关。完整分析见
[中文报告 results/REPORT.zh.md](results/REPORT.zh.md) · [English report](results/REPORT.md)。

## 架构

```
coldstart_test.py ──InvokeAgentRuntime(全新 40 字符 sessionId)──▶ AgentCore Runtime
  (boto3, 关闭重试,                                                ├─ 每 session 一个微VM
   threading.Barrier 同步                                          │  （预热池实例 或
   N 个并发请求)                                                    │   真实启动: ECR 拉取 + 启动）
                                                                   └─ ping-pong 容器
      3 个 runtime，每个镜像大小一个：                                  （BedrockAgentCoreApp，
      coldstart_ping_500mb / _1gb / _2gb                              返回 pong + proc_start_ts）
```

Agent 返回 `proc_start_ts`（进程启动时刻）与 `request_ts`，因此每次探测都能被分类：
进程在请求期间启动 → **真实启动**；否则 → **预热池命中**。镜像大小通过不可压缩的
`/dev/urandom` 填充层（≤500 MB 分块）精确校准，因此 ECR 压缩大小 ≈ 未压缩大小。

## 前置条件

- 支持 ARM64 构建的 Docker（AgentCore 要求 linux/arm64 镜像）
- 具备 ECR、IAM 及 `bedrock-agentcore*` 权限的 AWS 凭证；区域 us-west-2
- [uv](https://docs.astral.sh/uv/) 与 Python ≥3.11

## 快速开始

```bash
bash scripts/build_images.sh        # 构建 3 个大小校准的 ARM64 镜像（约 2 分钟）
bash scripts/test_local.sh          # 本地合约测试：/ping + /invocations，3x PASS
bash scripts/deploy.sh              # ECR 推送 + IAM 角色 + 3 个 runtime -> deployments.json（幂等）
uv sync
uv run python coldstart_test.py --smoke   # 对 500mb runtime 做单次 冷+warm 探测
uv run python coldstart_test.py --full    # 完整矩阵（约 15 分钟，约 300 个 session）
python3 scripts/gen_report.py             # 从数据重新生成 REPORT.md 与 REPORT.zh.md
```

常用参数：`--sizes 500mb,1gb --concurrency 1,10 --rounds-c1 5 --out results/`，详见
`--help`。逐请求原始数据写入 `results/raw/*.json`，聚合统计写入 `results/summary.json`。

## 值得了解的平台事实

- **镜像大小上限 2048 MB**（Service Quotas："Maximum size for a Docker image in an
  AgentCore Runtime"）——因此 "2GB" 变体实际为 1,950 MB。
- 冷启动 = 使用全新 **33+ 字符** `runtimeSessionId` 的首次 `InvokeAgentRuntime`
  （每个 session 独享一个微VM）。在空闲超时内复用同一 sessionId 会命中同一个已热的微VM。
- `InvokeAgentRuntime` 配额：每 agent 200 req/s——并发 50 的突发完全在配额内
  （240 次探测仅出现 1 次限流）。
- `UpdateAgentRuntime` 会清空预热池；几分钟内自动补充。

## 成本说明

- Runtime 按微VM活跃秒数计费。每次探测在 warm 跟测后立即调用 `StopRuntimeSession`
  结束 session，且 runtime 创建时设置了 `idleRuntimeSessionTimeout=60` 兜底——
  跑一次完整矩阵的计算成本远低于 1 美元。
- 三个镜像的 ECR 存储约 3.1 GB（压缩）≈ 每月 $0.31。
- 三个 runtime 空闲时（无活跃 session）不产生费用。

## 清理

```bash
bash scripts/cleanup.sh --dry-run   # 列出将删除的 3 个 runtime + ECR 仓库 + IAM 角色
bash scripts/cleanup.sh --yes      # 实际删除（runtime -> ECR -> 角色）
```

## Lambda MicroVMs 测试线

### 核心结果（2026-09-04，us-west-2，客户端在同区域）

| 镜像 | c=1 p50 | c=5 p50 | c=10 p50 | c=50 p50 | 快 / 慢模式 | warm p50 | 挂起→恢复 p50 |
|---|---|---|---|---|---|---|---|
| 500 MB | 1,520 ms | 1,460 ms | 1,453 ms | 4,030 ms* | ~1.5 s（68%）/ ~3.5 s（32%） | 4 ms | 8.1 s |
| 1 GB | 1,510 ms | 1,500 ms | 1,502 ms | 2,224 ms* | 同上 | 4 ms | 8.1 s |
| 2 GB（实际 1,950 MB） | 3,419 ms† | 1,372 ms | 1,398 ms | 2,396 ms* | 同上 | 4 ms | 6.2 s |

\* 并发 50 撞上 `RunMicrovm` 配额（5 TPS / 突发 5）：50 次启动中 5–13 次被限流，其余在 API
内排队约 0.9 s。† n=10，恰好全部落入慢模式；2 GB 的并发 5/10 单元格与小镜像无差别。

关键发现：在 Lambda MicroVMs 上**镜像大小无关紧要**——每次启动都是恢复快照，填充层按需加载——
但冷启动呈**双峰分布**：约 68% 的启动在 ~1.5 s 内应答（入口代理把首个请求挂住约 1 s 直到应用可达；
guest 侧 `/run`→请求只有几十 ms），约 32% 需要 ~3.5 s（代理挂住约 3 s 后返回 **502**，紧接着的重试
立刻成功，而此时 guest 已就绪约 1.8 s）。同类对比下，AgentCore 的一次真实启动（8.1 / 11.4 / 13.5 s）
是 MicroVM 一次启动（全部单元格 p50 约 2.2 s）的 3.7–6 倍。热请求约 4 ms（同区域直连 VM 专属
HTTPS 端点），AgentCore 经 `InvokeAgentRuntime` 为 70–100 ms。完整分析见
[results/COMPARE.zh.md](results/COMPARE.zh.md)（脚本生成）与
[results/MICROVM_NOTES.zh.md](results/MICROVM_NOTES.zh.md)（默认配额、为何与镜像大小无关、快/慢模式解读）。

### 架构

```
microvm_coldstart_test.py ──RunMicrovm(镜像 vN)──▶ Lambda MicroVMs 控制面
  (boto3 lambda-microvms,      ◀── microvmId + https 端点           │ 恢复 Firecracker 快照
   关闭重试, Barrier 同步)      ──CreateMicrovmAuthToken──▶          │（内存+磁盘，构建时拍摄）
                               ──POST https://<端点>/invocations ──▶ VM ──▶ :8080 BedrockAgentCoreApp
                                 X-aws-proxy-auth, 遇 502 重试           │      :9000 生命周期钩子
                               ──TerminateMicrovm──▶                    └ /run 钩子 -> run_hook_ts
      3 个镜像，每个大小一个：coldstart-ping-microvm-500mb / -1gb / -2gb
      （由 Lambda 从 S3 zip 构建：microvm/Dockerfile + microvm/app.py）
```

`cold_ms` = 调用 `RunMicrovm` 起 → 首个 HTTP 200 完整响应体。Agent 返回 `proc_start_ts`
（镜像构建时间——进程在 `/ready` 后被快照）、`run_hook_ts`（Lambda 对这台 VM 调用 `/run` 的时刻）
与 `request_ts`，因此可以量化 guest 侧承担的那部分冷启动。`/validate` 会在测试 VM 上真实跑一次
`/invocations`，让 Lambda 预取热路径触及的快照页。

### 快速开始

```bash
bash scripts/test_local_microvm.sh                 # docker 合约测试：agent + 6 个钩子，PASS
bash scripts/deploy_microvm.sh                     # S3 桶 + 构建/执行角色 + 3 个镜像（约 3 分钟）-> deployments_microvm.json
uv run python microvm_coldstart_test.py --smoke --resume
uv run python microvm_coldstart_test.py --full --resume            # 并发 1,5,10 x 3 尺寸，约 10 分钟，150 台 VM
uv run python microvm_coldstart_test.py --full --concurrency 50 --sizes 500mb   # 每次只跑一个尺寸（等配额桶回填）
python3 scripts/gen_compare_report.py              # -> results/COMPARE.md + COMPARE.zh.md
bash scripts/cleanup_microvm.sh --dry-run | --yes  # VM -> 镜像 -> 桶 -> 角色
```

每次启动的原始数据在 `results/microvm/raw/*.json`，汇总在 `results/microvm/summary.json`
（schema 见模块 docstring）。

### 值得了解的平台事实

- **两种资源**：`MicrovmImage`（Lambda 在托管基础镜像 `al2023-1` 上执行你的 Dockerfile、启动
  `CMD`、等待 `/ready` 钩子后拍快照）与 `Microvm`（`RunMicrovm` → 专属 HTTPS 端点；令牌来自
  `CreateMicrovmAuthToken`，请求头 `X-aws-proxy-auth`，默认目标端口 8080）。`CreateMicrovmImage`
  没有 build args，所以部署脚本按尺寸改写 `PADn_MB` 默认值。
- **就绪 = 能应答**：`GetMicrovm.state` 最终一致，文档建议直接连接探测。应用可达前代理返回 502。
- **配额（本账号）**：`RunMicrovm` 5 TPS / 突发 5（可调），`SuspendMicrovm` 2 TPS，`TerminateMicrovm`
  10 TPS，`CreateMicrovmAuthToken` 50 TPS，MicroVM 总内存 1,024 GB，10 个并发镜像构建。仅 ARM64。
  基线 0.5–8 GB（默认 2 GB / 1 vCPU，4 倍纵向突发，8 GB 磁盘）。
- **快照语义**：构建时产生的一切（随机种子、ID、`proc_start_ts`）被同镜像的所有 VM 共享；用 `/run` 重新播种。
- **空闲策略**是成本保险：`maxIdleDurationSeconds=60, suspendedDurationSeconds=0` 让 VM 在最后一个请求
  后一分钟内被终止，即使客户端崩溃。`--resume` 探针改用 `autoResumeEnabled=true`。

### 成本说明

- VM 处于 RUNNING 时按 2 GB 基线每秒计费；每个探针在 warm（及 resume）后立即终止 VM，一次完整矩阵只
  有几个 VM 分钟。挂起的 VM 只收存储费。
- 镜像版本（3 个快照约 0.5 + 1 + 2 GB 磁盘）在删除前持续产生快照存储费——跑完请执行
  `cleanup_microvm.sh --yes`。

## 目录结构

```
app/main.py           ping-pong agent（BedrockAgentCoreApp）
docker/Dockerfile     基础镜像 + PAD1..4_MB urandom 填充层
scripts/build_images.sh  构建并校验 3 个镜像的大小
scripts/test_local.sh    本地 /ping + /invocations 合约测试
scripts/deploy.sh        ECR + IAM + 3 个 runtime（幂等），生成 deployments.json
scripts/gen_report.py    从记录数据重新生成 REPORT.md 与 REPORT.zh.md
scripts/cleanup.sh       资源清理（--dry-run | --yes）
coldstart_test.py        基准测试客户端（--smoke | --full）
deployments.json         生成文件：ARN + 镜像大小
results/                 原始探测数据、summary.json、REPORT.md、REPORT.zh.md

microvm/app.py               同一 agent + 生命周期钩子服务（:9000），用于 Lambda MicroVMs
microvm/Dockerfile           al2023-minimal 基础 + python3.12 + SDK + 填充层（EXPOSE 8080 9000）
scripts/test_local_microvm.sh  docker 合约测试，含 6 个钩子
scripts/deploy_microvm.sh      S3 + IAM + 3 个 MicroVM 镜像（幂等），生成 deployments_microvm.json
scripts/cleanup_microvm.sh     VM -> 镜像 -> 桶 -> 角色（--dry-run | --yes）
scripts/gen_compare_report.py  从两份 summary 重新生成 COMPARE.md 与 COMPARE.zh.md
microvm_coldstart_test.py      MicroVM 基准测试客户端（--smoke | --full [--resume] [--cw-logs]）
deployments_microvm.json       生成文件：镜像 ARN/版本、填充大小、角色、桶
results/microvm/               原始启动数据、summary.json、运行日志
results/COMPARE.md(.zh.md)     AgentCore vs Lambda MicroVMs 并排对比报告（脚本生成）
results/MICROVM_NOTES.md(.zh.md)  手写分析笔记：默认配额、镜像大小无关的原因、快/慢模式
```
