# AgentCore 默认 microVM 共享 Runtime Session 多用户隔离与并发实测报告

- 报告日期：2026-08-14（UTC）
- 测试日期：2026-08-14（UTC）
- AWS 账号：`434444145045`
- 区域：`us-west-2`
- 项目：`16-shared-runtime-microvm/`
- 测试对象：AgentCore 默认 Runtime 计算模式；未配置 Capacity Provider
- 模型：`us.anthropic.claude-sonnet-4-6`
- 云端部署和计费调用：**已实际执行**

## 结论摘要

本轮在真实 AgentCore Runtime 上完成了隔离 smoke、短程 `2/4/8` 并发和长程 `1/2/4/8` 并发测试。测试不是对 `15-shared-runtime-instance/` 中 EC2 Capacity Provider 数据的转述；本报告的所有数值均来自本目录下 2026-08-14 生成的 JSON。

主要结论如下：

1. **同一 Runtime Session 内的应用层多用户隔离通过。** 最终 smoke 的 26/26 项检查全部通过：alice、bob、carol 使用同一个 `runtimeSessionId`，命中同一个完整服务进程指纹，但拥有 3 个独立工作区和 3 条独立 Claude 会话链。相对路径和绝对路径两种跨工作区探测都实际触发 `PreToolUse` deny，`denied_count=1`，且没有泄露其他用户 token。
2. **短程 2、4、8 真并发全部成功。** 共 14/14 个请求通过精确 marker、工作区唯一性和单服务进程检查。8 并发 p50/p90/max 为 6.790/7.410/7.885 秒；聚合 CPU 采样峰值达到 100%，但最低可用内存仍为 6507 MB。
3. **长程 1、2、4、8 真并发全部端到端成功。** 共 15/15 个用户、30 个连续阶段通过 Agent marker、resume chain、工作区/进程一致性和命令侧六文件确定性验证，即 90 个最终文件均通过内容契约。8 并发 p50/p90/max 为 368.6/557.4/587.8 秒，最低可用内存 5971 MB。
4. **8 是本轮配置和测试上限，不是测出的硬容量上限。** 服务端 `MAX_PARALLEL_AGENTS=8`，因此本轮没有测试 8 个以上 Agent 同时驻留，也没有观察到首个失败档位。不能据此宣称 microVM 最大只支持 8，或安全支持任意高于 8 的并发。
5. **操作建议：延迟敏感场景先用 4 个活跃 Agent 槽位；吞吐优先且能接受约 9 分钟 p90 时可用本轮已验证的 8。** 4 到 8 长程并发成功率没有下降，但 p90 从 370.6 秒升至 557.4 秒，工具调用均值从 31.0 升至 35.1。资源没有逼近内存耗尽，因此长尾不能简单归因于 microVM 内存；Agent 行为和模型响应差异也是明显变量。
6. **共享 session 不适合互不信任的租户。** 这里的用户共享一个 FastAPI 进程域、OS 用户和 Runtime 凭证。独立目录、工具白名单和路径 hook 是应用层控制，不是用户之间的 microVM/内核隔离边界。

## 1. 最终部署配置

删除前控制面快照保存于 `results/runtime_metadata_before_cleanup_20260814.json`。

