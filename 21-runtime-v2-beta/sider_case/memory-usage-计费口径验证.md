# AgentCore Runtime 内存/vCPU 计费口径实测（2026-09-12 ~ 09-13）

用受控负载压测 + USAGE_LOGS 对账，回答"内存按什么计费"。

> ⚠️ 2026-09-13 修订：早前版本的结论"内存恒按 8GB 规格计费、与用量无关"**是错的**。
> 并发 A/B 对照证明该指标会跟着用量涨、也会在无负载时衰减，但**释放后永不下降**——
> 是只涨不落的高水位。已按此重写全部内存相关结论。相关工单见
> [TT-agentcore-memory-billing.md](TT-agentcore-memory-billing.md)。

- Runtime: `session_snapshot_test-C5bkUXCPHn`（us-west-2，VPC 模式，**platformVersion 字段不存在 = v1 口径**）
- Endpoint: `test_endpoint` → v12（镜像 `v13-wsv2-memstat-20260912`，1GB ballast）
- lifecycle: `idleRuntimeSessionTimeout=60` / `maxLifetime=600`（原为 900/28800，为让计费窗口有界而改）
- 12 个 session，4 种负载形态，每 session 保持 60s、逐秒采样；另加 1 组 360 秒并发 A/B 对照
- 工具：[test_ws_coldstart_v2.py](test_ws_coldstart_v2.py) + [collect_usage_logs.py](collect_usage_logs.py) + [hyst.py](hyst.py) + [test_dropcache.py](test_dropcache.py)

## 结论速览

| 问题 | 结论 |
|---|---|
| 内存按实际用量计费吗 | **半是半不是。**平台值跟着"容器摸过的内存"往上涨，但**释放后不退**——是只涨不落的高水位 |
| 平台值的上限 | **8 GiB 顶格饱和**。不是恒定常量：纯 CPU 负载组只到 4.67/4.99/5.70，从没到 8 |
| 释放内存能降下来吗 | **不能。**A/B 并发对照：B 释放 4GB 后 240 秒斜率 **0.000**，恒 8.000；同时段对照组 A 以 1 GB/min 自然衰减到 5.58 |
| vCPU 按实际用量计费吗 | **是。**已知 CPU 负载让 vCPU-hours 涨了 8.9 倍 |
| 空闲时间计费吗 | **计费。**60s 工作 → 130~171s 计费秒，多出来的就是 idle timeout |
| 峰值还是平均 | 都不是。也不是滚动窗口峰值（107s 和 240s 尾段都零衰减，窗口早该滑过去了）。行为像 host 侧 footprint 带滞后 |
| 采样粒度 | 确认 1 秒；每条是**增量**不是累计 |
| 能手动清 page cache 省钱吗 | **不能，也没用。**`/proc/sys` 只读；逼内核回收 2.3GB 后平台值仍是 8.000 |
| 预热池的启动时间算钱吗 | **算。**启动那 19~40 秒记在之后才连上的 session 头上（已在 6+ session 复现）；池中等待不算 |

## 关键前提：容器没有 cgroup 限制

```
vm_mem_total_mb   = 8017.3      <- microVM 整机 8GB
vm_cpu_count      = 2           <- 2 vCPU
cgroup_mem_max    = "max"       <- 容器无内存上限
cgroup_vcpu_limit = null        <- 容器无 CPU 配额
```

容器能用满整个 microVM，所以容器摸到多少内存，microVM 层面就真的被占掉多少。这是后面所有现象的前提。

## 测试时序（各组实验怎么打内存、怎么和平台对齐）

guest 侧全部用 `memwatch interval_s=1.0` 逐秒读 `/proc/meminfo`。**每组只做一次 alloc 一次 free**，
没有反复循环；区别只在高位撑多久、释放后观察多久：

| 实验 | 脚本 | 时间轴 |
|---|---|---|
| `spike2g` | [test_ws_coldstart_v2.py](test_ws_coldstart_v2.py) | 连上 → 立刻 alloc 2048MB → 高位撑 **20s** → `free()` → 低位再撑 **40s** |
| `flat4g` | 同上 | 连上 → alloc 4096MB → 撑 **60s** 不释放 |
| `cachepress` | 临时脚本，结果在 `results/cachepress_20260912_212045/` | 每 **5s** 加一级 2048MB（2048→4096→6144）→ 到顶立刻 `free()` → 释放后采 **45s** |
| A/B 对照 | [hyst.py](hyst.py) | 前 **60s** 空转 → **t+61s** alloc 4096MB → 撑 **60s** → **t+121s** 释放 → 尾段 **240s**。A 组同时间轴但从不 alloc |

