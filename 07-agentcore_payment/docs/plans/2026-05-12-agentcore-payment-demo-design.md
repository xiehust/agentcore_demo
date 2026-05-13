# AgentCore Payments Demo Agent — 设计文档

- **日期**：2026-05-12
- **目标**：一个最小可运行的 demo，展示 AWS Bedrock AgentCore Payments
  如何让 Strands agent 自动处理 x402 付费端点。
- **非目标**：生产级架构、完整的错误分类、多 provider 切换、CI/CD、观测性。

## 1. 背景

AgentCore Payments 让 agent 在遇到 HTTP `402 Payment Required` 时自动完成加密支付
并重放请求。组成部分：

- **Payment Manager** — 顶层资源，指定授权方式和 IAM 角色。
- **Payment Credential Provider** — 存 Coinbase CDP / Stripe Privy 凭证到
  AgentCore Identity（底层 Secrets Manager）。
- **Payment Connector** — 把 Manager 绑到 provider。
- **Payment Instrument** — 嵌入式加密钱包（ETHEREUM / SOLANA）。
- **Payment Session** — 时限 + 花费上限。
- **Strands Plugin** — `AgentCorePaymentsPlugin` 自动拦截 402 并处理。

## 2. 范围

- Provider：Coinbase CDP（单一）。
- 集成：Strands Agent + payments plugin（非裸 boto3）。
- 付费端点：`PAID_URL` 可配置，默认 `https://drvd12nxpcyd5.cloudfront.net/market-recap`
  （AWS 官方文档示例，Base Sepolia 测试网）。
- 资源管理：混合模式 —— `setup.py` 幂等地创建/复用资源。

## 3. 文件布局

```
agentcore_payment/
├── README.md              # 先决条件、凭证获取、运行步骤、排错、清理
├── requirements.txt       # boto3, bedrock-agentcore[strands-agents], python-dotenv
├── .env.example           # 所有可配置项
├── .gitignore             # .env, .env.local
├── setup.py               # 幂等资源创建，子命令式
├── agent.py               # Strands agent 运行时
└── docs/plans/2026-05-12-agentcore-payment-demo-design.md
```

## 4. 资源生命周期（setup.py）

命令行子命令：

| 子命令       | 动作                                                      |
|--------------|-----------------------------------------------------------|
| `all`        | 依次创建/复用 credential provider、manager+connector、instrument、session |
| `manager`    | 只做 Payment Manager + Connector + Credential Provider（一次调用） |
| `instrument` | 只做 Payment Instrument                                   |
| `session`    | 只做 Payment Session（最常用，session 过期需重建）        |
| `status`     | describe 现有资源，打印健康状态                           |

幂等策略：

1. 先读 `.env.local` 里的 ID；有 → `describe` 校验还活着 → 跳过。
2. 资源已失效或不存在 → `list_*` 按名称前缀查找同名资源 → 复用。
3. 都找不到 → 创建，把 ID 追加写入 `.env.local`。

## 5. 关键代码形态

### 5.1 Credential Provider + Manager + Connector（一键）

```python
from bedrock_agentcore.payments import PaymentClient

pc = PaymentClient(region_name=REGION)
resp = pc.create_payment_manager_with_connector(
    payment_manager_name=f"{PREFIX}-manager",
    authorizer_type="AWS_IAM",
    role_arn=PAYMENTS_ROLE_ARN,
    payment_connector_config={
        "name": f"{PREFIX}-coinbase-connector",
        "payment_credential_provider_config": {
            "name": f"{PREFIX}-coinbase-cdp",
            "credential_provider_vendor": "CoinbaseCDP",
            "credentials": {
                "api_key_id": CDP_API_KEY_ID,
                "api_key_secret": CDP_API_KEY_SECRET,
                "wallet_secret": CDP_WALLET_SECRET,
            },
        },
    },
    wait_for_ready=True,
)
```

### 5.2 Payment Instrument（附打印充值链接）

```python
from bedrock_agentcore.payments import PaymentManager

mgr = PaymentManager(payment_manager_arn=MANAGER_ARN, region_name=REGION)
instrument = mgr.create_payment_instrument(
    user_id=USER_ID,
    payment_connector_id=CONNECTOR_ID,
    payment_instrument_type="EMBEDDED_CRYPTO_WALLET",
    payment_instrument_details={
        "embeddedCryptoWallet": {
            "network": "ETHEREUM",
            "linkedAccounts": [{"email": {"emailAddress": USER_EMAIL}}],
        },
    },
)
print("Fund wallet:",
      instrument["paymentInstrumentDetails"]["redirectUrl"])
```

