# AgentCore Code Interpreter 中国区实测报告

2026-09-21 UTC，使用 `agentcore_cn` 在宁夏 `cn-northwest-1` 实测。
**9 项中，8 项在下述配置和验证范围内通过，1 项部分通过。2.6 原生单次执行超时尚未验证通过，
因此不能判定全部 P0 验收完成。**

`PUBLIC` 网络下可从中国境内 PyPI 镜像安装依赖；默认系统沙箱无法解析这些外部域名。
两种配置的结果均保留，未覆盖首轮失败记录。

## 环境与结论

| 项目 | 实测值 |
| --- | --- |
| AWS 账号 | `447150580482` |
| 调用身份 | `arn:aws-cn:iam::447150580482:user/agentcore-test-user` |
| 区域 | `cn-northwest-1` |
| 数据面 | `https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn` |
| 主测试资源 | `aws.codeinterpreter.v1` |
| 包源对照资源 | 临时自定义解释器，`networkMode=PUBLIC`，测试后已删除 |
| 客户端 | Python 3.12.3、boto3 1.43.87、botocore 1.43.87 |
| SDK 自动重试 | 关闭，`total_max_attempts=1` |
| 主测试时间 | 15:58:04–16:01:56 UTC |
| PUBLIC 依赖测试 | 16:01:54–16:02:08 UTC |
| 资源清理 | 共 67 个测试会话已停止；唯一临时自定义解释器已删除 |

| 编号 | 优先级 | 结果 | 证据与边界 |
| --- | --- | --- | --- |
| 2.1 | P0 | **PASS** | 基础算术、math、json、datetime、statistics、hashlib 返回预期结果。 |
| 2.2 | P0 | **PASS（探针范围）** | 沙箱无法读取客户端宿主随机标记文件；两个会话文件互不可见。未执行 AWS 底层宿主逃逸审计。 |
| 2.3 | P0 | **PASS（SDK 工具入口）** | Python stdout、stderr、受控异常完整传回调用端。未调用 LLM 或额外 Agent 框架。 |
| 2.4 | P1 | **PASS** | pandas、numpy、matplotlib 实际计算和 PNG 生成成功。 |
| 2.5 | P1 | **PASS** | 文本及二进制上传、沙箱修改、下载及 SHA-256 校验全部成功。 |
| 2.6 | P0 | **PARTIAL** | 原生执行 180 秒未超时；调用端设置期限后使用 `stopTask`，取消与后续执行成功。尚无原生自动执行超时证据。 |
| 2.7 | P0 | **PASS** | 1/1、10/10、50/50 个新会话创建及首次执行成功，无自动重试。 |
| 2.9 | P1 | **PASS** | 同会话三轮变量正确保留，另一会话不存在同名变量。 |
| 2.10 | P0 | **PASS（PUBLIC）** | 清华镜像无缓存下载并安装 `pytimeparse==1.1.8` 成功；默认系统沙箱网络探测和安装失败。 |

编号沿用需求清单，没有 2.8。完整计划见 [PLAN.md](PLAN.md)。
结构化验收汇总见 [final_summary.json](results/final_summary.json)。

## 2.1 Python 基础执行

实测 `(7 + 5) * 3 = 36`、`10! = 3628800`、`sqrt(81) = 9`、
`mean([2, 4, 6]) = 4`、`2026-09-21 + 9 天 = 2026-09-30`。
中文 JSON 编解码和 SHA-256 结果也与客户端预期一致。

证据：[基础执行事件](results/main-20260921T155804Z/api/2.1-basic.json)。

## 2.2 沙箱隔离

客户端先创建并读取一个随机临时目录下的标记文件，确认它真实存在，
再让沙箱尝试读取完全相同的绝对路径及 `/proc/1/root` 对应路径。
两条访问都返回 `FileNotFoundError`，没有读到宿主标记。
会话 A 创建并读取自己的随机文件成功；会话 B 中对应路径不存在。
客户端临时标记文件随后删除。

这里验证的是客户端宿主标记与独立会话之间的隔离。AWS 底层宿主没有提供可访问的测试标记，
因此不能据此声称所有宿主文件和所有逃逸路径都经过验证；
沙箱自身文件系统可读也不应直接判定为隔离失败。

