# 关切点 3：Agent 操作审计 —— tool call / network access 的监测

> 结论先行：审计要分**四层**采集，每层回答不同问题，AgentCore 原生覆盖前三层：
> ① 谁调用了 agent / 谁拿了 token（CloudTrail）；② agent 调了什么工具、传了什么参数、拿回什么
> （AgentCore Observability 的 OTEL span + 自定义审计 hook）；③ 工具调用是否被授权（Gateway + Policy/Cedar，
> 可 LOG_ONLY 只审计不拦截）；④ 出网去了哪（**只有 VPC 模式**才有 Flow Logs / DNS 日志 / Network Firewall，
> PUBLIC 模式下平台不提供出网明细，只能靠 sandbox 内自采）。

---

## 1. 四层审计矩阵

| 层 | 问题 | 数据源 | 粒度 | 是否原生 |
|---|---|---|---|---|
| L1 控制/数据面 API | 谁在什么时候调用了 `InvokeAgentRuntime` / `InvokeAgentRuntimeCommand` / `GetResourceOauth2Token` / `InvokeGateway` | AWS CloudTrail（Runtime、Identity 为管理+数据事件；Gateway `InvokeGateway` 需显式开数据事件，身份用 JWT `sub` 记录） | 每次 API 调用：caller、源 IP、request id、resource ARN | ✅ |
| L2 Agent 内部 | 每一步推理调用了哪个工具、输入/输出、耗时、token 数 | AgentCore Observability：Runtime 自动 OTEL 注入（Strands / LangGraph / CrewAI…），span 落 CloudWatch `/aws/bedrock-agentcore/runtimes/<id>-<endpoint>` 或 `aws/spans`；GenAI Observability 控制台；可 OTLP 导出到 Langfuse / Datadog / Dynatrace | span 级（`execute_tool`、`chat`、`invoke_agent`） | ✅（需开 CloudWatch Transaction Search） |
| L2' Agent 内部（确定性） | 与 L2 同，但要**保证记录、可拦截、可脱敏** | 框架 hook：Strands `BeforeToolCallEvent` / `AfterToolCallEvent` → JSON Lines 到 stdout（进 CloudWatch Logs）+ 写入当前 span 属性；见 [`demo/audit/tool_audit_hooks.py`](../demo/audit/tool_audit_hooks.py) | 每次工具调用，含 deny-list 决策 | 代码实现（本 demo） |
| L3 工具网关 | 这次工具调用**该不该**被放行；参数是否越权 | AgentCore Gateway + Policy Engine（Cedar）；`LOG_ONLY` 模式只记录"本来会被拒"的请求并出 CloudWatch 指标；REQUEST/RESPONSE interceptor Lambda 可做自定义审计/改写 | 每次 tool call：principal（IAM 或 JWT 用户）、action=工具名、参数 | ✅ |
| L4 网络 | sandbox 连了哪些 IP / 域名 / 端口 | **VPC 模式**：VPC Flow Logs（IP 五元组）、Route 53 Resolver query logs（域名）、AWS Network Firewall（TLS SNI 域名白名单 + alert 日志）、DNS Firewall；**PUBLIC 模式**：无 | 连接级 | VPC 模式 ✅ |
| L4' 网络（sandbox 内自采） | 某次工具调用触发了哪些出网连接 | 用户态 socket hook：[`demo/audit/network_audit.py`](../demo/audit/network_audit.py) 在 `socket.connect` 记录目的地并关联当前 tool span；可选 allowlist 直接拒绝 | 连接级 + 工具上下文 | 代码实现（本 demo） |
| L5 命令 | 运维通过 `InvokeAgentRuntimeCommand` 在 session 内执行了什么 | Runtime 把 request id + 命令原文写入 agent 的 CloudWatch 日志组；与 CloudTrail 用 request id 关联 | 每条命令 | ✅ |

> 官方安全最佳实践原文（[Security best practices for AgentCore Runtime → Auditing and monitoring](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)）
> 列出了 CloudTrail、命令审计日志、request id 关联、metric filter 告警、user-id 委托关系记录、VPC Flow Logs。

---

## 2. L2：AgentCore Observability 拿到的 tool call 数据

- Runtime 内启用 observability 后，平台自动用 ADOT + OpenInference 注入，Strands 的每次工具调用产生
  `execute_tool <name>` span，属性含 `gen_ai.tool.name`、输入参数、输出、`gen_ai.usage.*` token 数、latency。
- 平台自带的 `InvokeAgentRuntime` span 带 `session.id`、`aws.request_id`、`latency_ms`、`error_type`。
- 控制台：CloudWatch → GenAI Observability → Bedrock AgentCore → Agents / Sessions / Traces。
- 采样与成本：按 CloudWatch 价计费（Logs 摄入 + Transaction Search span 索引）。高频 agent 建议 head-based sampling，
  但**审计场景要求 100% 记录时用 L2' hook 把关键字段写 JSON Lines**，OTEL 采样不影响它。
- 导出：设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 等环境变量可同时（或改为）发到 Langfuse / Datadog 等
  （见 [`17-claude-sdk-evaluation`](../../17-claude-sdk-evaluation) 的 Langfuse 路径）。

