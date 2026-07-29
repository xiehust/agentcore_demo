# AgentCore 无 VPC Egress / 无 Private IdP 支持时的落地指南

> Markdown 版指南，内容与同目录的 `AgentCore 中国区无 VPC Egress Workaround 方案.html` 一致。
> 方案一、方案二与 Private IdP 绕行均已于 2026 年 7 月 29 日在 us-east-2 完成真机验证。
> 可复现代码见 [`11-vpc-no-egress-workaround/`](.)、[`12-strands-nova2-runtime/`](../12-strands-nova2-runtime)、
> [`13-private-idp-workaround/`](../13-private-idp-workaround)。

**适用场景**：Bedrock AgentCore 在某区域首发时（尤其中国区宁夏 `cn-northwest-1`、北京 `cn-north-1`），
Runtime 与 Gateway 尚未提供基于 **Amazon VPC Lattice** 的原生私有出站（VPC Egress），
且 AgentCore Identity 无法连接 VPC 内的 **Private IdP**。本文给出不依赖上述能力的落地路径。

---

## 目录

- [1. 结论](#1-结论)
- [2. Egress 与 Ingress 的区分](#2-egress-与-ingress-的区分)
- [3. 方案总览矩阵](#3-方案总览矩阵)
- [4. 方案一 · Lambda 桥接（首选）](#4-方案一--lambda-桥接首选)
- [5. 方案二 · API Gateway + VPC Link](#5-方案二--api-gateway--vpc-link)
- [6. Runtime 侧：将出站访问下沉为工具](#6-runtime-侧将出站访问下沉为工具)
- [7. 入站（Ingress）私有连接](#7-入站ingress私有连接)
- [8. Private IdP 绕行（interceptor Lambda）](#8-private-idp-绕行interceptor-lambda)
- [9. 勘误清单（9 条实测发现）](#9-勘误清单9-条实测发现)
- [10. 选型建议](#10-选型建议)
- [11. 迁移到中国区的检查清单](#11-迁移到中国区的检查清单)
- [12. 如何复现](#12-如何复现)
- [13. 参考来源](#13-参考来源)

---

## 1. 结论

**访问 VPC 内的私有资源，不依赖 Gateway 与 Runtime 自身的 VPC Egress 能力。**

验证环境的 VPC 构造为零互联网出口：无 Internet Gateway，无 NAT Gateway，
路由表中亦不含 `0.0.0.0/0`。在该网络条件下，AgentCore Gateway 仍可读取私有
RDS MySQL（`PubliclyAccessible=false`）的真实数据。对于 AgentCore Identity 无法访问的私有 IdP，
则由 interceptor Lambda 承担入站验签与出站令牌交换。

| 能力 | 绕行方式 | 实测结果 |
|---|---|---|
| Gateway → 私有资源 | 方案一：Lambda target（Lambda 挂 VPC） | ✅ 直调 p50 **730 ms**，经 Runtime 上的 Agent p50 **1924 ms** |
| Gateway → 私有资源 | 方案二：区域型 REST API + VPC Link → NLB | ✅ 直调 p50 **737 ms**，经 Runtime 上的 Agent **1820 ms** |
| Identity ← Private IdP | interceptor Lambda 自行验签、换令牌 | ✅ **9/9** 验证用例通过 |

上述两条链路各完成两层验证：本地 MCP 客户端直调，以及部署在 `networkMode=PUBLIC`、
未配置任何 `vpcConfig` 的 AgentCore Runtime 上的 Agent 端到端调用。
Runtime 侧的实现方式见[第 6 节](#6-runtime-侧将出站访问下沉为工具)。

> 按官方文档原文配置存在缺陷。实测共发现 **9 条**文档记载错误或未记载的行为，
> 其中 4 条涉及安全或功能（勘误 1、2、6、9）。详见[第 9 节](#9-勘误清单9-条实测发现)。

---

## 2. Egress 与 Ingress 的区分

方向判断错误将导致方案完全不适用。

| 方向 | 含义 | 对应能力 | 本文重点 |
|---|---|---|---|
| **Egress（出站）** | Agent 或 Gateway **主动访问**你 VPC 内的私有资源，如 RDS、内部 API、自建 MCP | VPC Lattice `privateEndpoint`；Runtime VPC 配置（ENI） | ✅ 核心 |
| **Ingress（入站）** | 你 VPC 内的应用**私有调用** AgentCore 的 API，不经公网 | Interface VPC Endpoint、PrivateLink | 见[第 7 节](#7-入站ingress私有连接) |

Egress 之所以构成难点，在于它要求 AgentCore 托管服务进入你的 VPC，
底层依赖 VPC Lattice resource gateway 与 ENI 的编排能力，而新区域最容易缺失的正是该编排。
Lambda、API Gateway 这类中间层已位于你的 VPC 内，AgentCore 只需通过骨干网端点调用它们，
即可规避对 egress 的依赖。

> **两者同源。** 官方文档明确说明，Private IdP 的私有连接
> *“is established using Amazon VPC Lattice … **following the same pattern used by
> AgentCore Gateway VPC egress**”*。因此 VPC Lattice 缺失时，
> 访问私有数据资源与连接 Private IdP 会同时失效，绕行思路亦相同：
> 将“进入 VPC”这一步下沉到本身即可进入 VPC 的中间层。

---

## 3. 方案总览矩阵

| 方案 | 数据路径 | 需 VPC Egress | 私有性 | 改造成本 | 推荐度 | 实测 |
|---|---|---|---|---|---|---|
| **① Lambda 桥接** | Gateway → Lambda(挂 VPC) → 私有资源 | **否** | 高 | 低 | **首选** | ✅ 730 ms |
| **② API GW + VPC Link** | Gateway → 区域 REST API → VPC Link → NLB → 私有资源 | **否** | 高 | 中 | **首选** | ✅ 737 ms |
| **③ Private IdP 绕行** | interceptor Lambda 在 VPC 内验签、换令牌 | **否** | 高 | 中 | IdP 在内网时必选 | ✅ 9/9 |

> 730 与 737 ms 包含本地客户端进程启动，以及一次完整的 MCP `initialize` 握手，
> 并非纯服务端耗时。两条链路的差异位于噪声范围内，不应作为选型依据。

---

## 4. 方案一 · Lambda 桥接（首选）

Gateway 原生支持 **Lambda target**，官方描述为开箱即用、无需额外配置。
Gateway 经 AWS 骨干网 invoke Lambda，而 Lambda 自身挂载至你的 VPC（`VpcConfig`），
从而可访问 RDS、ElastiCache 与内部 API。

```
Agent → AgentCore Gateway ──(骨干网 invoke)──▶ Lambda(挂 VPC) ──▶ RDS / 内部 API / 自建 MCP
```

### 为何不依赖 VPC Egress

Gateway 调用 Lambda 属于 service-to-service invoke，Gateway 无需进入你的 VPC。
“进入 VPC”由 Lambda 自身的 VPC 能力完成，该能力早已成熟，中国区完整支持。
因此整条链路不使用 AgentCore 的任何 egress 特性。

### 落地要点

- Lambda 配置 `VpcConfig`，至少两个可用区的私有子网与安全组，且置于可路由至目标资源的子网。
- Gateway 执行角色遵循最小权限，仅允许 invoke 指定的 Lambda ARN，不应使用 `lambda:InvokeFunction *`。
- Lambda 若需访问 S3、DynamoDB 等 AWS 服务而子网无 NAT，配置对应的 VPC Endpoint 即可。
- 打包依赖时需注意 CPU 架构。`pymysql` 为纯 Python，不受影响；`cryptography` 等含二进制的包
  必须按目标架构构建，详见[第 9 节](#构建期注意事项)。

### 实测结果

| 项目 | 值 |
|---|---|
| 端到端 p50 | 730 ms |
| 硬证据 | MySQL `USER()` 返回 `agentadmin@10.30.11.161`，即 Lambda ENI 在私有子网内的地址 |
| Schema 校验 | Gateway 会强制校验 `toolSchema`，将 `limit` 传为字符串会被 `ValidationException` 拒绝 |
| 端到端（经 Runtime） | 部署于 `networkMode=PUBLIC` Runtime 的 Strands Agent，4 个用例全部命中，p50 **1924 ms**（1680–2197 ms） |

**优点**：零 egress 依赖、开箱即用、流量私有、成本低。
**限制**：受 Lambda 15 分钟超时与负载上限约束；长连接与流式 MCP 场景需另行评估；
工具逻辑需封装进 Lambda。

---

## 5. 方案二 · API Gateway + VPC Link

该模式由官方针对“无原生 VPC egress”场景提供。Gateway 经骨干网调用 API Gateway，
再由 **VPC Link** 进入 VPC。

```
Agent → Gateway ──(骨干网)──▶ 区域型 REST API ──▶ VPC Link ──▶ 内网 NLB ──▶ 私有资源
```

### 落地要点

以下三条均已实测确认：

- Gateway 仅支持区域型（regional endpoint）REST API 作为直接 target。
- VPC Link 的后端必须为 NLB，REST API 的 VPC Link 不支持 ALB。
- 出站鉴权仅有两种：IAM（SigV4，使用 Gateway 执行角色）或 API Key，不支持 OAuth 与跨账号。

另有两条易被遗漏：

- 创建 target 时必须提供 `apiGatewayToolConfiguration.toolFilters`，
  该字段为必填，见[勘误 3](#勘误-3--apigatewaytoolconfigurationtoolfilters-是必填项)。
- 资源策略需按**执行角色 ARN** 收敛，不可按 `aws:SourceArn` 编写 `Deny`，
  否则将导致 Gateway 自身被拒绝，见[勘误 1](#勘误-1安全-deny-绝不能用-awssourcearn-收敛)。

### 可用的资源策略（中国区分区为 `aws-cn`）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAgentCoreGatewayRole",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws-cn:iam::ACCT:role/my-gateway-exec-role" },
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws-cn:execute-api:cn-northwest-1:ACCT:api-id/stage/*/*"
    },
    {
      "Sid": "DenyEveryoneElse",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws-cn:execute-api:cn-northwest-1:ACCT:api-id/stage/*/*",
      "Condition": {
        "ArnNotEquals": {
          "aws:PrincipalArn": "arn:aws-cn:iam::ACCT:role/my-gateway-exec-role"
        }
      }
    }
  ]
}
```

### target 配置（`toolFilters` 必填）

```json
{
  "mcp": {
    "apiGateway": {
      "restApiId": "xxxxxxxxxx",
      "stage": "prod",
      "apiGatewayToolConfiguration": {
        "toolFilters": [
          { "filterPath": "/orders", "methods": ["GET"] }
        ],
        "toolOverrides": [
          { "name": "listOrders", "description": "...",
            "path": "/orders", "method": "GET" }
        ]
      }
    }
  }
}
```

### 实测结果

| 项目 | 值 |
|---|---|
| 端到端 p50 | 737 ms |
| 硬证据 | MySQL `USER()` 返回 `agentadmin@10.30.11.109`，即私有子网内 EC2 的地址 |
| 收敛验证 | 无凭证直调返回 `403 Missing Authentication Token`；账号内管理员经 SigV4 直调返回 `403 explicit deny`；经 Gateway 调用返回 `200` 与真实数据 |
| 端到端（经 Runtime） | 同一 Agent 指定该链路，获取到 EC2 私有 IP `10.30.11.109`，**1820 ms**（需注意[勘误 9](#勘误-9--apigateway-target-注入-basepath-参数模型填充后返回-403)） |

### 私有 REST API 的变体

若 API Gateway 为私有（private）REST API，则不能直接作为 target。官方 workaround 是将其导出为
OpenAPI schema，以 **OpenAPI target** 方式使用，并将 `routingDomain` 指向 API Gateway 的 VPCE DNS。
但该变体本身依赖 `privateEndpoint` 与 VPC Lattice，区域缺失时同样不可用，
只能退回方案一。

---

## 6. Runtime 侧：将出站访问下沉为工具

本节不构成第三条方案，而是 Runtime 侧的实现方式。方案一与方案二说明的是 Gateway 如何进入 VPC，
本节回答另一个问题：**Runtime 中的 Agent 代码本身需访问私有资源，而 Runtime 不具备 VPC
配置能力时，应如何实现。**

原则是不由 Agent 直接出站，而将出站访问下沉为一个工具。具体有两种实现：
一是将所有需私有访问的操作改为调用 Gateway 工具，后端接方案一的 Lambda 或方案二的 API Gateway；
二是将主要逻辑外置至挂载 VPC 的 Lambda、ECS 或 EKS，Runtime 中的 Agent 仅负责编排与对话。

该方式不具备独立的数据路径，其实现即方案一或方案二，相应的端到端验证已归入上述两节。
[`12-strands-nova2-runtime/`](../12-strands-nova2-runtime) 中的 Agent 基于 Strands Agents SDK 实现，
部署于 `networkMode=PUBLIC`、未配置任何 `vpcConfig` 的 Runtime，5 个用例分别覆盖两条链路：
4 个经 `rdsLambda___*`（方案一），1 个指定 `rdsApi___getDbInfo`（方案二）。

---

## 7. 入站（Ingress）私有连接

若需求方向相反，即 VPC 内的应用需私有调用 AgentCore API，则属于 Ingress，
应使用 **Interface VPC Endpoint（AWS PrivateLink）** 实现，与 egress 无关。
若区域尚未提供 PrivateLink，可先经公网并使用 IAM 与 SigV4 调用，走 HTTPS 且严格鉴权，
待 PrivateLink 上线后切换。

---

## 8. Private IdP 绕行（interceptor Lambda）

**Private IdP** 指自建于 VPC 内网、公网无法访问的 OAuth2 或 OIDC 授权服务器，
如 Keycloak、PingFederate。AgentCore Identity 需从**两个方向**访问它。

| 方向 | 访问 IdP 的什么 | 用途 | 原生配置位置 |
|---|---|---|---|
| **入站** | OIDC discovery 与 **JWKS** | 获取公钥以校验 JWT | `authorizerConfiguration.customJWTAuthorizer.privateEndpoint` |
| **出站** | **token endpoint** | 交换下游访问令牌 | OAuth credential provider 上的 `privateEndpoint` |

两个方向均依赖 VPC Lattice，该能力缺失时会同时失效。替代方案是 interceptor Lambda。

```
Agent ──(带 JWT)──▶ Gateway ──▶ REQUEST interceptor Lambda(挂 VPC) ──▶ 内网 IdP: /jwks
                                        │ 非法 → 403 短路
                                        ▼ 合法
                                     tool Lambda(挂 VPC) ──▶ 内网 IdP: /token ──▶ 私有 RDS
```

interceptor 分为两类，每类最多配置一个，且目前仅支持 Lambda。REQUEST 在 Gateway 调用 target
之前执行，用于请求校验、改写与自定义授权；RESPONSE 在返回调用方之前执行，
用于脱敏或改写响应。
详见官方文档 [Using interceptors with Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html)。

### 入站：在 interceptor 内完成 JWT 校验

1. 从 `mcp.gatewayRequest.headers.Authorization` 取出 JWT，前提是 `passRequestHeaders: true`。
2. 经私有网络访问内网 IdP 的 JWKS 获取公钥，**必须缓存**，随后本地验签，并校验 `iss`、`aud`、`exp` 与 scope。
3. 校验不通过时返回 `transformedGatewayResponse`，Gateway 将立即响应，不再调用 target。

```json
{
  "interceptorOutputVersion": "1.0",
  "mcp": {
    "transformedGatewayResponse": {
      "statusCode": 403,
      "body": { "jsonrpc": "2.0", "id": 1,
                "error": { "code": -32001, "message": "token rejected" } }
    }
  }
}
```

4. 校验通过时**必须回显原始 body**，否则请求全部失败，见
   [勘误 6](#勘误-6--mcp-request-interceptor-放行时必须回显原始-body)。

```json
{ "interceptorOutputVersion": "1.0",
  "mcp": { "transformedGatewayRequest": { "body": { "...原始 JSON-RPC body..." } } } }
```

### 出站：由工具 Lambda 完成令牌交换

工具 Lambda 本身位于 VPC 内，可直接执行 `client_credentials`，并对令牌做缓存。
该路径完全不涉及 AgentCore Identity，因此不存在“是否支持 Private IdP”的问题。

> **另一路径，不建议依赖。** 由 interceptor 改写 `Authorization` 头。
> 官方 payload 示例中，HTTP target 的 `transformedGatewayRequest` 明确包含 `headers`，
> 而 MCP target 仅列出 `body`。本次未验证 MCP target 上的头改写，
> 原因是对 Lambda target 而言 HTTP 头不可观测，Lambda 接收的是工具入参而非 HTTP 请求，
> 无法构造有效的观测实验。上述路径已实测通过，建议直接采用。

### 配置示例

```json
"interceptorConfigurations": [
  {
    "interceptor": { "lambda": {
      "arn": "arn:aws-cn:lambda:cn-northwest-1:ACCT:function:my-jwt-interceptor" } },
    "interceptionPoints": ["REQUEST"],
    "inputConfiguration": { "passRequestHeaders": true }
  }
]
```

Gateway 执行角色需具备 `lambda:InvokeFunction`，且仅授权该函数，不应使用通配。

### 实测结果（[`13-private-idp-workaround/`](../13-private-idp-workaround)）

9 个用例全部通过。IdP 运行于同一隔离 VPC 内的 EC2，`PublicIpAddress = null`，
仅 Lambda 安全组可访问，自 VPC 外部连接返回 `TimeoutError`。

| # | 用例 | 结果 | interceptor 返回原因 |
|---|---|---|---|
| 1 | 合法令牌 | ✅ 到达 target | 2 条 PENDING 订单，出站令牌来自 `10.30.11.13` |
| 2 | 无 Authorization 头 | 🚫 拦截 | `missing bearer token` |
| 3 | 畸形令牌 | 🚫 拦截 | `token rejected` |
| 4 | **攻击者密钥伪造签名** | 🚫 拦截 | `signature verification failed` |
| 5 | 过期令牌 | 🚫 拦截 | `token expired` |
| 6 | 错误 audience | 🚫 拦截 | `wrong audience` |
| 7 | 错误 issuer | 🚫 拦截 | `wrong issuer` |
| 8 | 缺少必需 scope | 🚫 拦截 | `missing required scope orders.read` |
| 9 | 未知签名 `kid` | 🚫 拦截 | `token rejected` |

其中最具说明力的是第 4 与第 9 个用例。用例 4 的令牌由第二把 RSA 私钥签发，
该私钥从未提供给 IdP，既然能被识别，表明验签使用的确实是 IdP 的公钥。
用例 9 的 `kid` 在 JWKS 中并不存在，可反向证明 JWKS 确实自私有 IdP 获取。
用例 2 与 5 至 8 均携带合法签名，仅声明存在问题，表明校验并未止于签名层。

### 性能开销与缓存

将 `PyJWKClient` 置于模块作用域，缓存即可随执行环境复用。

```python
_jwk_client = PyJWKClient(IDP_JWKS_URL, cache_keys=True, lifespan=300)
```

| 指标 | 实测值（n=216） |
|---|---|
| 温调用 | **p50 2.00 ms**，最小 1.43 ms |
| 冷启动 init | 约 **250 ms**，主要来自导入 `cryptography` 与 `PyJWT` |
| 冷启动后首次调用 | **254 ms**，含获取 JWKS |

若不做缓存，每次 Gateway 调用都会向 IdP 发起一次请求，延迟与 IdP 负载均会被放大。
关键路径上的 Lambda 冷启动会直接体现为工具调用延迟，必要时配置 Provisioned Concurrency。

### 附带效果：不受公信证书约束

原生 `privateEndpoint` 路径要求 IdP 的 discovery URL 必须为 HTTPS，且证书必须公信，
否则需在其前端部署带公有 ACM 证书的内网 ALB。经 interceptor 实现时不存在上述两项约束，
因为 HTTP 客户端由你自行实现。生产环境仍建议启用 TLS，只是不再受公信证书限制。

### 其他注意事项

- `passRequestHeaders` 会将 `Authorization` 以明文传递给 Lambda。官方已明确提示 headers
  中含认证令牌与凭证，需确认 interceptor 未将其写入日志。
- interceptor 必须幂等。Gateway 在失败或超时时可能重试。
- 官方另有一条限制：配置了 `privateEndpoint` 的 target 不能使用 `NO_AUTH` 入站，
  除非 Gateway 上配置了 interceptor Lambda。可见 interceptor 在设计上即可承担鉴权职责。

---

## 9. 勘误清单（9 条实测发现）

### 勘误 1（安全）· `Deny` 绝不能用 `aws:SourceArn` 收敛

官方文档给出的资源策略如下：

```json
{ "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
  "Condition": { "ArnEquals": { "aws:SourceArn": "<gateway ARN>" } } }
```

该 `Allow` 本身有效。实测删除执行角色身份策略中的 `execute-api:Invoke` 后，
仅凭该资源策略 Gateway 仍返回 200，表明在该次授权判断中 `aws:SourceArn` 存在，
且等于 gateway ARN。

问题出在 `Deny`。由于[勘误 2](#勘误-2安全-只写-allow-的资源策略不等于锁死)所述，
仅编写 `Allow` 无法实现收敛，必须补充显式 `Deny`。而一旦将 `Deny` 同样基于 `aws:SourceArn` 取反：

```json
{ "Effect": "Deny", "Principal": "*",
  "Condition": { "ArnNotEqualsIfExists": { "aws:SourceArn": "<gateway ARN>" } } }
```

Gateway 会立即被自身策略拒绝：

```
User: arn:aws:sts::ACCT:assumed-role/my-gateway-exec-role/gateway-session-...
is not authorized to perform: execute-api:Invoke ...
with an explicit deny in a resource-based policy
```

机制如下：该请求还会以 Gateway 执行角色的临时会话身份被再次判定，
而该上下文中 `aws:SourceArn` 缺失。IAM 的取反类操作符，
包括 `ArnNotEquals` 与 `StringNotEquals`，在键缺失时判定为 true，`...IfExists` 亦然，
于是 `Deny` 命中。而显式 `Deny` 始终优先于 `Allow`。

正确写法是 `Allow` 与 `Deny` 均按执行角色 ARN（`aws:PrincipalArn`）编写。
该键对 SigV4 调用者必然存在，且 assumed-role 会话会解析为裸角色 ARN。
完整策略见[第 5 节](#可用的资源策略中国区分区为-aws-cn)。

附带发现：只要资源策略中存在针对执行角色 ARN 的 `Allow`，
执行角色的身份策略即不再需要 `execute-api:Invoke`，实测删除后仍可调用成功。

#### 6 组策略对照实测矩阵

表中“其他主体”指同账号内另一个持有宽泛 `execute-api:Invoke` 的管理员角色。
每次修改策略均需重新部署 stage，并等待约一至两分钟才实际生效。

| # | 资源策略 | 身份策略给了 `execute-api:Invoke` | Gateway | 其他主体 | 结论 |
|---|---|---|---|---|---|
| 1 | 无 | 是 | 200 | 200 | 完全未收敛 |
| 2 | 服务主体 `Allow` 加 `ArnEquals SourceArn`（原文档） | 是 | 200 | 200 | **无法收敛**，同账号取并集 |
| 3 | 同上 | **否** | 200 | 200 | 证明该 `Allow` 确实生效 |
| 4 | #2 再加 `Deny *` 与 `ArnNotEqualsIfExists SourceArn` | 是 | **403** | 403 | **Gateway 被拒绝** |
| 5 | 角色 ARN `Allow` 加 `Deny *` 与 `ArnNotEquals PrincipalArn` | **否** | 200 | 403 | 可用，身份策略非必需 |
| 6 | 同 #5 | 是 | 200 | 403 | ✅ **推荐** |

无 SigV4 的裸请求在全部 6 行中均返回 `403 Missing Authentication Token`，
因为方法鉴权为 `AWS_IAM`。

### 勘误 2（安全）· 只写 `Allow` 的资源策略不等于“锁死”

同账号调用时，API Gateway 会对身份策略与资源策略取并集，
因此账号内任何持有 `execute-api:Invoke` 的主体均可调用成功。
实测以管理员身份直接经 SigV4 调用，返回了 `200` 与完整的 RDS 数据。必须补充显式 `Deny`。

### 勘误 3 · `apiGatewayToolConfiguration.toolFilters` 是必填项

官方文档未记载该项。仅提供 `restApiId` 与 `stage` 会直接触发参数校验失败：

```
ParamValidation: Missing required parameter in
targetConfiguration.mcp.apiGateway: "apiGatewayToolConfiguration"
```

必须通过 `toolFilters` 白名单列出 path 与 method。另建议同时配置 `toolOverrides`，
为工具指定对 Agent 友好的名称与描述，否则工具名将由 API 结构自动推导，可读性较差。

### 勘误 4 · Gateway 入站可直接使用 `AWS_IAM`，验证阶段无需 Cognito

文档与多数示例均采用 CUSTOM_JWT 加 Cognito。实测 `--authorizer-type AWS_IAM` 完全可用：
以 SigV4 对服务名 `bedrock-agentcore` 签名，直接 POST 至 MCP 端点即可完成
`initialize`、`tools/list` 与 `tools/call`。POC 阶段可省去整套 IdP。
生产环境仍建议 CUSTOM_JWT，因需承载用户身份。

> 需注意，入站使用 `AWS_IAM` 时 `Authorization` 头已被 SigV4 占用，
> 业务 JWT 必须改用自定义头。这也是 Private IdP 场景倾向 `NONE` 加 interceptor 的原因。

### 勘误 5 · 两处时序相关问题

- **IAM 传播。** `CreateGatewayTarget` 会立即校验执行角色对 Lambda 的
  `lambda:InvokeFunction`。新建角色后立即创建 target 会失败，报
  `Gateway execution role lacks permission to invoke Lambda function`，
  此时策略本身正确，重试即可通过。`CreateAgentRuntime` 同理，报
  `ValidationException: Role validation failed for '<arn>'`。
  应仅对含 `Role validation failed` 的情况重试，其余 `ValidationException` 才视为致命。
- **API Gateway 部署限流与策略生效延迟。** `CreateDeployment` 限流严格，
  会报 `TooManyRequestsException`，需退避重试。资源策略修改后必须重新部署 stage，
  实际生效还需等待约一至两分钟。本次首轮验证“应被拒绝”的场景时仍返回 200，轮询后才变为 403。
  因此自动化校验必须采用轮询，否则可能得到错误结论。

### 勘误 6 · MCP REQUEST interceptor 放行时必须回显原始 body

放行时返回 `{"interceptorOutputVersion":"1.0","mcp":{}}`，客户端会收到 `HTTP 200`
及一个 JSON-RPC `Parse error - Invalid JSON format`，请求无法到达 target。

该问题极易误判。故障发生时，所有拒绝用例均表现正常，
即经 `transformedGatewayResponse` 短路的路径无异常，仅放行路径异常，
表现形式更接近自研鉴权逻辑存在缺陷。

正确写法是将原始 JSON-RPC body 回显于 `transformedGatewayRequest.body`。
官方文档中“返回空对象即原样透传”一句（`{"interceptorOutputVersion":"1.0","http":{}}`）
位于 HTTP target 的 RESPONSE interceptor 一节，不适用于 MCP target 的 REQUEST interceptor。

### 勘误 7 · `passRequestHeaders=false` 返回空字典，而非缺失字段

关闭该开关后，interceptor 接收到的是 `headers: {}`，而非不含 `headers` 键。因此：

```python
if headers is None:      # ← 永远不成立，无法检测配置错误
```

结果是配置错误被表现为“客户端未携带令牌”。实测观察到的症状是 `missing bearer token`，
实际原因为该开关未启用，容易导致排查方向偏离。应改为按空值判断：

```python
if not headers:          # ← 可捕获空字典
    return deny(request_id, "interceptor cannot see request headers",
                "set inputConfiguration.passRequestHeaders=true")
```

### 勘误 8 · `NONE` 入站加 interceptor 可用，但 interceptor 成为唯一关卡

`authorizerType=NONE` 搭配 REQUEST interceptor 可正常创建并工作，
且 `Authorization` 头会完整传递给 interceptor。此点较为关键：
入站改用 `AWS_IAM` 时，该头会被 SigV4 占用。

⚠️ 代价是 Gateway 端点不具备平台层鉴权，interceptor 一旦报错、超时或被误删，
结果即为完全开放或全部拒绝。生产环境应为其配置 Provisioned Concurrency 与告警，
执行角色仅授权该函数，也可考虑 `AWS_IAM` 加自定义头构成双层。

### 勘误 9 · `apiGateway` target 注入 `basePath` 参数，模型填充后返回 403

由 `apiGateway` target 自动生成的工具，其输入 schema 中会额外包含一个 `basePath` 字段，
既无描述也无约束：

```json
{ "type": "object",
  "properties": { "basePath": { "type": "string" },
                  "limit": { "type": "string" }, "status": { "type": "string" } } }
```

`basePath` 属于管道参数，而非业务入参。手工构造的客户端不传该参数，不受影响，
但**模型可能自行填充**。一旦填充，请求 URL 即被破坏，返回：

```
Server URL parameter 'basePath' contains invalid character '/'
Client error: API request failed with status: 403 - {"message":"Forbidden"}
```

该问题的困难之处在于**不稳定**：同一 Agent、同一提问，模型有时不填充即成功，
有时填充后返回 403。本次最初验证方案二的 Runtime 端到端时链路连通，
间隔一段时间后重跑则连续三次 403，排查方向极易被引向 IAM。

**判断是否为权限问题**，查看 403 的响应体即可：

| 响应体 | 含义 |
|---|---|
| `{"Message":"User: ... is not authorized to perform: execute-api:Invoke ..."}` | 确为 IAM 拒绝，大写 `Message`，含主体 ARN |
| `{"message":"Forbidden"}` | 路径未匹配，小写 `message`，无细节 |

为确认该结论，本次还将 Runtime 执行角色一并加入资源策略的 `Allow`，
403 依旧，由此排除权限方向。

**解法**有三种，按推荐顺序：

1. 在 system prompt 中明确禁止。实测加入 “never pass a `basePath` argument” 后，
   连续三次调用全部成功，稳定获取 `10.30.11.109`。
2. 面向 Agent 的工具优先使用 **Lambda target**。其 schema 来自自行编写的 `toolSchema`，
   完全可控，不会额外产生参数。`toolOverrides` 仅可修改名称、描述、path 与 method，**无法修改输入 schema**。
3. 若工具必须由 `apiGateway` target 暴露，则在客户端侧将 `basePath` 从工具定义中移除后再交给模型。

### ✅ 被实测证实正确的部分

- Lambda target 的“开箱即用、无需额外配置”成立，挂载 `VpcConfig` 后即可访问 RDS。
- `apiGateway` target 仅支持区域型 REST API，确认。
- VPC Link 后端必须为 NLB，确认，不支持 ALB。
- 出站鉴权仅有 IAM 与 API Key，确认，无 OAuth 选项。
- 整条链路无需 Gateway 进入 VPC，确认。VPC 内不含任何出网路由，全程仍可用。
  这是本文最核心的一条判断。
- interceptor Lambda 可替代 Private IdP 支持，确认，9 个用例全部通过，温调用耗时 2 ms。
- 将私有访问下沉为工具，确认，`networkMode=PUBLIC` 的 Runtime 仍可读取私有 RDS，见第 6 节。

### 构建期注意事项

- **AL2023 AMI 的 `python3` 为 3.9，且未预装 pip。** wheel 需按 `cp39` 解析，
  随后在实例上以 `python3 -m zipfile -e` 直接解包，wheel 本身即为 zip。
  按 3.11 解析会得到 `cp311-abi3` 的 `cryptography`，无法在 3.9 上安装。
- **构建机与目标架构可能不一致。** Lambda 或 EC2 为 x86_64 而构建机为 arm64 时，
  跨平台安装必须写为
  `pip install --platform manylinux2014_x86_64 --only-binary=:all: --target ...`，
  普通 `pip install` 会安装 arm64 版本的 `cryptography`，部署后 import 失败。
  AgentCore Runtime 镜像则必须为 `linux/arm64`，构建完成后以
  `docker image inspect --format '{{.Architecture}}'` 校验，避免推送后才发现。

---

## 10. 选型建议

按以下顺序判断：

1. **默认选方案一，Lambda 桥接。** 零 egress 依赖、开箱即用、流量私有、成本低。
   约 90% 的“工具访问私有资源”需求应从此起步。已实测。
2. **内网已有大量 REST 服务时，选方案二，API Gateway 加 VPC Link。**
   采用区域型 REST API 配 VPC Link，便于统一治理，鉴权走 IAM SigV4。已实测。
   资源策略务必按[勘误 1](#勘误-1安全-deny-绝不能用-awssourcearn-收敛)使用 `aws:PrincipalArn` 编写。
3. **Agent 代码本身需私有访问时，不应寻求 Runtime 的 VPC 配置。** 将私有访问下沉为 Gateway 工具，
   底层仍走方案一或方案二，Agent 不直连。实现方式见[第 6 节](#6-runtime-侧将出站访问下沉为工具)，已实测。
4. **使用内网自建 IdP 时，采用 interceptor Lambda。** 入站验签置于挂载 VPC 的 REQUEST interceptor，
   下游令牌交换由工具 Lambda 自行完成。已实测，9 个用例全部通过。
5. **入站需私有调用 AgentCore API 时，待 PrivateLink 就绪后使用。** 未就绪则先经公网加 SigV4 过渡。

> 上述方案均将“访问私有资源”封装为工具或中间层。
> 待区域补齐原生 VPC Lattice egress 后，可平滑切换至 `privateEndpoint`，
> Agent 逻辑无需改动，迁移成本较小。

---

## 11. 迁移到中国区的检查清单

- [ ] **ARN 分区改为 `aws-cn`**，所有策略中的 `arn:aws:...` 均需替换为 `arn:aws-cn:...`。
- [ ] **资源策略按执行角色 ARN 编写**，见勘误 1。中国区服务主体名有时带区域后缀，
      而实测表明正确写法本就不应依赖服务主体，该结论与分区无关，可直接沿用。
- [ ] 确认 AgentCore 各子服务，即 Runtime、Gateway、Identity、Memory，
      在 `cn-north-1` 与 `cn-northwest-1` 的具体可用性与 GA 时间。
- [ ] 确认 VPC Lattice 集成、PrivateLink 与支持的可用区列表是否随首发提供。
- [ ] 确认跨区域推理是否受限，以及可用模型清单。
- [ ] 本文使用的 S3 网关端点、SSM 接口端点、VPC Link、内网 NLB 与 Lambda VPC
      在中国区均为成熟能力。

---

## 12. 如何复现

| 项目 | 内容 |
|---|---|
| [`11-vpc-no-egress-workaround/`](.) | 零出网 VPC 与私有 RDS，方案一、方案二，以及策略矩阵 |
| [`12-strands-nova2-runtime/`](../12-strands-nova2-runtime) | Runtime 侧实现，Strands 加 Nova 2 Lite 部署于 PUBLIC Runtime |
| [`13-private-idp-workaround/`](../13-private-idp-workaround) | Private IdP 绕行，私有 IdP 与 interceptor Lambda |

```bash
# 1) 零出网 VPC + 私有 RDS + 方案一 / 方案二
cd 11-vpc-no-egress-workaround
bash scripts/01-vpc-rds.sh          # 约 8-10 分钟
bash scripts/02-lambda.sh
bash scripts/03-gateway.sh
bash scripts/04-apigw-vpclink.sh    # 约 5-8 分钟
bash scripts/05-apigw-target.sh
python3 scripts/06-harden-apigw.py  # 可用版资源策略（--docs 复现被拒绝的版本）
bash scripts/07-collect-evidence.sh

# 2) Runtime 侧实现：PUBLIC Runtime 上的 Strands Agent
cd ../12-strands-nova2-runtime
bash scripts/deploy.sh
python scripts/invoke.py

# 3) Private IdP 绕行
cd ../13-private-idp-workaround
bash scripts/01-idp.sh
bash scripts/02-lambdas.sh
bash scripts/03-gateway.sh
python scripts/04-verify.py         # 9 项验证矩阵

# 清理（逆序）
bash ../13-private-idp-workaround/scripts/cleanup.sh --yes
bash ../12-strands-nova2-runtime/scripts/cleanup.sh --yes
bash ../11-vpc-no-egress-workaround/scripts/cleanup.sh --yes
```

全部部署的成本约为 **$0.11 每小时**，其中 RDS db.t4g.micro 约 $0.016，
两台 EC2 t3.micro 约 $0.021，NLB 约 $0.0225，3 个接口端点约 $0.03，其余按调用计费。
全过程未使用 NAT Gateway。

---

## 13. 参考来源

- [Configure AgentCore Gateway VPC Egress for Gateway Targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-vpc-egress.html)，涵盖 Lambda、API Gateway、VPC Link 与私有 REST API 的 workaround
- [Connect to private resources in your VPC using VPC Lattice](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-egress-private-endpoints.html)，managed 与 self-managed 两种模式，以及私有证书的 ALB workaround
- [Configure AgentCore Runtime and tools for VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)，Runtime ENI、可用区与 VPC endpoint
- [Use interface VPC endpoints (PrivateLink) for AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)，Ingress 私有连接
- [Connect to private identity providers](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-private-idp.html)，Private IdP 入站与出站的 `privateEndpoint`、服务关联角色与限制
- [Using interceptors with Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html)，REQUEST/RESPONSE interceptor、`passRequestHeaders`、幂等与重试、payload 契约
- [AgentCore 区域可用性](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
- [AWS ML Blog — Configuring AgentCore Gateway for secure access to private resources](https://aws.amazon.com/blogs/machine-learning/configuring-amazon-bedrock-agentcore-gateway-for-secure-access-to-private-resources/)
- [AWS Networking Blog — Private connectivity patterns for AgentCore Gateway Targets](https://aws.amazon.com/blogs/networking-and-content-delivery/private-connectivity-patterns-for-amazon-bedrock-agentcore-gateway-targets/)

---

*本指南依据 AWS 官方文档整理，针对“缺失原生 VPC Egress 或 Private IdP 支持”的场景。
其中方案一、方案二与 Private IdP 绕行已于 2026 年 7 月 29 日在 us-east-2 完成真机验证。
中国区的实际能力、服务主体、ARN 分区与可用区列表，请以上线时的官方文档为准。*