证据：[宿主路径探针](results/main-20260921T155804Z/api/2.2-host-isolation.json)、
[会话 A 文件](results/main-20260921T155804Z/api/2.2-own-file.json)、
[会话 B 文件](results/main-20260921T155804Z/api/2.2-peer-file.json)。

## 2.3 stdout / stderr 透传

`executeCode` 返回的 `structuredContent.stdout` 为 `CN_STDOUT_中文_123`，
`structuredContent.stderr` 为 `CN_STDERR_错误_456`，两个通道没有混淆。
另一次执行在打印 `BEFORE_CONTROLLED_ERROR` 后抛出
`ValueError("CN_EXPECTED_EXCEPTION")`，调用端同时收到异常前的 stdout、
stderr 中的异常详情，以及 `isError=true`。

测试程序使用 Python 工具适配入口消费完整 AWS 事件流，并把 Agent 侧收到的对象保存为
[agent_received.json](results/main-20260921T155804Z/agent_received.json)。
此项覆盖 Python 代码执行的 API 到工具调用端，不包含 LLM 理解结果的能力评估，
也未扩展为 `executeCommand` 所有终端输出模式的验证。

## 2.4 数据分析库

| 库 | 版本 | 实际操作 |
| --- | --- | --- |
| numpy | 1.26.4 | 矩阵乘法结果 `[[7, 10], [15, 22]]` |
| pandas | 2.3.1 | DataFrame 分组求和结果 `{"a": 30, "b": 40}` |
| matplotlib | 3.9.0 | Agg 后端生成 7,945 字节 PNG，文件头校验成功 |

证据：[执行事件](results/main-20260921T155804Z/api/2.4-libraries.json)、
[实际生成并下载的图片](results/main-20260921T155804Z/downloads/analysis.png)。

## 2.5 文件上传 / 下载

使用 `writeFiles` 上传含中文的 CSV，以及包含全部 0–255 字节值、总长 4,096 字节的二进制文件。
沙箱中先校验上传内容的 SHA-256，再给 CSV 增加一行、反转二进制内容。
随后使用 `readFiles` 下载处理后的文件与分析图，客户端逐字节核验内容并保存哈希。

全部通过。此次覆盖小文件 API 往返，不包含 100 MB 大文件边界或 S3 大文件链路。

证据：[上传](results/main-20260921T155804Z/api/2.5-upload.json)、
[沙箱校验](results/main-20260921T155804Z/api/2.5-file-hashes.json)、
[下载](results/main-20260921T155804Z/api/2.5-download.json)、
[本地下载文件](results/main-20260921T155804Z/downloads/)。

## 2.6 执行超时：尚未满足全部验收条件

当前 SDK 的 `InvokeCodeInterpreter.arguments` 没有单次执行 timeout 字段。
`sessionTimeoutSeconds` 是会话参数，SDK `read_timeout` 是客户端等待参数，
两者都不能直接当作服务端单次任务终止的证据。

原生 `executeCode` 运行一个每秒写入计数、总长 180 秒的任务。
请求耗时 **180.467 秒**，服务返回执行时间 **180.092 秒**，`exitCode=0`，
最终计数为 `179`、完成标记存在。这证明此次 180 秒任务正常完成；
不能据此断言更长任务永远没有服务端上限。

另外，使用 `startCommandExecution` 启动计划运行 120 秒的 Python 任务，
调用端等待 5 秒后调用 `stopTask`：

- 取消前 `getTask` 返回 `working`。
- `stopTask` 返回成功，之后 `getTask` 返回 `canceled`。
- 取消后等待 2 秒检查，再等待 5 秒检查，计数均为 `5`，完成标记均不存在。
- 同会话后续执行 `6 * 7` 返回 `42`。

**主动取消路径通过，原生自动执行超时仍未证实，所以本项为 PARTIAL。**
若业务需要明确的任务期限，目前可采用已验证的“调用端计时 + 异步任务 + stopTask”路径。
完成原生超时验收仍需要内部测试版本给出支持的单次执行超时参数或服务端上限，
然后按该期限复测，确认返回后任务确实停止。

证据：[180 秒执行](results/main-20260921T155804Z/api/2.6-native-long-execution.json)、
[主动取消](results/main-20260921T155804Z/api/2.6-async-stop.json)、
[取消后的文件检查](results/main-20260921T155804Z/api/2.6-cancel-check-1.json)、
[任务最终状态](results/main-20260921T155804Z/api/2.6-async-after.json)。