内存申请用 `bytearray` 并**逐页写入**（每 4096 字节写一下），确保物理页真实落地而不只是保留地址空间。

平台侧对齐（`collect_usage_logs.py` 的 `analyze_session`）：

1. 平台每 session **每秒一条**，`time_elapsed_seconds` 恒 1.0 → 瞬时值 = `gb_hours × 3600`；
2. 把 `event_timestamp`（epoch **毫秒**）换成秒，去 guest 样本里找最近的一条，**差超过 3 秒就丢弃**；
3. 按 guest 样本自带的 `_phase`（peak / released / tail…）分组求均值——**用 guest 的 alloc/free 时刻把平台序列切段**再比，不是硬性逐秒一对一。

之所以要分段：平台记录比 guest 采样窗口长得多，前面多一段预热池启动（19~40 条），
后面多一段 idle timeout 尾巴（约 60 条）。另外平台数据要等 5~10 分钟才查得到，都是压完回头拉。

## 决定性证据：并发 A/B 对照

前面的单臂实验有个洗不掉的疑问——"平台值不降，是不是因为它本来就不会降？"
所以做了并发对照：两个 session **同时**建立在同一 runtime / endpoint / 镜像上，
唯一区别是 B 在 t+61s 占 4096MB、t+121s 全部释放，A 全程不碰内存，之后两者都跑 240 秒尾段。

- 数据：`results/hysteresis_20260913_080222`，`2026-09-13T08:02:22Z` 起各 360 秒
- A `hystA-f0c75f59e2ed47299561f969685df4c2-b83e891a29874f5bb8e54258d`
- B `hystB-cde41740be9b40ffb44f46e073e98133-b8ae46363f8c4883b7ea37b9f`

| | **A（对照，不碰内存）** | **B（占 4GB 后释放）** |
|---|---|---|
| 平台值起点 | 8.006 | 7.862 |
| 0~120s 斜率 | **−1.077 GB/min** | +0.105 GB/min（涨到 8.00） |
| 尾段 240s 稳定值 | **5.581** | **8.000** |
| 尾段 240s 斜率 | −0.079 GB/min | **−0.000 GB/min** |
| 尾段 guest 真实用量 | 0.48 GB | 0.53 GB |
| 尾段 guest 用量+缓存 | 4.00 GB | **3.76 GB（比 A 还低）** |

三个结论：

1. **这个指标有能力下降**——对照组 A 以约 1 GB/min 平滑衰减，收敛到 5.58；
2. **guest 一旦摸过内存就被永久钉住**——B 释放后 240 秒零衰减。不是"来不及降"，是不降；
3. B 的 guest 内存占用**比 A 更低**，每秒计费值却比 A **高 43%**（8.000 vs 5.581）。

这条对照把"平台值恒等于 8GB 规格"和"平台值跟随实时用量"两个假设一起否掉了：
它是**只涨不落的高水位**。

## 尖峰-释放实验（同一规律的另一面）

让容器占 2GB 撑 20 秒再释放，观察平台值是否跟着降（`spike2g` cell）：

| session | phase | 容器自述 GB | 平台反推 GB |
|---|---|---|---|
| 4156c9e2 | peak | 2.511 | 7.909 |
| 4156c9e2 | released | 0.507 | **8.000** |
| faef1d9e | peak | 2.513 | 7.073 |
| faef1d9e | released | 0.485 | **7.147** |
| da858fca | peak | 2.479 | 7.900 |
| da858fca | released | 0.473 | **8.000** |

容器用量降了 5 倍，平台值**反而略微上升**——与 A/B 对照一致：释放不退。

## 补充实验：清掉 page cache 也没用

上面的尖峰实验留了个口子——释放后"用量+缓存"还有 4 GB，还能勉强猜"平台在数缓存"。
所以又做了一发把缓存也清掉。

先确认**手动清不了**：`/proc/sys` 在容器里是只读挂载，三种写法全失败。

```
proc /proc/sys proc ro,relatime 0 0
--w------- 1 root root 0 /proc/sys/vm/drop_caches
echo 3 > /proc/sys/vm/drop_caches   -> Read-only file system  rc=2
sysctl -w vm.drop_caches=3          -> not found              rc=127
echo 3 | tee ...                    -> Read-only file system  rc=1
```

