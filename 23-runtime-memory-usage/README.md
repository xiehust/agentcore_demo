# AgentCore Runtime 内存计费用量对照实验

**已完成一次真实 AWS 实测：见 [RESULTS.md](RESULTS.md)。** 小镜像约 22 MiB 应用 RSS，对应约 1.2 GB-equivalent 资源用量遥测；这些遥测不是权威账单。

## 要验证什么

比较三层数据，**不把容器内 RSS 当成计费内存，也不把 CloudWatch 遥测叫做最终账单**：

1. 应用：`/proc/self/status` 的 VmRSS/VmHWM、`smaps_rollup` 的 RSS/PSS、`ru_maxrss`。
2. 运行环境：可读的 cgroup v1/v2 内存、`/proc/meminfo`、进程列表和挂载信息。
3. AWS：每个 session 的 `USAGE_LOGS` 和 Runtime 级 `MemoryUsed-GBHours`。

探针仅使用 Python 标准库，没有 Agent SDK、LLM、业务依赖或外部请求。宿主脚本复用本机已有 boto3 和 Docker。

| Runtime 镜像 | 会话负载 | 阶段（默认每段 30 秒） |
|---|---|---|
| small | 空载 | baseline / idle_control |
| small | 256 MiB 私有匿名内存，每页写入 | baseline / allocated / released |
| small | 256 MiB 临时文件，1 MiB 缓冲读写 | baseline / file_cached / after_fadvise / file_closed |
| padded | 镜像多 256 MiB 随机文件，但不读 | baseline / idle_control |
| padded | 读取该镜像文件 | baseline / file_cached / after_fadvise / file_closed |

每项使用独立 UUID 会话，串行执行。匿名内存用 `mmap.close()` 直接解除映射，避免 Python 分配器不归还内存造成混淆。文件使用 `fsync` 后按文件给出 `POSIX_FADV_DONTNEED` 建议，不执行全局 `drop_caches`，不改内核设置。该建议未必生效，特别是 tmpfs，必须根据挂载和缓存数据解释。

固定基础镜像 digest 和应用代码，仅改变镜像填充层。填充数据不可压缩，以实际增加镜像层传输/存储体积。一次实验只能发现现象，不能证明所有冷启动或所有镜像行为。

## 运行

```bash
python3 -m unittest -v
python3 lab.py deploy --region us-west-2
python3 lab.py run --phase-seconds 30
python3 lab.py collect --wait-seconds 3900
python3 analyze.py
```

`deploy` 会创建独立命名的 ECR 仓库、最小用途执行角色、两个 PUBLIC 网络 microVM Runtime，以及各自的日志投递资源；不会修改已有 Runtime。PUBLIC 网络不等于匿名调用，仍使用 IAM 签名认证。不要用于生产流量。`deploy` 复用 `.state/resources.json` 中资源，不自动更新已部署代码；改代码后应使用独立实验目录/状态，避免混用版本。

`run` 产生少量真实 AWS 费用，无大模型费用。每次调用返回后请求 `StopRuntimeSession`；停止失败也有 60 秒 idle timeout 和 600 秒 maxLifetime 兜底。最长负载为 4 × 45 秒，内存负载上限 512 MiB。CLI 默认 256 MiB。日志保留 7 天。会话停止后，ECR 镜像和日志仍有存储费用；不自动删除实验资源或证据。

部署、调用是长任务，应在后台执行。`collect` 只读云端，随时可以重跑；资源用量遥测最多延迟 60 分钟，新 metric 的 ListMetrics 发现也可能延迟。**空结果不是零费用。**

本地证据保存到被 git 忽略的 `.state/`：

- `resources.json`：资源 ARN、镜像 URI、会话 UUID、调用窗口、停止响应。
- `<session UUID>.json`：返回的全部逐秒应用样本、阶段窗口、环境。
- `telemetry.json`：CloudWatch 原始日志事件与 metric Sum 数据。
- `comparison.json`：`analyze.py` 生成的阶段中位数/范围、AWS 用量与覆盖率。

`collect --wait-seconds` 每分钟查询一次，最多 3900 秒，在所有阶段内部窗口达到 99% 遥测覆盖率后结束；这不保证 shutdown 尾部记录已齐。`analyze.py` 从每个阶段首尾各剔除 3 秒，只匹配同 session 的 AWS 记录。未到达的窗口显示 missing，绝不补零。

不要上传整个 `.state/`；资源标识和底层环境信息应保留在私有位置。Docker 登录使用独立 `.state/docker` 配置，push 后 logout，不改用户 Docker 登录。

## 指标口径与限制

- 当前官方定价：按秒计费，内存采用“截至该秒的峰值”，最低 128 MB；包含系统开销；从 microVM 启动、初始化、处理、空闲，到终止都在计费生命周期内。因此释放内存后，当前 RSS 下降不代表计费立即下降。
- 进程 `VmRSS` 是当前驻留量，`VmHWM`/`ru_maxrss` 是进程历史峰值，不能混用。RSS/PSS 也不是 Python 堆大小。
- cgroup 包含的进程/内核/文件缓存范围取决于实际暴露的控制组；`memory.peak` 可能不可读。探针保留路径和错误，不把缺失值写成零。
- `/proc/meminfo` 是 **guest 内核视角**，不是 AWS 物理 host。这里不能读取 AWS host 的 Docker 解压缓存或 hypervisor RSS。`MemTotal - MemAvailable` 只是 guest 压力估计，不是 AWS 计费公式。
- `MemoryUsed-GBHours` 是用量积分，不是 GB gauge。用 Sum，不能直接把一分钟 Sum 当成内存 GB，不能把 runtime/endpoint 两层维度相加。每分钟可能包含启动/结束的部分会话秒数。
- `USAGE_LOGS` 文档列出 `session.id`、`event_timestamp`、`elapsed_time_seconds` 和 `agent.runtime.memory.gb_hours.used`。本次实际结构为 `attributes.session.id`、epoch 毫秒 `event_timestamp`、`attributes.time_elapsed_seconds`（区间时长）和 `metrics.agent.runtime.memory.gb_hours.used`。分析器据此转换，并保留零间隔记录的非零 GB-hours；未来 schema 变化会报告错误而不是猜测。详见 RESULTS.md。
- 官方说明遥测可能因聚合时机、精度和对账与实际计费不同，最终以 AWS 账单为准。本实验不声称读取到内部 billing meter。
- 官方该页面没有明确 GB 的二进制/十进制定义。应用数据统一存 bytes，展示 MiB；AWS 数据保留 GB-hours，换算时写清假设，利用已知 256 MiB 增量交叉验证。
- 探针每秒采样、stdout 日志和保存 JSON 也占用少量内存；所有组使用相同逻辑，应比较受控增量，不把所有差额都归因于平台开销。阶段边界附近的样本应剔除再比较稳态。
- 当前探针在最后一条样本之后序列化整份响应，该瞬时开销没有被应用采样覆盖（本地复核约 0.6 MiB，随返回长度变化）。阶段稳态比较不包含响应尾部；会话完整用量差额包含该开销，不能全部归因于 AWS。
- `image_read` 固定读取镜像中完整的 256 MiB 文件；该模式的 `mib` 参数不控制读取量。CLI 固定传 256，其他大小的实验应重新构建对应镜像。

## 参考文档

- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Runtime 资源使用遥测](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)
- [HTTP 协议约定](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
- [配置可观测性](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [PutDeliverySource 日志类型](https://docs.aws.amazon.com/boto3/latest/reference/services/logs/client/put_delivery_source.html)

