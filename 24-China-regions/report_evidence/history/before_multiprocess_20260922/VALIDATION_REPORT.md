# 中国区 AgentCore 验证报告

**验证范围：Code Interpreter 与 Runtime 冷启动压测**  
**区域：宁夏 `cn-northwest-1`、北京 `cn-north-1`**  
**账号：`447150580482`；AWS profile：`agentcore_cn`**  
**Owner：River**  
**测试日期：2026-09-21 至 2026-09-22，时间均为 UTC**  
**资源状态补充复核：2026-09-22 07:06 UTC；2.6 超时补测于同日完成并补充收尾记录**

## 1. 验收结论

原始清单共 **13 项**：Code Interpreter 9 项、Runtime 4 项。
其中 **13 项均有本报告所注明范围或接入条件下的通过证据**。
2.6 经补测由部分通过更新为**条件通过**：采用原生会话 TTL，
或在沙箱内用 GNU timeout 为终端任务设置自动期限。
这不等于所有执行入口均有独立原生 timeout 参数，也不等于无条件完成上线验收。
EFS 是额外验证项，两区均通过，不计入原清单的 13 项。

主要结论：

- 两区 Runtime 冷请求 P50/P99、暖请求 P99 和 50 并发错误率均满足本轮样本的目标。
  北京暖请求 P99 为 **199.440 ms**，距 200 ms 阈值仅 **0.560 ms**，属于临界通过。
- 两区 Code Interpreter 的基础 Python、数据分析库、文件往返、多轮状态、
  所测隔离探针及 SDK 工具入口的输出透传均通过。
- 两区 Code Interpreter 的 1、10、50 并发批次均无失败，
  额外各 100 次串行新会话也全部成功。
- 中国境内 PyPI 镜像安装在 **PUBLIC 网络配置**下通过；
  默认系统沙箱安装失败的原始记录保留，不能将 2.10 写成无条件通过。
- 上次 180 秒任务未越过 900 秒会话 TTL，证据不足。本次改为 60 秒 TTL，
  两区同步/异步任务均自动结束，外部心跳约在创建后 63–64 秒停止。
  默认沙箱的命令级自动 TERM/KILL 也通过父子进程终止检查。
  原生 TTL 的终止和同步错误返回有延迟，不能描述为准点硬截止。
- 两区 EFS 均完成真实 NFS 挂载和三个会话间的读写持久化验证。
  删除测试解释器后仍有服务 ENI 占用临时安全组；有限重试已超时，列为待跟进项。

本报告针对当前账号已开通的中国区内部测试能力，不据此推断其他账号或公开发布状态。
Owner 表示验收及后续跟进负责人；实际执行方式为自动化 API 测试。

## 2. 已填写验收表

状态说明：

- **✅ 通过**：两区均满足本次测试判定。
- **✅ 通过（限定范围/条件）**：只对备注中明确的探针、调用入口或配置成立。
- **🟡 部分通过**：存在已验证能力，但尚未满足完整目标。

### 💻 Code Interpreter

