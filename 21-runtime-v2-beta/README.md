# 21 · AgentCore Runtime V2（beta）账号可用性验证

验证当前账号是否已被放入 AgentCore Runtime V2 的 allowlist。V2 通过
`CreateAgentRuntime` / `UpdateAgentRuntime` 上新增的 `platformVersion="V2"`
参数启用；该字段只存在于私有的 botocore 模型（`Boto3CliV2Artifacts.zip`）里。

## 目录

| 文件 | 说明 |
| --- | --- |
| `check_v2.py` | 端到端检查脚本：模型字段 → 创建 V2 runtime → READY 并回显 `platformVersion` → 数据面 invoke → 清理 |
| `.venv/` | 已安装私有 boto3/botocore 1.43.87 的虚拟环境（不入库） |
| `Boto3CliV2Artifacts.zip`、`*.whl` | 私有 SDK 产物（不入库） |
| `v1_control_run.log`、`v2_us-east-2_run.log`、`v2_us-east-1_run.log` | 本次验证的原始输出 |

## 私有模型相对公开模型的差异

用 `.venv` 里的 `service-2.json` 与公开 botocore 1.43.87 做 diff，控制面只多了一个字段，数据面无差异：

```
CreateAgentRuntimeRequest  +platformVersion   (string, 1..128)
UpdateAgentRuntimeRequest  +platformVersion
GetAgentRuntimeResponse    +platformVersion
enum AgentRuntimeStatus / AgentRuntimeEndpointStatus  +DELETE_FAILED
```

服务端校验给出的合法取值：`platformVersion must be one of: V1, V2.`

## 运行

```bash
cd 21-runtime-v2-beta
.venv/bin/python check_v2.py                     # us-west-2，结束后删除 runtime
.venv/bin/python check_v2.py --region us-east-2  # 换区域
.venv/bin/python check_v2.py --keep              # 保留 runtime 供后续观察
.venv/bin/python check_v2.py --platform-version V1   # 对照组
```

默认复用 `10-runtime-coldstart` 留下的 ping-pong 镜像
`agentcore-coldstart-pingpong:500mb` 与执行角色 `AgentCoreColdstartRole`，
可用 `--image` / `--role` 覆盖。镜像必须与 `--region` 同区域。

## 最新复测（2026-09-11，账号 434444145045）

使用现有 `.venv` 私有 SDK 1.43.87，分别运行 `check_v2.py --region us-west-2` 和
`check_v2.py --region us-east-1`；未修改脚本、镜像或角色权限。

| 区域 | platformVersion | 结果 |
| --- | --- | --- |
| us-west-2 | V2 | **已开通，端到端通过**：创建成功，188s 后 READY，`GetAgentRuntime.platformVersion='V2'`；首次调用 HTTP 200 / `pong`（2.1s），第二次 HTTP 200（2.13s）；已提交临时 Runtime 删除请求，脚本退出码 0 |
| us-east-1 | V2 | **仍未开通**：创建被拒，`ValidationException: platformVersion is not enabled for this account.`；脚本退出码 1 |

本次结果仅代表当前账号在上述两个区域的可用性；未复测 us-east-2。
输出保存于 `v2_us-west-2_2026-09-11_run.log` 和 `v2_us-east-1_2026-09-11_run.log`。

## V2 冷启动测试（2026-09-11，us-west-2）

完整报告：[results/COLDSTART_V2_REPORT.zh.md](results/COLDSTART_V2_REPORT.zh.md)。
本轮为 **500mb / 1gb / 2gb × 并发 1 / 10 / 50 / 100 / 200 的 15 格多进程矩阵**，
每格一轮、尝试数等于并发。工作进程数为 `min(8, c)`（c1 是一个 spawn 子进程、一个样本），
各进程独立客户端与线程池，通过全局屏障释放。12 格新测；500mb c50/c100/c200 复用同日多进程证据。

表内为 **成功首调用 p50／单次值（ms）；成功数/尝试数**。N/A 表示无成功延迟，c1 不估计尾分位数。

| 镜像 | c1（单次） | c10 | c50 | c100 | c200 |
|---|---:|---:|---:|---:|---:|
| 500mb | 2615.7；1/1 | 2611.4；10/10 | 2533.2；50/50（复用） | 2429.9；99/100（复用） | 2561.1；182/200（复用） |
| 1gb | 2869.7；1/1 | 2499.2；10/10 | 2547.6；50/50 | 2413.7；100/100 | 2464.6；176/200 |
| 2gb | N/A；0/1 | 2572.6；5/10 | 2678.0；26/50 | 2627.4；80/100 | 2517.9；148/200 |

**正式 1083 次尝试：938 成功、145 次 HTTP 429，无其他首调用错误；938 次 warm 全部成功。**
限流均为 `New session creation rate exceeded`，对应账号共享的新 session 25 TPS 配额。
2gb/c1 紧接 1gb/c200，唯一请求被限流，未重试；不能归因于镜像本身。
并发格中仅三个 c10 达到 ≤100 ms 的 `before-send` 跨度目标，全部 c50 以上未达到；该 hook 不是线上发包时间。
这是新 session 首调用 E2E，不是已确认的 microVM 启动耗时；单轮、固定顺序、共享配额及跨时段复用限制了性能比较。