容器里是 **uid=0(root)、CapEff=00000000a82425fb**，权限不是问题，是挂载点被锁了。

改用**内存压力逼内核自己回收**（分配到接近上限），成功（单位 MB）：

| 阶段 | 真实用量 | Cached | 用量+缓存 | **平台 GB** |
|---|---|---|---|---|
| 加压到 2048 | 2543 | 3716 | 6138 | **8.002** |
| 加压到 4096 | 4589 | 3433 | 7898 | **8.000** |
| 加压到 6144 | 6621 | **1419** ↓ | 7893 | **7.999** |
| 释放后 | **465** | 1421 | **1737** | **8.000** |

真实用量摆动 **14 倍**（465→6621），缓存被回收 2.3 GB，"用量+缓存"变化 4.5 倍
（1737→7898）——**平台值 7.999~8.002，波动 0.04%。**

至此"平台在数 page cache"也排除。**释放 anonymous 内存不退、连缓存被回收也不退。**

注：这台容器已运行 1600 秒、早就饱和在 8.000，我们是在饱和状态上做释放，
所以看不到任何变化——和 A/B 对照的 B 组是同一件事。

## 8.000 是饱和上限，不是下发规格常量

```
下发规格         8 GiB    = 8388608 kB
guest MemTotal            = 8209720 kB   ← 少约 175 MB, 被 firmware/内核保留
平台饱和值       8.000 GB
```

平台的饱和值 **比容器自己能看到的整机内存还大**（8.000 GB > 7.829 GiB），差的约 175 MB
正是 8 GiB 规格里被 firmware/guest 内核吃掉的部分——**倾向于说明这个数采自 host 侧，不是 guest 侧**。

但它**不是**恒定常量：同一 runtime、同一镜像，纯 CPU 负载组只稳定在 **4.67 / 4.99 / 5.70**，
序列峰值也只有 6.3~6.5，从没摸到 8。摸过的内存越多，平台值越高，到 8 GiB 顶格：

| 负载 | 容器摸过的内存峰值（用量+缓存） | 平台稳定值（3 session） | 序列峰 |
|---|---|---|---|
| CPU 负载，无内存负载 | 4.00 GB | **4.67 / 4.99 / 5.70** | 6.3~6.5 |
| 空载 | 4.01 GB | **5.38 / 6.64 / 7.03** | 8.01 |
| 占 2 GB 后释放 | 6.03 GB | **6.66 / 8.00 / 8.00** | 7.2~8.0 |
| 持续占 4 GB | 7.73 GB | **8.00 / 8.00 / 8.00** | 8.0~8.2 |

## 预热池的启动时间计费，池中等待不计费

`cachepress` session 的计费记录分两段，中间断 26 分钟：

```
12:52:54 ~ 12:53:11   19 条   内存 0.81→6.3 GB 爬升, vCPU 1.6~2.0
（中间 1596 秒无任何记录）
13:19:47 ~ 13:21:50  124 条   我的 session, 内存锁死 8.000
```

容器自报 `uptime_s=1600`，倒推启动时刻 = **12:53:08**，与第一段吻合。结论：

1. microVM 提前 26 分钟就启动进了预热池；
2. **启动那 19 秒被计费，且记在 26 分钟后才连上来的 session id 下**；
3. **在池子里干等的 26 分钟不计费**（记录为空）——好消息；
4. session 一连上计费立即恢复。

这同时解释了前面观察到的"内存爬升期"：**那不是 session 期间的爬升，是容器启动时内存真的在被填满**，
vCPU 同时是 1.6~2.0（两核都在忙启动）。也解释了大半个"平台值飘 4.78~8.00"——
`faef1d9e` 有 161 条记录（另两个 137/126），多出的约 25 条低值来自启动爬升期，把中位数拖低了。
剩下的差异由"摸过多少内存"解释（见上表）；`faef1d9e` 存活期均值 7.07~7.15 而非 8.00，
即它还没完全饱和，这一点的精确机制仍未查明。

## 四种负载形态横向对比

每 session 平均值：

| cell | 容器实际用量 | 平台 GB-h/session | 平台 vCPU-h/session | 计费秒 |
|---|---|---|---|---|
| idle（无负载） | 0.48 GB | 0.2549 | 0.001419 | 129.8 |
| spike2g（2GB 尖峰后释放） | 峰 2.50 / 均 1.16 GB | 0.2875 | 0.003533 | 140.7 |
| flat4g（持续占 4GB） | 4.50 GB | 0.3065 | 0.003685 | 143.1 |
| cpu2core（CPU 负载） | 0.49 GB | 0.2380 | **0.012645** | 170.6 |

