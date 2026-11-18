# DSH Agent 在 Nitro Sandboxes / EC2 上的实测报告

## 结论

**已在真实 AWS EC2 Nitro 硬件上完成部署与测试，不是 Docker 或模拟进程结果。**

- 无模型启动：confidential / direct 各完成 **20/20 轮**。Sandbox 就绪 p50 分别为 **3.570 秒 / 2.232 秒**；再跑完 DSH CLI help 的新 VM 全程 p50 分别为 **5.404 秒 / 4.074 秒**。
- 真实 DSH + Bedrock：计划 20 轮，**实际完成 4 轮、8 个回合**，全部完成模型文本回复；冷回合“新建 VM → 完整响应”p50 为 **4.923 秒**（范围 **4.713–5.030 秒**），spawn → 首条文本 p50 **2.622 秒**。样本少，仅作为本机探索性观测，不代表稳定高分位/SLA。
- **严格答案不稳定**：8 个回合只有 **3 个**精确匹配 `pong`（**37.5%**；cold 1/4，warm 2/4）。连续 3 轮出现不匹配触发保护，未继续扩到 20 轮。5 条不匹配记录没有丢弃；它们是“完成回复但答案不合预期”，不是超时、进程、模型执行或 Nitro 启动失败。
- 性能表采用**全部已完成回合**，同时保留仅精确匹配样本的原始 `summary`；两种统计不可混称。不能据此宣称真实 Agent 的 20 轮验证全部成功。
- 父实例已停止，原始证据已下载并核验 SHA256；EBS、代码、IAM role/profile 和安全组保留，可复查和恢复。

## 测试环境与部署

- 实测日期：2026-11-17 UTC；账号 `434444145045`，区域 `us-east-1` / AZ `us-east-1a`。
- 专用父实例：`i-062d42b82e041af2d`，`m7g.xlarge`，Graviton ARM64，4 vCPU（4 核、每核 1 线程）、16 GiB 标称内存。
- AMI：`ami-04bde3808fe5f70cf`，`al2023-ami-2023.12.20261112.0-kernel-6.18-arm64`，AWS owner `137112412989`。
- 实际父/guest kernel：`6.18.62-127.179.amzn2023.aarch64`，启动参数 `nitro_enclaves.mempool=8G@0`。内核日志确认 8192 MiB CMA pool、4 个 slot；首次检查运行中 slot 为 0。
- 父实例启用 Nitro Enclaves、IMDSv2 required / hop limit 1；通过 SSM 管理，无 SSH key，无安全组入站规则。公网 IPv4 仅用于软件下载和公网 Bedrock 出口，guest HTTP 端口不对外开放。
- 根卷：`vol-04b232b3446b422cc`，30 GiB gp3、3000 IOPS、125 MiB/s，已加密，DeleteOnTermination=true。
- 软件：Firecracker `v1.18.0`、DSH `0.2.2-rc.1`，Node `v24.20.0`；由父实例原生构建 initramfs，保留完整 stock DSH node_modules，不剪裁未使用工具包。
- 构建产物 SHA256：kernel `dbd969beb742560ac5f3446617f38bfa144281349bf4dfc58547cfe800066061`；initrd `2c7ed41bb74ae00965a377a0210ca1a01700528501344d527c9001f6b718c140`。kernel **12,601,592 bytes**，initrd **148,015,542 bytes（141.159 MiB）**；所有正式批次使用同一镜像。
- 实例角色 `dsh-nitro-bench-20261117`：SSM managed-node 权限，以及仅针对 `arn:aws:bedrock:us-east-1::foundation-model/deepseek.v3.2` 的 `InvokeModel` / `InvokeModelWithResponseStream`。不使用现有高权限实例角色。
- 部署前单独调用真实 Bedrock Converse 成功返回 `pong`（maxTokens=32，input/output token=10/3）；这是模型访问预检，**不属于 DSH benchmark 样本**。

## 方法与指标

每个正式批次计划 20 轮、每轮 cold/warm 各一次，批次为 `startup-confidential`、`startup-direct`、`agent-direct-batch2`。Agent 首次严格答案校准批次另行保留，不混入正式统计。另有 1 轮 debug confidential 冒烟，不混入正式统计。

