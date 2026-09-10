# s5cmd 云端实测：原生 S3 与 JuiceFS S3 Gateway

> 2026-09-10，us-west-2。一个 AgentCore Runtime，多用户 session。主体记录 s5cmd 实测；第 3.3 节补录此前 boto3 逐文件读写的历史汇总，用于对照，不合并两套测试统计。

## 1. 结论

已更新现有单 Runtime 镜像并重跑 s5cmd 云端基准。**8、32、64、128 workers 各三轮完成；256 workers 第二轮失败，保留为未完成结果，不参与三轮统计。**

本次固定网关配置下：

- **保存工作区：原生 S3 在所有完整 worker 配置下更快。**
- **32 和 128 workers：原生 S3 在保存、冷恢复、热恢复均更快。** 例如 32 workers 保存 clone 工作区的 batch 中位数为 6.78 秒，JuiceFS 为 17.79 秒；热恢复为 9.59 秒对 12.88 秒。
- **8 workers：JuiceFS 冷恢复约快 1.34–1.36×、热恢复约快 2.74–2.76×**；但原生 S3 增加 worker 后也明显缩短了等待。
- **64 workers：热恢复接近，JuiceFS batch 约快 5%–7%**，保存和冷恢复仍是原生 S3 更快。

因此，不能给出“JuiceFS 总是加速”的结论。对本应用的 S3 协议小文件持久化，优先使用 s5cmd 调优原生 S3 是合理的起点；是否引入 JuiceFS 应结合文件系统能力、缓存复用和运维需求，而不是只看低并发热读倍率。

以上结论仅依据本轮 s5cmd 实验。此前 boto3 历史数据单独补录在第 3.3 节；不恢复旧测试代码，不重建已删除的原始 JSON，也不把历史数据混入 s5cmd 的三轮中位数。

## 2. 环境和方法

- 单一 Runtime：`jfsbench_d6935b4639-5IfNz2BMEZ`，更新后版本 **3**，无第二个 Runtime。
- 镜像 tag：`s5cmd-ca71702f36`；ECR digest：`sha256:63292ec1e61fd4b54bdb8be5ec97e8acbfd367309741111f2b5150a941dcb034`。
- s5cmd **v2.3.0**，ARM64 官方包；固定 `--retry-count 2`、`cp --concurrency 1 --raw --no-follow-symlinks`。不通过 Python SDK 传输测试文件。
- 网关：EC2 `m7g.large`，2 vCPU / 8 GiB，us-west-2a；30 GiB gp3（3,000 IOPS、125 MiB/s），SQLite 元数据，JuiceFS 1.4.1，1 GiB 磁盘读缓存，关闭 writeback。
- 对照：相同区域/物理 S3 bucket 的原生对象 prefix 与 JuiceFS 数据块区域分开；两路径使用各自租户受限凭证。网关走 HTTPS 私网地址，S3 配置 VPC gateway endpoint。
- 每个 worker 配置使用一组新的用户 A/B session。一个配置内的所有性能比较都在 A 的同一个 session 中交替执行；B 用于隔离验收。配置顺序为 32、8、64、128、256，各三轮；没有并行运行多个配置。

工作负载为 Django 5.2.6，commit `75c4403f07b8ad25893f7832dbe8fc6814b53b2d`：

| 工作区 | 普通文件数 | 普通文件字节 | 目录数 | 符号链接 |
|---|---:|---:|---:|---:|
| clone，包含 `.git` | 6,905 | 58,057,582 | 3,257 | 4 |
| unzip 后源码树 | 6,897 | 44,841,006 | 3,249 | 4 |

源码在镜像内，clone/unzip 只用于生成并冻结小文件工作集，不包含 GitHub 网络下载。完整复制使用一个 s5cmd run 进程执行约 6,900 条精确 cp 命令；manifest 单独复制。两个后端复用同一个可信 manifest，恢复时总是创建新目录并校验内容、模式、链接和 Git HEAD。