| # | 验证项 | 优先级 | 状态 | Owner | 备注 |
| --- | --- | --- | --- | --- | --- |
| 2.1 | Python 代码执行（基础运算、标准库）正常返回结果 | 🔴 P0 | ✅ 通过 | River | 两区均通过基础运算及 math、json、datetime、statistics、hashlib 校验。 |
| 2.2 | 代码执行沙箱隔离验证（无法访问宿主系统文件） | 🔴 P0 | ✅ 通过（探针范围） | River | 无法读取客户端宿主随机标记文件，独立会话私有文件互不可见；未覆盖 AWS 底层宿主逃逸审计。 |
| 2.3 | 执行结果（stdout / stderr）正确透传至 Agent | 🔴 P0 | ✅ 通过（SDK 工具入口） | River | 两区 stdout、stderr、异常前输出及受控 ValueError 均正确返回；未覆盖特定 Agent 框架或 LLM 的端到端集成。 |
| 2.4 | 常用数据分析库可用（pandas, numpy, matplotlib 等） | 🟡 P1 | ✅ 通过 | River | 两区均完成矩阵运算、DataFrame 聚合及 PNG 生成；numpy 1.26.4、pandas 2.3.1、matplotlib 3.9.0。 |
| 2.5 | 文件上传 / 下载至 Code Interpreter 会话正常 | 🟡 P1 | ✅ 通过 | River | 文本及 4,096 字节二进制上传、沙箱修改、下载及 SHA-256 校验通过；未测大文件边界。 |
| 2.6 | 执行超时机制有效（超时后任务正确终止） | 🔴 P0 | ✅ 通过（指定超时方案） | River | 两区 60s 原生会话 TTL 自动终止同步/异步任务，S3 心跳约 63–64s 停止；默认沙箱 GNU timeout 5s／KILL 宽限 2s 后父子进程均停止，会话可继续使用。无独立 executeCode timeout 字段，非硬实时保证。 |
| 2.7 | 沙箱冷启动测试（1、10、50）并发 | 🔴 P0 | ✅ 通过 | River | 两区 1/10/50 并发均全部成功，SDK 无重试；50 并发端到端 P95：宁夏 1.310 s、北京 1.555 s。 |
| 2.9 | 多轮会话中代码执行状态（变量）正确保留 | 🟡 P1 | ✅ 通过 | River | 同一 session 三轮定义、修改、读取均正确，另一 session 不存在该变量。 |
| 2.10 | 中国区镜像 / 依赖包源（PyPI 镜像）可正常拉取 | 🔴 P0 | ✅ 通过（PUBLIC 配置） | River | 两区 PUBLIC 解释器从清华镜像无缓存安装 pytimeparse==1.1.8 并调用成功；默认系统沙箱安装失败。此项不包含 OCI 镜像仓库。 |

编号沿用原清单，不新增 2.8。

### Runtime 冷启动压测

| # | 验证项 | 优先级 | 目标值 | 状态 | Owner | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 7.1 | 冷启动基线：单实例首次请求延迟 P50 | 🔴 P0 | < 3s | ✅ 通过 | River | 每区 100 个串行新会话；宁夏 1.623 s，北京 1.737 s。 |
| 7.2 | 冷启动基线：单实例首次请求延迟 P99 | 🔴 P0 | < 5s | ✅ 通过 | River | 同一批 100 个冷样本；宁夏 2.008 s，北京 2.196 s。 |
| 7.3 | 暖实例对比：热实例 P99 延迟 | 🔴 P0 | < 200ms | ✅ 通过（北京临界） | River | 每区同会话 500 次暖请求；宁夏 154.920 ms，北京 199.440 ms。北京仅余 0.560 ms 裕量。 |
| 7.4 | 并发扩容：从 0 扩至 50 并发，无请求失败 | 🔴 P0 | 错误率 0% | ✅ 通过 | River | 从 0 个用户会话发起 50 并发，两区均 50/50 成功；50 个独立实例标记，应用处理峰值重叠均为 50。未验证底层物理实例从零启动。 |

可导入表格工具的文件：
[Code Interpreter CSV](report_tables/code_interpreter.csv)、
[Runtime CSV](report_tables/runtime_cold_start.csv)。

## 3. 环境与证据选取

### 3.1 正式性能测试环境

| 项目 | 宁夏 | 北京 |
| --- | --- | --- |
| 区域 | cn-northwest-1 | cn-north-1 |
| EC2 / AZ | i-0a685957c9b9355d7 / cn-northwest-1a | i-0ab621bda76e7f0af / cn-north-1a |
| 实例规格 | t3.small，2 vCPU / 2 GiB，x86_64 | t4g.small，2 vCPU / 2 GiB，ARM64 |
| Python | 3.12.14 | 3.12.14 |
| boto3 / botocore | 1.43.87 / 1.43.87 | 1.43.87 / 1.43.87 |
| 正式运行窗口 | 2026-09-22 00:29:58–00:35:53 | 2026-09-22 05:21:28–05:31:37 |
| 被测 Runtime | Python 标准库 HTTP echo，ARM64，无 LLM 或外部业务调用 | 同一应用与固定基础镜像内容 |
| 管理方式 | SSM、实例角色、IMDSv2、无 SSH 入站 | 同左 |

