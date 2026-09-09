# AgentCore Runtime 出站 mTLS 验证

> **环境状态（2026-09-09 11:48 UTC）：主要资源已清理，剩余网络资源清理受阻，后台等待已停止。** K3s EC2/EBS、公网 IPv4、7 个 endpoints、ECR、实验 IAM 角色和日志均已清理，Secret 已进入 7 天恢复窗口。Runtime 不再存在，但一张 AgentCore 服务网卡仍未释放，导致实验子网、路由表和 Runtime SG 暂留，详见末尾清理记录。下文部署地址、调用结果和证书有效期均为历史实验记录，不表示环境仍在运行。

## 架构

这是一个真实 AWS 实验，不是模拟 kubectl 输出。使用当前 EC2 所在的 **us-west-2 / vpc-0edf3a4e323c23b22**，而非之前已取消请求中的 us-east-2。

- 新建一台 Amazon Linux 2023 ARM64 `t4g.small`（2 vCPU / 2 GiB，12 GiB 加密 gp3），运行单节点 K3s `v1.36.4+k3s1`，SQLite 数据存储。关闭 Traefik、ServiceLB、metrics-server、local-storage。
- AgentCore Runtime：ARM64 Python 3.12 镜像，Strands `1.55.0`，AgentCore SDK `1.22.0`，`kubectl v1.36.4`。模型为 Bedrock `us.amazon.nova-2-lite-v1:0`，没有使用 Anthropic SDK。
- Runtime 位于新建的 `172.31.240.0/24` 隔离子网，无 NAT、无默认互联网路由。实验只选一个 AZ，不是高可用方案。
- 当前机器通过 AgentCore interface endpoint 的实际 DNS 名调用 Runtime（SigV4）。Runtime 经 VPC 内地址访问 K3s API `:6443`，使用 X.509 客户端证书。
- 六个单 AZ interface endpoints：AgentCore data plane、Bedrock Runtime、Secrets Manager、ECR API、ECR Docker、CloudWatch Logs；另建一个 S3 gateway endpoint，只关联实验路由表。
- 应用显式使用 AgentCore、Bedrock、Secrets Manager 的 endpoint URL，避免改变这些服务的全 VPC DNS。ECR 和 Logs 开启 private DNS（影响全 VPC），相应 endpoint SG 允许 VPC CIDR 的 HTTPS，保留现有工作负载访问；没有修改已有 SG/路由表。
- K3s 节点位于原公共子网，公网 IPv4 仅供下载软件和 SSM 出站。其 SG 没有公网入站规则，6443 只允许 Runtime SG 和当前机器私网 `/32`；不开放 SSH。

调用路径：当前 EC2 → AgentCore PrivateLink → Runtime / Strands → kubectl → K3s 私网 API。

`agent.py` 提供固定只读工具 `kubectl_read(operation)`，不接受 shell、任意 argv、任意 kubeconfig 或任意目标地址。模型工具只允许 `nodes`、`proof`、`pods`。返回实际命令的 exit code、stdout、stderr，并保留工具调用证据。

## 部署与调用

脚本是针对本次账号/VPC 的实验脚本，常量位于 `lab.py`。不要直接用于其他账号；部署前确认账号、区域、子网 CIDR 和命名无冲突。资源创建后立即记录到 `.state/resources.json`；中断后重跑前先核对记录及 AWS 实际状态，不要删除该文件。

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest -v test_agent test_lab

# 以下会创建收费资源。必须依次执行，不要同时启动多个部署进程。
.venv/bin/python lab.py infra
.venv/bin/python lab.py bootstrap
.venv/bin/python lab.py runtime

# 模型自行调用 kubectl 工具
.venv/bin/python lab.py invoke