`file_batch_seconds` 包含 s5cmd 启动、TLS/连接、复制、进程退出及 JSON 成功记录核验；`wall_seconds` 还包含 manifest、目录准备、哈希/模式校验。文中分别给出两个口径，不把完整恢复说成纯网络时间。

cold 表示重启网关并启用新空磁盘缓存，未清除 OS、SQLite、AWS 内部缓存。warm 表示同一远端内容再次完整恢复到新目录；s5cmd 进程仍是新的，不复用上一次进程连接。

## 3. 性能结果

下面每格均为 **原生 S3 / JuiceFS Gateway** 的三轮中位数，单位秒。倍率为两后端中位数之比，不是逐轮比值的中位数。先列 batch，再列完整 wall time；不取最佳一次，也不跨 worker 混算。

### 3.1 s5cmd 文件批量复制时间

| workers | 工作区 | 保存 | 冷恢复 | 热恢复 |
|---:|---|---:|---:|---:|
| 8 | clone | 24.52 / 32.48 | 34.10 / 25.52 | 33.04 / 12.05 |
| 8 | unzip | 24.67 / 31.48 | 33.87 / 24.97 | 32.63 / 11.80 |
| 32 | clone | 6.78 / 17.79 | 9.48 / 14.84 | 9.59 / 12.88 |
| 32 | unzip | 6.89 / 17.64 | 9.40 / 14.64 | 9.34 / 12.98 |
| 64 | clone | 9.30 / 18.36 | 15.53 / 16.61 | 15.47 / 14.71 |
| 64 | unzip | 9.27 / 18.47 | 15.55 / 16.36 | 15.70 / 14.61 |
| 128 | clone | 5.78 / 18.59 | 9.18 / 16.79 | 9.45 / 14.76 |
| 128 | unzip | 5.67 / 18.59 | 9.23 / 16.60 | 9.28 / 14.78 |
| 256 | 两种 | **未完成** | **未完成** | **未完成** |

### 3.2 完整保存/恢复时间（含 manifest 与验证）

| workers | 工作区 | 保存 | 冷恢复 | 热恢复 |
|---:|---|---:|---:|---:|
| 8 | clone | 26.40 / 34.20 | 36.18 / 27.57 | 35.20 / 14.00 |
| 8 | unzip | 26.45 / 33.25 | 35.94 / 27.03 | 34.75 / 13.76 |
| 32 | clone | 8.14 / 19.11 | 11.01 / 16.39 | 11.23 / 14.32 |
| 32 | unzip | 8.20 / 18.94 | 10.94 / 16.15 | 10.88 / 14.41 |
| 64 | clone | 11.10 / 20.10 | 17.57 / 18.65 | 17.62 / 16.68 |
| 64 | unzip | 11.05 / 20.19 | 17.57 / 18.42 | 17.75 / 16.53 |
| 128 | clone | 7.16 / 19.94 | 10.71 / 18.27 | 11.07 / 16.21 |
| 128 | unzip | 6.99 / 19.95 | 10.81 / 18.05 | 10.81 / 16.21 |

自动报告与完整精度数据：

| 配置 | 原始 JSON | 自动 Markdown | 状态 |
|---|---|---|---|
| 8 | [JSON](../results/s5cmd-cloud-w8.json) | [表格](../results/s5cmd-cloud-w8.md) | 36/36 完成 |
| 32 | [JSON](../results/s5cmd-cloud-w32-complete.json) | [表格](../results/s5cmd-cloud-w32-complete.md) | 36/36 完成 |
| 64 | [JSON](../results/s5cmd-cloud-w64.json) | [表格](../results/s5cmd-cloud-w64.md) | 36/36 完成 |
| 128 | [JSON](../results/s5cmd-cloud-w128.json) | [表格](../results/s5cmd-cloud-w128.md) | 36/36 完成 |
| 256 | [失败 JSON](../results/s5cmd-cloud-w256.json) | [不完整表格](../results/s5cmd-cloud-w256.md) | 15 个完成单元，随后失败 |

上述 s5cmd 完整配置的总计为 **144 个测量单元、993,744 次普通文件复制**，不含 manifest、预热/隔离、失败配置及以下 boto3 历史记录。

