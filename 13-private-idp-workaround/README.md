# Private IdP 无 VPC Lattice 时的 Workaround —— 实测验证与落地指南

## [English](README.en.md)

对应设计文档 `AgentCore 中国区无 VPC Egress Workaround 方案.html` 第 09 节
（补充 · AgentCore Identity 不支持 Private IdP 的 Workaround）的**真机验证**。

> 该节原本标注"未实测"。**本目录已把它实测完毕，结论成立**，并在过程中发现 3 个
> 文档没写的行为（其中一个会让请求全部失败）。文档已据此更新。

已在 **us-east-2** 部署验证，复用 [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)
那个**零互联网出口 VPC**（无 IGW、无 NAT）与其中的私有 RDS。

---

## 一句话结论

**interceptor Lambda 绕行方案完全可行，9/9 项验证全部通过。**

在一个 AgentCore Identity 无法访问的私有 IdP 场景下（IdP 无公网 IP、只允许
Lambda 安全组访问、VPC 无任何出网路由），我们做到了：

- **入站**：Gateway 入站鉴权设为 `NONE`，由挂在 VPC 里的 **REQUEST interceptor Lambda**
  私有拉取 IdP 的 JWKS 并完成 RS256 验签 + `iss`/`aud`/`exp`/`scope` 校验，
  非法令牌一律短路拒绝，**根本不会到达 target**。
- **出站**：工具 Lambda 自己在 VPC 内向私有 IdP 做 `client_credentials` 换令牌，
  再查私有 RDS，**完全不需要 AgentCore Identity 的 OAuth credential provider**。

| 指标 | 实测值 |
|---|---|
| 验证用例 | **9 / 9 通过** |
| interceptor 温调用开销 | **p50 2.00 ms**（min 1.43 ms，n=216） |
| interceptor 冷启动 | init ≈ **250 ms** + 首次调用 254 ms（含拉 JWKS） |
| IdP 公网可达性 | `PublicIpAddress = null`，从 VPC 外连接 → `TimeoutError` |

---

## 实测架构

```
  测试客户端（带 IdP 签发的 JWT）
        │  MCP over HTTPS，无 SigV4（Gateway 入站 = NONE）
        ▼
 ┌────────────────────────────────────────────┐
 │ AgentCore Gateway  (authorizerType=NONE)   │
 │   REQUEST interceptor ──┐                  │
 └───────────┬─────────────┼──────────────────┘
             │             │ passRequestHeaders=true
             │             ▼
             │   ┌──────────────────────────┐
             │   │ interceptor Lambda (VPC) │──┐ 拉 JWKS / 验签
             │   └──────────────────────────┘  │
             │        非法 → 403 短路           │
             ▼                                 │
      ┌──────────────────┐                     │
      │ tool Lambda (VPC)│──┐ client_credentials│
      └────────┬─────────┘  │                  │
               │            ▼                  ▼
               │   ┌──────────────────────────────────┐
               │   │ 私有 IdP (EC2 10.30.11.13:8081)   │
               │   │ 无公网 IP，仅 Lambda SG 可达       │
               │   │ /jwks  /token  /.well-known/...  │
               │   └──────────────────────────────────┘
               ▼
      ┌────────────────────────┐
      │ 私有 RDS MySQL 8.0.42  │   VPC：无 IGW / 无 NAT
      └────────────────────────┘
```

---

## 验证矩阵（9/9）

`python scripts/04-verify.py` 的实际输出，完整记录见
[`results/verification.json`](results/verification.json)：

| # | 用例 | 结果 | interceptor 返回的原因 |
|---|---|---|---|
| 1 | 合法令牌 | ✅ **到达 target** | 返回 2 条 PENDING 订单 + 出站令牌来自 `10.30.11.13` |
| 2 | 不带 Authorization 头 | 🚫 拦截 | `missing bearer token` |
| 3 | 畸形令牌 | 🚫 拦截 | `token rejected` |
| 4 | **用攻击者密钥伪造签名** | 🚫 拦截 | `signature verification failed` |
| 5 | 过期令牌 | 🚫 拦截 | `token expired` |
| 6 | 错误 audience | 🚫 拦截 | `wrong audience` |
| 7 | 错误 issuer | 🚫 拦截 | `wrong issuer` |
| 8 | 缺少必需 scope | 🚫 拦截 | `missing required scope orders.read` |
| 9 | 未知签名 `kid` | 🚫 拦截 | `token rejected` |