正式时延结果只采用两台**同区域 EC2**的数据。
区域和实例身份由 IMDSv2、STS 实例角色及 EC2 配置交叉核验。
工作站的部署、SSM 调度、结果下载和报告分析不在请求计时链路内。

### 3.2 功能测试与历史数据的使用

| 证据范围 | 宁夏 | 北京 |
| --- | --- | --- |
| CI 基础功能、超时、PUBLIC 包源 | 2026-09-21 完成；SDK 调用端为管理工作站，实际代码执行在宁夏 Code Interpreter | 2026-09-22 从北京 EC2 完整复测 |
| CI 冷启动与并发 | 2026-09-22 从宁夏 EC2 复测 | 2026-09-22 从北京 EC2 复测 |
| EFS | 2026-09-22 完成；管理工作站调用宁夏 VPC Code Interpreter | 2026-09-22 从北京 EC2 调用北京 VPC Code Interpreter |

不把宁夏最初从 us-west-2 工作站发起的 CI 时延，或早期以 Code Interpreter
作为 Runtime 压测客户端的结果，混入本报告的正式性能表。

Runtime 早期探针曾用启动时 UUID / guest boot ID 识别实例；这些字段会随预初始化状态复用，
导致样本完整性误判。正式结果采用**首次业务请求时生成的实例标记**，并检查请求序号。
原始误判记录保留，没有覆盖或改写。

北京的 Docker Hub 访问超时发生在准备阶段。固定基础镜像经私有 S3 传输后，
在北京 EC2 校验内容、离线构建、运行容器检查并推送北京 ECR。
Docker 版本对可选 inspect 字段的表示差异也单独处理并保留记录；这些准备时间不计入时延。

## 4. 测量方法与限制

1. **Runtime 冷请求**：并发 1，100 个全新 session 各调用一次。
   确认首次请求序号为 1、实例标记各不相同，每次调用后停止对应会话。
2. **Runtime 暖请求**：单独创建一个会话，首次准备调用不计入暖统计；
   随后连续调用 500 次，实例标记不变、序号从 2 连续至 501。
3. **Runtime 50 并发**：使用从未调用的独立 Runtime，屏障同时发出 50 个新 session 请求。
   每个处理函数停留 5 秒，以检查实际处理重叠；整批结束后统一停止会话。
4. **CI 并发**：1、10、50 三档各一批，每个工作线程先创建新会话，再首次执行代码；
   所有首次执行完成后才清理该批会话。
5. **CI 串行基线**：每区额外执行 100 个新会话，分别记录创建、首次执行及端到端耗时。
6. **计时**：客户端单调时钟计量，读取完整响应后结束。包含区内网络、SDK 和服务开销。
   CI 端到端还包含原脚本的少量本地记录开销；不将它当作纯服务端启动时间。
7. **分位数**：Runtime 和 CI 的 100 次串行基线使用 nearest-rank；
   排序后取第 `ceil(p × n)` 个样本。CI 1/10/50 并发沿用原脚本线性插值。
8. **重试与异常值**：测量 API 的 SDK 自动重试关闭，失败和异常值保留。
   文件传输、部署等待与准备阶段重试不属于测量请求。

“冷启动”表示新用户会话首次请求，“从 0”表示 0 个用户会话，
不表示已清空平台预热池或证明从 0 个物理实例启动。
CI 的端到端包含 StartSession + Invoke 两次 API，Runtime 则在首次 Invoke 内分配会话，
两者不是完全相同的调用链。

本次排除了性能客户端位于另一 AWS 区域的影响；仍存在 CPU 架构、服务状态、测试时间等差异。
100 个样本可以计算样本 P99，但不代表长期尾延迟保证。应用处理重叠的计算还依赖各 guest 时钟基本同步。

CPU 采样还记录了短时调度扰动：宁夏 Runtime 阶段 steal 峰值约 34.40%，
北京 CI 并发阶段约 11.40%。相关请求均保留，未通过删除异常值改善统计。
同区域部署不能消除这些客户端开销。

## 5. Code Interpreter 详细结果

### 5.1 代码、隔离、输出与状态