- **内存维度**：用量从 0.48 涨到 4.50 GB（9.4 倍），GB-h 只从 0.255 到 0.307（1.2 倍）。**注意这个 cell 级平均会骗人**——每个 session 都被预热池启动段和 idle 尾巴稀释了，而且四组的"曾经摸过的内存"差别没有"当时占用"那么大（4.0 / 6.0 / 7.7 / 4.0 GB）。要看内存维度的真实响应，得看上面的 A/B 对照和饱和表，而不是这行平均值。
- **CPU 维度**：`cpu2core` 的 vCPU-h 是 idle 的 **8.9 倍**。折算成 CPU 秒：基线 5.1s → 45.5s，净增 **40.4s**。

## vCPU 是按用量计费的

`spin` 是已知量负载，可以正面校验：

| 量 | 值 |
|---|---|
| 请求的负载 | 20 秒 × 2 线程 |
| 容器 `/proc/stat` 实测消耗 | **20.5 CPU 秒** |
| 平台净增（扣 idle 基线） | **40.4 CPU 秒** |

平台确实跟着 CPU 负载走，但**绝对值是容器自述的约 2 倍**，不是 1:1。两点说明：

1. 我的 `spin_cpu` 用 Python 线程，受 GIL 限制实际只跑满 1 核 —— 所以容器自述 20.5s（≈1 核 × 20s）是对的，不是 40s。
2. 平台记 40.4s ≈ 2 核 × 20s。可能平台按"CPU 活跃期间 × 分配核数"计，也可能 host 侧看到的忙碌程度与 guest 内核的归因不同。**用容器内 `/proc/stat` 预测 CPU 账单会低估约一半。**

单核 spin 的对照实验可以定论这一点，目前尚未做。

## 时间序列形态：爬升 → 稳定 → 只在无内存负载时衰减

平台反推 GB 不是从头就等于 8.0，形态是"爬升几十秒后停在某个值"：

```
flat4g   session 6abb8055：t=0s 5.91 → t=1s 8.01 → 之后 133 秒恒定 8.00
cpu2core session 4e3b94f2：t=0s 0.82 → 缓慢爬升 → 停在 5.00 直到结束
```

爬升期已查明是**容器在预热池里启动**（见上一节），不是 session 期间的爬升。
不同 session 停在不同值（**8.00 / 7.95 / 7.82 / 7.16 / 6.67 / 6.11 / 5.00 / 4.78**），
差异主要由"这台容器曾经摸过多少内存"解释。

**无内存负载的 session 会自行缓慢下降。**空载 session `...b4cfbd52`：

```
t+  0s   8.011
t+ 30s   6.455
t+ 60s   5.908
t+ 90s   5.780
t+131s   5.308
```

131 秒降 2.7 GB，约 20 MB/s（1.2 GB/min），平滑单调——与 A/B 对照里的 A 组（−1.077 GB/min）速率一致。
**但只要 guest 主动摸过内存再释放，这个衰减就完全停止**（A/B 的 B 组、缓存回收实验）。

用 guest 内的瞬时指标去拟合平台值都不成立：

| 假设 | 平均绝对误差 |
|---|---|
| `MemTotal − MemAvailable`（真实用量） | 3.50 ~ 7.47 GB ❌ |
| `MemTotal − MemFree`（用量 + page cache） | 0.28 ~ 3.96 GB ❌ |

原因现在清楚了：平台值不是"当前用量"，是**历史高水位**，所以任何瞬时口径都拟合不上。
另外 107 秒（缓存实验）和 240 秒（A/B 尾段）都零衰减，**排除了 ≤100 秒的滚动峰值窗口**——
那样第 61 秒就该回落。

仍未解决：钉住/衰减的确切机制与公式，以及 microVM 是否启用了 balloon / free page reporting。

## 空闲时间在计费

`hold_s=60`、E2E 约 3 秒，但计费秒是 **129.8~170.6**。多出来的约 60 秒正是我们设的 `idleRuntimeSessionTimeout=60`。

**含义**：session 关闭后到 idle 超时之前一直在计费。原配置 `idleRuntimeSessionTimeout=900` 意味着**每个 session 用完后要空转 15 分钟计费**——一次 3 秒的调用会被按 ~15 分钟收 8GB 内存。这是最容易被忽略的成本项，把 idle timeout 调到业务能接受的最小值是最直接的省钱手段。

