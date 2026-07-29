# Strands + Nova 2 Lite on AgentCore Runtime（PUBLIC）—— 端到端可运行示例

## [English](README.en.md)

用 **Strands Agents SDK** 写一个 Agent，模型使用 **`global.amazon.nova-2-lite-v1:0`**，
部署到 **AgentCore Runtime（`networkMode=PUBLIC`，无任何 VPC 配置）**，
并通过 AgentCore Gateway 的 MCP 工具读取 [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)
里那个**零互联网出口 VPC 中的私有 RDS MySQL**。

已在 **us-east-2** 部署并验证通过。

---

## 端到端链路

```
   本地 / 任意调用方
        │  InvokeAgentRuntime (SigV4)
        ▼
 ┌──────────────────────────────────────┐
 │ AgentCore Runtime                    │   networkMode = PUBLIC
 │ strands_nova2_orderdesk              │   ← 完全没有 vpcConfig
 │                                      │
 │  Strands Agent                       │──── Bedrock ──▶ global.amazon.nova-2-lite-v1:0
 │  + MCPClient (SigV4 签名)             │
 └───────────────┬──────────────────────┘
                 │ MCP over HTTPS (AWS_IAM 入站)
                 ▼
        ┌────────────────────┐
        │ AgentCore Gateway  │  4 个工具
        └─────┬──────────┬───┘
              │          │
      ┌───────▼──┐   ┌───▼──────────────────────────┐
      │ Lambda   │   │ API GW → VPC Link → NLB → EC2│
      │ (挂 VPC) │   │                              │
      └───────┬──┘   └───┬──────────────────────────┘
              │          │
              ▼          ▼
      ┌────────────────────────────────┐
      │ 私有 RDS MySQL 8.0.42          │  隔离 VPC：无 IGW / 无 NAT
      │ PubliclyAccessible = false     │
      └────────────────────────────────┘
```

<b>这条链路同时也是原设计文档里方案一与方案二的端到端验证</b>：Runtime 本身
没有任何 VPC 出站能力，但把「访问私有资源」下沉成 Gateway 工具后，Agent 依然能读到内网数据库。

---

## 验证结果

`python scripts/invoke.py` 的实际输出（完整记录见 [`results/invocations.json`](results/invocations.json)）：

| # | 提问 | 回答 | 调用的工具 | 延迟 |
|---|---|---|---|---|
| 1 | 有多少 pending 订单，订单号是什么？ | There are 2 pending orders: ORD-1002 and ORD-1005. | `rdsLambda___list_orders` | 2198 ms |
| 2 | 哪个订单被取消了，金额多少？ | The cancelled order was ORD-1004 for $15.25. | `rdsLambda___list_orders` | 1859 ms |
| 3 | 数据库 MySQL 版本和主机？ | MySQL **8.0.42** on host **ip-172-31-0-80**. | `rdsLambda___db_info` | 1989 ms |
| 4 | 所有已发货订单总额？ | **$1,020.99**（ORD-1001 $129.99 + ORD-1003 $891.00） | `rdsLambda___list_orders` | 1680 ms |
| 5 | 指定用 API Gateway 那条链路查 | ec2_private_ip `10.30.11.109`，db_user `agentadmin@10.30.11.109` | `rdsApi___getDbInfo` | 1820 ms |

要点：

- **数据是真的。** 第 4 题的 $1,020.99 是 Nova 2 Lite 拿到工具返回的两条记录后自己算出来的；
  第 5 题返回的 `10.30.11.109` 是私有子网里 EC2 的地址。
- **两条链路都通。** 默认模型偏向选 `rdsLambda___*`（描述更直接），显式点名时
  `rdsApi___*`（API GW → VPC Link → NLB → EC2）同样可用。
- **Runtime 无 VPC 配置**，实测 `get-agent-runtime` 返回仅 `{"networkMode":"PUBLIC"}`。
- `invoke.py` 在任意一题「没有调用工具」或「返回 error」时以非零码退出，可直接用于 CI 冒烟测试。

---

## 关键实现点

### 1. Nova 2 Lite 通过 global 推理配置调用

```python
model = BedrockModel(model_id="global.amazon.nova-2-lite-v1:0", region_name=REGION)
```

`global.` 前缀是**跨区域推理配置**，可能路由到任意区域，所以 IAM 必须同时授权
**推理配置**与**底层基础模型**，且区域用通配：

```json
"Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
"Resource": [
  "arn:aws:bedrock:*:ACCOUNT:inference-profile/global.amazon.nova-2-lite-v1:0",
  "arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0"
]
```
只授权其中一个会报 AccessDenied。

### 2. Gateway 入站是 `AWS_IAM`，MCP 必须逐请求 SigV4 签名

Gateway 用 `AWS_IAM` 时不能用 Bearer token，而 SigV4 签名覆盖**请求体和时间戳**，
所以静态 `headers={...}` 无效——必须每个请求重新签。MCP Python SDK 的
`streamable_http_client` 接受 `httpx.AsyncClient`，正好可以挂一个自定义 `httpx.Auth`
（见 [`agent/sigv4_auth.py`](agent/sigv4_auth.py)）：

