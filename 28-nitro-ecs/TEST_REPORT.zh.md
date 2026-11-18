# ECS Nitro Sandbox DSH 实测报告

## 结论

- 已在 ECS Managed Instances 上运行原版 DSH，DescribeTasks 确认 `ISOLATED_SANDBOX`，不是普通容器模拟。
- 零已注册容量的首个 task：本机提交 RunTask → RUNNING 为 **70.368 秒**，→ HTTP ready 为 **72.996 秒**。这是包含容量准备的端到端时间，不是 Nitro guest 创建的单独耗时。
- 热容量下新建 task：ECS `createdAt → startedAt` 为 **17.090–17.622 秒**；该 task 的首次 DSH 文本为 **3.325–3.353 秒**，包含 DSH 初始化及 Bedrock 推理。
- 性能批次请求 5 轮，实际执行 2 轮、4 次 DSH 调用。4 次均正常完成，但仅 **3/4** 最终答案精确符合 `pong`；第 2 轮 warm 错答触发失败并停止。不能标为性能测试通过，也不能删除该慢样本。
- 磁盘隔离的 **13 项行为断言均通过**：同一 EC2 上 task 独立 guest/文件、根文件系统只读，task 重建后三处临时 marker 不保留，另一 task 不受影响。最终批次退出/清理状态见资源章节。
- Nitro Sandbox **不支持 VM snapshot/restore 或 pause/resume**。恢复应采用应用 checkpoint + 外部持久化，不等同于完整进程/内存快照。
- 2026-11-18 的 CPU 超分目标 1:2 实测**未达到目标**：16 个 CPU 未设限 task 被 Managed Instances 调度到 4 台 `m7g.2xlarge`，每 task 吞吐约等于单 task 基线。详细 placement、吞吐与清理证据见 `results/overcommit-1to2-20261118.json`。同日改用单宿主复测：限定 `m7g.large`（2 vCPU）容量池后，**4 个 Sandbox 落在同一台宿主，1:2 达成**；并发时每 task 吞吐 0.50× 基线，聚合 2.00× 基线（见下文“单宿主复测”）。

## 环境与方法

实测日期：2026-11-17（UTC）；性能批次开始于 15:40:42，隔离批次开始于 15:48:48。

| 项目 | 配置 |
|---|---|
| Region / AZ | `us-east-1` / `us-east-1c` |
| 宿主 | ECS Managed Instances，`m7g.2xlarge`，ARM64，开启 `nitroEnclavesEnabled` |
| Task | 2 vCPU / 4 GiB，`awsvpc`，`ISOLATED_SANDBOX`，无公网 IP |
| 软件 | `@deepseek-ai/dsh@0.2.2-rc.1`，Node `v24.22.3` |
| 模型 | Bedrock `deepseek.v3.2` |
| 镜像 | digest 固定：`sha256:2e22ea4d1efeb0d0b50d2916e99ff59629a8c50b7933991b26498906c79b89eb` |
| 磁盘 | 根文件系统只读；task 私有 `/workspace`、`/tmp`；`DSH_HOME=/workspace/.dsh` |
| 探针 | 私有子网 Lambda，由本机通过 IAM/SigV4 Invoke；8080 仅允许 probe SG |

性能实验先启动 capacity-anchor，随后保持它运行，每轮新建 task。每个 cold 请求前不执行 DSH `--help`，核对新的 guest `boot_id`；warm 请求仍新建 DSH 子进程，只复用 task 和文件/页缓存，不是长驻进程或会话。每次模型请求只有一次，不自动重放。

本次性能批次的 `client_submit_to_ready_ms` 在 GET `/health` 前额外执行了一次宿主查询，包含该 AWS CLI 调用耗时。当前代码已经把宿主查询移到 ready 计时之后；**本报告保留原数据，不将其当作新版本计时结果**。RUNNING 指标包含 CLI 和轮询观察误差；ECS lifecycle 指标使用服务端时间。凭证解析发生在 DSH spawn 前，单独报告。

磁盘实验 A/B 均落在 `i-05e8d28165b8a0758`，先核对同一 EC2、不同 guest boot，再写/读 task 内 marker，停止 A 并等待实际 STOPPED，重建 A 验证临时卷状态。

## 结果

### 冷启动与 DSH 响应

