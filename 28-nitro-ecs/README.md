# ECS Nitro Sandbox 上的 DSH：部署、冷启动与磁盘隔离

初学者先看 [Nitro Sandbox 图文上手教程（中文 HTML）](nitro-sandbox-guide.zh.html)：可双击离线打开，覆盖 EC2 / ECS、DSH、磁盘隔离、冷启动，以及没有 VM 快照时的应用层恢复。

## 范围与官方接口

本目录继续已有部署脚手架，用原版 `@deepseek-ai/dsh@0.2.2-rc.1` 验证 **ECS Managed Instances + `ISOLATED_SANDBOX`**，不是普通 ECS/EC2 Docker 隔离，也不是 Fargate 或传统 Nitro Enclaves。每个 task 独立 guest/kernel，同一 EC2 可放多个 task；同一 task 内的容器不构成新的租户边界。建议一个 task 对应一个租户/会话。

核心配置见 `configuration.py`：

```json
{
  "instanceLaunchTemplate": {"nitroEnclavesEnabled": true},
  "taskDefinition": {
    "requiresCompatibilities": ["MANAGED_INSTANCES"],
    "networkMode": "awsvpc",
    "trustedExecutionConfiguration": {"isolationMode": "ISOLATED_SANDBOX"}
  }
}
```

这是两份配置的字段摘录，不能整体提交一个 API。实际脚本分别调用 `CreateCapacityProvider` 和 `RegisterTaskDefinition`。`RunTask` 使用 `capacityProviderStrategy`；通过 `DescribeTasks.trustedExecutionSummary` 验证实际隔离模式，而不是仅查看期望配置。

