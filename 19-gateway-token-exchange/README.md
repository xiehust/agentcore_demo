# 19 — AgentCore Gateway RFC 8693 Token Exchange

这个 Demo 展示 Amazon Bedrock AgentCore Gateway 调用 MCP Server 时的 OAuth 2.0 On-Behalf-Of（OBO）Token Exchange 完整流程，并包含可直接部署到真实 AWS 账号的脚本。

## 核心流程

```text
Client
  │ JWT A: sub=alice, aud=agentcore-gateway-demo
  ▼
AgentCore Gateway (CUSTOM_JWT)
  │ AgentCore Identity / RFC 8693 Token Exchange
  ▼
OAuth Authorization Server
  │ JWT B: sub=alice, aud=mcp-server
  │        act.sub=agentcore-obo-demo-client
  ▼
Protected MCP Server
  └─ tools/call: whoami
```

验证目标：

- 用户身份 `sub` 在交换后保持不变；
- Token audience 从 Gateway 切换为 MCP Server；
- `act.sub` 记录代表用户调用下游的工作负载；
- MCP Server 校验 JWT 签名、issuer、audience、过期时间和 scope；
- 错误 audience 无法直接调用 MCP Server。

## 目录

```text
19-gateway-token-exchange/
├── demo.py                         # 零依赖本地闭环
├── test_demo.py                    # 本地单元测试
├── .gitignore
└── aws/
    ├── deploy.py                   # 创建真实 AWS 资源并端到端验证
    ├── cleanup.py                  # 清理 deploy.py 创建的资源
    ├── lambda_function.py          # OIDC IdP + RFC 8693 + MCP Server
    ├── create-oauth2-credential-provider.json
    └── create-gateway-target.json
```

## 方式一：本地运行

要求 Python 3.10+，不需要安装第三方依赖：

```bash
cd 19-gateway-token-exchange
python3 demo.py --subject alice
```

运行测试：

```bash
python3 -m unittest -v test_demo.py
```

本地版本会启动两个仅监听 `127.0.0.1` 随机端口的 HTTP Server，分别模拟 OAuth Authorization Server 和受保护的 MCP Server。

## 方式二：部署真实 AWS 资源

### 前置条件

- AWS CLI v2；
- Python 3.10+；
- 已配置 AWS CLI profile，例如：

```bash
aws sso login --profile YOUR_PROFILE
aws sts get-caller-identity --profile YOUR_PROFILE
```

- 目标 Region 已支持 Amazon Bedrock AgentCore；
- 当前身份具有创建 KMS、IAM Role、Lambda Function URL、AgentCore Gateway、Credential Provider 和 Gateway Target 的权限。

### 部署

```bash
cd 19-gateway-token-exchange
python3 aws/deploy.py \
  --profile YOUR_PROFILE \
  --region us-west-2
```

脚本会创建独立且带时间戳的资源：

1. KMS RSA-2048 `SIGN_VERIFY` Key，用于 RS256 JWT；
2. Lambda Execution Role；
3. AgentCore Gateway Service Role；
4. Lambda Function URL，提供：
   - `/.well-known/openid-configuration`
   - `/.well-known/jwks.json`
   - `/oauth2/token`
   - `/demo/user-token`
   - `/mcp`
5. AgentCore OAuth2 Credential Provider：
   - `grantType=TOKEN_EXCHANGE`
   - `actorTokenContent=NONE`
6. `CUSTOM_JWT` AgentCore Gateway；
7. `DYNAMIC` MCP Server Target；
8. `initialize → tools/list → tools/call(whoami)` 端到端验证。

部署结果写入当前目录的 `.deployment.json`。文件只包含资源标识和验证结果，不保存 client secret 或 access token，并已加入 `.gitignore`。

### 预期结果

`whoami` 返回类似：

```json
{
  "delegatedUser": "alice",
  "actor": "agentcore-obo-demo-client",
  "audience": "mcp-server",
  "scope": "mcp:invoke"
}
```

## 清理

清理会删除 Gateway Target、Gateway、Credential Provider、Lambda、两个 IAM Role 和 KMS Alias，并将 KMS Key 安排在 7 天后删除：

```bash
python3 aws/cleanup.py
```

清理属于破坏性操作。执行前请检查 `.deployment.json`，确认其中只包含本 Demo 创建的资源。

## 安全说明

- `/demo/user-token` 是教学用途的公开 Token 签发端点，只应短期运行；
- Demo Token 只能用于本 Demo 的 audience 和 scope；
- client secret 由部署脚本随机生成，只写入 Lambda 环境变量及 AgentCore 托管 Credential Provider；
- `.deployment.json` 不包含密钥或 Token；
- 生产环境应使用企业 IdP、私有/受控 Token 签发流程、密钥轮换、告警和最小权限策略；
- MCP Server 必须验证 `iss`、`aud`、`exp`、签名和 scope，不能只检查 Token 是否存在。

## 协议版本

Demo 使用 MCP `2025-06-18` Streamable HTTP。MCP `2026-07-28` 使用新的 `_meta` 和 `Mcp-Method` 路由约定，需要相应调整客户端与 Server 的消息格式。

## 参考资料

- [MCP server targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html)
- [On-behalf-of token exchange with AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-behalf-of-token-exchange.html)
- [OAuth 2.0 Token Exchange — RFC 8693](https://datatracker.ietf.org/doc/html/rfc8693)