| 指标 | 样本数 | 最小–最大 |
|---|---:|---:|
| 首个 task：RunTask → RUNNING | 1 | 70.368 秒 |
| 首个 task：RunTask → ready | 1 | 72.996 秒 |
| 热容量新 task：RunTask → RUNNING | 2 | 20.197–20.378 秒 |
| 热容量新 task：RunTask → ready（旧口径） | 2 | 22.873–22.966 秒 |
| 热容量新 task：ECS created → started | 2 | 17.090–17.622 秒 |
| ECS image pull | 2 | 128–130 ms |
| Cold：spawn → first event | 2 | 2.606–2.700 秒 |
| Cold：spawn → first text | 2 | 3.325–3.353 秒 |
| Cold：spawn → 完成 | 2 | 3.411–3.432 秒 |
| Warm：spawn → first text（含错答） | 2 | 2.610–8.334 秒 |
| Warm：spawn → 完成（含错答） | 2 | 2.698–8.418 秒 |
| Cold：凭证解析 | 2 | 56.467–56.810 ms |
| Warm：凭证解析 | 2 | 2.158–2.279 ms |

128–130ms 的镜像 pull 符合本轮热缓存表现，不能称为 image-cold 测试。样本只有 1–2 个；脚本提供 nearest-rank 百分位，但本报告不将小样本 p99 宣称为生产 SLA，也没有普通 ECS/Fargate 对照组可用于归因 Nitro 开销。

| 回合 | first text | 完成 | 精确 `pong` |
|---|---:|---:|---|
| 第 1 轮 cold | 3.353 秒 | 3.432 秒 | 是 |
| 第 1 轮 warm | 2.610 秒 | 2.698 秒 | 是 |
| 第 2 轮 cold | 3.325 秒 | 3.411 秒 | 是 |
| 第 2 轮 warm | 8.334 秒 | 8.418 秒 | **否，批次失败** |

错答请求退出码为 0，`stop_reason=completed`，因此完成耗时与正确率分开报告。未保留模型输出正文，只保存精确匹配布尔值；**不能从该证据诊断答案偏离的原因**。

### 磁盘行为验证

| 断言 | 结果 |
|---|---|
| `same_physical_EC2_instance`、`different_guest_boots` | 通过：同宿主、不同 guest |
| `isolation-A_root_readonly`、`isolation-B_root_readonly` | 通过：写根目录得到 `EROFS` |
| `B_cannot_see_A_new_file` | 通过：B 不可见 A 新建 workspace 文件 |
| `same_filename_different_content` | 通过：A/B 同名文件内容互不覆盖 |
| `DSH_HOME_task_private` | 通过：两 task 的 home marker 不同 |
| `tmp_task_private` | 通过：B 不可见 A 的 `/tmp` marker |
| `new_task_new_guest` | 通过：A 重建后 guest boot 改变 |
| `ephemeral_workspace_not_persistent` | 通过：A 重建后 workspace marker 不存在 |
| `ephemeral_DSH_HOME_not_persistent` | 通过：A 重建后 home marker 不存在 |
| `ephemeral_tmp_not_persistent` | 通过：A 重建后 tmp marker 不存在 |
| `B_unaffected` | 通过：B 的 workspace 文件保持不变 |

这验证的是 task 文件可见性和生命周期行为，不是安全擦除、内核逃逸、宿主对抗或 IOPS/QoS 隔离测试。多个 task 仍共享实例级 EBS 吞吐/IOPS；默认每 task 20 GiB 临时存储不能当作持久化磁盘。

## CPU 超分目标 1:2 实测（2026-11-18）

本次把 task-level `cpu` 留空，task memory 设为 512 MiB；基线和并发阶段各运行 10 秒、单 Node worker 的 CPU-bound 探针，不调用 DSH 或 Bedrock。8-vCPU `m7g.2xlarge` 的目标是 16 个 Sandbox。任务定义的可信执行配置为 `ISOLATED_SANDBOX`，ECS 返回的 `cpu` 为 `0`；宿主 EC2 的 `EnclaveOptions.Enabled` 为 `true`。CloudFormation 创建任务定义是为了绕过本次 AWS MCP ECS SDK 模型不认识 `TrustedExecutionConfiguration` 的限制；本次运行使用 MCP 直接调用 ECS/Lambda API，不是通过本地 `overcommit.py` 发起。

