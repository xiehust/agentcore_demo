# s5cmd 性能与正确性测试说明

> 测试对象：同一个 AgentCore Runtime 内，通过 s5cmd 比较原生 S3 与 JuiceFS S3 Gateway。这里定义测试方法和验收，不预填性能倍率。

## 1. 固定数据与版本

- s5cmd：v2.3.0，官方 Linux ARM64 包，SHA-256 `1439f0d00ecedcd2a2f1f2c6749bbb0152b2257bf5086f29646ec8ae38798e24`。
- JuiceFS：1.4.1；TLS、共享卷、固定 1 GiB 读缓存、禁用 writeback。
- 仓库：[Django 5.2.6](https://github.com/django/django/tree/5.2.6)，commit `75c4403f07b8ad25893f7832dbe8fc6814b53b2d`。
- git-clone：镜像内 shallow bare 仓库，本地 clone，包含 `.git`，约 6,900 个普通文件。
- unzip：14.52 MiB ZIP 展开约 6,897 个普通文件、3,249 个目录、4 个符号链接，约 42.8 MiB；九成文件不超过 16 KiB。
- 固定 fixture 见 [准备记录](../results/juicefs-workspace-fixture.json)。不运行 Django 代码，不下载依赖，不调用模型。clone/unzip 只构建真实小文件工作集，其时间单独记录。

同一实验内冻结一次快照，两个后端使用同一文件集、内容和 manifest。不同 session 生成的 `.git/index`/reflog 可能不同，因此不可混用 manifest。

## 2. 矩阵和计时口径

推荐按 **8、32、64、128、256 workers** 分别执行，每个 worker 配置使用新 session、新 run prefix、新报告，先从默认 32 开始。资源不足时停止扩展并记录原因，不能静默降低并发。每配置默认三轮，两个后端顺序轮流交换。

s5cmd 的 `--numworkers` 控制文件/命令并发；`cp --concurrency 1` 固定单文件分片并发，并不关闭 multipart/range。v2.3.0 会把 `--numworkers 1` 提升为 2，因此本方案不提供“1 worker 串行”伪对照。默认分片大小 50 MiB，当前 fixture 普通文件最大值需以 manifest 为准；下载可能包含 HEAD 和 ranged GET，不能把文件数当 HTTP 请求数。

每个 workload 的阶段：

1. **persist-small-files**：一个 s5cmd batch 复制所有普通文件，核对成功输出，再校验源快照未变，单独复制 manifest。
2. **cold-first-pass**：重启网关并换新空 cache 目录；下载 manifest，批量复制到全新目录，校验文件内容及文件系统元数据。
3. **warm-repeat**：同一远端快照再次复制到另一个全新目录，不能跳过文件或复用本地恢复结果。

一个 worker 配置共 `2 workload × 2 backend × 3 phase × 3 repetition = 36` 个单元。冷 cache 仅是网关进程/磁盘缓存，不保证 AWS、SQLite、OS 缓存为空。每个阶段都启动全新的 s5cmd 进程；不在另一个进程中“预热连接”后宣称计时进程复用了连接。

| 指标 | 定义 |
|---|---|
| `file_batch_seconds` | s5cmd 子进程启动、TLS、文件复制、进程退出及 JSON 验收的时间；不含 manifest 和哈希校验 |
| `manifest_seconds` | 独立 manifest 复制时间 |
| `validation_seconds` | 文件/目录模式、链接、哈希和 Git HEAD 验证；写入阶段检查源快照 |
| `wall_seconds` | 上述步骤及目录创建/命令准备的完整耗时，不含初始 clone/unzip |
| `completed_files` | 与期望 source/destination/size 完全匹配的成功复制数 |
| `file_batch_mib_per_second` | 文件字节数除以 batch 时间，仅完整成功时计算 |

报告分别给 batch 和 wall 的轮次中位数，不生成不存在的“单文件 p95”或“SDK 重试次数”。CLI 仅记录配置的 retry-count=2；不能从退出码推断底层重试为零。失败、缺失和重复轮次不可用于计算加速比。

## 3. 正确性与隔离验收

### 文件树

- 退出码为 0、无 JSON/纯文本错误，成功记录逐项匹配期望 source、destination、size；拒绝空 batch、重复复制、漏文件和非预期路径。
- s5cmd 对零字节对象省略 JSON `size`，固定版本解析按 0 处理，并有单测。
- `.git`、点文件和字面通配符文件名必须保留；不跟随符号链接，链接存入可信 manifest 后单独恢复。
- 下载 manifest 必须匹配 session 内原始 hash；批量恢复必须落入新私有目录。保留执行位，目录显式 chmod；不以“规范化后的读数”掩盖实际权限错误。
- 最终扫描比较路径、类型、内容 SHA-256、大小、模式、链接；clone 额外检查 HEAD。两次恢复均复制所有文件。
- 文件 batch 超时由 subprocess 杀掉并回收子进程；阶段最终超时不得报告成功。校验和控制面不能保证硬实时终止，失败结果必须保留。单次阶段默认 720 秒；控制器预算 45 分钟。

### session 与租户

仍为一个 Runtime ARN、A/B 不同 session ID。两组租户凭证先分别写入已有 marker，然后交叉测试 s5cmd 的读取、列举、上传、删除和 COPY 源/目标拒绝，每后端双向共 12 项；两个后端共 **24 项**。测试后各自读回 marker，确认未被覆盖/删除。

s5cmd v2.3.0 的 `head` 错误可能被转换，故不把独立 HEAD 拒绝算入已验证项目。`rm` 使用 DeleteObjects，固定版本只保留 per-object message；验收要求非零退出且明确 `AccessDenied`，或仅对 rm 接受精确 `Access Denied[.]`，以及 AWS 返回的明确 `s3:DeleteObject` identity-policy 拒绝消息。404、连接失败、超时、InvalidAccessKeyId 不算通过。生产需补充直接服务日志和完整权限审计。

会话 token 串用、改变 tenant、缺少 context、凭证过期应拒绝。共享执行角色不得拥有数据/Secret/AssumeRole 权限。静态网关凭证仍是租户级，不提供同租户 session 级 S3 权限。

## 4. 执行命令与结果文件

先按 [Demo 方案](05-s5cmd-demo.md) 完成 fixture、本地 s5cmd、镜像构建以及已有 Runtime 的 update。

```bash
python3 -m pytest tests -q
python3 scripts/06-juicefs-local-smoke.py --workspace --out results/s5cmd-local-check.json

# 云端一组 worker 配置。之后分别改为 8/64/128/256 并使用不同结果路径。
python3 scripts/05-juicefs-demo.py run --workers 32 --repetitions 3 --out results/s5cmd-cloud-w32.json
python3 scripts/08-verify-benchmark-results.py results/s5cmd-cloud-w32.json --collect-cloud-evidence --out results/s5cmd-cloud-w32-validation.json
```

JSON schema 为 `s5cmd-workspace-v1`。自动生成同名 Markdown，保存逐轮聚合指标、文件清单 hash、session 路由、cache reset、错误和停止结果。验收脚本检查矩阵完整性和原始 JSON hash，并可采集实际部署镜像/网关证据。

不要在两个控制机同时运行同一网关的 cache-reset 测试；本地锁无法跨主机保护。run 完成只停止 session，云端网关等保留资源继续收费。

## 5. 验证状态与解释边界

本地单测 **108 项通过**，有一条来自 AgentCore SDK 的 Pydantic 弃用警告。已完成本地真实网关验证，见 [s5cmd-local-complete.json](../results/s5cmd-local-complete.json)：两租户共 12 项 CLI 跨租户拒绝检查通过，107 个包含点文件、空格和字面通配符的文件复制通过，Django clone/unzip 两棵完整目录保存与恢复成功。此前三次适配失败记录保留在 `results/s5cmd-local-smoke*.json` / `s5cmd-local-verified.json`，原因分别为 CLI 错误格式及空文件 size 字段处理，不计入通过结果。云端 Runtime 已更新为 s5cmd 镜像版本 3，8/32/64/128 workers 三轮矩阵完成并验收；256 workers 第二轮失败，不计算三轮倍率。详见 [实测报告](07-s5cmd-cloud-results.md)。旧 SDK 测试及倍率已从项目移除，不作为新结论。

本地网关使用文件存储后端而非 AWS S3，仅证明客户端兼容性与文件树校验，不能给出云端加速倍率。复用旧云资源时需先 update，新引擎检查可防止误跑旧镜像。

有界限制：最多 15,000 文件/目录条目、512 MiB 工作区、单文件 128 MiB。Django fixture 在边界内。不是通用恶意 ZIP 解压服务、不是完整备份产品、不支持跨 session 的生产恢复，也不证明 HA、断电持久性或高并发租户 QoS。

s5cmd 仍逐对象读写；与按 ZIP/tar 打包上传下载是不同方案。本轮只比较同一 CLI 在两个后端上的行为。
