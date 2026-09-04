# 20 — AgentCore Identity Consent Portal

本目录介绍 Amazon Bedrock AgentCore Identity 于 2026 年 9 月发布的 **Consent Portal(托管同意门户)**,与此前基于 OAuth2 Credential Provider 自建回调的三方授权(3LO)方案做对比,并提供一个可直接部署的 **Google Calendar** 示例:Cognito 作为入站 IdP,Gateway 通过 OpenAPI target 以用户身份调用 Google Calendar API,用户在 Consent Portal 上完成一次 Google 授权。

## 目录

```text
20-consent-portal/
├── README.md
├── deploy.py                    # 创建全部 AWS 资源(可重入)
├── invoke.py                    # 以测试用户身份通过 Gateway 调用 Google Calendar 工具
├── cleanup.py                   # 删除 deploy.py 创建的资源
├── google_calendar_openapi.json # Calendar v3 只读子集(listCalendars / listEvents)
├── requirements.txt
├── .env.example                 # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
└── .gitignore                   # .deployment.json / .env / .venv
```

## 一句话总结

Consent Portal **不是 OAuth2 Credential Provider 的替代品**,而是叠在它之上的一层 AWS 托管 Web UI。它接管了原来 3LO 流程里最麻烦的一块——你自己要托管的公网 HTTPS 回调端点,以及 `CompleteResourceTokenAuth` 会话绑定逻辑。底层的 Credential Provider、Token Vault、`GetResourceOauth2Token` 机制完全不变,Portal 只是替你调用。

## Consent Portal 是什么

一个由 AWS 托管的 Web 门户,做三件事:

1. 用 OIDC IdP 认证终端用户(登录门户);
2. 在 **Connections** 页列出该用户可以授权 Agent 访问的下游资源(GitHub、Slack、Salesforce 等);
3. 收集用户同意,把下游 access token / refresh token 存进 AgentCore Identity Token Vault。

整个 OAuth 流程在服务端完成,**浏览器从不持有 token**。

### 核心约束

| 项目 | 说明 |
|---|---|
| 绑定对象 | 一个 Portal 只能挂 **一个** Gateway(`sources` 类型仅 `agentcore-gateway`,数量 min=max=1) |
| Gateway 要求 | 入站认证必须是 **CUSTOM_JWT**;创建 Portal 时 AWS 会校验 Gateway authorizer 与 `idpConfig` 里的 Credential Provider 指向 **同一个 OIDC issuer** |
| 主 IdP(primary IdP) | 必须是签发 **JWT access token** 的 OIDC IdP(Cognito、Okta、Entra ID、Auth0 等)。GitHub / Slack / Salesforce / Atlassian / LinkedIn 这类 OAuth2-only 厂商不能当主 IdP,但可以当下游 outbound provider |
| 执行角色 | 需要 `executionRoleArn`,信任 `bedrock-agentcore.amazonaws.com`;权限包括读 Gateway/Target、读 OAuth2 Credential Provider、`CompleteResourceTokenAuth` / `GetResourceOauth2Token` / `GetWorkloadAccessTokenForJWT`,以及读取 Secrets Manager 中的 client secret |
| Connections 列表来源 | Gateway 上 `grantType=AUTHORIZATION_CODE` 的 target;`CLIENT_CREDENTIALS`(2LO)target 不需要用户同意,不会出现;列表缓存最多 5 分钟 |
| 显示名 | Target 名和 Credential Provider 名 **原样展示给终端用户**,命名要面向最终用户 |
| 可用区域 | 所有 AgentCore Identity 商用区域 |

### 两类 Credential Provider,两条回调 URL

这是最容易混的地方。一个 Portal 至少涉及两个 OAuth2 Credential Provider,各自有不同的回调地址:

```text
                ┌─ 主 IdP Credential Provider(用户登录门户用的身份)
                │    IdP 应用上注册回调: <portalUrl>/callback        ← 不能带尾部斜杠
Consent Portal ─┤
                └─ Outbound Credential Provider(GitHub / Slack 等下游资源)
                     下游厂商上注册回调: 该 provider 自己的 callbackUrl
                                        https://bedrock-agentcore.amazonaws.com/identities/callback/<id>
                     Gateway Target 上设置: defaultReturnUrl = <portalUrl>/connect/callback
```

- 主 IdP 的 Credential Provider 通过 `idpConfig.credentialProviderArn` 传给 Portal;IdP 应用必须是「授权码 + client secret」的 OIDC Web 应用,允许的 scope 必须包含 `openid`(Portal 会始终额外请求 `openid`)。
- Outbound Credential Provider 通过 Gateway Target 的 `credentialProviderConfigurations.providerArn` 引用。一个 outbound provider 可以支撑多个 target。
- `defaultReturnUrl` 必须 **精确等于** `<portalUrl>/connect/callback`,否则用户授权完成后被送到别处,同意无法绑定到门户会话。
- **存量 target 也要改**:Portal 创建之前建的 target,`defaultReturnUrl` 是别的值或为空,必须逐个更新后才会在 Connections 页生效。