| 指标 | 实测 |
|---|---:|
| 目标任务数 | 16（8 vCPU × 2） |
| 实际宿主数 / CPU 总量 | 4 台 `m7g.2xlarge` / 32 vCPU |
| 每宿主 Sandbox 数 | 5、5、5、1 |
| 总体密度 / 单宿主最高密度 | 0.50 / 0.625 Sandbox 每 vCPU |
| 单 task 基线 | 779.91 M ops/s（1 个样本） |
| 并发 16 task 平均 / 中位吞吐 | 784.21 / 783.80 M ops/s |
| 并发 task 吞吐范围 | 779.19–791.06 M ops/s |
| 并发均值 / 单 task 基线 | 1.006× |

**判定：`target_not_observed`。** 调度器扩展到 4 台宿主，而不是在一台 8-vCPU 宿主上运行 16 个 Sandbox；并发 task 的单任务吞吐也没有明显低于基线。因此这次没有观测到 1:2 CPU 竞争，不能宣称 ECS 达到超分。该结果只说明本次容量池/任务设置下未达到目标，不证明 ECS 所有配置都无法超分；公开文档没有给出把 capacity provider 因子精确设为 2× 的 API 字段，需先确认该控制参数如何暴露，再做可控的密度对照。

运行完成后，16 个 task 均停止、4 台 ECS Managed Instances 均由 ECS 回收、临时 probe Lambda 和日志组删除、CPU 测试镜像 tag 删除、临时 task definition 注销。原有 cluster、IAM、SG、ECR repository 和原始镜像未删除。结构化指标见 `results/overcommit-1to2-20261118.json`。

## CPU 超分 1:2 单宿主复测（2026-11-18，`overcommit.py` 端到端）

上一轮的 `m7g.2xlarge` 池把 16 个 task 扩散到 4 台宿主。ECS Managed Instances 的 `memberOf` 只支持 subnet/AZ/arch/instance-type 属性，不能按 instance-id 固定宿主，因此本轮新建只允许 `m7g.large`（2 vCPU、8 GiB、Graviton 每核 1 线程）的专用容量池，目标为 4 个 Sandbox。`overcommit.py` 一旦发现 task 落到第二台宿主即失败。本轮由本地 `overcommit.py` + AWS CLI 端到端执行，task 级 `cpu` 未设置，memory 512 MiB，基线和并发各 10 秒单线程 CPU 探针。

| 指标 | 实测 |
|---|---:|
| 宿主 | 1 台 `m7g.large`（`i-0a74b91637ac12cc8`），2 vCPU / 2 核 / 每核 1 线程 |
| Sandbox 数 / 密度 | 4 / **2.0 每 vCPU** |
| 启动到 ready | 首个 73.7 s（含宿主冷启动）；后 3 个 22.0–22.2 s |
| 单 task 基线 | 773.86 M ops/s |
| 4 task 并发吞吐 | 379.3–395.4 M ops/s，均值 387.44 M |
| 并发均值 / 基线 | **0.501×** |
| 聚合 / 基线 | **2.00×**（= 2 个物理核） |

ECS 调度视图（`DescribeContainerInstances`）：注册 CPU 2048、MEMORY 6088 MiB。4 个 task 后 CPU 剩余仍为 2048，说明未设 `cpu` 的 task 不占 CPU 调度额度；MEMORY 每 task 扣 774 MiB（512 MiB 请求 + 约 262 MiB Sandbox 开销），4 个后剩 2992 MiB。按内存估算可放约 7 个，但后续探测实测每宿主上限是 5 个（见下文），即 `m7g.large` 最高 2.5:1。此外，`m7g.large` 最多 3 个 ENI，但 4 个 awsvpc task 照常运行（账户 `awsvpcTrunking` 为 disabled），因此本例中宿主 ENI 数量不是 Sandbox 数的上限。

**判定：`passed`。** 一台宿主上运行 2 倍于 vCPU 数的 Nitro Sandbox，CPU 按核公平分时：每 task 约降为一半，总吞吐等于物理核数，未见额外损耗。这是 CPU-bound 的经验测量：单样本、10 秒、单线程，不代表 I/O 或内存密集负载，也不是性能承诺。上一轮 `m7g.2xlarge` 每宿主只放 5 个 task 的原因见下一节：每台宿主最多 5 个 Sandbox，与机型大小无关。

