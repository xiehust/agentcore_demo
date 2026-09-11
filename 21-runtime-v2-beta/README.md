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
参考 `../10-runtime-coldstart/`，复用三档镜像、相同并发与样本设计；通过
`coldstart_v2.py` 创建独立 V2 Runtime，直接调用原压测实现。

| 镜像 | c=1 p50 | c=5 p50 | c=10 p50 | c=50 p50 |
|---|---:|---:|---:|---:|
| 500mb | 2149.3 ms | 2280.8 ms | 2161.9 ms | 2985.3 ms |
| 1gb | 2482.8 ms | 2150.8 ms | 2212.0 ms | 2197.9 ms |
| 2gb | 2377.3 ms | 2220.0 ms | 2149.3 ms | 2191.1 ms |

**300/300 矩阵样本成功，无限流；warm 整体 p50 为 107.5 ms。** 相比 2026-07-09
历史基线，高并发 c=10/50 的首次调用 p50 降低 63.4%–84.0%，但低并发 c=1/5 的 p50
升高，warm 也更慢，并非全面加速。这里测量的是新 session 首次调用 E2E，不能从旧
`fresh_boots` 启发式证明 V2 底层是否发生了冷启动；跨日期比较的限制见报告。

原始数据、完整响应、V2 配置及清理记录在 `results/coldstart_2026-09-11/`。
303 个 session（含 smoke）均停止成功，3 个临时 Runtime 已确认删除。

```bash
# 独立核验本次结果，不调用 AWS
.venv/bin/python verify_coldstart.py results/coldstart_2026-09-11

# 重新实测：会创建临时云资源并产生费用，输出目录必须不存在
.venv/bin/python -u coldstart_v2.py --out results/coldstart_new_run
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