| 项目 | 实测值 |
|---|---|
| Runtime 名称 | `shared_runtime_microvm` |
| Runtime ID | `shared_runtime_microvm-KkqELu3xji` |
| Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/shared_runtime_microvm-KkqELu3xji` |
| 最终版本 | `3` |
| DEFAULT endpoint | `READY`，`liveVersion=3` |
| 网络 | `PUBLIC` |
| idle timeout | 900 秒 |
| max lifetime | 28800 秒（8 小时） |
| `MAX_PARALLEL_AGENTS` | 8 |
| `MAX_TURNS` | 64 |
| 用户根目录 | `/tmp/agentcore-users` |
| Capacity Provider | 未配置 |
| 外部文件系统 | 未配置 |
| 镜像 | `launchpad-agents:shared-runtime-microvm-v1` |
| 镜像 digest | `sha256:64a5d04da16537ecbb97a8313a8e338edea2c8c151c02272b8b027f871ae96a4` |
| 镜像大小 | 266,682,540 bytes |

运行内探测保存于 `results/runtime_probe_20260814.json`：

- `os.cpu_count()` 返回 **2**；
- `/proc/meminfo` 的 `MemTotal` 为 **8,209,720 kB**，约 7.83 GiB；
- `cpu.max` 为 `max 100000`，`memory.max` 和 `pids.max` 均为 `max`；
- 探测命令和应用返回相同 boot ID；
- 探测 session 最后通过 `StopRuntimeSession` 返回 HTTP 200。

这些是容器内观测值。由于 cgroup 没有暴露数值型 CPU、内存或 PID 上限，本报告不把 `/proc` 可见总量解释为服务端承诺的硬配额。

## 2. 拓扑与测试方法

```text
alice ─┐  runtimeUserId=alice                    一个活跃 Runtime Session
bob   ─┼─ InvokeAgentRuntime ─────────────────► 默认 AgentCore Runtime
carol ─┘  runtimeSessionId=<同一个随机 ID>        ├─ FastAPI PID 1
                                                  ├─ /tmp/agentcore-users/*
operator ─ InvokeAgentRuntimeCommand ────────────► ├─ /proc + cgroup v2 采样
                                                  └─ 六文件确定性 verifier
```

每个测试脚本创建一个新的随机 shared session；同一档的虚拟用户通过 barrier 同时释放。每个用户携带不同的 `runtimeUserId` 和 payload `user_id`，服务端要求两者匹配，并把用户映射到哈希后缀的 `0700` 工作区。

隔离机制包括：

- 每用户独立工作区、`HOME`、Claude session ID 和 `asyncio.Lock`；
- 不同用户可以并行，全局 semaphore 限制同时运行的 Agent 数；
- 只允许文件类工具，禁止 Bash、Web 和 Task 工具；
- `PreToolUse` hook 对文件路径、Glob、`..`、绝对路径和 symlink 逃逸做守卫；
- Agent 执行必须使用支持双向控制协议的 `ClaudeSDKClient`，否则 Python function hooks 不会参与工具调用控制。

命令侧监控在同一个 active session 内每约 2 秒读取 `/proc/stat`、`/proc/meminfo`、`/proc/loadavg`、Agent 进程名和 cgroup v2。短程共采到 15 个样本；最终成功的三次长程运行共采到 821 个样本。所有最终运行的 `monitor_errors` 都为空，monitor cleanup 均成功。

命令 API 的 HTTP 200 本身不算成功。客户端还检查响应 session ID、`contentStart`/`contentDelta`/`contentStop` 顺序、stream exception、`COMPLETED` 状态和 exit code 0。所有测试都在 `finally` 中调用 `StopRuntimeSession`。

## 3. 实测中发现并修复的问题

这次云端执行发现了本地 mock 无法证明的五类集成问题。

| 问题 | 真实表现 | 修复和回归保护 |
|---|---|---|
| 创建 Runtime 缺少网络配置 | 首次 `CreateAgentRuntime` 被控制面拒绝，没有留下半成品 Runtime | create/update 都显式传入 `networkMode=PUBLIC`；新增 `test_deploy_contract.py`，并禁止意外加入 Capacity Provider |
| command 不隐式解释 shell | `printf ... \| base64 -d \| bash` 被当普通 argv，stdout 只是 base64 文本，导致首轮 smoke JSON 解析失败 | `run_shell_script()` 显式生成 `/bin/bash -c <pipeline>`，原脚本继续使用 base64 运输；新增运输回归测试 |
| 一次性 `query()` 不执行 Python hooks | 越权请求没有泄露，但 `denied_count=0`，说明没有验证到真实 hook deny | 服务端改用 `ClaudeSDKClient` 的双向控制协议；加入确定性 `[PATH-GUARD-INTEGRATION-PROBE]`，最终两类探测均 `denied_count=1` |
| 长程 marker 判定过严 | Agent 完成文件后在 marker 前输出摘要，流程误判 phase 1 失败 | 长程只要求结果包含 marker；短程精确 marker 仍保持严格相等；最终正确性继续由 verifier 决定 |
| prompt 与 verifier 的 `innerHTML` 契约不一致 | 第二次单用户运行 Agent 两阶段成功，但 `app.js` 的注释仍出现该字面量，verifier 拒绝 | foundation 和 final-QA prompt 都要求代码及注释完全不出现 `innerHTML` 字面量 |

最终镜像对应 Runtime version 3。`app/server.py` 中的 `ClaudeSDKClient` 和 `scripts/runtime_session.py` 中显式 `/bin/bash -c` 都是实测所需行为，不能退回旧实现。

## 4. 多用户隔离 smoke

最终数据：`results/multiuser_20260814T160656Z.json`

| 检查 | 结果 |
|---|---|
| 总检查数 | 26/26 通过 |
| warmup | 成功，14.216 秒 |
| command/app 同环境 | boot ID 和 hostname 均一致 |
| 服务进程一致性 | 全程 9/9 指纹完整，distinct fingerprint=1 |
| 用户工作区 | 3 个用户，3 个独立工作区 |
| 初始写入/读回 | alice、bob、carol 全部成功 |
| Claude 会话 | 三个初始 session ID 互不相同 |
| 相对路径越权探测 | 调用成功、无泄露、`denied_count=1` |
| 绝对路径越权探测 | 调用成功、无泄露、`denied_count=1` |
| 记忆召回 | 三个用户都只召回自己的 token |
| resume chain | 三个用户均恢复各自上一条 Claude session |
| 清理 | `StopRuntimeSession` HTTP 200，返回 session ID 匹配 |

该结果证明了当前工具集合和应用路径控制下的隔离行为；它不证明同 session 内存在 OS 级租户隔离。如果未来开放 Bash、自定义 MCP 工具或其他能够绕开文件 hook 的能力，必须重新做威胁建模和越权测试。

## 5. 短程并发结果

原始数据：`results/load_test_20260814T160811Z.json`

短程提示要求每个用户精确返回自己的唯一 marker，不读写长程项目文件。三个档位都是真并行，因为请求并发没有超过 `MAX_PARALLEL_AGENTS=8`。

| 并发 | 成功 | p50 | p90 | 最大延迟 | 独立工作区 | 服务进程数 |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 2/2 | 4.147 s | 4.747 s | 4.897 s | 2 | 1 |
| 4 | 4/4 | 4.596 s | 5.855 s | 6.392 s | 4 | 1 |
| 8 | 8/8 | 6.790 s | 7.410 s | 7.885 s | 8 | 1 |

资源窗口：

| 并发 | 样本 | CPU 平均/峰值 | 内存已用峰值 | 最低可用内存 | load1 峰值 | cgroup current 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 3 | 14.1% / 31.1% | 736 MB | 7281 MB | 0.39 | 349 MB | 2 |
| 4 | 4 | 21.9% / 63.4% | 1060 MB | 6958 MB | 0.33 | 675 MB | 4 |
| 8 | 4 | 45.8% / 100.0% | 1511 MB | 6507 MB | 0.98 | 1148 MB | 9 |

短程 8 并发时 CPU 窗口出现 100% 峰值，p50 比 4 并发增加约 48%，说明短突发首先表现为 CPU 竞争和启动重叠，而不是内存压力。Agent 进程采样峰值 9 不代表有 9 个稳定执行槽；进程退出和下一进程启动可能在采样点短暂重叠。应用侧仍记录到一个 FastAPI 服务进程和 8 个独立用户工作区。

## 6. 长程两阶段并发结果

成功数据：

- 1 并发：`results/load_test_longrun_20260814T162033Z.json`
- 2/4 并发：`results/load_test_longrun_20260814T162540Z.json`
- 8 并发：`results/load_test_longrun_20260814T163857Z.json`

每个用户先执行 foundation，再恢复同一 Claude session 执行 final-QA。最终 verifier 要求 `webapp/` 只包含以下六个普通文件：

```text
index.html  about.html  styles.css  app.js  README.md  loadtest.json
```

除文件集合外，verifier 还检查文件大小、引用关系、v2.0 footer、480/768px 断点、`localStorage`、`textContent`、`innerHTML` 禁令、唯一 run token 和 manifest 精确值。

| 并发 | Agent 成功 | 产物成功 | 端到端成功 | p50 | p90 | 最大延迟 | 平均工具调用 | 工作区/服务进程 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1/1 | 1/1 | 1/1 | 273.9 s | 273.9 s | 273.9 s | 25.0 | 1 / 1 |
| 2 | 2/2 | 2/2 | 2/2 | 343.7 s | 376.8 s | 385.1 s | 25.5 | 2 / 1 |
| 4 | 4/4 | 4/4 | 4/4 | 344.6 s | 370.6 s | 378.2 s | 31.0 | 4 / 1 |
| 8 | 8/8 | 8/8 | 8/8 | 368.6 s | 557.4 s | 587.8 s | 35.1 | 8 / 1 |

1 并发只有一个样本，其 p50/p90/max 相同，不应当作延迟分布。8 并发中最慢用户耗时 587.8 秒、48 次工具调用；另有用户在 final-QA 阶段耗时 380.5 秒。相比之下，最快用户为 287.6 秒、28 次工具调用。这个跨度表明长尾同时受到 Agent 迭代次数和模型响应路径影响。

长程资源窗口：

| 并发 | 样本 | CPU 平均/峰值 | 内存已用峰值 | 最低可用内存 | load1 峰值 | cgroup current 峰值 | Agent 进程峰值 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 138 | 1.3% / 18.1% | 658 MB | 7360 MB | 0.53 | 297 MB | 1 |
| 2 | 194 | 2.0% / 29.7% | 889 MB | 7128 MB | 0.34 | 500 MB | 2 |
| 4 | 190 | 3.9% / 49.0% | 1284 MB | 6733 MB | 0.15 | 923 MB | 5 |
| 8 | 295 | 5.4% / 86.4% | 2046 MB | 5971 MB | 0.75 | 1662 MB | 8 |

长程 CPU 平均值低，是因为任务大部分时间等待模型和工具往返，不能解读为 CPU 对并发没有影响。随着并发从 1 增至 8，cgroup 当前内存峰值从约 297 MB 增至 1662 MB，趋势清晰，但 8 并发仍保留约 5.97 GB 可用内存。本轮没有出现 OOM、API 错误、monitor error、artifact error 或 session cleanup error。

## 7. 单用户长程诊断与契约收敛

最终扩档前实际执行了三次单用户长程运行：

| 数据文件 | Agent | 产物 | 端到端 | 结论 |
|---|---:|---:|---:|---|
| `load_test_longrun_20260814T160911Z.json` | 0/1 | 0/1 | 0/1 | foundation 已生成 4 文件和 14 次工具调用，但摘要位于 marker 前；过严 marker 判定阻止 phase 2 |
| `load_test_longrun_20260814T161339Z.json` | 1/1 | 0/1 | 0/1 | 两阶段成功，唯一 verifier 错误是 `app.js` 出现 `innerHTML` 字面量 |
| `load_test_longrun_20260814T162033Z.json` | 1/1 | 1/1 | 1/1 | prompt 与判定契约一致后全部通过，总耗时 273.9 秒 |

前两次没有被包装成成功结果，也没有用脚本进程 exit code 0 代替 workload 成功。三份 JSON 的 monitor cleanup 和 session cleanup 都成功，`StopRuntimeSession` 均返回 HTTP 200。

隔离 smoke 也保留了诊断证据：

- `multiuser_20260814T160000Z.json`：command 管道未被 shell 执行，解析失败；session 已停止；
- `multiuser_20260814T160116Z.json`、`multiuser_20260814T160338Z.json`：无 token 泄露，但两种探测的 `denied_count=0`，未满足真实 hook deny 合约；session 均已停止；
- `multiuser_20260814T160656Z.json`：切换 `ClaudeSDKClient` 后 26/26 通过。

## 8. 容量建议与证据边界

### 已被本轮数据支持的结论

- 在当前镜像、Sonnet 4.6、2 个可见逻辑 CPU、约 7.83 GiB 可见内存和 `MAX_PARALLEL_AGENTS=8` 下，短程和长程 8 真并发各完成一轮，成功率均为 100%。
- 4 并发长程 p90 为 370.6 秒，资源余量充足，适合作为更保守的默认活跃槽位。
- 8 并发长程没有资源故障，但 p90 为 557.4 秒；它适合吞吐优先、能接受更大尾延迟的场景。
- 同一 shared session 内的所有用户命中一个服务进程；增加外部请求数不等于把一个 session 自动拆到多个隔离单元。

### 尚未被证明的结论

- **没有测出硬失败边界。** 8 是 semaphore 上限，9+ 真并行未测试。
- 每个档位只有一次最终成功运行，没有重复样本、小时级 soak、突发反复或故障注入。
- 没有测试不同模型、不同 prompt、网络波动、区域差异或 Runtime 平台升级后的表现。
- 没有测试不可信用户、恶意工具输入或新增 Bash/MCP 工具后的隔离。
- CPU、内存数据来自 session 内 `/proc` 和 cgroup，不是宿主机级监控；cgroup 上限显示为 `max`。

因此建议把 **4** 作为初始生产活跃 Agent 槽位，把 **8** 作为本轮已验证但需要接受长尾的上限配置。若要提高到 12 或 16，必须先提高 `MAX_PARALLEL_AGENTS`，从 9/12 等较小档位重新爬坡，保持确定性产物验证和每档 session cleanup，并重复接近首个失败点的测试。

## 9. 与 `15-shared-runtime-instance/` 的方法差异

目录 15 测试的是显式 EC2 Capacity Provider；本目录测试的是未配置 Capacity Provider 的默认 Runtime。两者工作负载形状可以对照，但容量数值不能混用。

| 维度 | 本目录：默认 microVM Runtime | 目录 15：EC2 Capacity Provider |
|---|---|---|
| 计算拓扑 | AgentCore 默认 Runtime，无 Capacity Provider 字段 | 显式 c7g/m7g 实例、ASG/Capacity Provider |
| session 内诊断 | `InvokeAgentRuntimeCommand` | SSM 进入承载 EC2 |
| 资源视角 | session 容器内 `/proc` 和 cgroup v2 | EC2 宿主机级 CPU、内存、进程 |
| 文件系统 | `/tmp/agentcore-users`，session 生命周期内临时数据 | `/mnt/scratch/users`，实例挂载 scratch 卷 |
| 产物验证 | command API 在同一 session 内执行 verifier | SSM 从实例侧验证 |
| 横向容量 | 本轮只验证一个 shared session，未涉及 ASG | 可研究实例池，但一个 session 仍固定到一个实例 |
| 清理 | 每轮 `StopRuntimeSession`，最终删除 Runtime | 还涉及 Runtime、Capacity Provider、ASG/EC2 生命周期 |

本报告没有复制目录 15 的 c7g/m7g 延迟、内存边界或并发建议。即使本轮容器内也看到 2 个逻辑 CPU 和接近 8 GiB 内存，也不能把它等同于 `m7g.large`：管理边界、监控范围、文件系统、调度和生命周期都不同。

## 10. 原始证据清单

最终成功数据：

- `results/multiuser_20260814T160656Z.json`
- `results/load_test_20260814T160811Z.json`
- `results/load_test_longrun_20260814T162033Z.json`
- `results/load_test_longrun_20260814T162540Z.json`
- `results/load_test_longrun_20260814T163857Z.json`
- `results/runtime_probe_20260814.json`
- `results/runtime_metadata_before_cleanup_20260814.json`
- `results/runtime_cleanup_20260814.json`

诊断数据：

- `results/multiuser_20260814T160000Z.json`
- `results/multiuser_20260814T160116Z.json`
- `results/multiuser_20260814T160338Z.json`
- `results/load_test_longrun_20260814T160911Z.json`
- `results/load_test_longrun_20260814T161339Z.json`

控制台与部署日志：

- `results/deploy_20260814.log`
- `results/smoke_console_20260814.log`
- `results/short_console_20260814.log`
- `results/long1_console_20260814.log`
- `results/long24_console_20260814.log`
- `results/long8_console_20260814.log`

## 11. 质量验证与资源清理

最终质量门在删除 Runtime 前执行，结果如下：

- `uv lock --check`、`uv sync --frozen`：通过，解析 42 个 package；
- `python3 -m compileall -q app scripts tests`：通过；
- `python -m unittest discover -s tests -v`：**45/45 通过**；
- Ruff check、Ruff format check：通过；
- Pyright：0 errors、0 warnings；
- 两个 shell 脚本的 `bash -n`：通过；
- 三个测试 CLI 的 `--help`：通过；
- `import app.server`：通过；
- `git diff --check`：通过。

清理步骤和终态：

1. 所有 smoke、短程、长程、诊断和运行环境探测 session 的 JSON 均记录 `StopRuntimeSession` 成功，HTTP 200 且返回 session ID 匹配。
2. 2026-08-14 16:54:32 UTC 执行 `bash scripts/cleanup.sh`，专用 Runtime 进入 `DELETING`；脚本删除本地 `runtime.json`，默认没有删除 ECR image 或 IAM role。
3. 持续轮询控制面，2026-08-14 16:58:06 UTC 的 `GetAgentRuntime` 返回 `ResourceNotFoundException`。
4. 最终 `ListAgentRuntimes` 对该 ID 的过滤结果为 `[]`，确认 Runtime 已不在控制面列表中。
5. ECR `launchpad-agents:shared-runtime-microvm-v1` 仍为 `ACTIVE`，digest 仍是 `sha256:64a5d04da16537ecbb97a8313a8e338edea2c8c151c02272b8b027f871ae96a4`。
6. 未修改现有 IAM、VPC、Capacity Provider、ASG、目录 15 Runtime 或其他 Runtime。

删除证据保存于 `results/runtime_cleanup_20260814.json`。至此专用测试 Runtime 和所有已知测试 session 均已停止，不再保留本轮 Runtime 计算资源；按既定策略只保留可复用 ECR 镜像。