### 搭建顺序

步骤之间有依赖(`portalUrl` 只有 Portal 变为 `ACTIVE` 后才存在),不能乱序:

1. 创建主 IdP 的 OAuth2 Credential Provider。IdP 应用的 redirect URI 先留空或填占位值,并准备至少一个可登录的测试用户。
2. 准备一个 CUSTOM_JWT Gateway,authorizer 指向同一个 OIDC issuer。Target 可以后加。
3. 创建执行角色。首次可省略信任策略里的 `Condition`,拿到 Portal ARN 后再补 `aws:SourceAccount` / `aws:SourceArn`。
4. 创建 Portal:

   ```bash
   aws bedrock-agentcore-control create-consent-portal \
       --name "my-consent-portal" \
       --execution-role-arn "arn:aws:iam::<account-id>:role/<execution-role-name>" \
       --idp-config '{
           "credentialProviderArn": "arn:aws:bedrock-agentcore:<region>:<account-id>:token-vault/default/oauth2credentialprovider/<credential-provider-id>",
           "scopes": ["openid", "email", "profile"],
           "audience": "<audience>"
       }' \
       --sources '[{"identifier": "<gateway-id>", "type": "agentcore-gateway"}]'
   ```

5. 用 `get-consent-portal --consent-portal-identifier <id>` 轮询,状态从 `CREATING` 变为 `ACTIVE` 后取 `portalUrl`;若为 `FAILED`,看 `statusReason`。
6. 到主 IdP 应用注册 `<portalUrl>/callback`(Cognito 叫 Allowed callback URLs,Okta 叫 Sign-in redirect URIs)。
7. 创建 outbound Credential Provider,到下游厂商(GitHub 等)注册响应里返回的 `callbackUrl`。
8. 给 Gateway 加 Target:`providerArn` + `grantType=AUTHORIZATION_CODE` + `defaultReturnUrl=<portalUrl>/connect/callback`。
9. 终端用户打开 `portalUrl` → 登录主 IdP → 在 Connections 页对目标资源点 **Connect** → 在下游厂商完成登录与授权 → 被送回 `<portalUrl>/connect/callback` 完成绑定 → 状态显示已连接,Agent 可代表该用户调用资源。

## 与之前的 OAuth2 Credential Provider 3LO 方案对比

### 旧方案回顾:authorization URL session binding

Consent Portal 出现之前(该方案现在依然可用,而且对非 Gateway 场景仍是唯一方案),3LO 流程是这样的:

1. Agent 调用 `GetResourceOauth2Token`,因为 Vault 里没有该用户的 token,得到一个 **授权 URL** 和 **session URI**(10 分钟有效);
2. 应用把 URL 展示给用户,用户去下游厂商授权;
3. AgentCore Identity 把浏览器重定向到 **你自己托管的公网 HTTPS 回调端点**。该端点必须提前用 `UpdateWorkloadIdentity` 注册为 `AllowedResourceOauth2ReturnUrl`;
4. 你的回调端点从浏览器 cookie / local storage 读出当前登录用户,确认「当前用户 == 发起授权的用户」,然后调用 `CompleteResourceTokenAuth(session_uri, user_id)`;
5. AgentCore Identity 拿授权码换 token 存入 Vault;Agent 再次调用 `GetResourceOauth2Token` 即可拿到 token。

其中第 3、4 步需要你自己写并托管一个 Web 端点,还得处理 CSRF(state 参数)和用户会话校验。这正是 Consent Portal 要消除的部分。

### 逐项对比