## 3. L2'：确定性审计 hook（demo）

[`demo/audit/tool_audit_hooks.py`](../demo/audit/tool_audit_hooks.py) 提供 `ToolAuditHook(HookProvider)`：

```python
from strands import Agent
from tool_audit_hooks import ToolAuditHook, AuditPolicy

audit = ToolAuditHook(
    policy=AuditPolicy(
        deny_tools={"shell", "delete_file"},          # 直接取消工具调用并记录 decision=deny
        redact_keys={"authorization", "token", "password", "secret", "api_key"},
        max_value_chars=512,
    ),
    sink=print,   # 默认 stdout → CloudWatch Logs；可换成 Firehose / S3 写入器
)
agent = Agent(tools=[...], hooks=[audit])
```

每条记录形如：

```json
{"ts":"2026-09-03T07:30:12.412Z","event":"tool_call","phase":"after","session_id":"...","agent":"orderdesk",
 "tool":"http_request","tool_use_id":"tooluse_...","input":{"url":"https://api.github.com/...","headers":{"authorization":"[REDACTED]"}},
 "decision":"allow","status":"success","duration_ms":231,"output_preview":"{\"login\":\"octo\"}","trace_id":"...","span_id":"..."}
```

- 纯函数 `build_audit_record()` 可单测（本目录 `tests/`），HookProvider 只是把它挂到 Strands 生命周期。
- `decision=deny` 时设置 `event.cancel_tool = "<原因>"`，模型会收到取消说明，行为可控。
- 记录里带 `trace_id` / `span_id`，可与 L2 的 OTEL span、L1 的 CloudTrail request id 三方关联。

## 4. L3：Gateway + Policy 做"授权即审计"

- 把 GitHub / GitLab / 内部 API 全部作为 Gateway target（OpenAPI / Lambda / MCP server）暴露，agent 只能通过 Gateway 调工具
  → 工具调用天然有单一审计出口。
- Policy Engine 用 Cedar 写规则，例如"只有 `group=fsi-ops` 的用户能调 `github___create_pull_request`"；
  gateway 侧 `mode=LOG_ONLY` 先观察一周（CloudWatch 指标里能看到"若启用会被拒"的请求数），再切 `ENFORCE`。
- Interceptor Lambda（REQUEST）可以把每次调用写入客户自己的审计库（DynamoDB / OpenSearch），
  也能在这里做参数级 DLP。注意 MCP REQUEST interceptor 放行时**必须回显 body**（见 13 号 demo 的实测坑）。
- 计费：Policy $0.000025 / 授权请求；Gateway $0.005 / 1,000 次调用。

## 5. L4：网络访问审计 —— 必须用 VPC 模式

真机探测（[`results/fuse_probe.json`](../results/fuse_probe.json)）显示 sandbox 内是 root + 全 capability + 无 seccomp，
所以**不能**指望 sandbox 内的进程级管控挡住恶意/被注入的 agent 代码；网络管控必须放在 sandbox 之外：

1. Runtime `networkMode=VPC`，子网无 IGW，出网只经 NAT → **AWS Network Firewall**（TLS SNI 域名 allowlist，
   `github.com`、`gitlab.example.com`、`vault.internal` …），拒绝流量出 alert 日志到 CloudWatch/S3。
   参考 AWS 博客 *Control which domains your AI agents can access*。
2. **Route 53 Resolver query logging + DNS Firewall**：补 SNI 的盲区（直连 IP、DNS 隧道）。
3. **VPC Flow Logs**：全量五元组，到 S3 → Athena 查询；ENI 归属 Runtime 安全组即可过滤。
4. AWS 服务访问用 VPC endpoint（bedrock-runtime、bedrock-agentcore、s3、logs、sts、secretsmanager），
   endpoint policy 再限制到具体资源；无出网 VPC 的完整做法见 [`11-vpc-no-egress-workaround`](../../11-vpc-no-egress-workaround)。
5. sandbox 内 L4' hook 只作"带工具上下文的补充证据"，不是安全边界。

PUBLIC 模式的诚实说明：平台不向客户暴露 microVM 的出网明细，也没有 VPC Flow Logs；
只能靠 agent 自身日志（L2/L2'/L4'），因此**金融客户建议一律 VPC 模式**。

## 6. 落地清单

| # | 动作 | 证据 |
|---|---|---|
| 1 | 开 CloudTrail 组织 trail + Gateway 数据事件 | `GetResourceOauth2Token` 事件中 token 被隐藏、provider 可见 |
| 2 | Runtime 开 observability + CloudWatch Transaction Search | GenAI Observability 里看到 `execute_tool` span 及参数 |
| 3 | 部署带 `ToolAuditHook` 的 agent | CloudWatch Logs Insights `filter event="tool_call" and decision="deny"` |
| 4 | Gateway 挂 Policy Engine `LOG_ONLY` | CloudWatch 指标 `PolicyEngine…WouldDeny` 曲线 |
| 5 | Runtime 切 VPC + Network Firewall allowlist | 访问未列域名 → 连接超时 + NFW alert 日志 |
| 6 | Logs Insights 用 `aws.request_id` 关联 L1/L2/L5 | 一条 InvokeAgentRuntime 的完整时间线 |
