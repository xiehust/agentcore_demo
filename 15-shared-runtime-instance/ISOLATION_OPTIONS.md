# AgentCore Runtime Instances 隔离选项

> **结论先行（核验于 2026-08-13 UTC）：**在 Runtime Instances 中，AWS 定义的隔离键是 **`(capacity provider, session ID)`**，该组合与一个 EC2 实例 1:1 映射。Runtime、endpoint、agent container、`runtimeUserId` 都不是额外的基础设施隔离键。同一 session 内的容器和进程共享该 EC2 信任边界，AWS 明确不把它们视为彼此之间的安全边界；共置 workload 必须互相信任。

本文区分四级证据：

- **[官方]** AWS 公开服务契约或安全说明。
- **[当前实测]** `15-shared-runtime-instance/runtime.json` 指向的环境，2026-08-13 UTC 通过控制面与 `InvokeAgentRuntimeCommand` 验证。
- **[历史实测]** 旧实例上的现场观察，仅说明当时实现，可能已经失效。
- **[待验证]** 尚无官方保证且本次未验证的推论。

## 1. 边界、生命周期与威胁模型

### 1.1 先分清五个对象

1. **Runtime endpoint** 负责把调用路由到 agent 版本，不是租户隔离边界。
2. **逻辑 session** 由调用方提供的 `runtimeSessionId` 标识；Instances 中还必须加上 capacity provider 才构成精确键。
3. **当前 compute** 是该 Instances session 此刻使用的 EC2。它可因 stop、provider 生命周期、14 天上限、故障或维护而替换，不能把 session 理解成永久绑定某个 instance ID。
4. **agent container/process** 是同一 session EC2 内的 workload。相同 provider 与 session ID 可让不同 Runtime 的 agent 共置；官方上限为每个 Instances session 20 个 agents，但这些 container/process **不互相隔离**。
5. **`runtimeUserId`** 只有在认证后端生成或校验并注入时，才能作为应用收到的身份信号；它不会创建 session、container、UID 或文件系统边界。AgentCore 不校验 user↔session 归属；后端必须维护绑定，且不能接受客户端任意选择 session ID 或 `runtimeUserId`。

因此：

- 不同 `(provider, session)`：Instances 在同一时刻使用不同 EC2，适合不互信 tenant；成本是每个 active session 一台 EC2。
- 相同 `(provider, session)`：共享 EC2、文件系统和信任边界。应用的工作区路径守卫、用户锁和审计只是纵深防御，不能升级为 AWS 提供的租户隔离。
- 相同 session ID、不同 provider：是两个不同 session。
- `StopRuntimeSession` 停止当前 compute；以后再次调用同一逻辑 ID 可创建新 compute。停止不是删除 Runtime，也不应与 endpoint `READY` 混为一谈。

### 1.2 本文采用的威胁模型

**强模型（默认）：**用户可能提交恶意 prompt/代码，尝试读取其他用户文件或进程、耗尽 CPU/内存/磁盘、访问共享网络资源或 execution-role credentials，并利用跨重启残留。对此模型，同 session 用户不能被视为已隔离。

**弱模型：**用户属于同一受信团队，只需防误操作、路径串线和普通并发冲突。此时可共享 session，但仍需身份绑定、最小权限、工作区命名、锁、配额、审计与清理。

需要分别评估：文件/进程、CPU/内存、网络、IAM 凭证、持久卷和生命周期。仅有目录权限或不同 `runtimeUserId` 不覆盖其余维度。

## 2. 当前环境证据

### 2.1 控制面快照 [当前实测]

