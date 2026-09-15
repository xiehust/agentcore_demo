# Sider 内存释放 A/B：Runtime V2 / us-east-1 复测

**结论：本次 Runtime V2 A/B 复测未复现“释放 4 GiB 后，平台内存值在 240 秒观察期仍钉在高位”的现象。** B 的进程 RSS 在 free 后恢复到约 50 MiB；平台内存遥测由高位末段 **4.906** 回落到末段 **0.919**，释放约 **17 秒**后已降至 **1.1 以下**并维持低位。对照 A 的同期末段约 **1.024**。

这说明本次 V2 条件下确实观察到释放后的平台用量回收；不证明所有场景均已修复，不证明真实账单已修复，也不能把差异单独归因于 V2。

释放滞后带来的多计是有界的：平台值在 free 后约 2 秒开始、以约 0.25 GB/s 匀速回收，约 16 秒回到低位；free 后 60 秒内相对基线多计约 **40 GB·s（0.011 GB-h）**，约等于持有期 4 GiB 用量的 **16.6%**，或多算约 8–10 秒满量持有。详见下文“释放延迟与多计量化”。

## 复测范围与方法

- 当前账号 `434444145045`，区域 `us-east-1`，新建临时 Runtime，显式指定 `platformVersion="V2"`，并通过 GetAgentRuntime 核验。
- 原 `main.py`、Dockerfile、requirements.txt 保持不变。950 MiB 随机镜像填充不由测试代码主动读取。
- A 全程空载；B 先采样 60 次，分配并逐页写入 4096 MiB，采样 60 次后释放，再采样 240 次。间隔 1 秒。
- 保留原 `hyst.py` 的时序；新增区域/ARN/endpoint 参数、内存余量检查、响应校验、超时和增量证据保存。原 memwatch 首次立即采样，所以 60/240 次的首末样本跨度约为 59/239 秒，不强称精确 t+60/t+120。
- PUBLIC/HTTP，DEFAULT endpoint，复用 AgentCoreColdstartRole，不修改 IAM 或配额。idle 60 秒、maxLifetime 600 秒；控制面 SDK 禁用自动重试。
- 测试前启用专用 USAGE_LOGS。采样后立即停止两会话、删除临时 Runtime，保留日志以等待异步投递。

## 镜像与依赖

- ECR：`434444145045.dkr.ecr.us-east-1.amazonaws.com/agentcore-coldstart-pingpong:sider-v2-20260914`。
- 部署固定 digest：`sha256:9141f6fc6c19a101a74547286dbf1fbff50efaeb2092cf1d12b2cceabb70ae90`。
- ECR 压缩大小 1,071,340,882 bytes；填充文件总计 996,147,200 bytes（950 MiB）。
- 本地镜像 ID：`sha256:24cb1285137734baeaf265774a726e1d2125dccddd614ad349a62fdf07842a33`。
- 容器内 main.py 与本地源码 SHA-256 均为 `bcc6ff204150f0b0efe28b6a920a84e2387974b4a828078f95b4dfd634aae9b8`；本地隔离容器的 4 MiB alloc/free 冒烟通过。
- 重新构建解析到 bedrock-agentcore 1.23.0、boto3/botocore 1.43.93、websockets 17.1；客户端复用已安装的 bedrock-agentcore 1.22.0、websockets 15.0.1，并通过 PYTHONPATH 优先加载私有 boto3/botocore 1.43.87。未安装新的本地依赖。

## 判读规则

首先确认 B 实际分配和释放：held_mb 为 4096/0，RSS 上升与下降均超过 3900 MiB；A 不执行 alloc，两个进程不重启。

平台每条记录按 `memory.gb_hours.used × 3600 / time_elapsed_seconds` 还原区间平均值；阶段均值按时长加权。只接受本次 runtime ARN 和两个精确 session ID，分页收集、检查重复时间戳及每阶段覆盖率。阶段两端排除 3 秒，避免 alloc/free 边界混淆；完整覆盖后再等待两次 60 秒轮询无新增记录。

比较 B 高位末段、释放后 10–60 秒、180–240 秒，并查看自身基线和同时运行的 A。若 guest/RSS 回落但平台仍接近高位，记录为本窗口仍有滞留；若平台明显回落并趋近基线，记录为本次观察到回收。数据不完整则不作二元结论。

## 结果

应用采样与平台日志均已完成。应用侧阶段均值：

| 阶段 | A RSS MiB | B RSS MiB | A guest used MiB | B guest used MiB |
|---|---:|---:|---:|---:|
| pre，60 样本 | 49.76 | 49.77 | 400.95 | 387.95 |
| held/noop，60 样本 | 49.76 | 4161.87 | 400.82 | 4496.64 |
| tail，240 样本 | 49.76 | 50.22 | 400.39 | 402.01 |

两个会话各 360 样本，共 720 样本；B alloc/free 回显 4096/0。Cached 全程约 188 MiB，没有加载约 1 GiB 镜像填充文件。guest MemTotal=8016.8 MiB，2 vCPU，cgroup memory.max=max、cpu.max=max 100000。