清理：4 个 task 已停止并注销实验 task definition，删除容量池 `dsh-nitro-ecs-20261117-oc-m7g-large`（`INACTIVE/DELETE_COMPLETE`，EC2 已由 ECS 终止），删除 probe Lambda 及其日志组，注销 revision 4。保留 ECR 镜像 tag `nitro-cpu-oversub-20261118`、cluster、IAM、SG。证据：`results/overcommit-1to2-m7g-large-20261118/results.json`（含逐 task CPU 样本和每次放置后的宿主资源快照）。

## 为什么 `m7g.2xlarge` 会扩散：每宿主 5 个 Sandbox 上限（2026-11-18）

**结论：不是部署代码问题，也不是 CPU、内存或 ENI 不够。ECS Managed Instances 每台宿主最多只放 5 个 `ISOLATED_SANDBOX` task**；`m7g.large` 与 `m7g.2xlarge` 实测都是 5。公开文档未写明该上限，`DescribeContainerInstances` 的资源里也看不到。

| 证据 | 观察 |
|---|---|
| 首轮 CloudTrail | 首轮由 AWS MCP 发起：两个单 task 基线后，`RunTask count=10` 与 `count=5` 相隔 1 秒提交。宿主 A 当时已有 1 个 task，只接收 4 个（共 5 个），余下 11 个挂起；4 秒内 ECS 再 `CreateFleet` 3 台 `m7g.2xlarge`，分布为 5/5/1。CloudFormation 创建的容量池参数与 `configuration.py` 一致（8 vCPU / 32 GiB / `m7g.2xlarge` / Nitro 开） |
| 用仓库脚本顺序复测 `m7g.2xlarge` | `overcommit.py` 每次 count=1、等 ready 再启动下一个：第 1–5 个同宿主，第 6 个不放到该宿主，而是触发新建实例（该 AZ 当时 `InsufficientInstanceCapacity`）。第 5 个后宿主仍剩 CPU 8192、MEMORY 23864 MiB |
| `m7g.large` 探上限 | 目标 10 个：第 1–5 个同宿主，第 6 个落到新宿主。第 5 个后剩 MEMORY 2218 MiB，仍够约 2 个 774 MiB 的 Sandbox |
| ENI | 每个 task ENI 都是 `branch` 类型（Managed Instances 自动 trunking，与账户 `awsvpcTrunking=disabled` 无关），且不挂在宿主实例上；按 attachment 查宿主 ENI 返回 0。`m7g.large` 最多 3 个 ENI 仍放了 5 个，ENI 不是上限 |

据此，单宿主可达的 Sandbox/vCPU 比例由宿主大小决定：`m7g.large` 最高 2.5:1，`m7g.xlarge` 1.25:1，`m7g.2xlarge` 0.625:1。所以 8 vCPU 宿主无论怎么调度都放不下 1:2 所需的 16 个；要提高密度应使用小机型。旁证：EC2 Firecracker 路线（`27-nitro-ec2`）中 Nitro Enclaves 驱动报告 `pcie_slots_total=20`、可用 slot 由宿主配置决定（该 `m7g.xlarge` 为 4）。Managed Instances 宿主无法登录读取该 sysfs，因此“ECS 为每台宿主配置 5 个 Sandbox slot”只是推断，未证实。上限是否随区域、机型族或版本变化未测，生产规划前请向 AWS 确认。

证据：`results/overcommit-1to2-m7g-2xlarge-seq-20261118/`、`results/overcommit-cap-m7g-large-20261118/`（含每次放置后的宿主资源、宿主 ENI 与 task ENI 快照）。两次复测的容量池、task、probe Lambda、日志组均已删除，3 台实例已由 ECS 终止。

## 原始证据与失败

