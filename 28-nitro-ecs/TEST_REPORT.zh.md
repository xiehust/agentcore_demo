# ECS Nitro Sandbox DSH 实测报告

## 结论

- 已在 ECS Managed Instances 上运行原版 DSH，DescribeTasks 确认 `ISOLATED_SANDBOX`，不是普通容器模拟。
- 零已注册容量的首个 task：本机提交 RunTask → RUNNING 为 **70.368 秒**，→ HTTP ready 为 **72.996 秒**。这是包含容量准备的端到端时间，不是 Nitro guest 创建的单独耗时。
- 热容量下新建 task：ECS `createdAt → startedAt` 为 **17.090–17.622 秒**；该 task 的首次 DSH 文本为 **3.325–3.353 秒**，包含 DSH 初始化及 Bedrock 推理。
- 性能批次请求 5 轮，实际执行 2 轮、4 次 DSH 调用。4 次均正常完成，但仅 **3/4** 最终答案精确符合 `pong`；第 2 轮 warm 错答触发失败并停止。不能标为性能测试通过，也不能删除该慢样本。
- 磁盘隔离的 **13 项行为断言均通过**：同一 EC2 上 task 独立 guest/文件、根文件系统只读，task 重建后三处临时 marker 不保留，另一 task 不受影响。最终批次退出/清理状态见资源章节。
- Nitro Sandbox **不支持 VM snapshot/restore 或 pause/resume**。恢复应采用应用 checkpoint + 外部持久化，不等同于完整进程/内存快照。

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

本地校验：**20 项 Python unittest + 1 项 Node test 均通过**，Node/Bash 语法与 Python compileall 通过。新增测试覆盖历史任务记录缺失与其他查询错误的区分、只追踪当前已确认任务的失败启动清理、已受理 StopTask 不重放且等待真实 STOPPED。单测不计入云端性能/隔离样本。