```python
class SigV4HttpxAuth(httpx.Auth):
    requires_request_body = True          # 先取到 body，签名才对得上
    def auth_flow(self, request):
        creds = self._session.get_credentials().get_frozen_credentials()
        signable = AWSRequest(method=request.method, url=str(request.url),
                              data=request.content,
                              headers={"Content-Type": "application/json"})
        _BotoSigV4(creds, "bedrock-agentcore", self._region).add_auth(signable)
        for k, v in signable.headers.items():
            request.headers[k] = v
        yield request
```

只签一个最小 header 集即可：SigV4 只要求**被签的 header 原样送达**，httpx 之后追加的
`accept` / `mcp-session-id` / `content-length` 不影响校验。

### 3. httpx client 的生命周期必须自己管，否则每次调用泄漏连接池

`streamable_http_client(url, http_client=...)` **不会关闭**外部传入的 client
（源码里 `client_provided=True` 时不进 ExitStack）。所以用一层 async context manager
把它包住，在 MCP 自己的事件循环里完成关闭：

```python
@asynccontextmanager
async def transport():
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(60.0, read=300.0),
                                 follow_redirects=True) as http_client:
        async with streamable_http_client(GATEWAY_URL, http_client=http_client) as streams:
            yield streams

return MCPClient(transport)
```

> 注意：老的 `streamablehttp_client(url=..., auth=...)` 已 deprecated，但它**自己管**
> client 生命周期。换用新 API 时如果只是照搬参数，就会漏掉这一步。

MCP 会话按**每次 invocation** 建立并释放，避免并发 runtime session 共用同一会话。

### 4. `apiGateway` 那条链路：必须禁止模型填 `basePath`

`apiGateway` target 自动生成的工具，输入 schema 里会多出一个 `basePath` 字段，
既没有描述也没有约束。模型看到就可能去填，一填 URL 就被改坏：

```
Server URL parameter 'basePath' contains invalid character '/'
Client error: API request failed with status: 403 - {"message":"Forbidden"}
```

这个坑**不稳定**，同一个提问模型有时不填就成功，有时填了就 403，很容易误判成 IAM 权限问题。
分辨方法看 403 的响应体：`{"Message":"User: ... is not authorized ..."}` 是真的 IAM 拒绝，
`{"message":"Forbidden"}` 只是路径没匹配上。

解法是在 system prompt 里明确写一句 never pass a `basePath` argument。
加上之后连续三次调用全部成功，稳定拿到 `10.30.11.109`。
面向 Agent 的工具更推荐用 Lambda target，schema 完全由你自己的 `toolSchema` 决定。

### 5. 镜像必须是 arm64

AgentCore Runtime 只接受 `linux/arm64` 镜像。`deploy.sh` 构建后会校验
`docker image inspect --format '{{.Architecture}}'`，不是 arm64 直接失败，
避免推上去再报错。

### 6. 新建角色后 CreateAgentRuntime 会因 IAM 传播失败

刚创建执行角色就调 `CreateAgentRuntime`，会得到：

```
ValidationException: Role validation failed for '...'.
Please verify that the role exists and its trust policy allows assumption by this service
```

这是**传播延迟**，不是配置错误。`deploy.sh` 对「`ValidationException` 且消息含
`Role validation failed`」重试，其余 `ValidationException` 才视为致命——
不要把所有 `ValidationException` 都当成配置错误直接退出。

---

## 目录结构

```
agent/agent.py          Strands Agent + BedrockAgentCoreApp entrypoint
agent/sigv4_auth.py     httpx.Auth，为 MCP 请求做 SigV4 签名
agent/requirements.txt  锁定版本
docker/Dockerfile       linux/arm64 镜像
scripts/deploy.sh       构建 → ECR → IAM → 创建/更新 PUBLIC runtime（幂等）
scripts/invoke.py       调用已部署 runtime，跑 5 题验证套件并存档
scripts/test_local.sh   本地起容器打 /ping 与 /invocations
scripts/cleanup.sh      删除 runtime / ECR / role
runtime.json            部署产物（runtime ARN、镜像、模型等）
results/invocations.json 实测原始输出
```

---

## 如何运行

前置：先跑完 [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)（提供 Gateway 与私有 RDS）。
`deploy.sh` 会自动从它的 `state.env` 读取 `GW_URL` / `GW_ARN`。

```bash
cd 12-strands-nova2-runtime
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt

# 建议先本地验证，比部署快得多
bash scripts/test_local.sh "How many orders are pending?"

# 部署到 PUBLIC AgentCore Runtime
bash scripts/deploy.sh

# 验证
.venv/bin/python scripts/invoke.py
.venv/bin/python scripts/invoke.py "Which orders were cancelled?"
```

不想接 Gateway、只想跑一个纯 Nova 2 Lite Agent，把 `GATEWAY_URL` 设为空即可
（Agent 会以无工具模式运行）。

## 成本

Runtime 按实际调用的 CPU/内存计费，空闲不收费（`idleRuntimeSessionTimeout=900`）；
Nova 2 Lite 按 token 计费，是 Nova 系列里最便宜的档位之一。ECR 存储约 220 MB。
主要成本来自 `11-vpc-no-egress-workaround` 的 RDS/NLB/EC2（约 $0.10/小时）。

## 清理

```bash
bash scripts/cleanup.sh --yes                        # 本目录的 runtime / ECR / role
bash ../11-vpc-no-egress-workaround/scripts/cleanup.sh --yes   # Gateway / VPC / RDS
```