- 基础运算、阶乘、平方根、均值、日期计算、中文 JSON 编解码和 SHA-256 均得到预期值。
- 在客户端宿主创建随机标记文件并确认可读，再从沙箱读取同一绝对路径及对应
  `/proc/1/root` 路径，均未能读到该文件。会话 A 可读取自己的私有文件，B 中对应路径不存在。
  沙箱自己的系统文件可读不应直接认定为隔离失败。
- stdout、stderr 的中文标记分别返回到正确字段；受控 ValueError 的错误信息及异常前输出均保留。
- 同一会话三轮把计数从 40 更新至 42，历史列表正确保留；另一会话不存在同名变量。

这些证据覆盖客户端宿主标记、跨会话私有状态及 SDK 工具调用入口，
不扩大为 AWS 物理宿主安全审计或特定 Agent 框架的集成认证。

### 5.2 数据分析库与文件往返

两区版本相同：

| 库 | 版本 | 实测操作 |
| --- | --- | --- |
| numpy | 1.26.4 | 矩阵乘法结果 `[[7, 10], [15, 22]]` |
| pandas | 2.3.1 | 分组求和结果 `{"a": 30, "b": 40}` |
| matplotlib | 3.9.0 | Agg 后端生成 7,945 字节 PNG，文件头正确 |

通过 `writeFiles` 上传中文 CSV 和包含 0–255 全部字节值的 4,096 字节二进制文件。
沙箱校验输入哈希，修改文件，再通过 `readFiles` 下载。
客户端逐字节核验输出，并检查 SHA-256；生成图片也成功下载。
未验证 100 MB 上传上限、S3 大文件链路或超大数据集性能。

### 5.3 超时：补测后按指定方案通过

以下为第一轮的结果，作为历史证据保留：

| 检查 | 宁夏 | 北京 |
| --- | --- | --- |
| 原生 executeCode 观察时长 | 180 秒 | 180 秒 |
| 客户端观测耗时 | 180.467 秒 | 180.178 秒 |
| 原生任务结果 | 正常完成，计数 179，完成标记存在 | 同左 |
| 是否证明原生自动执行超时 | 否 | 否 |
| 5 秒调用端期限后 stopTask | 成功 | 成功 |
| 取消后两次检查的计数 | 均为 5 | 均为 4 |
| 取消后的完成标记 / 状态 | 标记不存在，任务 canceled | 同左 |
| 同会话后续执行 | `6 * 7` 返回 42 | 同左 |

两次取消后文件检查之间间隔 5 秒。计数停止且任务状态为 canceled，支持主动取消有效的结论。
当前 SDK 的 `InvokeCodeInterpreter.arguments` 没有单次执行 timeout 字段；
客户端 `read_timeout` 和会话 `sessionTimeoutSeconds` 不能替代执行终止证明。
也不能因为 180 秒任务未超时，就断言更长任务永远没有服务端上限。

第一轮据此标为部分通过。后续核对发现，当前 SDK 文档将 `sessionTimeoutSeconds`
明确规定为绝对会话 TTL，即使有正在执行的任务也会到期终止。
第一轮没有跨越 900 秒 TTL，因此不足以判断该机制是否有效。

**2026-09-22 补测结果：**

| 机制 | 宁夏 | 北京 | 结论 |
| --- | --- | --- | --- |
| PUBLIC 会话 TTL 60s，executeCode 计划运行 180s | 约 62.117s 观察到 TERMINATED；S3 末次心跳约 63.992s | 约 62.204s 观察到 TERMINATED；S3 末次心跳约 63.152s | 会话级自动终止通过 |
| PUBLIC 会话 TTL 60s，异步任务计划运行 180s | 约 62.459s 观察到 TERMINATED；S3 末次心跳约 62.985s | 约 62.640s 观察到 TERMINATED；S3 末次心跳约 63.152s | 会话级自动终止通过 |
| 默认沙箱 GNU timeout，普通父子进程，期限 5s | 5.191s 返回，退出码 124 | 5.306s 返回，退出码 124 | 父子进程停止，完成标记未出现 |
| 默认沙箱 GNU timeout，父子进程忽略 TERM，5s 后 TERM、再 2s 后 KILL | 7.132s 返回，退出码 137 | 7.186s 返回，退出码 137 | 父子进程停止，会话后续执行正常 |

