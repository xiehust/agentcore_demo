# AgentCore Runtime 冷启动基准测试（ping-pong）

[English version / 英文版](README.md)

测量 Amazon Bedrock AgentCore Runtime 的**端到端冷启动延迟**随**容器镜像大小**
（≈500 MB / 1 GB / ~1.95 GB）与**调用并发度**（1 / 5 / 10 / 50）的变化。Agent 是运行在真实
`bedrock-agentcore` SDK（`BedrockAgentCoreApp`）上的最小 ping-pong 服务，不调用任何
LLM，因此测得的数字是纯基础设施延迟。

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
```