- guest：2 vCPU / 4096 MiB、CPU overcommit=true、没有块设备，boot-source 为 EFI-stub kernel + 自建 initramfs；配置显式包含 `enclave`，使用 Nitro 而不是 `/dev/kvm`。
- 每轮新建 Firecracker 和 Nitro Sandbox，健康响应必须匹配新 `run_id` + guest `boot_id`；启动后核对硬件 `pcie_slots_in_use=1`。cold 后在同一 guest 调用一次 warm，最后停止 VMM，等 slot 回到 0 才启动下一轮。
- `startup` 调用 stock `dsh headless --help`，无模型、无凭证、无出网，**只能表示 CLI 启动/退出路径**；该调用不会先于 agent 回合执行，防止预热 agent。
- `agent` 使用 stock `dsh headless --json --approval-policy never -`。prompt 为 `Reply with exactly: pong. Do not use tools.`；Bedrock `deepseek.v3.2`，provider model 配置明确 `maxTokens=512`。成功要求退出码 0、completed final、非空 text 事件，以及最终答案 trim 后等于 `pong`；仅保存匹配布尔值，不保存模型答案或凭证。
- 每次请求启动新的 DSH 子进程；warm 不是持久化 DSH 进程的第二轮对话，而是同一个已启动 guest 中的新 DSH 进程。
- 父实例的临时最小权限角色凭证通过 IMDSv2 在父实例上获取，放入 root-only 临时文件，转入 guest 子进程环境；文件在批次结束时移除。guest nft 显式拒绝父实例服务、IMDS、私网和 IPv6，真实回合只放行 Amazon DNS / 公网 HTTPS。

主要指标：

| 指标 | 测量边界 |
|---|---|
| Sandbox 就绪 | 父：创建 Firecracker 前 → 收到匹配身份的健康响应；含 VMM、UEFI/kernel/initramfs、包装器启动及探测 |
| DSH 首个事件 | guest：spawn → 首个合法 NDJSON；代理指标，不声称完全排除模型 |
| DSH 首条文本 | guest：spawn → 首个非空 text；包含初始化、网络与模型 TTFT |
| DSH 总耗时 | guest：spawn → 进程管道关闭；startup 为 help 全程，agent 含模型与退出 |
| POST 全程 | 父：POST 前 → 收到完整 JSON |
| 新 VM 第一条消息端到端 | 父：创建 Firecracker 前 → cold 请求完整响应 |

两侧分别用单调时钟，只计算本机时间差。逐个 cold 样本核对 `端到端 = Sandbox 就绪 + ready_to_invoke 编排间隔 + POST 全程`；不能把各指标 p50 相加当总 p50。分位数使用 nearest-rank，失败单列。

镜像构建、下载、父实例启动/重启、SSM 编排、网络建立、凭证准备及 image SHA256 读取都在计时外。每轮是**新的完整 guest 启动**，不是快照恢复；父实例镜像已在本地且页缓存不清除，guest root 位于 RAM-backed initramfs。因此不代表镜像首次下载/EBS 冷读、发布全链路或 AgentCore V2 snapshot restore。CPU overcommit 的父实例竞争、公网网络与模型波动属于本次实验条件。

## 实测结果

### A. 无模型 DSH CLI 启动（单位：秒）

每格 20 个样本，全部成功；`startup` 不测真实模型回复。

| 模式 / 阶段 | 指标 | p50 | p90 | 最小–最大 |
|---|---|---:|---:|---:|
| Confidential cold | Sandbox 就绪 | 3.570 | 3.612 | 3.515–3.641 |
| Confidential cold | DSH help 进程全程 | 1.841 | 1.864 | 1.802–1.883 |
| Confidential cold | 新 VM → help 完整响应 | 5.404 | 5.455 | 5.321–5.528 |
| Confidential warm | DSH help 进程全程 | 1.835 | 1.862 | 1.820–1.867 |
| Direct cold | Sandbox 就绪 | 2.232 | 2.251 | 2.168–2.273 |
| Direct cold | DSH help 进程全程 | 1.838 | 1.867 | 1.787–1.906 |
| Direct cold | 新 VM → help 完整响应 | 4.074 | 4.116 | 3.998–4.145 |
| Direct warm | DSH help 进程全程 | 1.829 | 1.845 | 1.791–1.872 |

这是本机、同镜像、顺序批次的模式差异，不是随机交叉实验；不能把 direct 的性能收益推广为所有实例/镜像的固定比例。两模式 DSH CLI 进程耗时接近，明显差异在进入 guest 健康状态之前；本实验没有进一步精确归因到加密、内存分配或设备缓冲的某一个阶段。

### B. 真实 DSH + DeepSeek 对话（单位：秒）

每阶段 **4 个已完成回合**，包含严格答案不匹配的回合；不对 4 个样本的 p90/p99 作可靠性结论。