原生 TTL 观察持续到约 210 秒，未见心跳恢复或正常完成标记；
同期独立的 S3 授权探针仍能成功写入。观察结束前没有主动 stopTask / StopSession。
同步 Invoke 返回 HTTP 409 / ConflictException，并明确指出配置的 session timeout。

原生 TTL 不是硬实时保证：收尾晚于 TTL，S3 最后一次更新也晚于首次终态观测。
同步 Invoke 错误返回耗时分别为 98.828s、71.881s，晚于心跳停止。
会话 TTL 从创建计时并终止整个会话；GNU timeout 属于终端任务包装，
子进程不与 notebook 直接共享全局变量。
如果要求同一多轮会话内每次 executeCode 都有独立原生 deadline，本次没有证明该模式。

命令分支首轮正常对照没有等待子进程，曾记为 ERROR；
修正对照后只重测命令分支，失败记录和原生 TTL 记录均未覆盖。
详见 [2.6 补测报告](code_interpreter/timeout_retest/REPORT.md)。

### 5.4 并发冷启动

单位：秒。端到端表示从发起创建到首次执行完成。

| 区域 | 并发 | 成功 | 创建 P50 | 端到端 P50 | 端到端 P95 | 端到端 max |
| --- | --- | --- | --- | --- | --- | --- |
| 宁夏 | 1 | 1/1 | 0.702 | 0.822 | 0.822 | 0.822 |
| 宁夏 | 10 | 10/10 | 0.849 | 0.995 | 1.039 | 1.047 |
| 宁夏 | 50 | 50/50 | 0.832 | 1.123 | 1.310 | 1.801 |
| 北京 | 1 | 1/1 | 0.792 | 0.914 | 0.914 | 0.914 |
| 北京 | 10 | 10/10 | 0.936 | 1.097 | 1.334 | 1.392 |
| 北京 | 50 | 50/50 | 0.836 | 1.099 | 1.555 | 2.078 |

每区三档合计 61 个会话。50 档全部首次执行结束前保持 50 个会话，
没有通过提前停止已完成会话降低实际会话并发数。
每档仅一批，尤其 1 并发的分位数仅是单个观测值。

### 5.5 100 次串行新会话基线

单位：毫秒；每区均成功 100/100。

| 区域 | 创建 P50 | 创建 P99 | 首次执行 P50 | 端到端 P50 | 端到端 P99 | 端到端 max |
| --- | --- | --- | --- | --- | --- | --- |
| 宁夏 | 776.137 | 1326.779 | 135.706 | 917.005 | 1443.306 | 1485.827 |
| 北京 | 815.630 | 1350.290 | 152.923 | 968.555 | 1493.453 | 1533.308 |

### 5.6 中国境内依赖包源

两区 PUBLIC 解释器均从 `https://pypi.tuna.tsinghua.edu.cn/simple`
安装 `pytimeparse==1.1.8` 成功。包在测试前未预装，使用 `--no-cache-dir`、
独立 `--target` 目录和固定版本，导入路径与目标目录一致。
实际执行 `timeparse("1h 20m")` 返回 `4800`。
PUBLIC 配置下使用默认源安装相同版本也成功。

默认系统沙箱两区均返回安装失败；宁夏额外 HTTPS 探测记录了外部域名解析失败。
北京默认沙箱没有单独复测 DNS 根因，因此这里只记录安装失败，
不把未经验证的根因当成两区共同结论。

2.10 的通过条件是本次验证过的 PUBLIC 配置和所选 Python 包。
不包含 OCI 镜像仓库、任意 PyPI 包或所有第三方域名的可用性保证。

### 5.7 补充验证：EFS

两区分别创建独立 EFS 文件系统、access point、挂载目标和 VPC Code Interpreter。
`/mnt/efs` 显示为 NFSv4.1；会话 A 写入后停止，B 读取并追加后停止，
C 读取最终内容且 SHA-256 一致。两区均通过。

EFS 需要 VPC 连通性、相应 AZ 的挂载目标、TCP 2049、
执行角色的 `ClientMount` / `ClientWrite` 权限及可写的 POSIX access point 配置。
测试使用 UID/GID 1000。