**为什么这些用例有说服力：**

- 用例 4 用的是**从未交给 IdP 的第二把 RSA 私钥**签的令牌。能识别出来，说明
  interceptor 真的拿到了 IdP 的公钥在验签，而不是只解了 base64。
- 用例 9 的 `kid` 在 IdP 的 JWKS 里不存在 → `PyJWKClient` 找不到对应 key 而失败，
  **反证了 JWKS 确实是从私有 IdP 拉回来的**。
- 令牌 2、5、6、7、8 都是用**真实私钥**签的合法签名，只是声明有问题——证明校验不止看签名。

---

## 3 个文档没写的行为（实测发现）

### ⚠️ 1. MCP REQUEST interceptor 放行时**必须回显原始 body**，否则请求全挂

直觉写法是"什么都不改就返回空对象"：

```json
{ "interceptorOutputVersion": "1.0", "mcp": {} }
```

**实测结果：客户端收到 `HTTP 200` + JSON-RPC `Parse error - Invalid JSON format`，
请求根本到不了 target。** 注意此时 9 个负例全部正常（`transformedGatewayResponse`
短路路径没问题），**只有放行路径是坏的**——很容易误判成"拦截逻辑写错了"。

正确写法是把原始 body 回显出来：

```json
{ "interceptorOutputVersion": "1.0",
  "mcp": { "transformedGatewayRequest": { "body": <原始 JSON-RPC body> } } }
```

官方文档里"原样透传就返回空对象"（`{"interceptorOutputVersion":"1.0","http":{}}`）
是写在 **HTTP target 的 RESPONSE interceptor** 一节的，**不适用于 MCP target 的
REQUEST interceptor**。

### ⚠️ 2. `passRequestHeaders=false` 时拿到的是**空字典**，不是缺失字段

关掉 `passRequestHeaders` 后，interceptor 收到的是 `headers: {}`，而**不是**没有
`headers` 这个键。后果：

```python
if headers is None:      # ← 永远不成立，检测不到配置错误
```

会让配置错误伪装成"客户端没带令牌"，把人引向错误的排查方向（我们实测时正是先看到
`missing bearer token`，而真实原因是 `passRequestHeaders` 没开）。应当按**空值**判断：

```python
if not headers:          # ← 空字典也能捕获
    return deny(request_id, "interceptor cannot see request headers",
                "set inputConfiguration.passRequestHeaders=true")
```

### 3. `NONE` 入站 + interceptor 的组合可以正常创建并工作

`authorizerType=NONE` 搭配 REQUEST interceptor **可以直接创建**，Gateway 正常
`READY`，且 `Authorization` 头能完整交给 interceptor 使用。这点很关键：入站换成
`AWS_IAM` 的话，`Authorization` 会被 SigV4 签名占用，业务 JWT 就得改走自定义头。

<div>

> ⚠️ **安全提醒**：`NONE` 意味着 Gateway 端点**没有平台层鉴权**，interceptor 是唯一
> 关卡。interceptor Lambda 报错、超时或被误删都会直接变成"敞口"或"全拒"。生产环境
> 应当：给 interceptor 配 Provisioned Concurrency 与告警；Gateway 执行角色
> **只**授权这一个函数的 `lambda:InvokeFunction`；并考虑 `AWS_IAM` + 自定义头做双层。

</div>

---

## 落地要点

### interceptor 的性能开销可以忽略（但要缓存）

`PyJWKClient` 放在**模块作用域**，缓存随执行环境复用：

```python
_jwk_client = PyJWKClient(IDP_JWKS_URL, cache_keys=True, lifespan=300)
```

实测（n=216）：温调用 **p50 2.00 ms**；只有冷启动后的第一次调用是 **254 ms**（要拉 JWKS）；
冷启动 init ≈ **250 ms**（导入 `cryptography` / `PyJWT`）。
**不缓存的话每次 Gateway 调用都会打一次 IdP**，延迟与 IdP 压力都会被放大。

### 出站优先让工具 Lambda 自己换令牌

工具 Lambda 本来就在 VPC 里，直接做 `client_credentials` 即可，还能顺手做缓存
（实测 `token_from_cache: true`）。这条路**完全不碰 AgentCore Identity**，
也就不存在"是否支持 Private IdP"的问题——比试图用 interceptor 改写 Authorization 更省事。