### 平台内存遥测

以下数值为平台 `GB-hours × 3600 / seconds`，保留其 GB 标记口径，不当作 guest GiB 或 RSS：

| 窗口 | A 对照 | B 分配后释放 | 每组平台记录数 |
|---|---:|---:|---:|
| pre 末段 | 1.231 | 1.096 | 27 |
| held/noop 末段 | 1.035 | **4.906** | 27 |
| tail 起点后 10–60 秒 | 1.022 | 1.145 | 50 |
| tail 起点后 180 秒至末样本前 3 秒 | 1.024 | **0.919** | 56 |

“末段”窗口为阶段末样本时间减 30 秒至减 3 秒，实际统计约 27 秒；tail 起点与 B free 返回时间相差约 0.069 秒。末尾窗口实际为约 +180 至 +236.4 秒，240 个 tail 样本首末跨度为 239.374 秒。

- B 从 held 末段到 late tail 降低 **3.987 个平台 GB 单位，约 81.3%**，低于其仍在自然衰减的 pre 均值。
- B late tail 范围 **0.912–0.938**，没有维持在 4.9 高位，也没有原报告的 8.000 饱和现象。
- 按服务端 free 响应内的 `ts` 对齐，平台约 +0.6s=4.907、+4.6s=3.752、+8.6s=2.751、+12.6s=1.749、+16.6s=1.062。第一次降至 1.1 以下后，在剩余平台记录内始终低于 1.1。
- 这里的约 17 秒是 guest 与平台时间戳对齐的描述，受约 1 秒区间粒度及跨时钟误差影响，不是精确回收 SLA。投递延迟另为 **1005–1115 秒（约 16.8–18.6 分钟）**，不能与回收延迟混淆。
- 全量 731 条：A 366、B 365，无重复 session 时间戳；729 条 1 秒记录，结束边界另有 0.29/0.66 秒记录。阶段内部统计共 679 条，六个阶段均完整覆盖，最大间隔约 1.001 秒。
- 06:20:12 UTC 完成收集，完整覆盖后连续两次 60 秒轮询记录集合不变。
- 独立复算没有引用 analysis.json 的统计结果，直接校验全部原始记录的账号、两个 region 字段、Runtime ARN、endpoint ARN、session ID、时间和持续时长。使用 **B 的完全相同墙钟窗口**选取 A 后，A 的 held/late-tail 均值为 **1.032368/1.023809**，仅下降 **0.008559**；B 同时下降 **3.986544**，两组下降差为 **3.977985**。因此 B 的大幅回落不能仅用 A 同期的小幅自然衰减解释；这仍不是跨平台的因果对照。

**仍未解决的口径问题**：释放后约 50 MiB RSS、约 402 MiB guest used 与约 0.919 的平台遥测仍不是同一数值；本次不能解释差额的具体组成。但“free 后平台不降”的特定现象，在这次测试中已不成立。

### 释放延迟与多计量化

以 B 的服务端 `free_result.ts`（1789365378.950）为 0 点，把 `analysis.json` 中 B 的每秒平台记录逐条对齐（2026-09-15 补充复算，脚本只读 `analysis.json`、`events.json`、`memory_samples.jsonl`，未改动原始数据）。

guest 侧释放是瞬时的：free 后 +0.07 秒的采样里 RSS 已从 4162 MiB 回到 50 MiB，cgroup `memory.current` 从 4157 回到 37 MiB，`vm_mem_used` 从 4497 回到 402 MiB。平台侧则呈匀速台阶下降：

| free 后 | 平台 GB 值 | 说明 |
|---:|---:|---|
| −0.4 s | 4.905 | 高位 |
| +0.6 / +1.6 s | 4.907 / 4.905 | 未动，起始延迟约 2 秒 |
| +2.6 s | 4.281 | 开始回收 |
| +4.6 s | 3.752 | |
| +8.6 s | 2.751 | 每 2 秒约降 0.5 GB |
| +12.6 s | 1.749 | |
| +16.6 s | 1.062 | 主体回收完成 |
| +17 s ~ +100 s | 1.062 | 小平台 |
| +110 s ~ +180 s | 1.046 → 0.92 | 第二阶缓慢回收约 0.14 GB |
| +180 s 以后 | 0.912–0.938 | 低位基线 |

- 回收速率约 **0.25 GB/s**（每 2 秒一格、每格约 0.5 GB），4 GB 用时约 16 秒。等间隔、等步长的台阶形态说明这是 host 侧按固定速率回收 guest 归还的内存，不是采样或投递延迟。
- 分配方向没有对称延迟：`alloc_result.ts` 后 **+0.08 秒**平台值即由 1.163 跳到 4.977，一步到位。即“涨是即时的，落是限速的”。
- 同期 A 每 30 秒抽样为 1.021–1.025，一条直线；B 的下降不是环境性衰减。

以 B 自身 late-tail 均值 0.919 为基线，按 `Σ (gb − 0.919) × seconds` 累计：