# 确定性正反例 + 真正的 Strands 模型工具调用
.venv/bin/python lab.py verify
```

`bootstrap` 通过 SSM 安装 K3s、创建 demo namespace/ConfigMap/RBAC、提交并批准一个 24 小时客户端 CSR。节点直接把证书材料写入 Secrets Manager；SSM 参数、镜像、代码仓库不包含私钥。`bootstrap` 再次执行会重新签发实验客户端证书。

证书由 Runtime 每次请求从 Secrets Manager 读取，在临时目录生成只有客户端证书的 kubeconfig，执行完删除。有效证书测试没有 bearer token、IAM K8s 插件或管理员 kubeconfig 作为认证回退。

`verify` 结果保存为 `.state/verification.json`，包括请求 ID、session ID、endpoint 私网解析、路由/SG 配置、证书主题与有效期、真实 kubectl 输出及模型调用。该文件没有客户端私钥。部署 SSM 输出位于 `.state/bootstrap-result.json`。

## 验证结果

**2026-09-09 09:21 UTC 实测：13/13 通过；另有 8/8 本地离线测试通过。** Runtime 为版本 `2`，状态 `READY`。所有在线检查由当前 EC2 通过 AgentCore PrivateLink 调用部署后的 Runtime 执行，不是本地 agent 代跑。

| 检查 | 实际结果 |
|---|---|
| 有效证书读取 nodes | exit 0，单节点 `Ready=True`，Kubernetes `v1.36.4+k3s1` |
| 有效证书读取 proof ConfigMap | exit 0，`expected_user=agentcore-mtls-reader` |
| 不带客户端证书的 HTTPS 请求 | API 返回 `401 Unauthorized`，服务端 CA/主机名校验仍开启 |
| 不受信任的客户端证书 | kubectl exit 1，服务端要求提供凭证 |
| 错误的服务端 CA | kubectl exit 1，`x509: certificate signed by unknown authority` |
| 错误的服务端主机名 | kubectl exit 1，SAN 不匹配 |
| 有效证书读取 Secrets | `Forbidden`，错误信息确认身份 `agentcore-mtls-reader` |
| 有效证书跨 namespace 读取 ConfigMaps | `Forbidden`（目标 `kube-system`） |
| create/update/patch/delete ConfigMaps 权限（4 项） | `kubectl auth can-i` 全部返回 `no` |
| Strands / Nova 2 Lite 自主调用工具 | 实际调用 nodes 和 proof，均 exit 0，约 8.18 秒 |

模型返回的摘要包含：

```text
Name: ip-172-31-27-40.us-west-2.compute.internal
Ready: True
ConfigMap: mtls-demo/mtls-proof
Message: AgentCore Runtime reached private K3s with an X.509 client certificate
```

模型验证请求 ID：`e1a32094-0eb1-4bb9-973f-1c6a6a4ebaa8`。完整逐项证据在 `.state/verification.json`，其中 `all_passed=true`。

### 实际 Case：让 Agent 查询集群基础信息

**执行时间：2026-09-09 09:27:05 UTC（北京时间 17:27:05）**。向已部署的 Runtime 版本 `2` 发送自然语言请求，由 Strands / Nova 2 Lite 自行调用工具；没有使用 `action=verify` 绕过模型，也没有在当前机器上代执行 kubectl。

#### 请求与复现

实际 prompt 如下。下面的命令会发起一次新的真实请求，不读取历史输出；状态可能随时间变化，并产生 Runtime/模型调用费用。

```bash
.venv/bin/python -c '
import json
from lab import call_runtime
prompt = "请查看当前 Kubernetes 集群的基础信息。调用 kubectl_read 的 nodes、pods 和 proof 各一次，只根据工具真实返回回答：API 地址、节点数量、节点名称与角色、Ready 状态、内网 IP、Kubernetes 版本、操作系统、CPU 架构、容器运行时、CPU/内存 capacity 与 allocatable，以及 mtls-demo 命名空间的 Pod 数量和 proof 内容。不要把容量当成实时使用量；不要将 mtls-demo 的 Pod 数量说成整个集群的 Pod 总数；未查询的信息请明确说明。用中文简洁回答。"
print(json.dumps(call_runtime({"prompt": prompt}), indent=2, ensure_ascii=False))
'
```

| 调用信息 | 本次实际值 |
|---|---|
| HTTP 状态 / 耗时 | `200` / `3.66` 秒 |
| Request ID | `d2c443ff-4c69-4fc2-946f-619112186433` |
| Session ID | `ccc15486-93fa-459e-bfdd-41fe3ae76e07` |
| 模型 | `us.amazon.nova-2-lite-v1:0` |
| AgentCore 私网入口 | `https://vpce-0bdd41b51f2141959-cd7c9zh0.bedrock-agentcore.us-west-2.vpce.amazonaws.com` |
| 原始证据 | `.state/cluster-info-case.json`（本地保留，包含完整请求、agent 回答和三次工具的 stdout/stderr，不含私钥） |