| 层 | 2026-08-13 观察值 | 正确解读 |
|---|---|---|
| Runtime | `shared_runtime_multiuser-X10bCH6p6u` v5；`DEFAULT`/Runtime 均 `READY` | 只证明控制面和 endpoint 就绪，不证明某个 session 应用响应。 |
| Runtime lifecycle 字段 | idle `900s`，max `28800s` | Runtime 配置层；不要与 provider 或 ASG 生命周期互换。 |
| Capacity provider | `capacity_provider_arm_kb-FQtDNVGq1t`；`LINUX_ARM64`；`c7g.large` | Instances session 的 provider 半边隔离键。 |
| Provider lifecycle | idle `7200s`，max `86400s`（1 天） | 当前 provider 配置；可早于服务 14 天上限结束 compute。 |
| Session storage | 50-GiB gp3 `scratch`，挂载 `/mnt/scratch` | session 持久卷；同 session agents 共享，不是 per-user 卷。 |
| 托管 ASG | `MaxInstanceLifetime=1209600s`（14 天） | 观察到的实现层设置，不是 provider `maxLifetime`，也不是稳定 AgentCore API。 |
| 官方服务上限 | Instances session 最长 14 天 | 服务行为；到期后可用同一 ID 在新 EC2 上恢复并重挂 session volumes。 |

Runtime lifecycle、provider lifecycle、托管 ASG replacement 和逻辑 session 有效期是不同层。当前三个数值分别为 8 小时、1 天和 14 天，不能用其中一个解释另一个。

### 2.2 Fresh session 容器探测 [当前实测]

唯一验证 session `isolation-probe-20260813T045641Z-01`：

- `2026-08-13T04:58:13.638341Z` 开始 warmup；76.310 秒后 `InvokeAgentRuntime` 返回 HTTP 200 和精确结果 `ready`。
- 同一 session 于 `04:59:29.948645Z` 使用官方 `InvokeAgentRuntimeCommand` 执行 30 秒上限的受控探测；HTTP 200，event stream `COMPLETED`。
- `04:59:30.182391Z` 进入 `finally` 清理；`StopRuntimeSession` 于 `04:59:31.557523Z` 返回 HTTP 200，响应 ID 匹配。已安装 SDK 的 Runtime data-plane API model 未暴露 Runtime-session get/list/status 操作，因此未用可能重新激活逻辑 session 的再次 invoke 做“验证”。

关键结果：

| 项 | 结果 | 判定 |
|---|---|---|
| 身份 | `uid=0(root) gid=0(root)` | 命令进程名义上是 root。 |
| capabilities | `CapInh/Prm/Eff/Bnd/Amb` 全为 0 | 当前命令进程没有 `SETUID`、`CHOWN` 等能力，bounding set 也为空。 |
| hardening | `NoNewPrivs=0`；`Seccomp=2`；1 个 filter | 当前值与旧实例不同；仅能确认 seccomp filter 生效，不能从这些字段恢复规则内容。 |
| namespaces | cgroup/ipc/mnt/net/pid/time/user/uts 均有 namespace | 有 namespace 不表示允许再创建嵌套 namespace。 |
| userns quota | `2147483647` | 配额未清零，但不代表 `unshare` 一定获准。 |
| scratch | ext4 `/dev/nvme1n1`，`rw,relatime` 挂载到 `/mnt/scratch` | 确认 session 内可见挂载；不证明宿主 bind 路径是公开契约。 |
| `os.setuid(1000)` | `EPERM`，exit 1 | 路线 A 的关键 syscall 在该执行路径失败。 |
| `os.chown(tmp,1000,1000)` | `EPERM`，exit 1；临时文件已删除 | 路线 A 的第二个关键 syscall失败，且无测试残留。 |
| `unshare -Ur true` | `EPERM`，exit 1 | 路线 B 当前不可用；失败根因仍可能是 seccomp/LSM/外层 userns/服务策略。 |

精确 stdout、逐项 exit code 和时间戳见 [`../.trellis/tasks/08-13-verify-agentcore-isolation-options/research/runtime-json-environment-verification.md`](../.trellis/tasks/08-13-verify-agentcore-isolation-options/research/runtime-json-environment-verification.md)。

### 2.3 历史观察与失效性 [历史实测]

