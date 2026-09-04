# 冷启动对比:AgentCore Runtime vs AWS Lambda MicroVMs

[English version](COMPARE.md)

由 `scripts/gen_compare_report.py` 根据 `results/summary.json`(AgentCore,2026-07-09 部署)与 `results/microvm/summary.json`(Lambda MicroVMs,2026-09-04 运行)生成,区域 us-west-2,客户端运行在同区域的 EC2 实例上。两侧使用同一个 ping-pong agent(`BedrockAgentCoreApp`,无 LLM 调用)、同样的 500 MB / 1 GB / 2 GB 填充梯度、同样的 Barrier 同时放行并发,客户端均关闭重试。

## 结论速览

- **Lambda MicroVMs 的冷启动与镜像大小无关**:全部单元格 p50 2,126–2,263 ms;并发 1 p50 500mb: 1,520 / 1gb: 1,510 / 2gb: 3,419;并发 10 p50 500mb: 1,453 / 1gb: 1,502 / 2gb: 1,398。每次 `RunMicrovm` 都是从镜像的内存+磁盘快照恢复出一台全新 VM,没有预热池可命中或错过;填充层躺在按需加载的快照磁盘上,而不是从 ECR 拉取。
- **但它是双峰分布**:68% 的启动落在约 1,537 ms(代理把首个请求挂住约 1,069 ms 直到应用应答;guest 侧 `/run`→请求仅 66 ms),32% 落在约 3,516 ms(代理挂住约 3,020 ms 后返回 **502**,紧接着的重试立刻成功——此时 guest 已就绪 1,763 ms)。各尺寸慢模式占比:500mb: 41%, 1gb: 24%, 2gb: 32%。
- **AgentCore 并发 1 的数字是预热池命中**(500mb: 412 / 1gb: 732 / 2gb: 1,176);真正的 microVM 启动(ECR 拉取 + 容器启动)p50 为 8,121 ms (500mb) → 11,428 ms (1gb) → 13,503 ms (2gb),并发超过池子后成为主导(并发 10 p50:500mb: 7,320 / 1gb: 11,401 / 2gb: 13,458)。同类对比下 MicroVM 的一次启动便宜 3.7–6 倍,且差距随镜像变大而拉开(见下表)。
- **热请求**:AgentCore 102 / 69 / 76 ms vs MicroVM 4 / 4 / 4 ms(p50,按尺寸顺序)。MicroVM 是同区域直连 VM 专属 HTTPS 端点并复用连接;AgentCore 经由 `InvokeAgentRuntime` 前门。
- **挂起 → 自动恢复**(MicroVM 独有能力,内存状态保留):从 `GetMicrovm` 报告 `SUSPENDED` 起到恢复后首个请求 5,982–8,400 ms (n=30, p50 6,290),同样呈双峰(约 6.2 s / 约 8.2 s)。对这个 2 GB 基线的镜像谈不上「近乎瞬时」,但远低于任意一方的冷启动,且状态完整保留。
- **扇出上限来自 `RunMicrovm` API,而不是启动时间**:并发 ≤10 共 0/150 次限流,但并发 50 出现 26/150 次 `ThrottlingException`(配额 5 TPS、突发 5——实测桶大小放行了 50 中的 37–45 个),且 `RunMicrovm` 本身排队到 p50 946 ms(并发 ≤10 时约 120 ms);并发 50 冷启动 p50:500mb: 4,030 / 1gb: 2,224 / 2gb: 2,396。AgentCore 的 `InvokeAgentRuntime` 配额为 200 req/s。

## 冷启动 p50 / p90 / max(ms)并排对比

| 镜像 | conc | AgentCore Runtime p50 / p90 / max | Lambda MicroVMs p50 / p90 / max |
|---|---|---|---|
| **500mb** | 1 | 412 / 436 / 503 | 1,520 / 3,404 / 3,426 |
| **500mb** | 5 | 667 / 7,339 / 7,432 | 1,460 / 3,412 / 3,415 |
| **500mb** | 10 | 7,320 / 7,591 / 7,723 | 1,453 / 3,447 / 3,568 |
| **500mb** | 50 | 8,149 / 8,348 / 10,285 | 4,030 / 4,270 / 4,815 |
| **1gb** | 1 | 732 / 1,170 / 1,208 | 1,510 / 3,440 / 3,516 |
| **1gb** | 5 | 763 / 11,438 / 15,409 | 1,500 / 3,398 / 3,410 |
| **1gb** | 10 | 11,401 / 11,470 / 11,503 | 1,502 / 3,439 / 3,496 |
| **1gb** | 50 | 11,422 / 11,501 / 39,581 | 2,224 / 4,206 / 4,261 |
| **2gb** | 1 | 1,176 / 1,811 / 2,090 | 3,419 / 3,470 / 3,653 |
| **2gb** | 5 | 1,155 / 13,478 / 13,619 | 1,372 / 3,402 / 3,425 |
| **2gb** | 10 | 13,458 / 13,610 / 15,620 | 1,398 / 3,413 / 3,438 |
| **2gb** | 50 | 13,493 / 13,634 / 15,778 | 2,396 / 4,351 / 4,387 |

AgentCore 列取 `results/raw/` 中每格最新一次,MicroVM 列同理(`results/microvm/raw/`)。AgentCore 并发 1/5 的中位数是预热池命中,真正的同类对比见下表。MicroVM 在所有并发 ≤10 单元格里 p90 ≈ 3.4 s,是上文的慢模式,不是镜像大小效应。

## 两个平台上一次真正启动的代价