依据：[ECS 概览/存储](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ict-overview.html)、[安全模型](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ict-security-model.html)、[隔离模式](https://docs.aws.amazon.com/help-panel/AmazonECS/latest/console/hp-taskdef-isolation-mode.html)。实际结果见 `TEST_REPORT.zh.md`。

## 部署与运行

前提：支持这些 API 字段的 AWS CLI v2、Python 3.11+、Docker/BuildKit，以及 ARM64 构建能力；AWS 凭证能管理示例 ECS/ECR/IAM/Lambda/SG，并访问 Bedrock `deepseek.v3.2`。本仓库是独立示例，不需要 Brazil 包或 boto3。默认 region `us-east-1`，示例 host 为 `m7g.2xlarge`，task 为 2 vCPU / 4 GiB。

使用**已有私有子网和 NAT**，不创建或修改共享 VPC/NAT。宿主和任务需要 ECR、Logs、ECS、Bedrock 所需出站连通性。task 没有公网 IP，安全组 8080 只接收 probe Lambda 的 SG。

```bash
cd 28-nitro-ecs
aws sts get-caller-identity
python3 deploy.py create --region us-east-1 --name dsh-nitro-ecs-$(date -u +%Y%m%d%H%M%S) \
  --subnet YOUR_PRIVATE_SUBNET
bash scripts/build_push.sh
python3 benchmark.py --rounds 5 --mode all --out results/my-run
python3 cleanup.py
```

`state.json` 已存在时**不要重新 create**。检查资源状态后，用 `build_push.sh` 产生新的 immutable image tag/任务 revision，或直接运行 benchmark。镜像通过 digest 固定；脚本不把本机凭证烘入镜像。`state.json`、`generated/`、临时 ZIP 已忽略。结果目录必须不存在，避免覆盖证据。

```bash
python3 benchmark.py --rounds 5 --mode performance --out results/performance-only
python3 benchmark.py --mode isolation --out results/isolation-only
python3 deploy.py register --image-tag YOUR_EXISTING_TAG
```

## CPU 超分实验：目标 1:2

`overcommit.py` 是 **ECS Managed Instances + Nitro Sandbox** 的 CPU 密度压测，不适用于 Fargate。AWS 文档说明 task-level `cpu` 留空时任务可共享宿主机核心，Managed Instances capacity provider 控制超分因子（最高 5×/vCPU）；但当前公开 API 示例没有说明如何指定一个精确的 2× 因子。因此本实验不声称配置出保证值，而是创建一个**不含 task-level `cpu`** 的实验任务定义，按宿主机每个 vCPU 启动 2 个 Sandbox，再记录实际放置密度和 CPU 吞吐。如果 ECS 把任务扩散到多个宿主，结果会报告实际比例，不会算作达到 1:2。

示例容量池是 `m7g.2xlarge`（8 vCPU），目标为 16 个 Sandbox task。实验使用 512 MiB task memory、无模型的 CPU-bound 探针；先测一个 task 的单任务基线，再同时压测目标 task。结果包括每个 EC2 宿主的 task/vCPU 比例、每 task 吞吐、相对基线吞吐及清理状态。`cpu` 未设置意味着**没有每 task CPU 保底/上限**，结果是经验测量，不是性能承诺。该实验会启动付费 ECS/EC2 容量，请只在专用测试环境运行。

Managed Instances 的 `memberOf` 不支持按 instance-id 固定宿主，而且实测**每台宿主最多 5 个 Sandbox，与机型大小无关**（`m7g.large`、`m7g.2xlarge` 都是 5），所以 8 vCPU 宿主放不下 16 个，任务会扩散到多台宿主。这不是部署代码问题，详见 `TEST_REPORT.zh.md`。可用 `--tasks-per-vcpu N` 探测单宿主上限。要在**一台机器**上测 1:2，新建只允许 `m7g.large`（2 vCPU）的专用容量池，目标为 4 个 Sandbox；`overcommit.py` 默认在任务落到第二台宿主时立即失败（`--allow-multiple-hosts` 可关闭）。

```bash
bash scripts/build_push.sh nitro-cpu-oversub-1to2
python3 deploy.py capacity --instance-type m7g.large   # 专用容量池 + 重建 probe Lambda
python3 overcommit.py --duration-seconds 10 --out results/overcommit-1to2-m7g-large
python3 cleanup.py
```

2026-11-18 单宿主实测：4 个 Sandbox 同在一台 `m7g.large`，并发时每 task 吞吐 0.50× 基线、聚合 2.00×，见 `TEST_REPORT.zh.md`。

脚本停止本次启动的 task，并注销临时 task definition；Managed Instances 宿主由 ECS 容量池回收，不要手动 terminate。若目标密度未观测到，命令以失败状态退出，但保留 `results.json` 和逐 task CPU 证据。CPU 配额说明见 [ECS Nitro Sandbox CPU 文档](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ict-getting-started.html)。

遇到 AZ 无容量，先确认已有任务停止，再创建新的**专用**容量池，显式保留 Nitro 开关：

```bash
python3 scripts/move_capacity_az.py --subnet ANOTHER_PRIVATE_SUBNET \
  --capacity-provider YOUR_NEW_DEMO_CAPACITY_PROVIDER_NAME
```

本次使用的 CLI 2.40.3 不接受 `UpdateCapacityProvider` 的 `nitroEnclavesEnabled` 字段；实测更新 launch template 后，describe 中该字段消失。不要把去掉不识别字段再更新当作安全的 AZ 迁移。脚本采用 Create → 验证 Nitro → 切换 cluster → 删除旧 provider；只适用于没有运行/待启动任务的专用 demo cluster。切勿用于生产容量池的无计划替换。

## 冷启动测量口径

| 指标 | 起止与限制 |
|---|---|
| `capacity_anchor` | 首个 task，记录启动前是否没有注册容器实例。只有 `capacity_was_zero=true` 才标为容量冷启动；包含 EC2/Bottlerocket 准备、调度、镜像及 guest/container 启动。 |
| `client_submit_to_running_ms` | 本机提交 RunTask 前 → polling 观察到 RUNNING；包含 AWS CLI 和最长约 2 秒轮询误差。 |
| `client_submit_to_ready_ms` | 同一起点 → Lambda GET `/health` 返回。包含 probe 调用/HTTP 开销，不等于 Nitro 创建耗时。 |
| `ecs_created_to_started_ms` | ECS `startedAt - createdAt`，使用服务端时间。 |
| `ecs_image_pull_ms` | ECS `pullStoppedAt - pullStartedAt`；不把它混同 DSH/模型 TTFT。 |
| `cold.first_event_ms` | 新 task 首次 DSH **子进程 spawn** → 首个 JSON event，可能只是 status。 |
| `cold.first_text_ms` | 同一起点 → 首个非空 text event；包含 DSH 初始化及 Bedrock 模型响应。 |
| `total_ms` / `credentials_ms` | DSH 子进程完成耗时 / spawn 前任务角色临时凭证解析耗时，分别报告。 |
| `probe_http_ms` | Lambda 发起 HTTP → 完整返回，包含凭证解析和 DSH 完成。 |

每轮新建 task、核对新 `boot_id`，首条 prompt 前不执行 `--help`，避免预热。anchor 保持运行，随后各轮是**容量已热、guest/task 新建**；同一 task-definition 的镜像可能缓存，不能声称每轮都是 image-cold。

每轮第二次 `warm` 请求仍新建 DSH 子进程，只复用 task 内页缓存/文件与潜在服务端缓存，**不是长驻 DSH 会话**。成功需要退出码 0、`completed`、实际 text，并断言最终文本恰好为 `pong`；错答案/超时立即停止，不自动重放模型 POST。p50/p90/p99 使用 nearest-rank，小样本的 p99 通常就是最大值，不代表生产 SLA。

`summarize.py --results results/my-run/results.json --out results/my-run/derived-summary.json` 可为已有失败批次补生成统计，不调用 AWS、不重放模型，也不覆盖源证据。摘要中的完成回合延迟与答案符合率分开；`status=failed` 不会因统计成功而变为通过。

## 磁盘隔离配置

### 临时磁盘：推荐默认

```json
{
  "volumes": [{"name": "workspace", "host": {}}, {"name": "tmp", "host": {}}],
  "containerDefinitions": [{
    "readonlyRootFilesystem": true,
    "mountPoints": [
      {"sourceVolume": "workspace", "containerPath": "/workspace", "readOnly": false},
      {"sourceVolume": "tmp", "containerPath": "/tmp", "readOnly": false}
    ]
  }]
}
```

完整 task definition 由脚本生成。无 `sourcePath`，不导出宿主目录；不传 Docker socket，不用 privileged。`HOME=/workspace`，`DSH_HOME=/workspace/.dsh`，DSH 工作目录 `/workspace`；bootstrap 只初始化挂载点所有权，随后降到 UID/GID 1000。根文件系统只读不妨碍 DSH 在这些可写位置运行。

ECS Nitro 每 task **20 GiB ephemeral storage**，镜像压缩/展开也占空间。不能用 task `ephemeralStorage` 调大，也不支持 `configuredAtLaunch=true` 为 task 单独挂 EBS。provider 的 `storageSizeGiB=80` 是**实例级数据盘容量**，不是每 task 80 GiB；实例级 EBS 可配 30–16,384 GiB，多 task 共享该盘 IOPS/吞吐，不等于磁盘 QoS 隔离。临时卷不是容器内存 `tmpfs`，task 销毁不应作为持久化方案。

`/storage` 记录 statfs 空间、`/health` 记录 `/proc/mounts`，同时验证写根文件系统得到 `EROFS`。statfs 输出是 guest 可见文件系统数据，不能只凭其值推导 EBS 物理分区、加密实现或 secure erase 保证。

隔离探针验证：A/B 同一 `ec2InstanceId`、不同 guest boot；同名 workspace/home 文件内容不同；B 看不到 A `/tmp` 文件；停止并重建 A 后三处临时 marker 均不存在，B 不受影响。**这是行为验证，不是内核逃逸/渗透测试。** 同一 task 内共享 DSH_HOME 给多个租户不能获得此边界。

### 持久化：EFS access point + 独立 task role

`examples/persistent_efs.py` 只生成配置，**不创建 EFS、不挂载、不声称已实测**：

```bash
python3 examples/persistent_efs.py --state state.json --tenant tenant-a \
  --filesystem-id fs-YOUR_ID --access-point-id fsap-YOUR_ID \
  --task-role-arn arn:aws:iam::YOUR_ACCOUNT:role/YOUR_TENANT_TASK_ROLE \
  --out generated/tenant-a
```

依次创建加密 EFS、私有 mount targets，2049 入站仅允许 task SG；按 `access-point-settings.json` 创建 access point（UID/GID 1000，`/tenants/tenant-a`，0700）。将 `efs-client-policy.json` 附加到该租户 task role，并保留调用 Bedrock 的权限。task definition 使用 IAM authorization + TLS + 指定 access point；`rootDirectory` 必须省略或 `/`。

`efs-filesystem-policy.json` 是**专用单租户文件系统**示例：显式拒绝其他 principal、其他 access point、非 TLS 及 ClientRootAccess，防止仅靠 task role allow 而仍可匿名/旁路挂载。不要直接套到已有共享文件系统：它会拒绝其他租户。多租户共享一个 EFS 时需要生成所有 role→access-point 的允许关系与对应拒绝条件，再统一审查/应用 filesystem policy；不要逐租户覆盖同一个 policy。应用 EFS policy 及注册 EFS task definition均需操作者自行执行、审查，生成器不会远程修改资源。

`ISOLATED_SANDBOX` 仍信任 parent；它的 ephemeral overlay 不提供“宿主不能读取/修改”的保证。若要防宿主，必须设计 `CONFIDENTIAL_SANDBOX` + attestation policy + KMS 按测量值放钥，不能只改枚举值。本示例没有部署 confidential 或 EFS 模式。按 2026-11-18 核对的[当前概览](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ict-overview.html)与[安全模型](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ict-security-model.html)，不应沿用“confidential 一律不支持 EFS”的表述：文档讨论 EFS 支持及其保密风险；TLS 不防有自己 IAM 身份的 parent 旁路挂同一文件系统，敏感数据应由应用先加密。此 EFS 生成器仅面向 isolated，confidential 组合仍需单独设计、验证。

## 快照与恢复：应用层 checkpoint，不是 VM snapshot

**Nitro Sandbox 当前不支持 snapshot/restore、pause/resume 或 live migration。** ECS 的 isolated/confidential task 使用这个 Sandbox，不能通过 ECS API 保存 guest RAM、进程、文件描述符或正在运行的 DSH 会话，再从原执行位置恢复。底层使用 Firecracker 不等于可以使用普通 Firecracker 的快照接口；父实例也不能开启 hibernation。依据：[EC2 Nitro Sandboxes 的 Lifecycle 限制](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nitro-sandboxes.html)。

可选 workaround：

| 需要恢复什么 | 做法 | 不能恢复什么 |
|---|---|---|
| 工作文件、DSH 磁盘数据 | isolated task 挂载租户专用 EFS access point，使用本目录 `examples/persistent_efs.py` 生成配置；新 task 挂回同一 access point | RAM、运行中的进程/连接；文件持久化本身不保证 DSH 自动恢复会话 |
| 对话、工具结果、任务进度 | 应用按完成的 turn/工具步骤保存版本化 checkpoint 到私有 S3/EFS/数据库；新 task 校验并加载，重新建立 DSH/模型上下文 | 原进程执行位置、原模型采样状态；重建后的回答可能不同 |
| 一个文件版本 | 在一致性边界生成文件归档，或使用 EFS backup / 外部数据卷备份 | Sandbox 完整快照；本示例 task 的临时卷没有可直接恢复的 task snapshot API |
| 尽量保留现有内存状态 | 保持 task 运行，配合应用 checkpoint 应对故障 | 不是真正暂停，持续占用容量/计费，也不能保证父实例替换后存活 |

本示例 `DSH_HOME=/workspace/.dsh`，EFS 方案会同时保留工作文件和 DSH 的磁盘目录，但 `/tmp` 仍是 task 私有临时卷。**当前 HTTP wrapper 每次 `/invoke` 都启动新的 `dsh headless` 子进程，没有实现会话 checkpoint/resume；不能把本目录的 warm 测量或 EFS 配置生成器当作已实现的会话恢复。** 具体 DSH 版本能否从自己的磁盘数据恢复，需要按它的原生会话接口单独验证；没有该接口时，应在应用层保存消息与工具结果，再显式重新提供上下文。

推荐的 checkpoint 元数据结构（设计示例，不是 DSH 自带的文件格式）：

```json
{
  "schema_version": 1,
  "tenant_id": "tenant-a",
  "session_id": "session-001",
  "checkpoint_id": "turn-0002",
  "dsh_version": "0.2.2-rc.1",
  "image_digest": "sha256:YOUR_IMAGE_DIGEST",
  "model_id": "deepseek.v3.2",
  "completed_turn": 2,
  "messages_ref": "messages.json",
  "tool_results_ref": "tool-results.json",
  "workspace_archive_ref": "workspace.tar.gz",
  "pending_operations": []
}
```

保存流程：在工具/文件写入完成的一致性边界，先落盘并 flush，写入不可变 checkpoint 文件及校验值，最后原子发布 manifest/最新版本指针；数据库用事务，多 writer 用条件更新/租约。恢复流程：新建 task → 校验租户、版本、校验值和授权 → 恢复文件 → 重新获取 task role 临时凭证 → 重建会话上下文 → 从最后完成步骤继续。不能对正在写的数据库/文件直接打包就假设一致；不能只在 SIGTERM 时保存，异常终止不保证给到清理时间。

有外部副作用的工具调用需保存 operation ID 和完成记录，并使用下游服务支持的幂等键；恢复时先核对未决操作，**不要盲目重放付款、发消息、写数据库等工具**。Checkpoint 不保存 AWS 临时凭证、token 或打开的 socket；对话和工具输出按敏感数据处理，S3 开启 Block Public Access、加密和按租户前缀的最小权限，EFS 使用前述 access point/role 约束。

EFS/S3/备份只解决数据层恢复，不能消除 ECS 调度和新 guest/task 的冷启动。降低冷启动应独立优化热容量、镜像缓存及应用初始化；如果需求是完整 RAM/进程快照或真正休眠，需要选择明确支持该能力的其他运行平台。

## 安全、清理和费用

探针入口是 AWS Lambda Invoke 的原生 IAM/SigV4 授权，没有 Function URL、ALB 或公网监听。Lambda role 不能调用 Bedrock，只有 task role 允许目标模型。任务仅绑定自己的 RFC1918 ENI 地址，8080 入站仅 probe SG；本地默认 loopback。删除了原脚手架自制 token，不把它当认证机制。能 Invoke probe、修改/绑定 probe SG、注册 task definition 或 RunTask 的身份都是特权身份，必须限定。

probe 只允许同 VPC CIDR 的 IPv4、固定端口/路由；这是网络范围限制，**不是生产级任意任务授权**。生产需要进一步绑定任务身份/租户与授权，不应把此 benchmark 服务直接对外开放。此示例 HTTP 只在可信 isolated 模式的私网传输固定测试 prompt/marker，敏感业务通信应改 TLS。DSH 子进程使用任务角色临时凭证，不写证据；限制请求/输出大小、120 秒进程超时、单任务一次操作；stdout/stderr 不持久化潜在敏感内容。AWS mutation 和模型 POST 不自动重试。

benchmark 的 `finally` 停止已被 RunTask 确认的 task，启动失败 task 也记录并清理；但 capacity 空闲等待仍可能计费。运行后执行 `python3 cleanup.py`，由 ECS 删除专用 capacity providers、回收 Managed Instances，并删除 probe Lambda；**不直接 terminate AWS-managed EC2**。中断/失败后检查 `state.json` 和真实 API 状态，再继续，不盲目重放 create/run。

cleanup 保留 ECR 镜像、7 天保留策略的 CloudWatch logs、IAM/profile、SG、空 cluster 和 task definition revisions，便于复核；ECR/日志仍可能有小额存储费。共享 VPC、NAT 不动，EFS 生成器也没有创建付费资源。删除保留资源前应先归档证据并确认名称/归属。

历史 STOPPED task 的 DescribeTasks 记录会过期。cleanup 只跳过明确返回该 ARN `MISSING` 的记录，并记录 `task_records_unavailable`；权限错误或其他查询失败不会跳过。仍需核对可查询任务实际 STOPPED 和 cluster 的 running/pending 数均为 0，才删除专用容量池，不能把 `MISSING` 写成已观察到 STOPPED。

`state.json` 的部署、迁移、benchmark、cleanup 命令要串行执行，不同时开多个 writer；独立输出目录不等于独立 AWS 实验环境。停止任务有异步控制面延迟，脚本给 600 秒 deadline；达到 deadline 会报错，不能把 StopTask 已受理等同已回收。

## 验证

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_server.mjs
node --check app/server.mjs
bash -n scripts/build_push.sh
```

单测使用本地 fixture/mock，不计入云端性能样本。可重复的云端实验由 `benchmark.py` 驱动，证据包含每次 RunTask ack、task/host/guest identity、每次 probe 和最终 `results.json`；失败样本独立保留，不能只挑成功样本报告。

文件分工：`deploy.py` 基础设施和任务注册；`configuration.py` Nitro/卷/IAM 配置；`app/server.mjs` DSH、磁盘和 CPU 探针；`app/probe_handler.py` 私网探测；`benchmark.py` 冷启动/隔离实验；`overcommit.py` 目标 1:2 CPU 密度实验；`cleanup.py` 回收；`TEST_REPORT.zh.md` 历史实测结果。