| 指标 | Cold p50 | Cold 最小–最大 | Warm p50 | Warm 最小–最大 |
|---|---:|---:|---:|---:|
| Sandbox 就绪 | 2.228 | 2.224–2.260 | 不重新启动 | — |
| spawn → 首个 JSON 事件 | 1.858 | 1.828–1.887 | 1.895 | 1.862–1.916 |
| spawn → 首条文本（含模型） | 2.622 | 2.402–2.687 | 2.511 | 2.472–2.733 |
| DSH 进程全程（含模型） | 2.694 | 2.481–2.766 | 2.585 | 2.544–2.805 |
| POST 完整请求 | 2.699 | 2.484–2.770 | 2.587 | 2.545–2.807 |
| 新 VM → 第一条消息完整响应 | **4.923** | **4.713–5.030** | 不适用 | — |

精确答案断言结果：cold **1/4**、warm **2/4**。原始严格 `summary` 的 cold 只有 1 个成功样本（全程 4.938 秒），不能拿它冒充 4 个或 20 个成功样本。答案全文没有保存，因此不能进一步断定不匹配是标点、格式还是内容差异，原因尚未定位。

### C. 数据与时间核对

- 正式批次合计 **44 轮、88 条记录、44 个不同 cold boot_id**；其中 80 条 CLI 记录和 8 条 Agent 记录均执行完成，执行失败 **0**；严格答案维度有 **5 条**未通过。
- 全部证据（含 debug 冒烟、首次严格校准）：**47 轮、93 条记录**。实际 DSH Agent 回合合计 **11 个**（校准 3 + 正式后续 8）；这不是 Bedrock API 调用次数，SDK/Agent 内部调用次数和实际账单未独立审计。
- 逐个已完成 cold 样本的端到端分解最大残差为 **0.0 ms**（未舍入原始值计算）。POST 减 DSH 全程的残差范围分别为 confidential CLI **1.587–4.675 ms**、direct CLI **1.558–4.357 ms**、Agent **2.169–4.522 ms**，仅作为 HTTP/配置/测量合计残差，不单独归因。
- 父实例 1/5/15 分钟 loadavg：confidential 开始 `[0.61,0.81,0.41]`、结束 `[0.10,0.53,0.37]`；direct 开始 `[0.10,0.53,0.37]`、结束 `[0.13,0.39,0.33]`；Agent 开始 `[0.00,0.18,0.26]`、结束 `[0.06,0.18,0.25]`。批次未并行执行，未测并发/满载/CPU overcommit 争用。
- 软件离线验证最终 **28 项 unittest** 通过，包含 native ABI 筛选、凭证校验、超时不重放、答案质量与执行完成分离、连续 3 轮保护；Python/Node/Bash 检查通过。

## 部署调试与失败样本

1. **构建失败 1**：Koffi 同包附带 glibc / musl 插件，脚本误将 musl 插件作为 AL2023 必需依赖。失败命令 `ffad9043-e0d9-446c-adf4-35eae1639bea`。
2. **构建失败 2**：排除 musl 后，node-pty 中附带的 x86_64 / Windows / macOS 变体仍被扫描。失败命令 `c2b5a6b9-20da-4c9e-81d8-0eef0e1d01d9`。修复为检查 ELF magic/class/endianness/e_machine 和 glibc ABI，只对当前 Linux ARM64 可加载的插件解析系统依赖；不删除包内其他变体。新增离线回归测试。第三次构建 `aa37cc84-8084-4a03-8b3c-0b065cd97f9d` 成功。
3. **硬件启动冒烟**：debug confidential 1 轮 cold/warm 都成功（命令 `42eaad9c-b0e4-445d-ac9f-663c99dba135`）；不计入正式性能样本。
4. **Agent 凭证前置校验失败**：IMDS 的临时凭证字段叫 `Token`，不是 AWS CLI process JSON 的 `SessionToken`。命令 `7a99292b-9496-4846-8b11-be376a42d0d4` 在启动 Sandbox 前失败；修复显式转换，仍校验临时凭证及过期时间，不降低权限、不打印凭证。
5. **严格答案校准批次提前停止**：命令 `14dd1e34-2bec-4f82-81b6-e8092cf4e2f0` 完成第 1 轮 cold/warm；第 2 轮 cold 退出码 0、completed、存在文本输出、无超时，但 `answer_matches_expected=false`，默认严格断言停止。3 条记录保存在 `agent-direct/`；其中 1 条是答案未精确匹配，**不是模型调用、DSH 进程或 Nitro 启动失败**。第 2 轮端到端 5076.234 ms（以原始文件计算确认），不能静默当作成功或丢弃。
6. 命令 `10c95a6c-f4e8-4d6c-9c86-e3e10c60c85b` 的正式 Agent 后续批次 `agent-direct-batch2/` 显式启用 `--continue-on-answer-mismatch`：仍逐条保留严格 `ok` 和答案匹配布尔值，但将完成回合性能与精确答案符合率分开统计；超时/HTTP/进程/模型执行失败立即停，连续 3 轮答案不符也停。新批次没有覆盖校准批次，不是重新发送同一请求或复用旧 VM。实际第 2、3、4 轮触发连续不匹配保护，4 轮后停止，不再自动启动模型测试。
7. 首次归档把输出放在被归档目录内，tar 报目录在读取时变化；改为在目录外生成临时归档，再移动到新文件名。没有覆盖第一份文件。最终归档命令 `c67b7916-835e-4978-8047-3e4783e458e2` 成功并通过下载后的 SHA256 核验。

