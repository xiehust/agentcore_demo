# AgentCore Demo

本仓库收集了 Amazon Bedrock AgentCore 的动手示例与实测验证，覆盖 Runtime、Gateway、Identity、Memory、Evaluations、Payments 等能力。每个编号目录都是一个**独立的 demo**，有各自的依赖与说明；构建与运行的通用步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)，具体命令以各目录内的 README 为准。

> ⚠️ 大多数 demo 会在真实 AWS 账号中创建资源并产生费用，用完请按对应目录的清理步骤删除资源。

## 目录索引

### 入门与基础部署

| 目录 | 简介 |
|---|---|
| [00-claudecode_agent](00-claudecode_agent/README.md) | 把 Claude Code 打包成容器部署到 AgentCore Runtime，用于代码生成、修复等开发任务；含 IAM/ECR 准备脚本与 MCP 配置。 |
| [01-async-agent](01-async-agent/) | 最小的异步长任务示例：用 `@app.async_task` 在 Runtime 中跑后台任务（状态变为 `HealthyBusy`），并通过脚本查询运行状态。无 README。 |
| [02-vpc](02-vpc/README.md) | 在 VPC 私有子网中部署 AgentCore Runtime（经 NAT 出网，配 ECR/S3/Logs/Bedrock VPC 端点），附 VPC 创建、部署、调用与清理脚本。 |
| [03-GA-new-features](03-GA-new-features/) | GA 新功能示例集合：`01-A2A`（编排 Agent 调用多个子 Agent 的 A2A 协议）、`02-MCP-as-Gateway`（Gateway 接入 MCP Server/Lambda target，含 Cognito 鉴权与 Quip MCP）、`03-memory`（Memory 短期记忆、记忆分支与 LangGraph checkpointer）。 |
| [06-agentcore_harness](06-agentcore_harness/) | 用 AgentCore CLI 创建的几个 Harness 项目样例（`labquotaadvisor`、`myresearchagent`、`test01`），包含 `agentcore.json` 项目配置与 Agent 代码。 |

### Gateway 与 MCP

| 目录 | 简介 |
|---|---|
| [05-mcp-proxy](05-mcp-proxy/README.md) | 把飞书 MCP Server 通过 FastMCP 代理部署到 AgentCore Runtime（MCP 协议），使用 Cognito 鉴权、Secrets Manager 存放飞书应用凭证。 |
| [19-gateway-token-exchange](19-gateway-token-exchange/README.md) | Gateway 调用受保护 MCP Server 时的 RFC 8693 On-Behalf-Of Token Exchange；提供零依赖本地闭环和真实 AWS 部署/清理脚本。 |

### Identity 与鉴权

| 目录 | 简介 |
|---|---|
| [13-private-idp-workaround](13-private-idp-workaround/README.md) | AgentCore Identity 无法访问私有 IdP 时的绕行方案：用 VPC 内的 interceptor Lambda 验签入站 JWT、工具 Lambda 自行换取出站令牌，9/9 项验证通过。 |
| [20-consent-portal](20-consent-portal/README.md) | 介绍 Identity 的托管 Consent Portal，并与自建回调的 3LO 方案对比；附 Google Calendar 可部署示例。 |

### 网络与安全

| 目录 | 简介 |
|---|---|
| [11-vpc-no-egress-workaround](11-vpc-no-egress-workaround/README.md) | 针对中国区无 VPC Egress 的两种方案（Lambda 桥接、API Gateway + VPC Link）的实测：在零出网 VPC 中让 Gateway 读到私有 RDS，并记录文档勘误。 |
| [12-strands-nova2-runtime](12-strands-nova2-runtime/README.md) | Strands + Nova 2 Lite 部署到 PUBLIC 模式 Runtime，经 Gateway 的 MCP 工具访问 `11` 中零出网 VPC 里的私有 RDS，端到端可运行。 |
| [22-runtime-mtls-verify](22-runtime-mtls-verify/README.md) | 验证 Runtime 在隔离子网内以 X.509 客户端证书（mTLS）访问自建 K3s API；含实验与清理记录。 |
| [18-runtimes-3rd-fs](18-runtimes-3rd-fs/README.md) | 面向金融客户关切的调研与 demo：Runtime 中的三方文件系统（FUSE）替代方案、sandbox 内无秘钥访问 GitHub/GitLab/Vault、tool call 与网络访问审计。 |

