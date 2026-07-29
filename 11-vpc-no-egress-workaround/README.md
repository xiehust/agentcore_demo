# AgentCore 无 VPC Egress Workaround —— 实测验证

## [English](README.en.md)

本目录是对 `AgentCore 中国区无 VPC Egress Workaround 方案.html` 中**方案一（Lambda 桥接）**
和**方案二（API Gateway + VPC Link）**的真实部署验证。

由于 AgentCore 尚未在中国区上线，验证在 **us-east-2（俄亥俄）** 完成。
为了尽可能贴近"没有原生 VPC Egress、网络强隔离"的中国区首发假设，目标 VPC 被刻意构造成
**完全没有互联网出口**：没有 Internet Gateway、没有 NAT Gateway、路由表里没有 `0.0.0.0/0`。

---

## 一句话结论

**两个方案都验证通过。** 在一个零互联网出口的 VPC 里，AgentCore Gateway 成功读到了私有
RDS MySQL 的真实数据。文档的核心判断是正确的：访问 VPC 内私有资源**不依赖** Gateway 自身的
VPC Egress 能力。

但实测中发现了 **4 个文档没有写、或者写错的地方**，其中一个是安全问题（见
[实测发现](#实测发现与文档勘误)）——照文档给的资源策略配置，要么锁不住，要么把 Gateway 自己锁死。

| 方案 | 结果 | 端到端延迟 p50 | 关键证据 |
|---|---|---|---|
| ① Lambda 桥接 | ✅ 通过 | 730 ms | RDS 记录到的客户端 IP = `10.30.11.161`（Lambda ENI，私有子网内） |
| ② API GW + VPC Link | ✅ 通过 | 737 ms | RDS 记录到的客户端 IP = `10.30.11.109`（EC2 私有 IP） |

> 延迟包含本地客户端进程启动与一次完整 MCP `initialize` 握手，不是纯服务端耗时；
> 两条链路差异在噪声范围内。原始数据见 `results/latency.json`。

---

## 实测架构

```
                    ┌─────────────────────────────────────────────────────┐
                    │  VPC 10.30.0.0/16  (无 IGW / 无 NAT / 无默认路由)     │
                    │                                                     │
  ┌──────────┐      │   ┌───────────────────┐                             │
  │  本地     │ MCP  │   │ Lambda (VPC 挂载)  │──┐                          │
  │  客户端   │─────┐│   │ 私有子网 a/b        │  │                          │
  │ (SigV4)  │     ││   └───────────────────┘  │  3306                    │
  └──────────┘     ││                          ▼                          │
                   ││   ┌──────────────────────────────┐                  │
   ┌───────────────▼┼──▶│ RDS MySQL 8.0.42             │                  │
   │  AgentCore     ││   │ PubliclyAccessible = false   │                  │
   │  Gateway       ││   │ 10.30.11.229                 │                  │
   │  (AWS_IAM 入站) ││   └──────────────────────────────┘                  │
   └───────┬────────┘│                          ▲                          │
           │ SigV4   │                          │ 3306                     │
           ▼         │   ┌───────────────────┐  │                          │
   ┌──────────────┐  │   │ EC2 (私有子网)     │──┘                          │
   │ REST API     │  │   │ 10.30.11.109:8080 │                             │
   │ (Regional)   │  │   └─────────▲─────────┘                             │
   └──────┬───────┘  │             │                                       │
          │ VPC Link │   ┌─────────┴─────────┐                             │
          └──────────┼──▶│ 内网 NLB (internal)│                            │
                     │   └───────────────────┘                             │
                     └─────────────────────────────────────────────────────┘
```

两条链路的共同点：**Gateway 从来没有"进入"VPC**。
- 方案一：Gateway 走 AWS 骨干网调用 Lambda 服务 API，由 **Lambda 自己的 VPC 能力**入网。
- 方案二：Gateway 走骨干网调用 API Gateway，由 **VPC Link** 入网。

---

## 部署的资源

| 类型 | 标识 |
|---|---|
| VPC | `vpc-018b5902896224dc5`（10.30.0.0/16，2 个私有子网） |
| VPC 端点 | S3 网关端点 + ssm / ssmmessages / ec2messages 接口端点 |
| RDS | `acdemo-noegress-mysql`，MySQL 8.0.42，db.t4g.micro，`PubliclyAccessible=false` |
| Lambda | `acdemo-noegress-db-tool`，python3.12，挂载私有子网 + `pymysql` |
| EC2 | `i-0d110aadb72bc6e83`，t3.micro，无公网 IP，跑 stdlib HTTP 服务 |
| NLB | `acdemo-noegress-nlb`，internal，TCP:80 → 实例 8080 |
| VPC Link | `asw9a8` |
| REST API | `ip8yrem2t4`，Regional，stage `prod`，方法鉴权 `AWS_IAM` |
| Gateway | `acdemo-noegress-gw-ongnqn4b1t`，protocol MCP，入站 `AWS_IAM` |
| Gateway targets | `rdsLambda`（lambda）、`rdsApi`（apiGateway） |

暴露给 Agent 的 4 个工具：

```
rdsLambda___db_info        rdsLambda___list_orders
rdsApi___getDbInfo         rdsApi___listOrders
```

---

## 验证证据

完整原始输出见 [`results/evidence.txt`](results/evidence.txt)。要点摘录：

**1. VPC 真的没有出网能力**

```
IGW 数量: 0        NAT 数量: 0
路由表:  10.30.0.0/16 -> local
        pl-7ba54012  -> vpce-0aec... (S3 网关端点)
```
没有 `0.0.0.0/0`。从本机连 RDS 3306 → `TimeoutError`。

**2. 方案一：Gateway → Lambda(VPC) → RDS**

```json
{
  "path": "AgentCore Gateway -> Lambda (VPC-attached) -> RDS",
  "rds_resolved_private_ip": "10.30.11.229",
  "version": "8.0.42",
  "db_user": "agentadmin@10.30.11.161"
}
```
`db_user` 里的 `10.30.11.161` 是 MySQL 自己记录的来源地址，落在私有子网 a 内 —— 这是
"查询确实从 VPC 内部发起"的硬证据。

**3. 方案二：Gateway → API GW → VPC Link → NLB → EC2 → RDS**

```json
{
  "path": "AgentCore Gateway -> API Gateway -> VPC Link -> NLB -> EC2 (private subnet) -> RDS",
  "ec2_private_ip": "10.30.11.109",
  "db_user": "agentadmin@10.30.11.109"
}
```

**4. 参数透传正常**，两个 target 都能正确按 `status` / `limit` 过滤：

```
rdsLambda___list_orders {"status":"SHIPPED"}   -> ORD-1001, ORD-1003
rdsApi___listOrders     {"status":"PENDING"}   -> ORD-1002, ORD-1005
```

**5. API 只有 Gateway 能调**

```
无凭证直接调用            -> HTTP 403 Missing Authentication Token
用管理员身份 SigV4 直调    -> HTTP 403 explicit deny in a resource-based policy
经 AgentCore Gateway 调用 -> HTTP 200 + RDS 数据
```

---

## 实测发现与文档勘误

### ⚠️ 1. `Deny` 千万不要用 `aws:SourceArn` 收敛，否则会把 Gateway 自己锁死

HTML 文档（以及 AWS 官方文档 `gateway-vpc-egress.html`）给出的资源策略是：

```json
{ "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
  "Condition": { "ArnEquals": { "aws:SourceArn": "<gateway ARN>" } } }
```

这条 `Allow` **本身是有效的**。实测甚至把执行角色身份策略里的 `execute-api:Invoke`
删掉后，仅凭这条资源策略 Gateway 依然能调通（HTTP 200）—— 说明在这次授权判断中
`aws:SourceArn` 确实存在且等于 gateway ARN。

**但问题出在 `Deny` 上。** 由于第 2 条（只写 Allow 锁不住），收敛必须加显式 `Deny`；
如果顺着文档思路把 `Deny` 也写成 `aws:SourceArn` 取反：

```json
{ "Effect": "Deny", "Principal": "*",
  "Condition": { "ArnNotEqualsIfExists": { "aws:SourceArn": "<gateway ARN>" } } }
```

**Gateway 立刻被自己的策略拒绝**，实测复现：

```
User: arn:aws:sts::434444145045:assumed-role/acdemo-noegress-gw-role/gateway-session-e465e38c-...
is not authorized to perform: execute-api:Invoke ...
with an explicit deny in a resource-based policy
```

从报错可以看出：这次请求还会**以 Gateway 执行角色的临时会话身份**再被判定一次，
而在那个上下文里 `aws:SourceArn` 是**缺失**的。IAM 的取反类操作符（`ArnNotEquals`、
`StringNotEquals`）在键缺失时会判定为 **true**，`...IfExists` 也一样，于是 `Deny` 命中；
而显式 `Deny` 永远优先于 `Allow`。

**可用的写法**：`Allow` 与 `Deny` 都按执行角色 ARN（`aws:PrincipalArn`）来写。
`aws:PrincipalArn` 对 SigV4 调用者一定存在，且 assumed-role 会话会解析成裸角色 ARN：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "AllowAgentCoreGatewayRole",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::<acct>:role/<gateway-exec-role>" },
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:<region>:<acct>:<api-id>/<stage>/*/*" },
    { "Sid": "DenyEveryoneElse",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:<region>:<acct>:<api-id>/<stage>/*/*",
      "Condition": { "ArnNotEquals": {
        "aws:PrincipalArn": "arn:aws:iam::<acct>:role/<gateway-exec-role>" } } }
  ]
}
```

实测结论：Gateway 正常调通，账号内其他任何主体（含管理员）一律 403。
两种策略都在 `scripts/06-harden-apigw.py` 里，`--docs` 可复现被锁死的版本。
6 种策略组合的完整实测矩阵见 [`results/policy-matrix.md`](results/policy-matrix.md)。

> 附带发现：只要资源策略里有针对执行角色 ARN 的 `Allow`，执行角色的**身份策略就不再需要**
> `execute-api:Invoke`（实测删掉后仍能调通）。脚本里仍然保留该授权，因为 AWS 文档这么建议，
> 且多给一层不影响收敛效果。

### ⚠️ 2. 只写 `Allow` 的资源策略不等于"锁死"

文档说"用资源策略把 API Gateway 锁死到 AgentCore 服务主体"。实测：同账号调用时
API Gateway 会把身份策略与资源策略取**并集**，所以账号内任何持有 `execute-api:Invoke`
的主体照样能调进来 —— 我用管理员身份直接 SigV4 调用，返回了 200 和完整 RDS 数据。
**必须显式 `Deny`** 才算收敛。

### 3. `apiGatewayToolConfiguration.toolFilters` 是必填项（文档未提及）

`apiGateway` target 不能只给 `restApiId` + `stage`：

```
ParamValidation: Missing required parameter in
targetConfiguration.mcp.apiGateway: "apiGatewayToolConfiguration"
```

必须用 `toolFilters` 白名单列出要暴露的 path + method；建议再配 `toolOverrides`
给工具起 Agent 友好的名字和描述，否则工具名由 API 结构自动推导，可读性差。

### 4. Gateway 入站可以直接用 `AWS_IAM`，测试不需要 Cognito

文档与多数示例都用 CUSTOM_JWT + Cognito。实测 `--authorizer-type AWS_IAM` 完全可用：
用 SigV4 对 `bedrock-agentcore` 签名直接打 MCP 端点即可（见 `mcp_client.py`）。
验证阶段能省掉整套 IdP，很实用。

### 5. 两个工程上的坑

- **IAM 传播**：`CreateGatewayTarget` 会**立即校验**执行角色对 Lambda 的
  `lambda:InvokeFunction` 权限。新建角色后马上创建 target 会失败
  （`Gateway execution role lacks permission to invoke Lambda function`），
  重试即可。脚本里用了 `until` 重试循环。
- **API Gateway 部署限流 + 策略生效延迟**：`CreateDeployment` 限流很严
  （`TooManyRequestsException`），需要退避重试；资源策略改完必须**重新部署 stage**，
  且实际生效还要再等约 1–2 分钟（我们第一次测"应该被拒"时仍返回 200，轮询后才变 403）。

### 6. 文档中被证实正确的部分

- Lambda target "开箱即用、无需额外配置" —— 确认。挂 `VpcConfig` 即可访问 RDS。
- `apiGateway` target 仅支持 **Regional REST API** —— 确认（API 只接受 `restApiId`+`stage`）。
- VPC Link 后端必须是 **NLB** —— 确认（REST API 的 VPC Link 不支持 ALB）。
- 出站鉴权只有 **IAM / API Key** 两种 —— 确认，无 OAuth 选项。
- 流量不需要 Gateway 进入 VPC —— 确认，VPC 无任何出网路由仍全程可用。

### 7. 迁移到中国区时要额外注意

- ARN 分区是 `aws-cn`，所有策略里的 `arn:aws:...` 都要改。
- **上面第 1 条的影响在中国区更大**：中国区服务主体名有时带区域后缀，而实测表明
  正确写法根本不该用服务主体，应该用执行角色 ARN —— 这条与分区无关，可直接沿用。
- 本目录用到的 S3 网关端点、SSM 接口端点、VPC Link、NLB 在中国区均为成熟能力。

---

## 如何复现

```bash
cd 11-vpc-no-egress-workaround