## 2.7 1 / 10 / 50 并发新会话

每档使用线程屏障同时发起请求；每个请求创建独立 session，然后执行唯一标记代码并校验结果。
整批首次执行全部结束后才统一停止会话，50 档确实保留了 50 个独立会话。

“冷启动”在此指客户端可见的全新会话创建和首次代码执行，
不代表已识别服务内部 microVM 的预热或新建状态。
耗时包含客户端到中国区 API 的网络开销；端到端时间还包含少量本地记录开销。

**创建会话耗时，单位秒：**

| 并发 | 成功 | p50 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| 1 | 1/1 | 1.174 | 1.174 | 1.174 | 1.174 |
| 10 | 10/10 | 2.108 | 2.487 | 2.532 | 2.577 |
| 50 | 50/50 | 2.280 | 2.940 | 3.160 | 4.008 |

**从发起创建到首次执行完成，单位秒：**

| 并发 | 成功率 | p50 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| 1 | 100% | 1.588 | 1.588 | 1.588 | 1.588 |
| 10 | 100% | 2.493 | 2.921 | 2.926 | 2.931 |
| 50 | 100% | 2.685 | 3.361 | 3.732 | 4.395 |

三档共 61 个会话，创建和首次执行均无失败、无自动重试。
50 档线程起跑的最大偏移约 **1.97 毫秒**。
没有预设时延 SLA，每档仅一批，因此这些数字是本次样本的观测值，
尤其 1 样本档位的分位数不能视为长期性能指标。

证据：[汇总与全部明细](results/main-20260921T155804Z/concurrency.json)、
[逐请求记录](results/main-20260921T155804Z/concurrency/)。
所有 Start / Invoke 的 request ID 可用于服务端追踪。

## 2.9 多轮状态

同一 session 第一轮创建 `{"counter": 40, "history": ["first"]}`，
第二轮更新为 `{"counter": 42, "history": ["first", "second"]}`，
第三轮读取仍为更新后的值。另一个 session 的 `globals()` 中没有 `cn_state`。
三轮调用均设置 `clearContext=False`。

证据：[第一轮](results/main-20260921T155804Z/api/2.9-turn1.json)、
[第二轮](results/main-20260921T155804Z/api/2.9-turn2.json)、
[第三轮](results/main-20260921T155804Z/api/2.9-turn3.json)、
[独立会话](results/main-20260921T155804Z/api/2.9-peer-state.json)。

## 2.10 中国境内 PyPI 镜像

默认系统沙箱首次 pip 安装返回 `No matching distribution found`。
随后直接使用 HTTPS 探测清华、阿里云和 PyPI 包索引，
三个域名均出现 `Name or service not known`。
因此首轮错误不能解释为这个包不存在，也不能据此否定中国区组件的联网能力。
系统资源的 Get 响应没有返回 networkMode；此处网络限制来自实际探测，
不是推断出一个未返回的配置值。

创建明确设置 `networkMode=PUBLIC` 的临时自定义解释器后：

| 测试 | 结果 |
| --- | --- |
| 清华 `https://pypi.tuna.tsinghua.edu.cn/simple/pytimeparse/` | HTTP 200，约 0.376 秒 |
| 阿里云 `https://mirrors.aliyun.com/pypi/simple/pytimeparse/` | HTTP 200，约 0.424 秒；仅检查索引可达性 |
| PyPI `https://pypi.org/simple/pytimeparse/` | HTTP 200，约 1.268 秒 |
| 从清华镜像安装 `pytimeparse==1.1.8` | 成功，API 往返约 1.505 秒 |
| 不指定镜像、使用默认源安装相同版本 | 成功，API 往返约 4.340 秒 |
| 两次安装后分别导入并执行 `timeparse("1h 20m")` | 均返回 `4800` |

包在安装前未预装。两次安装均使用独立的 `--target` 目录、`--no-cache-dir`、
`--no-deps` 和固定版本，检查导入路径来自对应安装目录。
TLS 证书验证保持启用，没有使用 `--trusted-host` 或关闭校验。
本项验证 Python 依赖包源，不涉及用户自带 OCI 容器镜像仓库。