### Runtime 性能、冷启动与容量

| 目录 | 简介 |
|---|---|
| [04-PressureTest](04-PressureTest/runtime/README_TEST.md) | Runtime 并发压测客户端，支持本地 HTTP 与已部署 Runtime 两种模式，统计延迟分位数与 token 用量；附架构、指标与排障文档。 |
| [10-runtime-coldstart](10-runtime-coldstart/README.zh.md) | 测量 Runtime 端到端冷启动延迟随镜像大小与并发度的变化，并与 AWS Lambda MicroVMs 做同条件对比。 |
| [14-instances](14-instances/) | 单次测量 Instances（Capacity Provider 缩容到零）计算类型的端到端冷启动延迟，结果在 `results/`。无 README。 |
| [15-shared-runtime-instance](15-shared-runtime-instance/README.md) | 多个用户共享同一 Runtime session（EC2 Capacity Provider 实例）的应用层隔离方案与容量测试，给出不同机型的建议并发。 |
| [16-shared-runtime-microvm](16-shared-runtime-microvm/README.zh.md) | 多用户共享同一 microVM Runtime session 的短程/长程并发测试，以及基于 DynamoDB + Router + reconciler 的 Session Pool 可运行实现。 |
| [21-runtime-v2-beta](21-runtime-v2-beta/README.md) | Runtime V2（`platformVersion="V2"`）的可用性、冷启动、多进程并发与内存验证，包含 Claude Agent SDK 分阶段延迟子项目。 |
| [23-runtime-memory-usage](23-runtime-memory-usage/README.md) | 对比应用 RSS、cgroup 内存与 AWS 用量遥测，验证 Runtime 内存计费口径；实测结果见 `RESULTS.md`。 |
| [24-China-regions](24-China-regions/VALIDATION_REPORT.md) | 中国区（宁夏、北京）Code Interpreter 与 Runtime 冷启动/并发压测验证报告，子目录含各项测试代码、原始结果与证据。 |
| [25-patch-manager-china](25-patch-manager-china/README.md) | 中国区 Patch Manager 批次 × AgentCore 自动值守实测：VPC 模式 Runtime + SourceVpce 收口、跨账号 Hub-Spoke（session tag 绑定单实例）、每台失败实例一个 session 的按实例流水线编排（Step Functions）、自建记忆独立模块 agentmem（DynamoDB STM + OpenSearch/Titan LTM，对齐 AgentCore Memory 策略，与 agent 代码解耦、可复用）、代码 + IAM 护栏、会话隔离实测；附客户方案说明 SOLUTION.zh.html 与架构设计 ARCHITECTURE.zh.html。 |

### 评估、优化与支付

| 目录 | 简介 |
|---|---|
| [07-agentcore_payment](07-agentcore_payment/README.md) | Strands agent 借助 AgentCore Payments 自动为 x402 付费接口结账（Base Sepolia 测试网），含本地运行与 Runtime 部署。 |
| [09-eval-optimization](09-eval-optimization/README.zh.md) | 部署托管 Harness 客服 Agent，用 AgentCore Evaluations 打分、Recommendations 优化提示词，演示"观测 → 评估 → 改进"闭环。 |
| [17-claude-sdk-evaluation](17-claude-sdk-evaluation/README.md) | Claude Agent SDK 的 trace 导出到 Langfuse，再提交给 AgentCore `Evaluate` API 评分，并演示 Recommendations 的使用限制。 |

> 注：仓库中没有 `08` 目录。

## 快速开始

1. 准备 AWS 账号与可访问 Bedrock AgentCore 的凭证，安装 Python 与 [uv](https://docs.astral.sh/uv/)；带 `Dockerfile` 的 demo 还需要支持 `linux/arm64` 的 Docker。
2. 进入感兴趣的目录，阅读其中的 README，按说明安装依赖、部署和运行。
3. 完成后按 README 中的清理步骤删除创建的 AWS 资源。

## Checks

每次推送和提交拉取请求时，GitHub Actions 会运行 `./scripts/check.sh`，检查仓库根目录的 `README.md`、`CHANGELOG.md` 和 `docs/FAQ.md` 是否存在。也可在本地运行该脚本；缺失文件时脚本会列出文件名并以状态码 1 退出。

## 许可证

[MIT License](LICENSE)