bash scripts/01-vpc-rds.sh          # VPC(零出网) + 私有 RDS，约 8-10 分钟
bash scripts/02-lambda.sh           # VPC 挂载 Lambda + 建表灌数据
bash scripts/03-gateway.sh          # Gateway + lambda target
bash scripts/04-apigw-vpclink.sh    # EC2 + NLB + VPC Link + REST API，约 5-8 分钟
bash scripts/05-apigw-target.sh     # apiGateway target
python3 scripts/06-harden-apigw.py  # 收敛资源策略（可用版本）
bash scripts/07-collect-evidence.sh # 跑全部检查并存档

# 手工调用
python3 mcp_client.py list
python3 mcp_client.py call rdsLambda___db_info
python3 mcp_client.py call rdsApi___listOrders '{"status":"PENDING"}'
```

所有资源 ID 写在 `state.env`（含 RDS 密码，已被 `.gitignore` 排除）。
脚本可重复执行，已存在的资源会跳过。

## 成本

约 **$0.10 / 小时**：RDS db.t4g.micro ≈ $0.016、EC2 t3.micro ≈ $0.0104、
NLB ≈ $0.0225、3 个接口端点 ≈ $0.03、Gateway/Lambda/API GW 按调用计费。
**没有 NAT Gateway**（省掉 $0.045/h 和 EIP 配额）。

## 清理

```bash
bash scripts/cleanup.sh --yes
```

按依赖顺序删除，并会等待 Lambda 托管 ENI 释放后才删安全组和子网
（否则 VPC 删不掉）。VPC Link 与 RDS 删除各需数分钟。