证据：[默认网络诊断](results/dependencies-default-20260921/network.json)、
[PUBLIC 资源配置](results/dependencies-public-20260921/custom-resource-ready.json)、
[PUBLIC 网络探测](results/dependencies-public-20260921/network.json)、
[安装日志与导入结果](results/dependencies-public-20260921/package_sources.json)。

## 复现方法

从本目录执行；命令会创建计费会话，测试程序在结束时停止本次会话。
凭证使用本地 profile，所有客户端显式指定中国区。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 默认系统解释器：全部主测试，1 / 10 / 50 并发。
.venv/bin/python verify_code_interpreter.py \
  --profile agentcore_cn --region cn-northwest-1 \
  --phase all --output results/my-main-run

# 默认沙箱的独立依赖网络诊断。
.venv/bin/python verify_public_dependencies.py \
  --network default --output results/my-default-network-run

# 新建 PUBLIC 解释器，测试依赖，停止会话并删除解释器。
.venv/bin/python verify_public_dependencies.py \
  --network PUBLIC --output results/my-public-network-run
```

主脚本也支持 `--phase functional`、`dependencies`、`timeout`、`concurrency`、
`cleanup`；`--concurrency 1 10 50` 和 `--observation-seconds 180` 为默认值。
每次复测请使用新结果目录；cleanup 指向要清理的原目录。
`--expected-account` 默认为本账号，身份不符时中止。

有 FAIL / PARTIAL / BLOCKED 或未确认停止的会话时，测试脚本返回非零退出码。
按当前实测行为，默认主测试会因 2.6 PARTIAL、2.10 FAIL 返回 1，
默认依赖诊断返回 1，PUBLIC 依赖测试返回 0。
最终验收汇总保留配置条件，不把主测试退出码改成成功。

```bash
# 中断后的会话清理：只匹配原结果目录的台账及 Start 请求名称。
.venv/bin/python verify_code_interpreter.py \
  --phase cleanup --output results/my-main-run

# 中断后的 PUBLIC 资源清理：只删除原目录记录的自定义资源。
.venv/bin/python verify_public_dependencies.py \
  --cleanup-resource --output results/my-public-network-run
```

`start_intents/` 在调用 Start 前记录请求名称，供成功响应尚未落盘时查找会话。
会话默认有效期 900 秒。资源创建响应记录在 `custom-resource.json`；
若进程恰好在创建请求成功但响应尚未保存时被强制终止，自定义资源需要根据创建名称在控制面核对。

## 清理与证据组织

独立复核确认 **66 个默认解释器会话均为 TERMINATED**；
另外 **1 个 PUBLIC 会话 Stop 成功**，其所属自定义解释器查询返回
`ResourceNotFoundException`，确认删除。
没有创建 IAM role、S3 bucket、VPC 等附属资源，没有修改 AWS profile。

最终证据：[final_cleanup.json](results/final_cleanup.json)。

| 路径 | 内容 |
| --- | --- |
| `verify_code_interpreter.py` | 主测试、事件流适配、断言、并发统计、会话台账与清理 |
| `verify_public_dependencies.py` | 默认 / PUBLIC 网络对照及临时资源生命周期 |
| `results/probe-20260921/` | 最初的 STS、创建、执行、停止探测 |
| `results/main-20260921T155804Z/` | 主测试全部结果，保留默认包源失败及超时 PARTIAL |
| `results/dependencies-default-20260921/` | 默认网络失败的单独诊断 |
| `results/dependencies-public-20260921/` | PUBLIC 包源成功、停止及资源删除证据 |
| `results/final_summary.json` | 汇总验收结果和配置条件 |
| `results/final_cleanup.json` | 独立的最终资源状态复核 |

原始 API 文件包含请求代码、会话 ID、request ID、HTTP 状态、事件内容和客户端耗时；
二进制字段以 base64 保存，并附长度与 SHA-256。没有保存 AWS 凭证。
主测试归档目录按真实开始时间命名，原始 environment 中保留了运行时的 output 参数。

官方接口背景参考：
[InvokeCodeInterpreter](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeCodeInterpreter.html)、
[API examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-api-reference-examples.html)、
[会话管理](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-resource-session-management.html)。
本报告中的中国区可用性和验收结论均基于实际账号调用结果。