| 维度 | 旧:Credential Provider + 自建回调 | 新:Consent Portal |
|---|---|---|
| 谁托管回调端点 | 你自己;必须公网可达 HTTPS,并注册为 Workload Identity 的 `AllowedResourceOauth2ReturnUrl` | AWS 托管(`<portalUrl>/callback` 与 `<portalUrl>/connect/callback`) |
| 会话绑定逻辑 | 你写代码:读浏览器会话、校验用户、调 `CompleteResourceTokenAuth` | Portal 内置(执行角色里有对应权限) |
| 用户端认证 | 你的应用自己负责 | Portal 用主 IdP(OIDC)登录,与 Gateway JWT authorizer 同 issuer |
| 授权时机 | 只能「用到时」触发:Agent 调用拿到授权 URL → 让用户去点 | 可以 **预授权**:管理员提前分发 `portalUrl`,用户在会话开始前就把 GitHub / Slack 连好 |
| 用户自助可见性 | 无,用户看不到自己授权了什么 | Connections 页显示每个资源的连接状态 |
| 适用客户端 | 需要一个能渲染 URL、能承接重定向的 Web 应用 | 专门解决 **IDE / 编码 Agent 客户端** 无法展示 OAuth URL、无法处理回调的问题 |
| 作用范围 | 任意 Workload Identity(Runtime、Gateway、自定义) | **仅 Gateway**,且一 Portal 一 Gateway |
| 入站认证限制 | 无特别要求 | Gateway 必须 CUSTOM_JWT,主 IdP 必须签发 JWT |
| 2LO / Token Exchange | 覆盖(`CLIENT_CREDENTIALS`、RFC 8693 `TOKEN_EXCHANGE`) | 不涉及;2LO target 不会出现在 Portal 上 |
| 额外资源 | Credential Provider + Workload Identity 更新 | 额外多一个主 IdP Credential Provider、一个执行角色、一个 Portal 资源 |
| 底层机制 | `GetResourceOauth2Token` / `CompleteResourceTokenAuth` / Token Vault | **完全相同**,Portal 只是替你调用 |

### 两个边界

- **Portal 没有取消「直接调用返回授权 URL」的行为。** 用户如果没在 Portal 上预授权就直接通过 Agent 调 target,或者已存 token 过期且无法刷新,AgentCore Identity 仍然返回一个授权 URL 而不是 token。这个 URL 同样受 session binding 约束,用户需要回到 Portal 完成授权。Portal 是推荐入口,不是唯一入口。
- **非 Gateway 场景 Portal 帮不上忙。** 例如 Runtime 上的 Agent 直接用 `@requires_access_token` 获取 Google token,仍然要走旧的自建回调方案。

## 与本仓库其他 Demo 的关系

- [19-gateway-token-exchange](../19-gateway-token-exchange/README.md) 使用 `grantType=TOKEN_EXCHANGE`(RFC 8693 OBO)的 2LO 路径,与 Consent Portal 正交。它的 Gateway、Credential Provider、Lambda 型 OIDC IdP 搭建脚本可以作为本 demo 的基础,但 target 需要改为 `AUTHORIZATION_CODE` 并接入真实的下游 OAuth2 厂商才能体现 3LO。

## 运行 Demo:Google Calendar 通过 Consent Portal 授权

### 架构

```text
[事前,一次性] 用户浏览器 ──► Consent Portal (portalUrl)
                                │ 1. Cognito Hosted UI 登录          回调 <portalUrl>/callback
                                │ 2. Connections 页 google-calendar → Connect
                                │ 3. Google 授权                    回到 <portalUrl>/connect/callback
                                └─► Token Vault[ gateway 身份 + Cognito 用户 ] = Google token

[运行时]
invoke.py ──Cognito JWT (USER_PASSWORD_AUTH)──► Gateway (CUSTOM_JWT, issuer = Cognito pool)
                                                  │ target google-calendar (OpenAPI, googleapis.com/calendar/v3)
                                                  │ outbound: GoogleOauth2 provider, AUTHORIZATION_CODE
                                                  │           按 Cognito 用户从 Vault 取 Google token
                                                  ▼
                                            Google Calendar API
```

