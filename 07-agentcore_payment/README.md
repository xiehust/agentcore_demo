# AgentCore Payments Demo Agent

一个最小可运行的 demo，演示 Strands agent 如何使用
[AWS Bedrock AgentCore Payments](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments-getting-started.html)
自动为 x402 付费接口结账。

**做什么**：agent 对一个 HTTP 接口发 `GET`。如果对方返回
`402 Payment Required`，AgentCore Payments 插件会自动在用户的嵌入式加密钱包
上签一笔 Base Sepolia 测试网交易并重放请求 —— agent 代码完全无感。

**文件**

| 文件                                           | 作用                                                                    |
|------------------------------------------------|-------------------------------------------------------------------------|
| `setup.py`                                     | 幂等创建/复用 AWS 支付资源（manager、connector、wallet、session），支持 Coinbase CDP 和 Stripe Privy |
| `agent.py`                                     | 本地运行 Strands agent，调用付费端点（开发调试用）                       |
| `agent_runtime.py`                             | AgentCore Runtime 部署目标，用 `BedrockAgentCoreApp` 包装 agent 逻辑     |
| `deploy.py`                                    | 把 `agent_runtime.py` 打包部署到 AgentCore Runtime，并附加 IAM 权限策略   |
| `invoke.py`                                    | 用 boto3 调用已部署的 runtime（测试 / CI 用）                            |
| `.env.example`                                 | 配置模板，含 AWS / Coinbase / Privy 所有可选变量说明                     |
| `.gitignore`                                   | 忽略 `.env` / `.env.local` / toolkit 自动生成的文件                      |
| `requirements.txt`                             | Python 依赖                                                             |
| `docs/plans/2026-05-12-agentcore-payment-demo-design.md` | 初始设计文档（brainstorming 产出）                          |

## 先决条件

1. **AWS 账号**，CLI 凭证已配置（`aws configure`）。
2. **AWS Region** 必须支持 AgentCore Payments：
   `us-east-1`、`us-west-2`、`eu-central-1`、`ap-southeast-2`。