#### Agent 实际执行的工具

以下顺序来自返回的 `tool_calls`，三次均为 `mode=valid`，目标均为 `https://172.31.27.40:6443`。命令列省略了实现自动添加的临时 kubeconfig/cache 路径与超时参数。

| 顺序 | 工具参数 | kubectl 命令 | Exit code | stderr |
|---|---|---|---|---|
| 1 | `operation=proof` | `kubectl get configmap mtls-proof -n mtls-demo -o json` | `0` | 空 |
| 2 | `operation=nodes` | `kubectl get nodes -o json` | `0` | 空 |
| 3 | `operation=pods` | `kubectl get pods -n mtls-demo -o json` | `0` | 空 |

#### 返回的真实基础信息

下表将 agent 的回答与对应工具 stdout JSON 逐项核对后整理，不仅依赖模型的成功描述。

| 字段 | 本次实际结果 |
|---|---|
| K8s API | `https://172.31.27.40:6443` |
| 节点数量 | `1` |
| 节点名称 | `ip-172-31-27-40.us-west-2.compute.internal` |
| 节点角色 | `control-plane`（节点标签） |
| Ready | `True` |
| 内网 IP | `172.31.27.40` |
| Kubernetes 节点版本 | `v1.36.4+k3s1`（`status.nodeInfo.kubeletVersion`） |
| 操作系统 | `Amazon Linux 2023.12.20260831` |
| CPU 架构 | `arm64` |
| 容器运行时 | `containerd://2.3.4-k3s1.36` |
| CPU capacity / allocatable | `2` / `2` |
| 内存 capacity / allocatable | `1885252Ki` / `1885252Ki` |
| `mtls-demo` 的 Pod 数量 | `0`（Pod 列表 `items: []`） |
| proof 的 `expected_user` | `agentcore-mtls-reader` |
| proof 的 `message` | `AgentCore Runtime reached private K3s with an X.509 client certificate` |

例如，agent 本次回答的原文片段为：

```text
**节点名称与角色**：`ip-172-31-27-40.us-west-2.compute.internal` (控制平面)

**Ready 状态**：Ready

**内网 IP**：`172.31.27.40`
```

**解读边界：** capacity/allocatable 是节点报告的资源容量与可分配额度，不是 CPU/内存实时使用量；这里只查询了 `mtls-demo` 的 Pod，不能据此断言整个集群没有 Pod。版本来自 kubelet 字段，本次没有额外查询 API Server `/version`。proof 中的 `expected_user` 是预置 ConfigMap 字段，不是本次身份查询；证书身份的独立证据见前面的 RBAC Forbidden 验证。

### 已部署资源