2026-08-12 曾在实例 `i-093ad3cf11259bdb3` 观察到：SSM root 可达、containerd namespace `agentcore`、nerdctl/runc、宿主路径 `/var/lib/agentcore/volumes/scratch` 与容器 `/mnt/scratch` 对应、ASG 当时有 5 台 `c7g.large`，以及 `NoNewPrivs=1`。该实例已于 `2026-08-12T15:48Z` 终止；2026-08-13 控制面观察到 ASG desired capacity 为 1，且 fresh probe 的 `NoNewPrivs=0`。

这些差异已证明旧实例快照会过时。SSM、containerd 产品/namespace、nerdctl、内部 bind 路径、tag 和 ASG 名称均未被 AgentCore 文档承诺为稳定扩展接口，不能作为生产方案前提，也不再作为容器探测的支持路径。

## 3. 三条路线复核

### 路线 A：同 container 内 per-user UID

**结论：当前镜像/执行约束下不可实施；证据强。**

动态切到 UID 1000 和为用户目录 `chown` 均实际返回 `EPERM`。当前 `NoNewPrivs=0` 修正了旧文档“失败依赖 NoNewPrivs=1”的说法；真正直接证据是 capabilities/bounding set 全零和两个 syscall 的失败。即使未来通过镜像或服务配置使它可运行，UID 也只覆盖 Unix 文件/进程权限，不自动解决 cgroup 资源、网络、共享 IAM credentials、共享卷命名和恶意 root-equivalent app bug。

重新部署或 Runtime 版本变化后应重跑同一 supported probe；在没有新证据前不要实现这条路线。

### 路线 B：同 container 内无特权 userns sandbox

**结论：当前环境不可用；根因未完全归因。**

`max_user_namespaces` 很大，但 `unshare -Ur true` 仍以 `EPERM` 失败。当前仅确认 seccomp filter 存在；没有通过宿主内部接口 dump 规则，也没有执行逃逸或绕过测试。bubblewrap/nsjail 依赖所需 namespace/mount syscall 获准，因此不能在此环境声称可用。

若以后平台或镜像改变，应先通过 `InvokeAgentRuntimeCommand` 重测最小 `unshare`；不要把 SSM/OCI 内部配置修改当作受支持的修复。

### 路线 C：托管宿主上的 sibling container

**结论：仅可作为高风险、非 AgentCore 托管契约的实验，不是“唯一活路”，也不能宣称获得 AgentCore 租户隔离。**

旧实例只说明当时观察到 SSM root、宿主 container runtime 等若干实验前提；本次及历史记录都没有实际启动并验证 sibling container，因此连完整路径是否技术可行也仍属待验证。即使实验跑通，也至少存在以下未解决风险：

- **支持边界：**直接依赖 SSM、宿主 containerd socket/namespace、nerdctl、内部 mount/tag/ASG，均无稳定 AgentCore API 保证。
- **生命周期：**provider 目前可在 1 天结束 compute，服务也会因故障、维护或 14 天上限换机；root disk、手装 runner、image cache 和 container metadata 不在 session EBS 持久化承诺内。
- **供应链：**谁拉取、校验、扫描和更新 sibling image，以及 digest pinning/回滚，必须自行负责。
- **控制面：**runner 需要高权限访问 container runtime；其认证、命令注入、socket ACL 和被攻陷后的 blast radius 都是新的宿主级风险。
- **资源：**必须自行配置 cgroup CPU/内存/PID/I/O/磁盘配额并处理 OOM；当前 AgentCore 不承诺 same-session agent 间资源隔离。
- **网络与凭证：**需自行实现 egress/ingress policy、metadata/credential 阻断和 per-user IAM；同实例任意恶意代码不应被假定无法读取可用 credentials。
- **存储与清理：**需安全 bind 仅本用户目录，防 symlink/hardlink/path traversal，并在超时、崩溃和换机时清理容器、mount、文件及凭证。
- **可观测与合规：**AgentCore health、patch、日志和审计不会自动覆盖自行创建的 workload。

