# 中国区方案一复测记录

## 范围与结果

- 本轮在线矩阵完成时间：2026-09-28 07:30:21 UTC；不是复用旧结果。
- Profile：`zhy`；区域：`cn-northwest-1`；账号：`765922807571`。
- Gateway：`acdemo-idp-gw-jwrsvig6hf`，`AWS_IAM`，READY。
- 业务 JWT 请求头：`X-Idp-Authorization`；REQUEST interceptor 开启 `passRequestHeaders`。
- 10/10 通过：合法 JWT + SigV4 成功 1 项；非法/缺失 JWT 返回预期 403 共 8 项；无 SigV4 返回 401 共 1 项。
- 合法工具调用返回 2 条 PENDING 订单，出站令牌 `token_from_cache=false`；另外的直接工具调用返回 2 条 SHIPPED 订单，令牌命中缓存。
- IdP：`10.30.11.122:8081`，无公网 IP，仅 Lambda 安全组允许访问；VPC 无 IGW、无 NAT，无互联网默认路由。
- `05-collect-evidence.sh` 退出码为 0；已断言 Gateway/interceptor/target 配置以及出站响应字段。
- 独立核对三个 Lambda 的云端 CodeSha256 与本地 ZIP 一致，ZIP 内源码与当前源码一致。

文件：[`verification.json`](verification.json)、[`evidence.txt`](evidence.txt)、[`outbound.json`](outbound.json)。

## 日志核对与边界

[`invocations.json`](invocations.json) 保留首次快照：当时 CloudWatch 尚未收齐日志。
07:31:01 UTC 对相同时间窗口（epoch ms `1790580560309` 至 `1790580623106`）做只读重查，
结果见 [`invocation-reconciliation.json`](invocation-reconciliation.json)。

- interceptor 观察到 18 次调用：9 次 initialize 与 9 次 tools/call；8 条拒绝日志与负例请求 ID/原因一致。
- 无 SigV4 的 initialize 请求 ID 19 未出现在 interceptor 日志中。
- 工具观察到 2 次调用，与 1 次合法 Gateway 调用加 1 次直接出站验证一致；无额外工具调用。

入站令牌是在本地用演示 IdP 的同一私钥构造，用于制造负例；并未验证完整终端用户登录流程。
签名调用器模拟使用 IAM Role 的 Agent 后端，不是公网代理，也不放在无出口 VPC 内。
IdP 仍是 HTTP 演示实现，不是生产部署。本轮没有测量中国区延迟。
离线检查覆盖 JWKS 故障/未知 kid 区分、异常出站响应、负例中的 5xx；Shell/Python 语法与 diff 检查通过。

## 复现

在现有部署和对应 `state.env` 保留的前提下，于本示例目录执行：

```bash
export AWS_PROFILE=zhy REGION=cn-northwest-1
export INBOUND_AUTH=AWS_IAM TOKEN_HEADER=X-Idp-Authorization
export RESULTS_DIR="$PWD/results/zhy-$REGION-$(date -u +%Y%m%dT%H%M%SZ)"
bash scripts/05-collect-evidence.sh
```

部署过程恢复了已清理的基础依赖；等待期间发现基础状态文件被另一个操作移走，
核对云端实际状态后恢复配置，再继续部署，未重复创建 RDS。
本轮结束时资源保留，未执行清理；EC2/RDS/VPC interface endpoints 等会继续计费。