| 资源 | ID / 地址 |
|---|---|
| AWS 账号 / 区域 | `434444145045` / `us-west-2` |
| 复用 VPC | `vpc-0edf3a4e323c23b22` |
| 新 K3s EC2 | `i-0834964d0054f3491`，`t4g.small` |
| 私网 K8s API | `https://172.31.27.40:6443` |
| 新隔离子网 | `subnet-032b21959bdb16c4f`，`172.31.240.0/24` |
| 新路由表 | `rtb-0d47448fbecbd7e53`，只有 VPC local 和 S3 prefix-list 路由 |
| Runtime ID | `mtls22_20260909-7894oR9BB3` |
| Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/mtls22_20260909-7894oR9BB3` |
| Runtime SG / K3s SG | `sg-064f19379af0835c1` / `sg-0e160ab9e9918a10d` |
| ECR 镜像 tag | `434444145045.dkr.ecr.us-west-2.amazonaws.com/mtls22-20260909:6b2be755c9e3` |
| 镜像 digest | `sha256:a930df402e3315c4908783832852409e6a27d9e020a9c6b3c64f8845bf39aeb3` |
| Secrets Manager | `mtls22-20260909/kubernetes-client` |

实际 DNS 解析：AgentCore endpoint 为 `172.31.240.58`，Bedrock endpoint 为 `172.31.240.146`，Secrets Manager endpoint 为 `172.31.240.129`。验证同时检查了 Runtime 配置、节点/子网的 VPC ID，以及隔离子网实际关联的路由表。

当前客户端证书有效期为 **2026-09-09 09:07:25 UTC 至 2026-09-10 09:07:25 UTC**（北京时间 9 月 10 日 17:07:25 到期）。之后需重新运行 `bootstrap`。私钥不在此文档或镜像中。

部署期间处理了两个实验问题：第一次 K3s 安装遇到 RPM 数据库锁冲突，确认没有残留 dnf/rpm 进程后重试成功，并增加 cloud-init 完成等待（这不能排除其他后台包管理任务的锁竞争）；kubectl 空用户交互提示导致原始无证书反例不能证明服务端拒绝，改为额外的严格 HTTPS 401 探针。均未通过关闭证书验证/GPG 检查来规避。

## 凭证方案比较：为什么本实验保留 Secrets Manager

**结论：对当前“单个 Runtime 使用 kubectl，通过 X.509 客户端证书访问自建 K3s”的场景，直接使用 AWS Secrets Manager 是最佳选择。** 这里的“最佳”指需求匹配、实现复杂度和可验证性，不代表它在所有 agent 身份认证场景中都优于 AgentCore Identity。

本节仅记录方案评估，**没有将线上实现切换到 Identity**。直接读取 Secrets Manager 的路径已实测；下面的两种 Identity 证书包适配均未部署验证，不能视为官方原生 mTLS 支持或已经验证可用的实现。

### 当前方案与两种 Identity 方案

当前链路为：Runtime 执行角色通过 IAM 授权读取指定 Secret，应用生成临时 kubeconfig，kubectl 使用证书和私钥连接 K3s。Secret 保存服务端 CA、客户端证书和私钥等字段，K3s 负责签发证书及通过 RBAC 授权。

AgentCore Identity 已有 API Key、OAuth2 等凭证接口；截至本次文档及 SDK 核对，未发现专门的 X.509/mTLS 证书提供方。为保持本实验的 mTLS 协议不变，本节评估的做法是把证书包序列化为字符串，借用 API Key provider 进行获取。**这是应用自定义适配，不会让 Identity 自动理解证书、签发证书或完成 TLS 握手。**

| 比较项 | 当前：直接 Secrets Manager | 方案一：Identity MANAGED + 证书包 | 方案二：Identity EXTERNAL + 自有 Secret |
|---|---|---|---|
| 存储方式 | 在自有 Secret 中保存证书包 JSON | 将序列化证书包作为 `apiKey` 值交给 Identity 管理 | 自有 Secret 的指定 JSON 字段保存序列化证书包，Identity 引用该字段 |
| 应用取凭证接口 | `GetSecretValue` | `GetResourceApiKey`，需要 workload identity token | 同左，由 Identity 读取被引用的 Secret 字段 |
| 与数据类型的匹配 | 通用 Secret 可直接保存证书、私钥等敏感文本 | 将证书包当 API Key 字符串使用，语义不匹配 | Secret 的存储语义合适，但经过 Identity 时仍作为 API Key 字符串返回 |
| 是否保留 Secrets Manager | 是，直接管理 | 凭证存储交由服务管理；不等于底层完全不用 Secrets Manager | 是，显式保留自有 Secret |
| 证书签发与续期 | K3s/PKI 与我们的流程负责 | 仍需 K3s/PKI；续签后更新 provider 中的值 | 仍需 K3s/PKI；续签后更新被引用的 Secret 字段 |
| kubectl 的 mTLS 处理 | 应用准备 kubeconfig，kubectl 执行 TLS | 不变，Identity 仅参与凭证获取 | 不变，Identity 仅参与凭证获取 |
| 授权与网络配置 | 指定 Secret 的 IAM 权限及现有 Secrets Manager endpoint | 增加 provider、workload identity token 获取、Identity IAM 和 endpoint policy 配置 | 同左，还要配置外部 Secret 引用与相应访问授权 |
| 对现有实验的改动 | 无 | 修改证书发布、获取、权限、验证与清理流程 | 可保留主要存储流程，但需调整 JSON 字段组织、获取方式、权限及验证 |
| 本项目验证状态 | mTLS/RBAC 检查和真实 agent 调用已通过 | 仅方案评估，未验证证书包适配 | Secret 引用能力有官方文档；本项目的证书包适配未验证 |

`MANAGED` 与 `EXTERNAL` 是 API Key provider 的凭证来源选项，不是两种 mTLS 认证协议。EXTERNAL 通过 `apiKeySecretConfig.secretId` 和 `jsonKey` 引用 Secret 内的字段，不能直接假定当前整个 JSON 证书包会原样作为 API Key 返回。

### 为什么直接 Secrets Manager 更适合这里

1. **解决的正是证书材料存储问题。** kubectl 需要 CA、客户端证书和私钥，本实验不需要 OAuth 授权码交换、用户同意或第三方 token 刷新。用通用 Secret 保存这组材料，比把它们包装成 API Key 更直接。
2. **Identity 不会消除 mTLS 的主要运维工作。** 三种方案都需要证书签发、到期检查、续签和更新，也都需要在 Runtime 中把私钥交给 TLS 客户端。将读取入口改为 Identity，并不能让私钥不进入容器，也不能自动续签 K3s 证书。Secrets Manager 本身同样不会自动完成这些 PKI 工作。
3. **当前权限边界已经可以清晰表达。** Runtime 执行角色只能读取本实验 Secret，节点角色负责写入；K8s 端再通过证书身份和 RBAC 限制操作。对于一个 Runtime、一套只读集群身份，尚无必须额外引入 workload identity 凭证分发层的需求。
4. **依赖更少，验证范围更聚焦。** 现有代码直接读取 Secret 后执行 kubectl。引入 Identity 会增加 provider、token 和策略配置等故障点，却不会改变最终 TLS 行为。减少中间步骤有利于把错误定位到网络、证书或 RBAC；这里不宣称未经测量的延迟或费用差异。
5. **已有真实端到端证据。** 当前实现已验证有效证书成功、无证书/不受信任证书失败、服务端 CA/主机名校验和 RBAC 拒绝，并完成自然语言查询集群信息。Identity 的证书包适配尚未实测，没有必要为这次 mTLS 验证引入额外不确定性。

因此，本实验继续采用 **Secrets Manager 存储凭证材料、K3s 签发与验证客户端证书、kubectl 执行 mTLS、Kubernetes RBAC 控制权限** 的分工。客户端证书仍按前文说明续签；这不是生产级自动轮换方案。

### 什么时候值得使用 AgentCore Identity

如果后续要统一管理多个 agent 对第三方 API 的 API Key/OAuth2 访问、实现用户委托授权，或按 workload identity 管理凭证访问，Identity 的价值会更明确。有自有 Secret 及既有轮换流程时可以考虑 EXTERNAL；希望由 Identity 管理 provider 凭证存储时可以考虑 MANAGED。

如果把 K8s 接入改成 OAuth/OIDC token 路径，还需配置集群能接受的 token 类型、issuer、audience 和权限映射，不能假定任意 OAuth access token 都能用于 kubectl。该方向属于新的认证方案，**不再是当前客户端证书 mTLS 实验的等价替换**。

若未来试验 Identity，可复用支持 Identity 数据面的 AgentCore VPC Endpoint，但当前 endpoint policy 只允许指定调用方执行 `InvokeAgentRuntime`，需要新增相应授权，不能只替换 `get_bundle()`。本次不做这些变更。

参考依据：

- [Identity 引用自有 Secrets Manager Secret](https://aws.amazon.com/blogs/machine-learning/reference-your-own-aws-secrets-manager-secrets-in-amazon-bedrock-agentcore-identity/)：运行时按 Secret ARN 和 JSON key 获取凭证。
- [GetResourceApiKey API](https://docs.aws.amazon.com/botocore/latest/reference/services/bedrock-agentcore/client/get_resource_api_key.html)：输入 workload identity token 与 provider 名称，返回 API Key 字符串。
- [AWS 身份认证最佳实践](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec03-bp01.html)：区分 Identity 的 OAuth 能力与证书型认证所需的 PKI/私钥存储职责。
- [AgentCore PrivateLink](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)：Identity 支持私网数据面与控制面访问。

## FAQ

### 用 K3s 验证，和常规 Kubernetes 等效吗？

**对本实验的出站 mTLS 能力验证，等效；对具体生产集群的上线验收，不能直接替代。** K3s 是真正的 Kubernetes 发行版，不是模拟器。这里使用的 kubectl、X.509 客户端证书认证、服务端 CA/主机名校验和 Kubernetes RBAC，与采用相同认证配置的常规自建 Kubernetes 机制一致。单节点、SQLite 存储等轻量化选择不改变这条认证路径。

本实验已验证有效证书访问成功、无证书请求返回 401、不受信任证书被拒绝、错误 CA/主机名校验失败，以及证书对应用户的 RBAC 越权拒绝。因此，足以证明 **AgentCore Runtime 能通过 VPC 私网，以客户端证书运行 kubectl 访问 Kubernetes API**。

迁移到目标集群时，仍需核对网络、客户端 CA 信任、证书用途/有效期、服务端 SAN 和 RBAC；若入口有 TLS 终止代理或负载均衡器，也需重新验证。EKS IAM/OIDC 等其他认证方式、证书自动轮换、HA 和性能不在本次验证范围内。

**注意：无证书请求实际返回的是 HTTP 401，不是 TLS 握手失败。** 本次证明的是 Kubernetes 客户端证书认证可用，并未证明服务端只允许 mTLS、在握手阶段拒绝所有无证书连接。

参考：[K3s 官方介绍](https://docs.k3s.io/)、[Kubernetes 认证机制](https://kubernetes.io/docs/reference/access-authn-authz/authentication/)。

## 安全边界与清理

- K3s 禁用匿名认证。客户端身份为 `CN=agentcore-mtls-reader`，仅能读取 nodes，以及 `mtls-demo` namespace 的 ConfigMaps/pods，不能读取 Secrets。
- 客户端证书有效期约 24 小时；请以实测报告中的 `not_after` 为准，过期后运行 `bootstrap` 重新签发。这是实验生命周期，不是生产证书自动轮换方案。
- Runtime 执行角色只能读取本实验的 Secret；节点角色具有 SSM 权限和向本实验 Secret 写入的权限。K3s 管理员配置保留在节点上，没有交给 Runtime。
- 此实验验证的是 **Runtime 出站 mTLS**。调用 Runtime 使用 SigV4 + PrivateLink，不是 Runtime 入站 mTLS。
- 显式使用 VPC endpoint 证明本次调用走私网，但不等于禁用了 AWS 的公共托管入口；如需强制 private-only，需要额外的 `aws:SourceVpce` 授权策略。
- K3s 没有配置为全局 TLS 层 `RequireAndVerifyClientCert`：它支持多种 Kubernetes 认证机制。本实验有效 kubeconfig 只携带证书，验证的是该客户端的双向证书认证路径。缺失证书可能返回 HTTP 401，而不是 TLS 握手错误。
- `kubectl` 对空用户配置会在发请求前询问用户名，非交互式执行返回 EOF，**这不算认证拒绝证据**。`no_client_cert` 另用 Runtime 内 Python 标准库发起无证书、无 Authorization header 的 HTTPS 请求，保持 CA/主机名校验，并断言 API 返回 401。其他 mTLS/RBAC 用例仍实际执行 kubectl。
- 错误服务端 CA 与错误主机名分别验证信任链和 SAN 校验。RBAC 用例拒绝读 Secrets、跨 namespace 读 ConfigMaps，并用 `kubectl auth can-i` 检查 create/update/patch/delete 均返回 no，不执行破坏性写入。
- 实验会持续产生 EC2、EBS、公网 IPv4、六个单 AZ interface endpoints、Secrets Manager、ECR、CloudWatch 与按使用量计费的 Runtime/模型费用。**不自动销毁，不要长期忘记运行中的环境；仅停止 EC2 不会停止 endpoints 的小时费用。**

清理需要显式确认，严格按 `.state/resources.json` 中记录的实验资源执行，不删除原 VPC、当前机器、原子网/路由表/安全组，也不删除共享 service-linked role：

```bash
.venv/bin/python lab.py cleanup --confirm
```

清理包括 Runtime、实验 EC2/EBS、公网 IPv4、7 个 endpoints、Secret（7 天恢复窗口）、ECR 仓库/镜像、两个 IAM role/一个 instance profile、实验子网/路由表/安全组、Runtime 日志。删除 ENI 可能耗时，脚本会等待，超时后可核对状态再运行。保留本地源代码、验证证据、虚拟环境及 Docker 镜像。

### 本次实际清理记录

2026-09-09 按用户要求执行 `.venv/bin/python lab.py cleanup --confirm`。截至 **10:48 UTC（北京时间 18:48）**，已独立查询确认：

- Runtime `mtls22_20260909-7894oR9BB3` 及其自动创建的 workload identity 已不存在。
- K3s EC2 `i-0834964d0054f3491` 已 terminated，EBS `vol-0a447d22a94317c44` 已删除，实例不再持有公网 IPv4。
- 7 个 VPC endpoints、ECR 仓库及镜像、两个实验 IAM 角色及 instance profile、Runtime 日志组已删除。
- K3s SG、应用 endpoint SG、平台 endpoint SG 已删除。ECR/Logs 新增的 Private DNS 已随 endpoint 删除撤销，解析恢复为公网地址。
- 实验 Secret 于 `2026-09-09 10:27:37 UTC` 标记删除，保留 7 天恢复窗口，尚未永久销毁。
- 当前开发机 `i-0785d8d0b8b950448` 仍 running；原 VPC、原子网及其他实验未删除。本地代码、历史验证结果、虚拟环境及 Docker 镜像保留。

**未完成部分：** 清理脚本等待实验子网的 ENI 释放 20 分钟后超时。`eni-03662f51ca15cda8a` 为 `agentic_ai` 网卡，标记 `AmazonBedrockAgentCoreManaged=true`，仍以 `in-use` 状态挂在 AWS 服务侧；没有强制 detach。其依赖的实验子网 `subnet-032b21959bdb16c4f`、路由表 `rtb-0d47448fbecbd7e53`（及关联）、Runtime SG `sg-064f19379af0835c1` 暂时保留。剩余 Runtime SG 的入站/出站规则已清空。

**11:48 UTC（北京时间 19:48）更新：** 后台额外等待 60 分钟后再次超时，任务已退出，不再自动重试。只读复查确认 Runtime 返回 `ResourceNotFoundException`，上述服务网卡仍为 `in-use`，子网、路由表及关联、Runtime SG 仍存在；SG 规则为空。尚未查明服务网卡滞留原因，不能宣称所有资源均已删除。

建议向 AWS Support 提供账号 `434444145045`、区域 `us-west-2`、Runtime ID `mtls22_20260909-7894oR9BB3`、ENI ID `eni-03662f51ca15cda8a` 及其 attachment `ela-attach-0f1bba63dea489051`，确认服务侧释放状态。未提交支持工单，也未强制拆卸网卡。待确认网卡已释放后，再运行上述清理命令完成收尾。

清理核对记录：`.state/cleanup-verification.json`。断点进度：`.state/resources.json` 的 `cleanup_done`。不要丢失状态文件，也不要同时启动多个清理进程。已开始清理的状态不能直接用于重新部署；上文调用命令和证书续签命令需要在重新部署环境后才能使用。

参考：[Runtime VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)、[PrivateLink](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)、[K3s 配置](https://docs.k3s.io/cli/server)、[Kubernetes kubeconfig](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/)。