## 三方数据源一致性

| 来源 | 值 |
|---|---|
| USAGE_LOGS 逐 session 累加（本次 12 session） | GB-h 合计约 3.28 |
| CloudWatch `MemoryUsed-GBHours`（账号级，同窗口） | 3.5222 |
| CloudWatch `CPUUsed-vCPUHours` | 0.0654 |

账号级指标略高，符合预期（窗口内还有其他 agent 活动 + 聚合时机差异）。量级一致，说明 USAGE_LOGS 的解析没错。

## 文档与实测的差异（写代码要注意）

| 文档 | 实际 |
|---|---|
| 字段平铺 | 嵌套在 `resource` / `attributes` / `metrics` 下 |
| `elapsed_time_seconds` | 实际叫 **`time_elapsed_seconds`** |
| 未说明累计还是增量 | 每条 `time_elapsed_seconds=1.0`，是**每秒增量** |
| — | `event_timestamp` 是 epoch **毫秒** |

投递配置的 `deliveryDestinationArn` 指向 delivery-destination 而非日志组，要再解一层
`get_delivery_destination` 才能拿到真正的日志组名（本次是 `/aws/vendedlogs/bedrock-agentcore/usage`）。

延迟实测：压测结束后约 5~10 分钟数据可查，远好于文档说的最长 60 分钟。

## 成本含义

内存计费是**只涨不落的高水位**，所以优化的方向和"降低平均占用"完全不同：

- **降低峰值有效，而且要从一开始就低。**摸过 4.0 GB → 平台停在 4.7~7.0；摸过 7.7 GB → 直接 8.00 顶格。
  能省的钱就在这个区间里。
- **峰值之后再优化稳态占用无效。**曾经摸到 8 GB，之后哪怕降到 0.5 GB 也不退——A/B 对照里 B 的
  guest 占用比 A 还低，账单反而高 43%。所以"用完就 free 一下省钱"这种做法没有意义。
- **一次性的大内存动作代价很高。**加载一个大模型/大文件、一次批量处理把内存顶到 8 GB，
  这个 session 剩余的整个存活期（含 idle timeout 那 60~900 秒）都按接近 8 GB 计。
  能拆小、能流式处理、能换成外部存储的，收益直接。
- **减少计费秒同样重要**：调小 `idleRuntimeSessionTimeout`、别让 session 空挂。高水位 × 时长两个因子都要压。
- **CPU 按真实用量算**，正常优化就有效。

## 本次测试的局限

- **v1 平台，不是 v2**。v2 是否有 cgroup 限制、microVM 规格是否可配，未知。
- 12 个 session + 1 组 A/B、单 runtime、单区域、每形态一轮。session 间 4.78~8.00 的离散度说明样本量不足以刻画分布。
- 文档明确"遥测值与实际账单可能不一致"。以上全部基于 USAGE_LOGS 遥测，**不是账单**。要定论需拿真实账单核对。
- `spin` 受 GIL 限制只跑满 1 核，2 倍差异的成因未分离（需单核 spin 对照）。
- **钉住与衰减的机制未查明**：不知道 microVM 是否启用 balloon / free page reporting，
  也不知道平台是取滑动最大值、EWMA 还是 host RSS。已向 AWS 开 TT 询问（见
  [TT-agentcore-memory-billing.md](TT-agentcore-memory-billing.md)）。
- `faef1d9e` 存活期稳定在 7.07~7.15 而非 8.00，为何没有饱和到顶仍未解释。
- 未测：session 之间（同一 microVM 复用）高水位是否会重置。这直接影响长生命周期 runtime 的成本模型。

## 复现

```bash
python3 test_ws_coldstart_v2.py -c 3 --label idle     --hold-s 60
python3 test_ws_coldstart_v2.py -c 3 --label spike2g  --hold-s 60 --alloc-mb 2048 --free-after-s 20
python3 test_ws_coldstart_v2.py -c 3 --label flat4g   --hold-s 60 --alloc-mb 4096
python3 test_ws_coldstart_v2.py -c 3 --label cpu2core --hold-s 60 --spin-s 20 --spin-threads 2

# 决定性的并发 A/B 对照（各 360s，B 在 t+61s 占 4GB、t+121s 释放）
python3 hyst.py

# drop_caches 可写性探测 + 加压/释放
python3 test_dropcache.py

# 等 5~10 分钟
python3 collect_usage_logs.py --run-dir results/<run> \
  --log-group /aws/vendedlogs/bedrock-agentcore/usage
```