> 关于原文档留的悬念"MCP target 能否通过 interceptor 改写 Authorization"：本次
> **未验证该点**。因为对 Lambda target 而言 HTTP 头不可观测（Lambda 收到的是工具
> 入参，不是 HTTP 请求），无法构造干净的观测实验。上面这条路径已经把出站需求解掉，
> 建议直接采用，不必依赖头改写。

### 不用 HTTPS 也能跑——这是绕行方案的意外优势

原生 `privateEndpoint` 路径要求 IdP 的 discovery URL **必须 HTTPS**、且证书必须
**公信**（否则得在前面加带公有 ACM 证书的内网 ALB）。走 interceptor 时**这些约束都不存在**，
因为 HTTP 客户端是你自己的代码——本 demo 的 IdP 就是纯 HTTP 的 `http://10.30.11.13:8081`。
内网短链路可以这么做，但**生产仍建议 TLS**，只是不再被"公信证书"绑住。

---

## 目录结构

```
idp/idp_server.py        最小 OIDC 授权服务器（Keycloak / PingFederate 的替身）
                         /.well-known/openid-configuration /jwks /token /health
lambda/interceptor.py    REQUEST interceptor：入站 JWT 校验（PyJWT + 私有 JWKS）
lambda/tool.py           工具 Lambda：出站 client_credentials + 查私有 RDS
scripts/01-idp.sh        生成密钥、打包依赖、在私有子网起 IdP 实例
scripts/02-lambdas.sh    构建并部署两个 Lambda，并确认 IdP 私有可达
scripts/03-gateway.sh    创建 NONE 入站 + REQUEST interceptor 的 Gateway 与 target
scripts/04-verify.py     9 项验证矩阵（本地铸造合法/非法令牌）
scripts/05-collect-evidence.sh  归档全部原始证据
scripts/cleanup.sh       清理本项目资源
results/                 verification.json / evidence.txt / interceptor-latency.json
```

## 如何复现

前置：先部署 [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)
（提供隔离 VPC、私有 RDS 与 S3 引导桶）。

```bash
cd 13-private-idp-workaround
python3 -m venv .venv && .venv/bin/pip install "pyjwt[crypto]" boto3

bash scripts/01-idp.sh          # 私有 IdP（EC2，无公网 IP），约 2-3 分钟
bash scripts/02-lambdas.sh      # interceptor + tool Lambda，并等 IdP 就绪
bash scripts/03-gateway.sh      # Gateway（NONE 入站 + interceptor）+ target
.venv/bin/python scripts/04-verify.py       # 9 项验证矩阵
bash scripts/05-collect-evidence.sh          # 归档证据
```

`scripts/04-verify.py` 任一用例行为不符预期即以非零码退出，可直接当回归测试用。

## 两个构建期的坑（脚本里已处理）

- **AL2023 AMI 的 `python3` 是 3.9，且没有 pip。** 所以 wheel 要按 `cp39` 解析，
  并在实例上用 `python3 -m zipfile -e` 直接解包（wheel 本身就是 zip）。
  按 3.11 解析会拿到 `cp311-abi3` 的 `cryptography`，在 3.9 上装不了。
- **构建机是 arm64，Lambda 与 EC2 是 x86_64。** 跨平台安装必须
  `pip install --platform manylinux2014_x86_64 --only-binary=:all: --target ...`；
  普通 `pip install` 会装成 arm64 的 `cryptography`，上云直接 import 失败。

## 成本

新增约 **$0.01/小时**（IdP 用的 t3.micro）。Lambda 与 Gateway 按调用计费。
其余成本来自 `11-vpc-no-egress-workaround`（RDS/NLB/EC2 约 $0.10/小时）。

## 清理

```bash
bash scripts/cleanup.sh --yes                                  # 本项目
bash ../11-vpc-no-egress-workaround/scripts/cleanup.sh --yes   # VPC / RDS / Gateway
```

## 安全声明

`idp/idp_server.py` 是**用于演示的最小实现**：无刷新令牌、无客户端注册、无速率限制、
无密钥轮转，且以 HTTP 提供服务。它只用来模拟"一个内网里的 OIDC 服务器"。
**请勿用于生产**——生产请用 Keycloak / PingFederate 等成熟实现。
安全关键的**校验**一侧（interceptor）用的是 `PyJWT` + `cryptography`，没有自研密码学。
