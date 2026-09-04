# Lambda MicroVMs 冷启动分析笔记

[English version](MICROVM_NOTES.md)

本文是对 [COMPARE.zh.md](COMPARE.zh.md)（脚本生成、数字与数据严格一致）的手写解读，记录三件事：
Lambda MicroVMs 的默认配额、为什么它的冷启动与镜像大小无关、以及数据里出现的"快 / 慢"双峰模式。
数据来自 2026-09-04 在 us-west-2 的 300 次 `RunMicrovm`（3 个镜像尺寸 × 并发 1/5/10/50），客户端位于同区域 EC2。
凡属推断而非文档结论的地方都单独标注。

---

## 1. 默认配额

来源：[Lambda quotas → Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html#microvms-quotas)；
并用 `aws service-quotas list-aws-default-service-quotas --service-code lambda --region us-west-2` 核对，两者一致。

### 计算与存储

| 资源 | 默认值 | 可调 |
|---|---|---|
| 所有 MicroVM 的总内存（每账号每区域） | **400 GB**；us-east-1 / us-east-2 / us-west-2 / ap-northeast-1 为 **1,024 GB**（512 台 2 GB VM）。可纵向突发到 4 倍 | 是 |
| 单台 MicroVM 最长执行时间 | 8 小时（28,800 s） | 否 |

### 镜像与版本

| 资源 | 默认值 | 可调 |
|---|---|---|
| MicroVM 镜像数（每账号每区域） | 100 | 是 |
| 每个镜像的版本数 | 50 | 是 |
| 并发镜像构建数 | 5；上述四个大区为 10 | 是 |

### 单台 MicroVM 吞吐（不可调）

- 并发连接数：8 (1 vCPU) / 16 (2) / 32 (4) / 64 (8) / 128 (16 vCPU)
- 每秒请求数：40（4 vCPU / 8 GB）、160（16 vCPU / 32 GB）

### API 速率（每账号每区域，均可调）

| API | 速率 (TPS) | 突发 |
|---|---|---|
| `RunMicrovm` | 5 | 5 |
| `ResumeMicrovm` | 5 | 5 |
| `SuspendMicrovm` | 2 | 2 |
| `TerminateMicrovm` | 10 | 10 |
| `GetMicrovm` | 100 | 100 |
| `CreateMicrovmAuthToken` | 50 | 50 |
| `CreateMicrovmShellAuthToken` | 5 | 5 |

### 实测与文档的出入

- 并发 10 同时发起 `RunMicrovm`（60 次 × 3 尺寸 = 150 次）**从未被限流**。
- 并发 50 同时发起时，50 个里放行了 **37–45** 个，其余 `ThrottlingException`；被放行的请求 API 本身耗时从 ~120 ms 升到 p50 ~0.9 s（服务端排队）。
- 连续两批并发 50 只间隔 5 s 时，第二批 44/50、第三批 49/50 被限流——**5 TPS 的持续速率是真实的**，只是令牌桶初始容量明显大于文档写的"突发 5"。
- 文档另提示：新账号的 Lambda 函数与 MicroVM 并发/内存配额会更低，随用量自动放宽。

对基准测试的影响：并发 ≤10 的数据不受配额干扰；并发 50 的 `cold_ms` 含 API 排队时间，且每个尺寸要单独跑、中间留够令牌桶回填时间。

---

## 2. 为什么冷启动与镜像大小无关

### 2.1 两个平台的"冷启动"做的事不同

| | AgentCore Runtime | Lambda MicroVMs |
|---|---|---|
| 启动时做什么 | 从 ECR **拉取整个容器镜像**（下载压缩层 + 解压 + 写盘）→ 启动容器 → Python import SDK → uvicorn 监听 | 从构建时拍好的**内存 + 磁盘 Firecracker 快照**恢复 VM；进程已在监听 8080 |
| 镜像大小影响什么 | 必须拉取/解压的字节数，线性增长：**每 +500 MB ≈ +2–4 s** | 只影响快照在存储中的体积，不影响启动时必须读取的量 |
| 数据 | 真实启动 p50 **8.1 / 11.4 / 13.5 s**（500 MB / 1 GB / 2 GB） | **2.2 / 2.1 / 2.3 s** |

OCI 镜像模型决定了 AgentCore 必须把镜像整体搬到宿主机才能起容器（层是 tar.gz，不解压就没有文件系统）。
MicroVM 的"镜像"则是 Lambda 在构建阶段跑完 Dockerfile、启动应用、等 `/ready` 返回 200 之后拍下的内存页 + 块设备快照——
所有 import、pip 包、uvicorn 的监听 socket 都已经在内存里。`proc_start_ts` 早于 VM 启动 78 秒（等于镜像构建时刻）就是这一点的直接证据。

### 2.2 快照按需分页加载，没被访问的字节不产生启动代价

文档对 `/validate` 钩子的描述是：让 Lambda "采样应用运行时访问了快照的哪些部分，以便**预取**这些部分以降低启动延迟"
（[MicroVM images → build hooks](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)）。
能"预取一部分"，就意味着其余部分是等到 page fault 时再从存储拉——Firecracker 快照 + 按需加载的做法，Lambda SnapStart 也是如此。

我们的填充层 `/opt/pad_1.bin … pad_4.bin`（257 MB → 1,707 MB）在应用运行期间**一个字节都没有被读**，
只是块设备上的冷数据，VM 启动时不会碰，自然不产生 I/O。数据表现为：

- guest 侧 `/run` 钩子 → 首个请求到达：三个尺寸都是**几十 ms**（快模式 p50 66 ms）；
- 冷启动分解里随尺寸变化的只有噪声；慢模式占比 41% / 24% / 32%，没有单调趋势。

### 2.3 那 1.5 s / 3.5 s 花在哪里

分解数据显示时间都在**控制面与入口代理**：`RunMicrovm` API ~120 ms，`CreateMicrovmAuthToken` ~150 ms，
然后代理把首个 HTTPS 请求挂住 ~1.0 s 才送进 VM（快模式），或挂 ~3 s 后返回 502、重试才成功（慢模式）。
这些都是平台调度/路由行为，与镜像里有多少 GB 无关。详见第 3 节。

### 2.4 什么情况下镜像大小会重新变得重要

结论有前提，不要过度推广：

- **启动路径的工作集**才是变量。若应用首个请求就要读一个 1.5 GB 模型文件，这些页会在首请求时被 page-fault 进来，
  冷启动随工作集增长——度量对象是"运行时访问的字节数"而不是"镜像总大小"。在 `/validate` 里跑一遍真实负载让 Lambda 预取，
  是文档推荐的缓解手段（本仓库的 `/validate` 正是这么做的）。
- **内存快照大小**取决于基线内存里实际驻留的页。本进程只有几十 MB 常驻；若构建时把大数据 load 进内存，快照的内存部分变大，恢复时的预取量随之变大。
- **构建时间**与镜像大小有关（写 1.7 GB urandom 花的时间），但那是一次性的，不在冷启动路径上。

一句话：AgentCore 的冷启动是"把镜像搬过来再启动"，成本 ∝ 镜像字节数；Lambda MicroVMs 的冷启动是"把已经跑起来的进程从快照里按需唤醒"，
成本 ∝ 启动路径真正触碰的页数——多数 agent 的启动路径只触碰几十 MB，所以 500 MB 和 2 GB 的镜像一样快。

---

## 3. 快模式与慢模式

这是 300 次冷启动数据中的**双峰分布**，"快 / 慢"是本文为方便描述取的名字，不是平台术语。
两种模式的出现概率约 68% / 32%，与镜像大小和并发度都无关。

### 3.1 测量流程

```
t0  RunMicrovm 调用                      (~120 ms 返回 microvmId + endpoint, state=PENDING)
    CreateMicrovmAuthToken               (~150 ms)
    POST https://<endpoint>/invocations   ← 第 1 次尝试
    …若非 200，休眠 100 ms 再试…
    首个 200 完整读完                     → cold_ms = 此刻 − t0
```

客户端记录每次 HTTP 尝试的耗时（`attempt_ms`）、非 200 状态码（`non_ok_statuses`），以及 VM 内部两个时间戳：
`run_hook_ts`（Lambda 恢复快照后立刻调用 `/run` 钩子的时刻）和 `request_ts`（请求真正到达应用的时刻）。

### 3.2 快模式（68%，cold_ms ≈ 1.3–1.5 s）

```
attempt_ms      = [~1,050]           一次尝试就成功
non_ok_statuses = {}
guest: request_ts − run_hook_ts ≈ 10–100 ms
```

第 1 个请求发出后，**入口代理把连接挂住约 1 秒**，然后返回 200。VM 内部看到的却是：`/run` 触发后只过了几十 ms 请求就到了。
即 VM 在 API 返回后很快就就绪，那 1 秒基本是代理路径（建立到新 VM 的路由 / 等待 VM 网络就绪）花掉的，应用本身几乎没等。

### 3.3 慢模式（32%，cold_ms ≈ 3.4–3.6 s）

```
attempt_ms      = [~3,020, ~25]      第 1 次失败，第 2 次立刻成功
non_ok_statuses = {"502": 1}
guest: request_ts − run_hook_ts ≈ 1,800 ms
```

第 1 个请求被代理**挂住约 3 秒后返回 502 Bad Gateway**（文档对 502 的定义："应用无响应"）。客户端 100 ms 后重试，**25 ms 拿到 200**。
而 VM 内部显示 `/run` 在请求到达前 1.8 秒就已触发——应用早就在监听，只是代理那次没把请求送进来，等超时后才放弃。

### 3.4 对照

| | 快模式 | 慢模式 |
|---|---|---|
| 占比 | 68% | 32%（500 MB 41% / 1 GB 24% / 2 GB 32%，无尺寸趋势） |
| cold_ms p50 | ~1,540 ms | ~3,520 ms |
| HTTP 尝试 | 1 次，挂 ~1.07 s 后 200 | 第 1 次挂 ~3.02 s → 502；第 2 次 ~25 ms → 200 |
| VM 内 `/run` → 请求 | ~66 ms | ~1,760 ms（VM 空等） |
| 差值 | — | ≈ 2 s，等于代理多挂的时间 |

原始记录中并发 50 的单元格带有 `attempt_ms`（该字段在跑完并发 ≤10 之后才加入客户端），可直接查看两种模式的逐次尝试耗时。

### 3.5 解读（推断，非官方说明）

两种模式里 VM 就绪的时间差不多（都在 `RunMicrovm` 返回后约 1 秒内），差别在**代理侧首个连接能否成功接上刚起来的 VM**。
快模式是代理等到 VM 网络就绪后把请求转进去；慢模式像是代理在 VM 尚未可达时就发起了转发、卡在一个约 3 秒的超时上，
超时后返回 502，而此时 VM 早已就绪，所以重试秒过。这与文档"`GetMicrovm.state` 最终一致，请通过连接探测判断就绪"的说法一致——首个连接失败是设计上允许的。

同一现象也出现在挂起 → 自动恢复路径上：30 次 resume 的耗时分为 ~6.2 s 与 ~8.2 s 两簇，差值同样约 2 s。

### 3.6 对使用者的意义

- 客户端**必须对首个请求做 502 重试**，否则约 1/3 的启动会直接失败；重试的代价只有 ~25 ms。
- p50 看起来是 1.5 s，但 p90 稳定在 ~3.4 s，做延迟预算时应按慢模式算。
- 这 2 秒差异完全在平台代理层，应用侧无法优化；能做的是把首请求超时设得比 3 s 长一点并快速重试，
  或者在 `/run` 钩子里主动向外报告就绪（例如写一条消息 / 回调），绕过"用首个请求探测就绪"的方式。
- 若配额允许，也可以把 VM 提前 `RunMicrovm` 好放进自己的池子——这正是 AgentCore 预热池在替你做的事，
  两个平台的差别本质上是"谁来维护这个池子"。

---

## 复现

```bash
bash scripts/deploy_microvm.sh
uv run python microvm_coldstart_test.py --full --resume            # 并发 1,5,10
uv run python microvm_coldstart_test.py --full --concurrency 50 --sizes 500mb   # 每个尺寸单独跑
python3 scripts/gen_compare_report.py
```

逐次尝试数据在 `results/microvm/raw/*_c50.json` 的 `attempt_ms` 字段；VM 内时间戳在每条记录的 `run_hook_ts` / `request_ts`。