- `results/run-20261117-ecs/results.json`：性能原始结果，`status=failed`，错误 `DSH answer is not exactly pong`；保留各轮回合及启动信息。
- `results/run-20261117-ecs/derived-summary.json`：从失败原始批次本地派生，未请求模型、未覆盖源文件。
- `results/run-20261117-ecs/*-run-task.json`、`*-ready.json`、`probe-*.json`：RunTask ack、实际 task/host/guest identity 与探针结果。
- `results/isolation-20261117/results.json`：13 项磁盘断言、存储探针、task 重建结果和清理错误；最终运行状态见下一节。
- `results/security-check/function-urls.json`：Function URL 列表为空；`unsigned-invoke-error.txt`：无签名 Invoke 返回 `MissingAuthenticationTokenException`。

部署过程中 `us-east-1b` 的 `m7g.2xlarge` 容量不足，改到 `us-east-1c`。CLI 2.40.3 的 UpdateCapacityProvider 路径不支持传入 Nitro 字段；更新 launch template 后 describe 中 Nitro 开关消失。因此后续改为创建新的专用 provider → 验证 Nitro 开关 → 切换空 cluster → 删除旧 provider。`scripts/move_capacity_az.py` 保留这一安全路径，不能以去掉不识别字段的更新作为替代。

性能批次记录了历史 capacity-anchor 的清理查询错误，后续查询该历史 ARN 返回 `MISSING`；不能把缺失记录伪写为已观察到 STOPPED。当前 benchmark 仅清理本次 RunTask 确认的任务；cleanup 脚本单独处理历史记录过期，并核对整个专用 cluster 无 running/pending task。

隔离批次所有断言均通过，但当时运行的旧版脚本在 finally 中查询了同一个已过期的历史 ARN，记录 `cleanup_errors=[RuntimeError]`，因此**原始批次整体 `status=failed`、退出码 1**。不能把 13 项断言通过改写为原始批次整体成功；清理脚本后续成功也不会改写原始结果。本次没有为获得绿色状态重复运行隔离实验。

## 资源状态与限制

`cleanup.py` 已完成。2026-11-17 **16:00:46 UTC** 独立只读 API 核验通过，证据见 `results/cleanup-verification-20261117.json`：

- 专用 cluster running/pending task 均为 **0**，注册容器实例为 **0**，ListTasks 无 desired RUNNING task。
- 6 个仍可查询 task 均为 **STOPPED**；1 个历史 ARN 明确返回 `MISSING`，保留这一事实，不将它记为观察到 STOPPED。
- 两个示例 capacity provider 均为 **INACTIVE / DELETE_COMPLETE**。
- 实测宿主 `i-05e8d28165b8a0758` 为 **terminated**；没有直接 terminate AWS-managed EC2，由 ECS 回收。
- probe Lambda 已不存在。未修改两个原始失败批次的状态。

该核验不是仅凭 StopTask/DeleteCapacityProvider 已受理就宣称资源回收。当前本地 `state.json` 指向已清理环境，不能直接用它再次运行 benchmark。

ECR 镜像、CloudWatch 日志、IAM roles/profile、SG、空 cluster 和 task definition revisions 按设计保留用于复核，镜像/日志仍可能产生小额存储费；共享 VPC/NAT 未删除。

未部署/实测：EFS 持久化、`CONFIDENTIAL_SANDBOX`、应用会话 checkpoint/resume、全量文件恢复、一致性备份、宿主对抗防护。`examples/persistent_efs.py` 只是配置生成器；README 的 checkpoint 结构只是恢复设计，不是已实现的 DSH 会话格式。EFS 文件持久化不保存 RAM/进程，不能将其称为 Nitro 快照。

`ISOLATED_SANDBOX` 信任 parent；要防 parent 读取敏感数据需 confidential + attestation/KMS + 应用内加密设计，不能仅依赖 EBS/SSE-KMS。Confidential 的 EFS 限制见官方 ECS 文档。

2026-11-17 初版本地校验：**20 项 Python unittest + 1 项 Node test 均通过**，Node/Bash 语法与 Python compileall 通过。2026-11-18 加入 CPU 探针与超分测量后，当前本地套件（含单宿主复测改动）**28 项 Python unittest + 1 项 Node test 均通过**，Node/Bash 语法、Python 编译与 HTML 桌面/移动检查通过。CPU 超分云端样本由 AWS MCP 直接 API 编排；由于当前 MCP ECS SDK 模型不含 `TrustedExecutionConfiguration`，首轮不是通过 `overcommit.py` 启动；单宿主复测已由 `overcommit.py` 端到端完成。单测不计入云端性能/隔离样本。