[总清单](results/coldstart_v2_matrix_2026-09-11/matrix.json)记录 12 格新测及 3 格复用来源。
含 15 个成功 smoke 共 1098 个唯一 session；953 次停止成功，145 次限流后停止返回未找到，15 个临时 Runtime 已确认不存在。
测量与清理完整，但非全部请求成功，运行器退出码 **1** 原样保留；独立核验通过。

```bash
# 离线核验，不调用 AWS
.venv/bin/python -B verify_coldstart_v2_matrix.py results/coldstart_v2_matrix_2026-09-11
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest test_matrix_multiprocess test_multiprocess_coldstart -v
```

实测入口为 `coldstart_v2_matrix.py`（新建 12 格、复用 3 格）；再次执行须明确授权云资源与费用，
输出目录必须位于本项目内且不存在，命令及证据说明见完整报告。

## V2 内存用量同期对照（2026-09-11，us-west-2）

完整报告：[results/MEMORY_V2_REPORT.zh.md](results/MEMORY_V2_REPORT.zh.md)。
复用 `../23-runtime-memory-usage/` 两份相同镜像和角色，对默认平台及明确返回
`platformVersion=V2` 的新 Runtime 各执行 5 个会话。

**V2 明显改善，但 RSS 与 AWS 内存遥测的巨大差额仍存在。** 小镜像空载约 22 MiB RSS，
V2 为 1.056 GB-equivalent，同期默认平台为 2.187。V2 未读大镜像的 guest 缓存不再大幅增加；
临时文件缓存增量由默认平台约 513 MiB 减为约 256 MiB，fadvise 后基本回收。
但首次读取镜像中 256 MiB 文件的阶段间隔在 V2 达到 64.920 秒，需单独关注。
这些是单次配对遥测结果，不是最终账单、确定的存储根因或普遍性能保证。

10 次调用和停止均成功；930 个应用样本、30 个阶段、720 条阶段内逐秒用量日志独立核验通过。
两个临时 V2 Runtime 已确认删除，原 Runtime 未修改；8 个日志组保留 7 天。
原始证据在私有且 git 忽略的 `.state-memory-v2/2026-09-11/`。
新增 `memory_v2.py` 为隔离复测入口，`test_memory_v2.py` 为离线测试，
`verify_memory_v2.py` 独立校验实验完整性，不能仅凭采集器 `complete` 接受结果。

```bash
.venv/bin/python -B -m unittest test_memory_v2 -v
.venv/bin/python -B verify_memory_v2.py .state-memory-v2/2026-09-11
```

## 历史验证结果（2026-09-08 至 2026-09-09，账号 434444145045）

| 区域 | platformVersion | 结果 |
| --- | --- | --- |
| us-west-2 | V2 | `ValidationException: platformVersion is not enabled for this account.` |
| us-west-2 | V1（对照） | 同样被拒，说明整个 `platformVersion` 参数受 allowlist 门控，与取值无关 |
| us-east-2 | V2 | `ValidationException: platformVersion is not enabled for this account.`（镜像与角色权限已就位，确认是 allowlist 拒绝） |
| us-east-1（2026-09-09） | V2 | `ValidationException: platformVersion is not enabled for this account.`（镜像与角色权限已就位，确认是 allowlist 拒绝） |

**当时结论：账号 434444145045 在 us-west-2 / us-east-2 / us-east-1 均尚未开通 Runtime V2**，需按 Chorus 文档第 10 节提交 allowlist 请求。当前状态以上方最新复测为准。

判定方式：只要 `CreateAgentRuntime(platformVersion=...)` 返回
`platformVersion is not enabled for this account`，账号就尚未放开 V2；
放开后脚本应打出 4 个 PASS 并在 `GetAgentRuntime` 中回显 `platformVersion: V2`。

## 注意

- 校验顺序是先查 ECR 镜像权限、再查 `platformVersion`。在没有镜像或角色没有该区域
  ECR 权限的区域探测，会先撞上 ECR 报错，看不到 allowlist 结论。
- 为在 us-east-2 探测，本次把镜像复制到了 us-east-2 的同名 ECR 仓库，并把
  `AgentCoreColdstartRole` 的 ECR/Logs 授权扩到 us-east-2（原策略备份见
  `/tmp/coldstart_policy_backup.json`）。
- 2026-09-09 为在 us-east-1 探测，同样新建了 us-east-1 的同名 ECR 仓库并推送镜像
  （digest 与 us-east-2 一致：`sha256:8fce75a8…`），角色的 ECR/Logs 授权再扩到 us-east-1
  （改前策略备份见 `/tmp/coldstart_policy_backup_useast2.json`）。