3. **Python 3.10+**。
4. **Coinbase Developer Platform 账号** —— 在
   [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/) 注册，然后拿
   下面三个凭证。见 [Coinbase CDP 凭证](#coinbase-cdp-凭证)。
5. **IAM 角色** 给 Payment Manager 用（见下）。

### Coinbase CDP 凭证

三个值，分别在两个不同的 portal 页面生成。

| 环境变量             | 所在页面             | 说明                                          |
|----------------------|----------------------|-----------------------------------------------|
| `CDP_API_KEY_ID`     | **API Keys**         | 创建 key 时显示。                             |
| `CDP_API_KEY_SECRET` | **API Keys**         | 创建时**只显示一次** —— 立即复制保存。        |
| `CDP_WALLET_SECRET`  | **Server Wallets**   | 独立页面。**只显示一次**。                    |

**步骤**：

1. 右上角选中你的 project。
2. 生成 API key：
   *API Keys* → **Create API key** → 同时复制 **API Key ID** 和
   **API Key Secret**。
3. 生成 Wallet Secret（**不在** API Keys 页面）：
   打开 <https://portal.cdp.coinbase.com/products/server-wallets>，
   找到 **Wallet Secret** 区域点 **Generate**。它是一段 base64 编码的
   PKCS8 EC 私钥，原样粘进 `CDP_WALLET_SECRET=`，不用加引号，不要重新换行。
4. **启用 Delegated signing**（容易漏，不开 AgentCore 签的交易会被 Coinbase 拒）：
   *Project → Wallet → Embedded Wallets → Policies* → 打开
   **Delegated signing** 开关。

任一凭证丢失只能重新生成，不可恢复。

### 切换到 Privy（可选）

默认用 Coinbase CDP。如果想改用 Privy（Stripe 钱包基础设施），在 `.env` 里：

```
PAYMENT_PROVIDER=privy
PRIVY_APP_ID=...
PRIVY_APP_SECRET=...
PRIVY_AUTH_ID=...
PRIVY_AUTH_PRIVATE_KEY=...
```

Privy 凭证从 <https://dashboard.privy.io/> 拿（建议建一个**专用于 AgentCore**
的 app，不要复用）：

- `PRIVY_APP_ID` / `PRIVY_APP_SECRET` —— app settings
- `PRIVY_AUTH_ID` / `PRIVY_AUTH_PRIVATE_KEY` ——
  *Wallet Infrastructure → Authorization → New Key*

> Privy 生成的私钥带 `wallet-auth:` 前缀，AgentCore 不接受。`setup.py`
> 会自动剥掉前缀，你也可以直接保存去前缀后的 base64 内容。

测试网流程完全一样（Base Sepolia、Circle 水龙头领 USDC、WalletHub 里
Grant permission）。Privy 相对 Coinbase 的核心差异 —— 用 Stripe 信用卡
**法币充值钱包** —— 在测试网被官方禁用，需要跑 mainnet 才能验证。

Coinbase 和 Privy 两条路径的资源命名带 provider 后缀（例如
`*-coinbase-cdp` vs `*-stripe-privy`），可以在同一账号里并存、互不干扰。

### 创建 IAM 服务角色

Payment Manager 需要一个信任 `bedrock-agentcore.amazonaws.com` 的角色。
最小 trust policy：

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
```

权限按
[IAM roles for AgentCore payments](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments-iam-roles.html)
配置。demo 用托管策略 `BedrockAgentCoreFullAccess`（或等价 inline policy）就够。

把角色 ARN 填到 `.env` 的 `PAYMENTS_ROLE_ARN`。

## 安装与配置

```bash
# 1. 装依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env：填 PAYMENTS_ROLE_ARN、CDP_*、USER_EMAIL

# 3. 在 AWS 上开资源（首次必做，幂等可重跑）
python setup.py all
```

`setup.py all` 成功后会在终端打印：

- 钱包地址（类似 `0x2A2c…B1B7`）
- **WalletHub 链接**（类似 `https://hub.cdp.coinbase.com/xxxxxx`）

下一步去那个 URL 给钱包开通权限 + 领测试币。

## 给测试钱包充值（Base Sepolia）

Base Sepolia 是 **免费测试网**，所有代币都从水龙头（faucet）无限免费领。
agent 的付款走 x402 协议、以 USDC 计价，Gas 费由 merchant 承担，所以
**只需要 USDC，不需要 ETH**。

### 1. 领 Base Sepolia USDC

打开 Circle 官方水龙头：<https://faucet.circle.com/>

- **Network**：选 **Base Sepolia**
- **Token**：选 **USDC**
- **Address**：粘贴你的 agent 钱包地址（`setup.py all` 打印的那个
  `0x…`，或 `python setup.py status` 里的 `address` 行）
- 点 **Send** → 10 秒左右到账

单次能领 10 USDC，足够这个 demo 跑几千次（x402 单次通常 ~0.001 USDC）。

### 2. 在 WalletHub 里 Grant permission（必须）

打开 `setup.py` 输出的那个 WalletHub URL，下方有 **Permissions** 区块。
点 **Grant permission** 完成授权 —— 这一步不做，agent 即使能签名也没法发
链上交易。

### 3. 验证余额

```bash
python setup.py status
```

看 `balance (BASE_SEPOLIA/USDC):` 这一行。显示 `10.0 USDC` 或类似数字就 OK 了。

> **注意：WalletHub UI 的余额只看主网，会显示 0，这是正常的。**
> 同一个 EVM 地址在主网和测试网上是两条完全独立的账本，你的测试币
> 确实在链上，WalletHub 那个消费级界面只是没展示。以 `python setup.py
> status` 或区块链浏览器（<https://sepolia.basescan.org/address/YOUR_ADDRESS>）
> 为准。

## 本地运行 agent

```bash
python agent.py
# 或指定别的付费端点：
python agent.py --url https://some-other-paid-api.example.com/resource
```

预期流程：

1. Agent 通过 `http_request` 工具发 `GET`。
2. 服务端返回 `402`，带 x402 支付要求。
3. 插件在 Base Sepolia 上签一笔极小额 USDC 交易。
4. 插件带上 `X-PAYMENT` 头重放请求，拿到 `200` 响应体。
5. Agent 打印响应内容。

## 部署到 AgentCore Runtime

本地 `agent.py` 每次跑都要拉依赖 + 本地 Python 环境；生产里一般希望把 agent
托管到云上的 **AgentCore Runtime**，通过 HTTP 调用它。

我们走的是 `bedrock-agentcore-starter-toolkit` 的 `Runtime` Python 接口，
部署目标是 `agent_runtime.py`（用 `BedrockAgentCoreApp` 把原来的逻辑包成
一个 HTTP entrypoint）。

### 1. 部署

```bash
python deploy.py
```

`deploy.py` 做这几件事：

1. `Runtime.configure(entrypoint="agent_runtime.py", ...)` —— 生成
   Dockerfile、.dockerignore、`.bedrock_agentcore.yaml`。
2. `Runtime.launch(env_vars={...})` —— 触发 CodeBuild 打镜像、推 ECR、
   创建/更新 AgentCore Runtime、等 endpoint 就绪。把 `PAYMENT_MANAGER_ARN`
   / `PAYMENT_INSTRUMENT_ID` / `PAYMENT_SESSION_ID` / `USER_ID` /
   `NETWORK_PREFERENCES` 从 `.env.local` 注入容器。
3. `get_agent_runtime` 取出自动创建的执行角色 ARN，给它附上
   `AgentCorePaymentDemoExtras` inline policy（payment data plane +
   Bedrock invoke 权限）。如果跳过这一步，runtime 里插件无法访问
   payment API。
4. 把 `RUNTIME_AGENT_ARN` / `RUNTIME_AGENT_ID` 写回 `.env.local`。

整个过程约 2–4 分钟（CodeBuild + endpoint provisioning）。

### 2. 调用

```bash
python invoke.py
# 或指定 URL / prompt：
python invoke.py --url https://some-other-paid-api.example.com/resource
python invoke.py --prompt "Summarize the market recap at https://..."
# 复用会话：
python invoke.py --session-id invoke-<uuid>
```

`invoke.py` 用原生 boto3 `bedrock-agentcore:InvokeAgentRuntime` 调用，
SigV4 签名走本地 AWS 凭证，不需要 starter toolkit 配置文件在场 ——
把 `RUNTIME_AGENT_ARN` 丢到 CI 里也能用。

### 3. 更新代码

改完 `agent_runtime.py` 直接再跑 `python deploy.py`；toolkit 会复用同一个
agent_name，触发新镜像构建，runtime 滚动到新版本。ARN 不变。

### 4. Session 过期后更新 runtime 的 env vars

Payment session 60 分钟过期。runtime 启动时把 session id 作为环境变量固化
在容器里，session 一过期，runtime 里的插件就会报
`PaymentSessionNotFound`。刷新流程：

```bash
python setup.py session   # 建新 session，PAYMENT_SESSION_ID 写到 .env.local
python deploy.py          # 重新 launch，把新的 session id 灌进容器
```

生产场景应该让 agent 按需自己 `create_payment_session`，而不是把一个固定
session id 写死到容器里。demo 偷懒了。

### 5. 看日志

CodeBuild 完成后脚本会打印 CloudWatch 日志组名，例如：

```
/aws/bedrock-agentcore/runtimes/agentcorePaymentDemo-<id>-DEFAULT
```

拉最近一次调用的日志：

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/agentcorePaymentDemo-XXX-DEFAULT \
    --since 5m --format short
```

插件的 DEBUG 日志已经在 `agent_runtime.py` 里开好了，留意这几个关键
事件：
- `AfterToolCallEvent: tool=http_request` —— 插件 hook 触发
- `Detected 402 Payment Required response` —— 检测到付费要求
- `Processing payment of type CRYPTO_X402` —— 调 ProcessPayment
- `PROOF_GENERATED` / `Session not found or expired` —— 成功 / 失败

### 6. 清理 runtime

```bash
# 删 runtime
agentcore destroy   # 交互式
# 或直接 API：
aws bedrock-agentcore-control delete-agent-runtime \
    --agent-runtime-id $RUNTIME_AGENT_ID --region $AWS_REGION

# 删 ECR 仓库（可选）
aws ecr delete-repository --repository-name bedrock-agentcore-agentcorepaymentdemo \
    --force --region $AWS_REGION
```

### Runtime 专属的故障排查

**Agent 响应里说"不能签 EIP-3009"** —— 插件的 after_tool_call hook 没触发，
agent 降级用 LLM 推理处理 402。常见原因：
- 执行角色缺 `bedrock-agentcore:ProcessPayment` 等权限 → 重跑 `deploy.py`
  让它重新附策略。
- Payment session 过期（日志里有 `PaymentSessionNotFound`）→
  `python setup.py session && python deploy.py`。

**`AccessDeniedException: ListPaymentInstruments`** —— 执行角色权限不够。
一般是 `deploy.py` 没跑完（比如中途 Ctrl+C），inline policy 没附上。
重跑 `python deploy.py`。

**首次调用特别慢（30+ 秒）** —— 容器冷启动 + Strands 初始化 + 插件
建立 PaymentManager 客户端。后续调用在同一 container 里会很快。

## 常用子命令

```bash
python setup.py status      # 查看所有资源状态、余额、session 预算
python setup.py session     # 新建 session（旧的过期后用这个）
python setup.py instrument  # 只重建 wallet
python setup.py manager     # 只重建 manager + connector + credential provider
```

Session 默认 60 分钟过期。`agent.py` 如果抛
`PaymentSessionConfigurationRequired`，跑一下 `python setup.py session`
然后重试即可。

## 故障排查

**`PAYMENTS_ROLE_ARN` 报错**：角色不存在、没信任
`bedrock-agentcore.amazonaws.com`，或权限不足。参考
[payments-iam-roles](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments-iam-roles.html)。

**Coinbase 返回 `InvalidCredentialException`**：三件 CDP 凭证有错，
`CDP_WALLET_SECRET` 必须是完整的 base64 字符串，不要换行或截断。

**Agent 收到 402 但插件没触发**：确认 Agent 实例的 `plugins=[plugin]`
传了，且使用的是 `http_request` 工具（插件 hook 的是 Strands 的 HTTP 工具）。

**WalletHub 显示 0 余额**：上面已经解释 —— UI 只看主网，用
`python setup.py status` 查真实余额。

**`balance: (unavailable: No balance found…)`**：钱包还没被充值，去 Circle
水龙头领一下。

**遗留的孤儿 connector**（只可能出现在 2026-05 之前的 setup.py 上）：
服务端的 connector name 正则接受下划线，但生成的 connector ID 正则不接受 ——
导致某些名字的 connector 一旦建出来就既 Get 不到也 Delete 不掉。
当前代码生成的名字是纯小写字母数字，两个正则都过，不会再产生孤儿。
历史遗留的孤儿不影响新资源运行，如需清理请开 AWS Support ticket。

## 清理

```bash
# 在部署的那个 region：
aws bedrock-agentcore delete-payment-session \
    --payment-manager-arn $PAYMENT_MANAGER_ARN \
    --payment-session-id $PAYMENT_SESSION_ID --region $AWS_REGION

aws bedrock-agentcore delete-payment-instrument \
    --payment-manager-arn $PAYMENT_MANAGER_ARN \
    --payment-instrument-id $PAYMENT_INSTRUMENT_ID --region $AWS_REGION

aws bedrock-agentcore-control delete-payment-manager \
    --payment-manager-id $(basename $PAYMENT_MANAGER_ARN) --region $AWS_REGION
```

剩余的 credential provider 在 AWS Console 的 AgentCore Identity 里删除。

## AgentCorePaymentsPlugin 原理

### 一句话

`AgentCorePaymentsPlugin` 是一个 Strands Agent 插件，通过 hook agent 的工具
调用生命周期，在 `http_request` 返回 `402` 时**自动签付 x402 交易并重放请求**，
agent 代码本身无感。

### 架构

```
┌──────────────────────────────────────────────────┐
│  Strands Agent                                   │
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │ http_request │  │ AgentCorePaymentsPlugin   │ │
│  │   (tool)     │  │                           │ │
│  └──────┬───────┘  │  hooks:                   │ │
│         │          │   before_tool_call()       │ │
│         │          │   after_tool_call()  ◄─────┼─┤ 拦截 402
│         │          │                           │ │
│         │          │  注入的 agent 工具:        │ │
│         │          │   get_payment_instrument   │ │
│         │          │   list_payment_instruments │ │
│         │          │   get_payment_instrument_  │ │
│         │          │     balance                │ │
│         │          │   get_payment_session      │ │
│         │          │                           │ │
│         │          │  内部:                     │ │
│         │          │   PaymentManager           │ │
│         │          │     .generate_payment_     │ │
│         │          │       header()             │ │
│         │          │     .process_payment()     │ │
│         │          └───────────────────────────┘ │
└──────────────────────────────────────────────────┘
                          │
                          ▼
              AWS AgentCore Payments 服务
              (ProcessPayment API → 签 EIP-3009)
                          │
                          ▼
              Coinbase CDP / Privy  (钱包签名)
```

### after_tool_call 核心流程

```
http_request 返回结果
       │
       ▼
auto_payment 开启？ ──── 否 ──→ 放行，agent 自行处理
       │ 是
       ▼
tool 在 allowlist 里？ ── 否 ──→ 放行
       │ 是 (或 allowlist=None)
       ▼
提取 HTTP status code
       │
       ▼
是 402？ ─────────────── 否 ──→ 放行
       │ 是
       ▼
解析 x402 payload
(从 PAYMENT-REQUIRED header 或 body 取 scheme / network / amount / asset / payTo)
       │
       ▼
调 ProcessPayment API
  → AWS 内部：取钱包凭证 → 构造 EIP-3009 transferWithAuthorization
  → 通过 Coinbase/Privy 签名 → 返回 X-PAYMENT header 值
       │
       ▼
用签好的 header 重放原始请求
       │
       ▼
重放返回 200？ ─── 是 ──→ 替换 event.result，agent 看到的是 200 body
       │ 否
       ▼
存储失败状态，raise interrupt
  → agent 的 result.stop_reason == "interrupt"
  → 我们的代码用 _handle_interrupt() 响应
```

### 两种模式

**自动模式**（默认 `auto_payment=True`）：
- 整个签付 + 重放在 `after_tool_call` hook 里完成
- agent 的 LLM 根本看不到 402 —— 它只看到最终的 200 body
- 如果签付失败（simulation error、session expired 等），plugin 通过 Strands
  的 **interrupt 机制** 把失败信息抛给 agent，agent 可以用注入的工具诊断后重试

**手动模式**（`auto_payment=False`）：
- hook 不自动签付
- 只注入 4 个查询工具，让 agent/人类自己决定要不要付
- 适合 human-in-the-loop 场景

### 关键设计细节

**为什么第一次经常 `simulation_failed`？**

`ProcessPayment` 在签名前会做链上 simulation（dry-run）。如果钱包的 USDC
allowance 或 nonce 状态不对，simulation 会失败。Plugin 把这个失败包成
interrupt 抛出，agent 用 `get_payment_instrument_balance` 查余额确认够用后
重试 —— 第二次 simulation 通常就过了（第一次请求在链上刷新了 nonce / state）。

**`payment_tool_allowlist`**：

如果 agent 有多个 HTTP 工具（例如 `http_request` + `browser`），可以只让
特定工具触发付款：

```python
config = AgentCorePaymentsPluginConfig(
    payment_tool_allowlist=["http_request"],  # browser 的 402 不自动付
    ...
)
```

默认 `None` 表示所有工具都参与 402 拦截。

**`network_preferences_config`**：

x402 payload 里 `accepts` 数组可能列出多条链/资产。Plugin 按你给的 CAIP-2
优先级排序，选第一个匹配的。Demo 里设了
`["base-sepolia", "eip155:84532"]`，Base Sepolia 优先。

**interrupt 不是异常**：

Strands 的 interrupt 是一种**协作式中断** —— `result.stop_reason == "interrupt"`
时，你给 `result.interrupts` 里每个 interrupt 一个 response，然后
`agent(responses)` 继续对话。这就是 `agent.py` 里 `while` 循环做的事：

```python
result = agent(prompt)
while getattr(result, "stop_reason", None) == "interrupt":
    responses = [_handle_interrupt(i) for i in result.interrupts]
    result = agent(responses)   # 带着 interrupt response 继续
```

## 设计文档

见 [`docs/plans/2026-05-12-agentcore-payment-demo-design.md`](docs/plans/2026-05-12-agentcore-payment-demo-design.md)。