| 镜像 | AgentCore 真实启动 p50 (ms) | MicroVM 冷启动 p50 (ms) | 倍数 |
|---|---|---|---|
| 500mb | 8,121 (n=57) | 2,221 (n=95) | 3.7× |
| 1gb | 11,428 (n=57) | 2,126 (n=92) | 5.4× |
| 2gb | 13,503 (n=61) | 2,263 (n=87) | 6.0× |

AgentCore「真实启动」= agent 进程在请求期间才启动的探针(`request_ts − proc_start_ts < cold_ms`),即 ECR 拉取 + 容器启动。MicroVM 的每个探针按定义都是真实启动(全新 `RunMicrovm`)。

## Lambda MicroVMs 冷启动分解(p50,ms)

| 镜像 | RunMicrovm API | CreateAuthToken | HTTP 尝试次数(均值) | 首个成功请求 | VM 内 /run → 请求 | 热请求 | 挂起 → 恢复 |
|---|---|---|---|---|---|---|---|
| 500mb | 282 | 150 | 1.8 | 26 | 309 | 4 | 8,139 (n=10) |
| 1gb | 153 | 150 | 1.7 | 26 | 79 | 4 | 8,136 (n=10) |
| 2gb | 145 | 150 | 1.7 | 26 | 83 | 4 | 6,220 (n=10) |

- `cold_ms` = 调用 RunMicrovm 起 → 首个 HTTP 200 完整响应体。API 返回到首个 200 之间,客户端每 100 ms 轮询一次 `POST /invocations`;代理会把请求挂住直到应用应答,或放弃并返回 **502**。原始记录中的 `attempt_ms`(并发 50 单元格)能看到两种模式:一次挂住约 1.0–1.2 s 后成功,或一次挂住约 3.0 s 后 502、随后约 25 ms 成功。
- 「VM 内 /run → 请求」是 Lambda 调用 `/run` 生命周期钩子(快照恢复后立刻触发)到请求到达的 guest 侧墙钟时间。快模式下只有几十 ms——API 返回时 VM 实际已就绪,那约 1 s 花在代理路径上;慢模式下 guest 空等约 1.8 s,代理却没有把请求送进来。
- `RunMicrovm API` 在并发 ≤10 时约 120 ms,并发 50 时升到约 0.9 s(服务端对突发排队),因此上表按尺寸汇总的这一列被并发 50 单元格抬高了。
- MicroVM 内的 `proc_start_ts` 是镜像*构建*时间:Python 进程在 `/ready` 之后被快照,`RunMicrovm` 时不再重新 import 任何东西。

## MicroVM 错误与限流

| 单元格 | 样本 | 成功 | 限流 | 其他错误 |
|---|---|---|---|---|
| 500mb c=1 | 10 | 10 | 0 | 0 |
| 500mb c=5 | 20 | 20 | 0 | 0 |
| 500mb c=10 | 20 | 20 | 0 | 0 |
| 500mb c=50 | 50 | 45 | 5 | 0 |
| 1gb c=1 | 10 | 10 | 0 | 0 |
| 1gb c=5 | 20 | 20 | 0 | 0 |
| 1gb c=10 | 20 | 20 | 0 | 0 |
| 1gb c=50 | 50 | 42 | 8 | 0 |
| 2gb c=1 | 10 | 10 | 0 | 0 |
| 2gb c=5 | 20 | 20 | 0 | 0 |
| 2gb c=10 | 20 | 20 | 0 | 0 |
| 2gb c=50 | 50 | 37 | 13 | 0 |

共启动 300 台 MicroVM,客户端主动终止 274 台(26 次限流,0 次其他错误;被限流的探针根本没拿到 VM)。空闲策略 `maxIdle=60s, suspendedDuration=0` 保证遗留 VM 一分钟内被终止;跑完后 `list-microvms` 显示 0 台未终止。

## 方法说明 / 注意事项

- 镜像 — AgentCore:500mb: 383 MB ECR, 1gb: 907 MB ECR, 2gb: 1,833 MB ECR;MicroVM:500mb: 500 MB target (base 244 MB + pad 257 MB), 1gb: 1024 MB target (base 244 MB + pad 781 MB), 2gb: 1950 MB target (base 244 MB + pad 1707 MB)。MicroVM 镜像由 Lambda 按同一套 Dockerfile 方案(`/dev/urandom` 填充层)在 `al2023-1` 之上构建,基线 2048 MiB / 1 vCPU。
- 两个客户端都从第一次 API 调用计时到调用方读完完整响应体,因此都包含 TLS 握手与 API 前门延迟。
- MicroVM 就绪通过连接探测判断(文档:`GetMicrovm.state` 最终一致),给 `cold_ms` 带来 ≤100 ms 的轮询粒度。
- 构建时的 `/validate` 钩子会在测试 VM 上真实跑一次 `/invocations`,让 Lambda 预取热路径触及的快照页——这是文档推荐的生产做法,不是只为跑分的技巧。
- 两者是不同的产品:AgentCore Runtime 是托管的 agent 服务平台(会话路由、身份、预热池、8 小时会话);Lambda MicroVMs 是需要自行编排的计算原语(每 VM 端点 + 鉴权令牌、显式生命周期)。
- 关于默认配额、为何与镜像大小无关、快/慢模式的详细讨论见 [MICROVM_NOTES.zh.md](MICROVM_NOTES.zh.md)。

## 复现

```bash
bash scripts/deploy_microvm.sh
uv run python microvm_coldstart_test.py --smoke --resume
uv run python microvm_coldstart_test.py --full --resume      # 并发 1,5,10 x 3 尺寸,约 10 分钟,150 台 MicroVM
uv run python microvm_coldstart_test.py --full --concurrency 50 --sizes 500mb   # 每次只跑一个尺寸,让 RunMicrovm 令牌桶回填
python3 scripts/gen_compare_report.py
bash scripts/cleanup_microvm.sh --yes
```