## 原始证据与复现

本机目录：`27-nitro-ec2/results/`（默认 gitignore，未提交凭证或自动提交原始数据）。

- `evidence-final.tgz`：**130,560 bytes**，SHA256 `cdcf9f69f19218a0c5ff74d4aa897c040667a39958ad9e6480b6fd84359640a2`，与父实例端 hash 一致；安全解包为 `evidence/`。
- `evidence/startup-confidential/`、`evidence/startup-direct/`、`evidence/agent-direct-batch2/`：逐轮 `records.json`、实际 `sandbox.json`、Firecracker 日志以及总 `results.json`。
- `evidence/agent-direct/`：首次严格校准的 3 条记录；`evidence/smoke-confidential/`：debug 冒烟证据。均保留，不混入正式统计。
- `evidence/host/`：kernel 命令行、CPU/内存、slot/nft/TAP/forwarding 清理状态、镜像 manifest、npm package-lock 与源文件 SHA256。
- `results/analysis.json`：由 `scripts/analyze_results.py` 从逐条记录重新计算的统计及审计；stored summary 与重算值一致，同轮 cold/warm boot_id 一致、跨 cold 不复用 VM。
- `results/ssm/`：每次命令的 UUID、状态、stdout/stderr，包括两次构建失败、凭证校验失败、答案保护停止和成功批次。下载过程不输出或上传模型凭证。

正式结果文件 SHA256：

| 文件 | SHA256 |
|---|---|
| startup-confidential/results.json | `a8d9b9ded40f41078b00462db6eda950b8c9a79f7dd5432e58d99bdee35bf603` |
| startup-direct/results.json | `3c913ea88c0a71e7449fbbc46a951f683a5c0f613466c483f147d2910e4e31e4` |
| agent-direct-batch2/results.json | `3deafb7773a9e25d64780086bd406afeda17d3c521cc011b0b6fc380f185dbf3` |

关键硬件证据不是只依赖 HTTP：Firecracker 日志显示 `Enclave VM created`、`direct_mode`、`NE_ADD_ANY_VCPU`、`CMA alloc`、4096 MiB 内存交付、`NE_ADD_DEVICE` 和 enclave start；运行时 sysfs slot 为 1，停止后为 0。`benchmark.py`、guest 包装器/init 和 build script 的本机源 hash 与父实例采集值全部匹配。

重新审计：

```bash
cd 27-nitro-ec2
python3 scripts/analyze_results.py results/evidence
python3 -m unittest discover -s tests -v
```

恢复实例后可先用 SSM 检查父实例，再在新的输出目录运行基准。**Agent 的连续不匹配保护已触发，本次没有继续自动扩样；继续测试前应由用户选择是否调整任务或答案评判方式。**

## 资源状态与限制

- 结束前验证：运行中 Sandbox **0**、测试 TAP **0**、`dshb_...` nft table **0**、临时凭证文件 **0**，IPv4 forwarding 恢复为 **0**。清理核验命令 `b9a5f3f1-9326-458f-ac4b-628f811576a9` 成功。
- EC2 `i-062d42b82e041af2d` 已通过 StopInstances 停止：请求时间 `2026-11-17 10:16:44 GMT`，随后 waiter 和 DescribeInstances 确认 **stopped**。停止是可逆操作，没有终止实例或删除原始数据。
- 30 GiB 加密 EBS、镜像与代码，以及专用 IAM role/profile、安全组仍保留。**EBS 仍会收费**；未删除其他现有资源。若不再需要，可在确认归档后由用户选择终止实例和删除专用 role/profile/安全组。
- 本次没有配置 NSM attestation/KMS secret release，不验证 confidential 模式真实 Agent 的密钥保密性；confidential 只跑无凭证 CLI。不能把 direct 模式解释为父实例不可读 guest 内存。
- 本次未验证并发、持久工具进程、快照恢复、真实 coding workload、首次镜像下载、端到端发布耗时或账单金额。Nitro Sandboxes 目前不支持 snapshot/restore，结果也不能作为 AgentCore V2 的同条件对照。