### 5.3 Payment Session

```python
session = mgr.create_payment_session(
    user_id=USER_ID,
    limits={"maxSpendAmount": {"value": str(SESSION_MAX_USD),
                               "currency": "USD"}},
    expiry_time_in_minutes=SESSION_EXPIRY_MINUTES,
)
```

### 5.4 Agent 运行时

```python
from strands import Agent
from strands_tools import http_request
from bedrock_agentcore.payments.integrations.config import (
    AgentCorePaymentsPluginConfig,
)
from bedrock_agentcore.payments.integrations.strands.plugin import (
    AgentCorePaymentsPlugin,
)

plugin = AgentCorePaymentsPlugin(config=AgentCorePaymentsPluginConfig(
    payment_manager_arn=PAYMENT_MANAGER_ARN,
    user_id=USER_ID,
    payment_instrument_id=PAYMENT_INSTRUMENT_ID,
    payment_session_id=PAYMENT_SESSION_ID,
    region=REGION,
    network_preferences_config=NETWORK_PREFERENCES,  # 默认测试网
))
agent = Agent(
    system_prompt="You access paid APIs when the user asks. Show the response body.",
    tools=[http_request],
    plugins=[plugin],
)

result = agent(f"GET {PAID_URL} and show me the JSON body")
while result.stop_reason == "interrupt":
    responses = [handle_payment_interrupt(i) for i in result.interrupts]
    result = agent(responses)
print(result.message)
```

`handle_payment_interrupt` 按官方 pattern 打印 interrupt reason，
demo 里遇到 `PaymentSessionConfigurationRequired` 时提示用户
重跑 `python setup.py session`。

## 6. 环境变量

用户填：
- `AWS_REGION`
- `PAYMENTS_ROLE_ARN`
- `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` / `CDP_WALLET_SECRET`
- `USER_ID` / `USER_EMAIL`
- `PAID_URL`（可选，有默认）
- `RESOURCE_PREFIX`（可选，默认 `agentcore-payment-demo`）
- `NETWORK_PREFERENCES`（可选，默认 `base-sepolia,eip155:84532`）
- `SESSION_MAX_USD` / `SESSION_EXPIRY_MINUTES`（可选）

setup.py 写入 `.env.local`：
- `PAYMENT_MANAGER_ARN`
- `PAYMENT_CONNECTOR_ID`
- `PAYMENT_CREDENTIAL_PROVIDER_ARN`
- `PAYMENT_INSTRUMENT_ID`
- `PAYMENT_SESSION_ID`

## 7. 有意不做的事

- 不写 `teardown.py` —— README 放 3 行 aws cli 足够。
- 不做 boto3 client 工厂 / DI —— 脚本直接 new。
- 不做 dry-run —— `status` 子命令已经够用。
- 不做 Stripe Privy 路径 —— 单一 provider 聚焦。
- 不做多 instrument / 多 session 管理。
- 不写单测 —— 端到端 smoke test 靠真跑。

## 8. 已知未验证项（实现时需复核）

- `bedrock_agentcore.payments` 顶层是否能同时导出 `PaymentClient` 和
  `PaymentManager`。文档两处用法并存 —— 先从 `bedrock_agentcore.payments.client`
  import，如不对再降级到顶层。
- `create_payment_manager_with_connector` 的返回结构字段大小写（`paymentManager`
  vs `PaymentManager`）以文档为准，代码里用 `.get()` 安全读取。
- `instrument["paymentInstrumentDetails"]["redirectUrl"]` 是否一定存在 ——
  文档说是，若无则从 `get_payment_instrument` 再取一次。
- Strands `result.stop_reason == "interrupt"` 的字段命名与 result.interrupts
  的结构 —— 按官方示例 copy。

## 9. 实现后的验证

1. 填 `.env`，跑 `python setup.py all`。
2. 浏览器点击输出的 wallet hub URL，充测试币 + 授权 agent。
3. 跑 `python agent.py`，看 agent 是否成功拿到 market-recap 响应体。
4. 跑 `python setup.py status` 检查 session 剩余预算。