若业务硬性要求“多个不互信用户装箱到一台机器”，应评估以该目标为公开调度模型的客户自管平台（例如单独的 ECS/EKS/Batch sandbox 层），而不是把 AgentCore managed host 内部实现当扩展 API。

## 4. 决策矩阵与建议

| 需求 | 选择 | AWS 隔离强度 | 成本/限制 | 建议 |
|---|---|---|---|---|
| 不互信用户、长时/GPU/自定义 EC2 | 每 tenant 不同 Instances session | **强：每 `(provider, session)` 一台 EC2** | active session 对应独立 EC2 | 首选；后端生成并绑定 session ID。 |
| 不互信用户、任务适合 serverless | 每 user/conversation 不同 microVM session | **强：每 session dedicated microVM** | 受 microVM 资源和最长 8 小时 compute lifecycle 限制 | 优先评估，通常比改 managed host 更符合服务模型。 |
| 同一受信团队协作、需共享盘/GPU | 同 provider + 同 session 的官方 shared agents | **session 外强、session 内无边界** | 最多 20 agents；共享故障域和资源 | 可用，但所有 agent/user 必须互信。 |
| 同 session 多用户，仅防误操作 | 当前 app 的 `runtimeUserId`、路径守卫、锁、配额和审计 | **逻辑控制，不是安全边界** | 仍共享进程、网络、凭证和资源 | 只用于弱威胁模型。 |
| 同 EC2 装箱不互信用户 | 路线 A/B/C | **A/B 当前失败；C 非托管保证** | 高研发、运维和支持风险 | 不推荐在本 Runtime 上声称强隔离；换 session/compute 模型。 |

**本项目推荐：**

1. 若用户可运行不可信代码或有严格数据边界，停止把共享 session 描述为“多租户隔离”；使用每 tenant 独立 session。Instances 成本不可接受时，优先验证 microVM，或把执行迁移到专门 sandbox 平台。
2. 若保留共享 session，正式把所有用户归为同一 trust domain；`runtimeUserId` 只由认证后的 BFF 注入，服务端维护 user↔session 绑定，execution role 最小权限，并为文件、并发、资源和审计加应用控制。普通调用路径不要获得 `InvokeAgentRuntimeCommand` 权限；将其与常规 agent invocation 权限分离，只授予专用运维/诊断角色。
3. 不以路线 C 为默认路线，不修改 AgentCore 托管 ASG/host/containerd。任何实验必须另行安全评审、在非生产 provider 上完成，并准备随服务更新失效。

## 5. 支持的复验方式

容器内探测使用 `InvokeAgentRuntimeCommand` event stream，而不是 SSM。下面每次运行只生成并使用**一个新测试 session**；warmup 后复用同一 ID，并无论 warmup/command 是否成功都在 `finally` 调用 `StopRuntimeSession`。应使用与普通应用调用角色分离的专用诊断角色运行；不要把生产共享 session 填入 cleanup 代码。

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config

runtime = json.loads(Path("15-shared-runtime-instance/runtime.json").read_text())
session_id = (
    f"isolation-probe-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
    f"{uuid4().hex[:8]}"
)
client = boto3.client(
    "bedrock-agentcore",
    region_name="us-west-2",
    config=Config(connect_timeout=30, read_timeout=900,
                  retries={"total_max_attempts": 1}),
)
probe = r'''exec 2>&1
id; echo "id_exit=$?"
grep -E '^(Uid|Gid|Cap[^:]*|NoNewPrivs|Seccomp[^:]*):' /proc/self/status; echo "status_exit=$?"
for p in /proc/self/ns/*; do printf '%s=' "${p##*/}"; readlink "$p"; done; echo "ns_exit=$?"
cat /proc/sys/user/max_user_namespaces; echo "userns_quota_exit=$?"
grep -F ' /mnt/scratch ' /proc/self/mountinfo; echo "mount_exit=$?"
python3 -c 'import os; os.setuid(1000)'; echo "setuid_exit=$?"
python3 - <<'PY_TMP'
import os, tempfile
path = None
try:
    fd, path = tempfile.mkstemp(dir="/tmp"); os.close(fd)
    os.chown(path, 1000, 1000)
finally:
    if path is not None: os.unlink(path)
PY_TMP
echo "chown_exit=$?"
unshare -Ur true; echo "unshare_exit=$?"
'''