EFS 和 PUBLIC 包源使用不同解释器配置分别验证；
未验证同一个 EFS/VPC 会话的外网包源访问、EFS 并发写一致性或长期性能。

## 6. Runtime 详细结果

正式批次每区 151 个会话、651 次请求：100 次冷请求、1 次暖准备、500 次暖请求、
50 次并发请求。全部调用成功，SDK 无自动重试。

单位：毫秒。

| 区域 | 阶段 | 成功 | P50 | P95 | P99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 宁夏 | 冷请求 | 100/100 | 1623.420 | 1945.193 | 2008.168 | 2086.260 |
| 北京 | 冷请求 | 100/100 | 1737.471 | 2082.109 | 2196.439 | 2202.745 |
| 宁夏 | 暖请求 | 500/500 | 95.385 | 125.552 | 154.920 | 303.922 |
| 北京 | 暖请求 | 500/500 | 119.264 | 160.045 | 199.440 | 364.094 |
| 宁夏 | 50 并发，含 5 秒处理 | 50/50 | 7149.135 | 7343.259 | 7606.326 | 7606.326 |
| 北京 | 50 并发，含 5 秒处理 | 50/50 | 7149.082 | 7344.014 | 7395.432 | 7395.432 |

北京有 5/500 次暖请求达到或超过 200 ms；其 nearest-rank P99 仍为 199.440 ms，
符合“P99 < 200 ms”的本轮目标，但不等于所有请求均低于 200 ms。

两区扩容测试都从未调用过的独立 Runtime 开始，返回 50 个不同的首次调用标记；
客户端请求区间和应用处理区间的峰值重叠均为 50。
含 5 秒工作负载的扩容时延不参与空载冷/暖 SLA 比较。

## 7. 限制与待跟进事项

| 编号 | 事项 | 当前判断 / 后续动作 | Owner |
| --- | --- | --- | --- |
| F1 | 超时方案接入边界 | 2.6 已按会话 TTL / GNU timeout 条件通过。落地时明确选用方案；独立 executeCode 原生 deadline 及硬实时返回不在已验证范围内。 | River |
| F2 | 北京暖请求裕量 | 当前样本临界通过。建议在不同时间窗口扩大采样或重复测试；本报告不修改既有批次。 | River |
| F3 | EFS 网络资源释放 | 两区仍有服务 ENI 占用临时安全组，有限重试已超时。需确认服务释放机制或协助回收，不能声称清理全部完成。 | River |

隔离探针、SDK 工具入口、样本量、客户端架构及默认/PUBLIC/VPC 配置边界见前文。
这些限制不能通过将全部状态统一改为“通过”消除。

## 8. 资源收尾状态

2026-09-22 07:06 UTC 做了只读补核：

| 区域 | 资源 | 当前状态 |
| --- | --- | --- |
| 宁夏 | EC2 i-0a685957c9b9355d7 | stopped，按用户要求保留 |
| 北京 | EC2 i-0ab621bda76e7f0af | stopped，按用户要求保留 |
| 两区 | EFS 测试解释器及文件系统 | 本次查询均不存在 |
| 宁夏 | 临时安全组 sg-049391a8216deb6b8 | 仍存在；被 eni-0951f5e08b0da0589 占用 |
| 北京 | 临时安全组 sg-02b4c907a70fbe1be | 仍存在；被 eni-026863d42344eab61 占用 |
| 北京 | EFS 执行角色 cn_ci_efs_53c6d40f78 | 为后续释放保留 |

两个 ENI 均仍为 in-use / attached，attachment owner 为 `amazon-aws`。
没有强制解绑服务接口。宁夏 EFS 临时角色已删除。
原有限重试分别于 02:49 UTC、06:09 UTC 以 `pending_after_timeout` 结束，
因此本报告将其记为**清理未完成**，不写成“后台仍在自动重试”。

按已保存的清理记录，临时 Runtime、ECR、PUBLIC 解释器和传输 bucket 已删除。
EC2 的 EBS、SSM 角色、instance profile 和安全组按保留要求留下。
07:06 的只读补核仅更新报告证据，没有重启实例、重复压测或修改历史测试结果。