### 3.3 历史补录：boto3 逐文件读写对照

**来源与状态：** 以下 boto3 数字来自本会话当时已经完成并独立核验的云端汇总记录。原始 JSON、自动 Markdown 和旧性能代码此前已按要求删除；本节仅恢复历史汇总，不伪造逐轮原始记录，也不声称本次重新执行或重新核验了已删除的 JSON。s5cmd 对照值则从目前保留的原始 JSON 重新计算。

当时同样使用本项目的单 Runtime、多 session、同一台网关和 S3 bucket；Runtime 为版本 **1**，镜像 digest 为 `sha256:aef84115df3765769fc5a1dd44a00553ce34679d9959d725a299bc1fc1bb09c2`，boto3/botocore 为 **1.43.87**。Django commit、文件数、文件字节数与上表相同；JuiceFS 为 1.4.1，SQLite/EBS，1 GiB cache，关闭 writeback。

boto3 直接对每个普通文件调用 `PutObject` / `GetObject`，使用 Python 线程池控制文件并发；不是每个文件启动一个进程，也不是整个目录一个 S3 请求。恢复包括 manifest、完整下载、本地写入、模式/链接恢复与哈希校验。历史表使用 **完整阶段 `wall_seconds`**，不能与 s5cmd 的 `file_batch_seconds` 混为同一口径。

#### 3.3.1 boto3 并发 8：三轮中位数

- 时间：2026-09-10 **01:44:37–02:00:44 UTC**。
- run ID：`run-c801a42d53e447679761e47f235b545b`。
- 36/36 测量单元完成；248,436 次普通文件操作、28/28 跨租户存储检查通过，两个 session 成功停止。以上为当时验收汇总；文件级错误为 0，成功文件操作记录到的 SDK 重试为 0，不代表所有控制面或后台请求均无重试。

| 工作区 | 操作 | boto3 原生 S3（秒） | boto3 JuiceFS Gateway（秒） |
|---|---|---:|---:|
| clone | 保存 | 27.9531 | 32.5147 |
| clone | 冷恢复 | 26.0089 | 25.9225 |
| clone | 热恢复 | 25.8089 | 12.6876 |
| unzip | 保存 | 27.5345 | 32.3437 |
| unzip | 冷恢复 | 26.2918 | 25.7790 |
| unzip | 热恢复 | 26.2633 | 12.8253 |

当时的结论是：在该 boto3 并发 8 配置下，网关热恢复约快 2.0×，冷恢复基本持平，写入耗时增加约 16%–17%。这是历史配置的观察，不是 JuiceFS 固有的加速倍率。

#### 3.3.2 boto3 单线程：仅一轮补充记录

- 时间：2026-09-10 **02:00:50–02:40:54 UTC**。
- run ID：`run-1a66deddda534948aeb309c02d0331c7`。
- 12/12 单元完成，82,812 次普通文件操作；当时汇总记录文件级错误为 0、28/28 隔离检查通过、两个 session 成功停止。下表是单次观察，不是三轮重复结果。

| 工作区 | 操作 | boto3 原生 S3（秒） | boto3 JuiceFS Gateway（秒） |
|---|---|---:|---:|
| clone | 保存 | 217.0717 | 226.7649 |
| clone | 冷恢复 | 242.8544 | 244.8774 |
| clone | 热恢复 | 251.6609 | 26.5572 |
| unzip | 保存 | 214.5286 | 227.5575 |
| unzip | 冷恢复 | 233.9109 | 233.2462 |
| unzip | 热恢复 | 239.2906 | 25.1318 |

本轮没有对应的 s5cmd 单 worker 串行测试；s5cmd 2.3.0 会将 `--numworkers 1` 提升为 2，不能拿它冒充相同串行配置。

#### 3.3.3 与 s5cmd 并排比较：统一使用完整 wall time

每格为 **原生 S3 / JuiceFS Gateway**，单位秒；三列均为三轮中位数。先看并发参数均为 8 的两列，再看本 demo 默认的 s5cmd 32 workers，避免把客户端变化与并发提升混为一谈。