try:
    warmup = client.invoke_agent_runtime(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
        runtimeUserId="isolation-probe",
        payload=json.dumps({
            "prompt": "Reply with exactly: ready",
            "user_id": "isolation-probe",
            "reset": True,
        }).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    warmup["response"].read()  # 应解析并确认 complete.result == "ready"
    response = client.invoke_agent_runtime_command(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
        body={"command": probe, "timeout": 30},
    )
    for event in response["stream"]:
        print(event)  # contentDelta.stdout/stderr；contentStop.exitCode/status
finally:
    stopped = client.stop_runtime_session(
        agentRuntimeArn=runtime["runtimeArn"],
        runtimeSessionId=session_id,
    )
    if stopped["ResponseMetadata"]["HTTPStatusCode"] != 200:
        raise RuntimeError("StopRuntimeSession did not return HTTP 200")
    if stopped["runtimeSessionId"] != session_id:
        raise RuntimeError("StopRuntimeSession returned a different session ID")
```

判定时记录 UTC 时间、Runtime/version、provider、session ID、HTTP 状态、所有 `contentDelta` 与 `contentStop`，并确认临时文件 cleanup。不要读取环境变量、metadata credentials、用户工作区或 secret，也不要进行逃逸测试。

## 6. 限制

- 当前结论只覆盖 Runtime v5、image `launchpad-agents:shared-runtime-v1` 的一次 fresh session；部署、镜像或服务更新后需复验。
- command probe 观察的是官方命令执行进程。结合旧 OCI 观察可高置信判断 A/B 当前失败，但没有在应用子进程内增加调试代码。
- 未探测 seccomp 规则内容、cgroup 配置、设备、网络、credentials、宿主路径或 container runtime；也未执行漏洞利用。
- `StopRuntimeSession` HTTP 200 且响应 session ID 匹配，是已安装 SDK/API model 当前能提供的清理确认；其 Runtime data-plane model 未暴露 get/list/status 操作，且再次 invoke 可能重新激活逻辑 session 并创建 compute，故没有用 re-invoke 验证。
- AWS Runtime Instances 于 2026-08-06 发布，文档和实现变化快；生产决策应重新核验访问日期。

## 7. AWS 官方参考

以下资料均于 **2026-08-13** 访问：

1. [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html) — session affinity、33 字符最小长度、stop/resume 与 409 生命周期冲突。
2. [Runtime Instances and capacity providers](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances.html) — Instances 与 managed EC2/capacity provider。
3. [Runtime Instances: how it works](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html) — 首次 provision、同 session 共置、14 天与 volume 恢复。
4. [Security model for Runtime Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html) — `(capacity provider, session ID)` 1:1 EC2；same-session containers/processes 无安全边界；user binding 责任。
5. [Runtime how it works (microVMs)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html) — dedicated microVM 的 CPU、内存、文件系统边界和 8 小时 compute lifecycle。
6. [InvokeAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html) — invocation 与请求 session ID 约束。
7. [InvokeAgentRuntimeCommand API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntimeCommand.html) — 受支持命令及 event-stream 响应。
8. [StopRuntimeSession API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_StopRuntimeSession.html) — 受支持 session stop。
9. [Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) — session-user mapping、最小权限与命令边界。
10. [Credentials management](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-credentials-management.html) — workload 可用 credentials 与最小权限责任。
11. [File system configurations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html) — per-session managed storage 与共享 EFS/S3 语义。
12. [Amazon EC2 managed instances](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-managed-instances.html) — operator/client 管理责任和 managed instance 限制。