此后为 2.6 补测重新启动了两区保留的 EC2，补测完成后又停止并保留。
本轮新增的 PUBLIC 解释器、S3 bucket 和临时内联授权已清理，
未操作上述既有 EFS 遗留。最新补测收尾证据见
[宁夏](code_interpreter/timeout_retest/results/20260922/cn-northwest-1/cleanup.json)、
[北京](code_interpreter/timeout_retest/results/20260922/cn-north-1/cleanup.json)。

## 9. 证据索引

| 内容 | 证据 |
| --- | --- |
| 宁夏同区 Runtime 原始请求与汇总 | [请求](runtime/results/20260922-ec2/benchmark_results.json)、[汇总](runtime/results/20260922-ec2/benchmark_summary.json) |
| 北京同区 Runtime 原始请求与汇总 | [请求](cn-north-1/results/20260922/runtime/benchmark_results.json)、[汇总](cn-north-1/results/20260922/runtime/benchmark_summary.json) |
| 宁夏 CI 功能与超时 | [功能结果](code_interpreter/results/main-20260921T155804Z/summary.json)、[API 原始事件](code_interpreter/results/main-20260921T155804Z/api/) |
| 北京 CI 功能与超时 | [功能结果](cn-north-1/results/20260922/code_interpreter/summary.json)、[API 原始事件](cn-north-1/results/20260922/code_interpreter/api/) |
| 宁夏 CI 并发及串行基线 | [并发](code_interpreter/results/20260922-ec2/concurrency.json)、[串行](code_interpreter/results/20260922-ec2/serial_summary.json) |
| 北京 CI 并发及串行基线 | [并发](cn-north-1/results/20260922/code_interpreter/concurrency.json)、[串行](cn-north-1/results/20260922/code_interpreter/serial_summary.json) |
| 宁夏 PUBLIC 包源 | [安装与导入](code_interpreter/results/dependencies-public-20260921/summary.json)、[默认网络诊断](code_interpreter/results/dependencies-default-20260921/network.json) |
| 北京 PUBLIC 包源 | [安装与导入](cn-north-1/results/20260922/code_interpreter_public/summary.json) |
| 两区 EFS 挂载与持久化 | [宁夏](code_interpreter/efs/results/20260922/result.json)、[北京](cn-north-1/results/20260922/efs/result.json) |
| 2.6 新增原生 TTL 与自动命令超时证据 | [补测报告](code_interpreter/timeout_retest/REPORT.md)、[独立验证汇总](code_interpreter/timeout_retest/results/20260922/verified_summary.json) |
| EC2 身份与环境 | [宁夏](ec2_benchmark/results/20260922/collected/results/ec2_environment.json)、[北京](cn-north-1/results/20260922/ec2/collected/results/ec2_environment.json) |
| CPU 采样与既有完整性检查 | [宁夏采样](ec2_benchmark/results/20260922/collected/results/cpu_samples.jsonl)、[宁夏检查](ec2_benchmark/results/20260922/evidence_audit.json)、[北京采样](cn-north-1/results/20260922/ec2/collected/results/cpu_samples.jsonl)、[北京检查](cn-north-1/results/20260922/evidence_audit.json) |
| 本次资源状态只读补核 | [cleanup_snapshot.json](report_evidence/cleanup_snapshot.json) |
| 本报告数字与表格一致性核对 | [consistency_check.json](report_evidence/consistency_check.json) |
| 已结束的网络清理重试 | [宁夏](code_interpreter/efs/results/20260922/network_cleanup_retry.json)、[北京](cn-north-1/results/20260922/efs/cleanup-retry.json) |

### 代码与复现入口

- [Runtime 探针](runtime/app.py)、[Runtime 压测](runtime/benchmark.py)。
- [Code Interpreter 测试](code_interpreter/verify_code_interpreter.py)。
- [EFS 验证](code_interpreter/efs/verify_efs.py)。
- [同区 EC2 测试入口](ec2_benchmark/run_benchmarks.py)。
- [宁夏运行说明](ec2_benchmark/README.md)、[北京运行说明](cn-north-1/README.md)。

复测需使用新结果目录及新建的被测资源；不要覆盖本报告引用的原始记录。