| 区间 | 高出基线的累计 | 说明 |
|---|---:|---|
| 持有期（alloc 后至 free，约 59.5 秒） | **240.2 GB·s** | 实际占用，应付部分 |
| free 后 60 秒 | **40.0 GB·s ≈ 0.0111 GB-h** | 释放滞后造成的多计 |

其中约 34 GB·s 来自 16 秒匀速回收斜坡，其余来自 1.062 的小平台。多计量约为持有期用量的 **16.6%**，等价于多算了约 8–10 秒的 4 GiB 满量持有。回收时间约 16 秒基本由释放量和固定速率决定，因此对“占大内存只撑几秒”的尖峰负载，多计比例会明显高于本例；对长时间持有的负载可忽略。

与 V1 原报告对比：原 B 组释放后 240 秒恒 8.000、斜率 0.000，同时 A 自然衰减到 5.58；本次 V2 中 B 由 4.906 降至 0.919，A 同窗口仅降 0.009。“释放后永不下降”的现象在本次 V2 观测中不成立，取而代之的是有界的、约 16 秒的限速回收。

口径提醒：上述秒数是 guest 时间戳与平台 `event_timestamp` 跨时钟对齐的结果，平台粒度 1 秒，误差约 ±1 秒；数值来自 USAGE_LOGS 的 `memory.gb_hours.used` 还原，不是 CloudWatch Metrics 聚合，也不是最终账单；只有一组 A/B，回收速率 0.25 GB/s 是否随释放量线性，需要再用 1 GB / 2 GB / 6 GB 释放量复测。

### 执行与清理

- 部署开始：2026-09-14 05:50:36 UTC；READY：05:54:14 UTC。
- 客户端执行：05:54:15–06:00:18 UTC。A 首末样本跨度 357.694 秒，B 为 359.082 秒。
- Runtime：`sider_v2_0a58091568-Rzs2Gr7Xnh`；GetAgentRuntime 确认 V2/READY，配置与创建请求一致。
- A：`hystA-168773459d4844bab1b7118e2bff0f9c-b0a161e6005b4f83bd8dfe6d1`。
- B：`hystB-643345bb15bb4293993f73cdcb6649bf-f0a66b3da331401795d533762`。
- 两个 StopRuntimeSession 均 HTTP 200；删除 Runtime 后于 06:00:25 UTC 确认 ResourceNotFoundException，并由独立查询再次确认。部署/负载/清理进程退出码 0。
- 收齐日志后，临时 delivery、source、destination 均已删除。ECR 镜像及日志证据保留，未删除现有数据；用量日志组 `/aws/vendedlogs/bedrock-agentcore/sider_v2_0a58091568` 保留 30 天，自动创建的应用日志组也保留。
- 原始证据：[`../results/sider_v2_us_east_1_2026-09-14/`](../results/sider_v2_us_east_1_2026-09-14/)，包括 state.json、events.json、memory_samples.jsonl、usage_raw.json 和 analysis.json。
- 本地部署源文件 5 项 SHA-256 与运行时保存值一致；15 项离线单元测试通过，覆盖 A/B 时序、分配确认、内存余量、清理传输失败、身份冲突、重复记录和不完整尾段。

## 离线复核与重跑

在项目根目录 `21-runtime-v2-beta` 执行（本机 Python 3.12 已有客户端依赖，私有 SDK 优先）：

```bash
PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages" \
  python3 -m unittest discover -s sider_case -p test_v2.py -v
PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages" \
  python3 sider_case/verify_v2.py verify \
  --out results/sider_v2_us_east_1_2026-09-14
```

再次执行真实负载会创建收费资源，须使用新的输出目录，并确认当前账号、区域及镜像仍可用：

```bash
PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages" AWS_MAX_ATTEMPTS=1 \
  python3 -u sider_case/run_v2.py run --out results/<new-run> \
  --image 434444145045.dkr.ecr.us-east-1.amazonaws.com/agentcore-coldstart-pingpong@sha256:9141f6fc6c19a101a74547286dbf1fbff50efaeb2092cf1d12b2cceabb70ae90
# run 自动停止会话、删除 Runtime；日志投递保留到收集完成。
PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages" \
  python3 -u sider_case/verify_v2.py collect --out results/<new-run>
PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages" \
  python3 sider_case/run_v2.py remove-delivery --out results/<new-run>
```

原 `collect_usage_logs.py` 要求 `sessions.json`，与 hyst.py 的输出不匹配，且会在首批非空日志到达时返回，因此本次使用独立的 `verify_v2.py` 收集并核验完整尾段，原采集器保持不变。

## 局限

- 原报告指向账号 `687912291502` 的 us-west-2、VPC、原镜像版本；本次账号、区域、网络和依赖解析版本均不同。不是严格单变量 V1/V2 因果对照。
- 一组 A/B 只能判断该次观测窗口，不证明所有工作负载均已修复，也不能断言“永不回收”。
- 平台 GB 标记遥测不等于 guest GiB、Firecracker VMM 开销或真实账单。此测试不能独立验证计费修复。
- 未重跑原报告的 CPU、12-session 横向矩阵及 cachepress 辅助实验；本次聚焦最直接检验释放后高位滞留的 A/B。