与 [agentcore-samples 的 3LO 教程](https://github.com/awslabs/agentcore-samples/tree/main/01-features/05-authenticate-and-authorize/02-outbound-auth/02-outbound-auth-3lo) 相比:Runtime 上的 Agent、`@requires_access_token`、本地 `oauth2_callback_server.py` 全部去掉;Google Calendar 的调用挪到 Gateway 的 OpenAPI target 上,授权由 Consent Portal 承担。原因是 Portal 存进 Token Vault 的 Google token 挂在 **Gateway 的 workload identity** 下,只有 Gateway 的出站认证能消费它。

### 前置条件

- Python 3.10+,当前身份能创建 Cognito、IAM Role、AgentCore Gateway / Credential Provider / Consent Portal。
- Google Cloud 项目:已启用 **Google Calendar API**;创建 **Web application** 类型 OAuth 2.0 Client;OAuth consent screen 为 External 时把你的 Google 账号加为 test user。
- `boto3 >= 1.43.88`(包含 `create_consent_portal`)。

```bash
cd 20-consent-portal
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 填入 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
```

### 部署

```bash
python deploy.py            # 默认使用 AWS 配置里的 region
```

脚本可重入,进度记录在 `.deployment.json`(已 gitignore,**包含测试用户密码**)。依次创建:

1. Cognito User Pool + Hosted UI 域名 + 两个 app client(`-api` 无 secret,供 `USER_PASSWORD_AUTH`;`-portal` 带 secret + 授权码流,供 Portal 登录)+ 测试用户 `testuser`
2. Gateway 服务角色 + `CUSTOM_JWT` Gateway(`allowedClients` 为两个 client,`supportedVersions` 含 `2025-11-25`)
3. Cognito 的 `CustomOauth2` Credential Provider(Portal 主 IdP)
4. Portal 执行角色
5. Consent Portal → 轮询 `ACTIVE` → 自动把 `<portalUrl>/callback` 写入 `-portal` client 的 CallbackURLs,并把执行角色信任策略收紧到 Portal ARN
6. `GoogleOauth2` Credential Provider(读 `.env`)
7. Gateway target `google-calendar`:OpenAPI inline schema,`grantType=AUTHORIZATION_CODE`,`scopes=[calendar.readonly]`,`defaultReturnUrl=<portalUrl>/connect/callback`,`customParameters={access_type: offline, prompt: consent}`

结束时会打印需要你在 **Google Cloud Console** 手工注册的 redirect URI(形如 `https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<id>`)。这一步只在用户第一次点 Connect 之前需要完成。

### 授权与验证

1. 浏览器打开 `portalUrl`,用 `testuser` 和 `.deployment.json` 里的密码登录 Cognito Hosted UI。
2. Connections 页会列出 OAuth client `google-calendar`(Provider: Google,状态 Not connected),点 **Connect**,在 Google 完成登录和授权,回到 Portal 后状态变为已连接。新 target 最多 5 分钟后才出现在列表里。
3. 运行验证脚本:

```bash
python invoke.py                  # 未来 7 天的事件(primary 日历)
python invoke.py --list-calendars # 列出所有日历
```

`invoke.py` 用 Cognito 换 access token,对 Gateway 依次调 `initialize` → `tools/list` → `tools/call`。如果该用户还没在 Portal 上连接 Google,Gateway 会以 MCP `2025-11-25` 的 **URL elicitation**(JSON-RPC error `-32042`)返回授权链接,脚本识别后提示去 Portal,退出码 3。

### 清理

```bash
python cleanup.py
```

按依赖逆序删除 target、Portal、Gateway、两个 Credential Provider、两个 IAM Role、Cognito 域名和 User Pool。执行前请检查 `.deployment.json`。

### 实测发现(与文档不一致或文档未提及)

- **执行角色策略**:官方文档把 `GetOauth2CredentialProvider` 的 Resource 写成 `token-vault/default/oauth2credentialprovider/*`。按此配置 Portal 的 `/login` 会返回 `login_unavailable`("We couldn't reach the identity provider"),CloudTrail 显示 Portal 后端调 `GetOauth2CredentialProvider` 被 `AccessDenied`。把 Resource 改成 `token-vault/default` 和 `token-vault/default/*` 后立即恢复。`deploy.py` 已按后者配置。
- **MCP 协议版本**:Gateway 默认只启用 `2025-03-26`。`AUTHORIZATION_CODE` target 在无凭证时报 `Cannot initiate authorization code grant flow. URL elicitation requires MCP version 2025-11-25 or newer`。需要在 `protocolConfiguration.mcp.supportedVersions` 里显式加入 `2025-11-25`,客户端也要用该版本协商。
- **Portal 内的 Cognito 回调可以自动化**:`<portalUrl>/callback` 用 `update_user_pool_client` 写入即可;只有 Google 侧的 redirect URI 必须手工注册。
- Portal 页面标题为 "Consent Dashboard",Connections 页以"OAuth client / Associated targets"树形展示:第一层是 Credential Provider(显示厂商,如 Google),第二层是使用它的 target。

## 工具链要求

`create-consent-portal` / `get-consent-portal` / `update-consent-portal` / `delete-consent-portal` 属于 `bedrock-agentcore-control` 服务,需要 `boto3 >= 1.43.88` 或 `aws-cli >= 2.36.39`。`deploy.py` 只依赖 boto3,不依赖 AWS CLI。确认方式:

```bash
python3 -c "import boto3; c = boto3.client('bedrock-agentcore-control', region_name='us-east-1'); print([o for o in c.meta.service_model.operation_names if 'Consent' in o])"
```

## 参考资料

- [Configure a consent portal](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal.html)
- [Consent portal prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal-prerequisites.html)
- [Consent portal execution role](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal-execution-role.html)
- [Create a consent portal with the AWS CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-create-consent-portal.html)
- [Configure a consent portal target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-configure-consent-portal-target.html)
- [OAuth 2.0 authorization URL session binding(旧方案)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/oauth2-authorization-url-session-binding.html)
- [create-consent-portal — AWS CLI Command Reference](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/create-consent-portal.html)
- [发布公告:Amazon Bedrock AgentCore Identity now offers a managed consent portal](https://aws.amazon.com/about-aws/whats-new/2026/09/amazon-bedrock-agentcore/)