| 工作区 | 操作 | boto3 8 线程（历史） | s5cmd 8 workers | s5cmd 32 workers |
|---|---|---:|---:|---:|
| clone | 保存 | 27.95 / 32.51 | 26.40 / 34.20 | 8.14 / 19.11 |
| clone | 冷恢复 | 26.01 / 25.92 | 36.18 / 27.57 | 11.01 / 16.39 |
| clone | 热恢复 | 25.81 / 12.69 | 35.20 / 14.00 | 11.23 / 14.32 |
| unzip | 保存 | 27.53 / 32.34 | 26.45 / 33.25 | 8.20 / 18.94 |
| unzip | 冷恢复 | 26.29 / 25.78 | 35.94 / 27.03 | 10.94 / 16.15 |
| unzip | 热恢复 | 26.26 / 12.83 | 34.75 / 13.76 | 10.88 / 14.41 |

s5cmd 两列来自当前保留的 [w8 JSON](../results/s5cmd-cloud-w8.json) 与 [w32 JSON](../results/s5cmd-cloud-w32-complete.json)，不是用旧文档中的四舍五入值再次推算。

**比较限制：** 这些不是同一 session 中随机交错的客户端 A/B 测试。boto3 与 s5cmd 使用不同镜像、不同 session、不同时间；8 线程和 8 workers 也不保证完全相同的 HTTP 并发。boto3 直接 GET，s5cmd 可能额外 HEAD/range；boto3 预热连接，s5cmd 每阶段启动新进程且计入启动/TLS；校验实现也不同（s5cmd 写入后重新扫描源快照，boto3 写入阶段没有这一步）。因此，表格只能对照当时的整体实现表现，不能将时间差全部归因于语言、客户端或文件系统。

可见 **s5cmd 并非在相同并发参数下所有操作都更快**：8 workers 的原生 S3 保存稍快，但恢复比历史 boto3 8 线程更慢；提升到 32 workers 后，原生 S3 的整体耗时明显降低。这支持继续调优传输并发，但不支持不带条件的“s5cmd 必然加速”结论。

## 4. 正确性、隔离和运行异常

四个完整配置均通过独立验收脚本：矩阵唯一且完整、s5cmd 退出 0、无错误记录、每个成功复制 source/destination/size 匹配、恢复 manifest/内容/目录模式/符号链接/Git HEAD 校验成功。每配置 **24 项跨租户 CLI 拒绝检查**和错误 session token 拒绝均通过。

验收与实际部署证据：[s5cmd-cloud-validation.json](../results/s5cmd-cloud-validation.json)，含四份成功报告的 SHA-256、网关/磁盘/Runtime 版本/镜像 digest/启动参数/源码 hash。失败组不伪装成通过验收。

本轮遇到的异常均保留说明：

1. **镜像更新接口**：GetAgentRuntime 返回的 `requireServiceS3Endpoint` 不能原样提交 UpdateAgentRuntime，产生 ValidationException。修复为仅提交原有 subnets/securityGroups 后更新成功，未创建新 Runtime。
2. **首次 w32 隔离验收**：AWS S3 DeleteObjects 返回增强型 IAM 拒绝说明，s5cmd 丢弃错误码后只保留 message，旧解析器未识别。原始操作被拒绝；补充精确拒绝文本匹配和回归测试后使用新 session 重跑。首次[失败记录](../results/s5cmd-cloud-w32.json)没有性能测量，排除统计。
3. **w256 未完成**：第二轮 clone 冷恢复的原生 S3 s5cmd batch 非零退出，记录 4 个错误、2,161 个成功复制，期望 6,905 个；非超时、非授权拒绝。聚合日志没有保留原始错误内容，**不能确定根因是资源、网络、CLI 或其他因素**。没有为取得成功而重跑该组，也没有将其部分结果混入三轮中位数。

四个成功配置共停止 8 个 session；w256 失败配置和最初 w32 隔离失败配置各停止 2 个，合计 **12 个测试 session 均收到成功 StopRuntimeSession 确认**。云端 Runtime/网关基础设施保留。

这些是本 demo 受限入口、固定数据和策略下的检查，不是生产完整安全认证；单独 HEAD 拒绝、所有 multipart 组合、匿名/管理端点和跨主机攻击不在此验收范围。

## 5. 解释边界

1. **第 3.1–3.2 节是 s5cmd 对两个后端的实测，第 3.3 节是另列的 boto3 历史对照。** 已删除的旧原始 JSON 和代码未恢复；历史汇总来自本会话记录，不混入 s5cmd 统计，跨客户端比较受第 3.3.3 节的口径差异限制。
2. **不是 POSIX 挂载性能**。clone/unzip 在本地目录执行，再按普通文件复制对象；没有在 JuiceFS FUSE 挂载点直接运行 git，也没有把整个目录压缩成一个对象。
3. **文件数不等于 HTTP 请求数**。s5cmd 下载可能先 HEAD 再 GET/range，multipart/分片行为受 CLI 和 Go SDK 实现控制；`--concurrency 1` 不是关闭分片。`--numworkers` 是 worker pool 参数，不是精确同时在途 HTTP 请求数。
4. **不同 worker 配置的 session 不同**。完整矩阵顺序执行，不保证宿主资源/调度完全相同；没有记录每个 microVM 的精确 CPU 分配。源码扫描基线也存在差异。因此配置间的非单调变化不能直接归因于 worker 参数，更不能断言某个数字是普遍最佳配置。可靠比较首先是同一 session 内原生 S3 与网关的配对结果。
5. **网关的资源和实现固定**。只有一台 m7g.large、SQLite 元数据和 1 GiB cache；没有系统采样证明瓶颈是 CPU、数据库、网络还是锁。结果不能代表 JuiceFS 的所有部署，更不能外推到生产多节点元数据/分布式缓存。
6. **校验与持久性**。复制日志需逐项匹配 source/destination/size，恢复后实际哈希和模式验证通过；但没有本地 fsync、强制断电或元数据灾备测试。关闭 writeback 防止用异步返回时间冒充持久保存，但不是完整灾备认证。
7. **样本和成本**。每配置三轮，取中位数；不是置信区间研究。未采集单文件 p95、后端请求数、缓存命中率和实际重试次数。retry-count=2 是配置，不是观测到两次重试。文件复制吞吐不等于账单吞吐。

不要仅因低 worker 时热缓存有优势就选择网关，也不要据此否定 JuiceFS 的文件系统价值。若当前需求只是应用自管小文件 checkpoint，应先用高效客户端调优直接 S3，再比较打包/增量保存和引入网关的运维成本。

## 6. 复现与保留资源

原有实验栈 `jfsbench-d6935b4639` 和单 Runtime 保留；本轮只更新镜像，没有新增第二个 Runtime。网关 `i-08523634aef9c3335`、S3 bucket `jfsbench-d6935b4639-databucket-wk0plgecvfxj` 及 VPC endpoint 保留。部署状态在 `build/juicefs-state.json`，不要删除。

复现一组（使用新的结果路径）：

```bash
python3 scripts/05-juicefs-demo.py run --workers 32 --repetitions 3 --out results/s5cmd-w32-rerun.json
python3 scripts/08-verify-benchmark-results.py results/s5cmd-w32-rerun.json --collect-cloud-evidence --out results/s5cmd-w32-rerun-validation.json
```

每次 run 结束尝试停止用户 session，但不停止 EC2 和 endpoint 计费。既有基础资源费用估算约 **$0.13/小时**，另加 S3/Runtime/日志/Secret/ECR 等用量费用；本轮没有账单归因，不能把此估算当作实付金额。

不再需要环境时可执行 `python3 scripts/05-juicefs-demo.py cleanup --destroy-demo`。此操作销毁 EBS 上 SQLite 元数据；需要保留文件系统时必须先导出并验证备份。脚本保留 S3/ECR/Secret/日志，后续还需显式处理残留存储费用。
