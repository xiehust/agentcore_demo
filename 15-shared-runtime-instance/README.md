# 15 — Shared Runtime Session 多用户隔离 Demo

## TL;DR

本 demo 使用 `launchpad-agents:shared-runtime-v1`（基于 Claude Agent SDK），让多个
真实用户通过同一个 `runtimeSessionId` 复用一台 `c7g.large`（2 vCPU / 4 GiB）上的
Agent 容器，并用 `runtimeUserId`、独立工作区、独立 Claude session 和路径守卫做
应用层隔离。

### 容量结论

| 场景 | 主要瓶颈 | 推荐活跃 Agent 并发 | 已验证边界 | 不可用边界 |
|---|---|---:|---:|---:|
| 短程任务（数秒、无文件操作） | CPU 峰值、进程启动和排队延迟 | **12～16** | 24 可完成，但 CPU 平均 94.6%、最低可用内存 447 MB | 32 真并行：0/32 |
| 长程任务（5～10 分钟、持续文件操作） | 内存余量、进程长时间驻留、模型长尾 | **8～12** | 16 可完成，但最低可用内存仅 573 MB | 24 真并行：两次 0/24 |

### 读数方式

1. **短程 40 并发成功，不代表 40 个 Agent 能同时运行。** 当
   `MAX_PARALLEL_AGENTS=16` 时，40 个请求实际是 16 个执行、其余排队；全部成功，
   p90 约 24.4 秒。
2. **去掉排队保护后，短程 24 真并行已接近极限。** 24/24 虽成功，但 CPU 平均
   94.6%，最低可用内存仅 447 MB；32 真并行则 0/32，并导致 SSM 失联。
3. **长程任务首先受内存约束。** 16 并发可以完成，但最低可用内存只剩 573 MB；
   24 并发在两台不同实例上均无法完成第一阶段。
4. **同一个 shared session 不能依赖 ASG 横向扩容。** 相同 `runtimeSessionId`
   始终固定在单一实例；需要更高总容量时，应按用户分片到多个 shared session。
5. **当前安全配置保持 `MAX_PARALLEL_AGENTS=16`。** 如果业务以长程任务为主，
   建议进一步降到 8～12。

完整数据与分析见：

- `results/REPORT.md`：短程与长程任务统一测试报告；
- `results/SHORT_NOQUEUE_REPORT.md`：短程任务无排队容量测试；
- `results/LONGRUN_CAPACITY_REPORT.md`：长程任务容量和失败边界。

## 1. 背景与目标

AgentCore Runtime 按 `runtimeSessionId` 路由：相同 session ID 的请求会落到同一个
运行实例（EC2 Capacity Provider 模式下即同一台 EC2 上的同一个容器进程）。
默认"一个用户一个 session"会导致每个用户独占实例，成本高、冷启动频繁。

本 demo 把 session 当作"共享算力池"：

```text
user alice ─┐                                      ┌───────────────────────────────┐
user bob  ──┼── InvokeAgentRuntime ──────────────► │  EC2 (c7g.large, 托管实例)     │
user carol ─┘   runtimeSessionId = 固定共享值       │  ┌─────────────────────────┐  │
                runtimeUserId    = 真实用户 ID      │  │ Agent 容器 (共享 session) │  │
                payload.user_id  = 真实用户 ID      │  │  FastAPI (async, 并发)    │  │
                                                   │  │  per-user 工作区 + 锁     │  │
                                                   │  │  Claude Agent SDK        │  │
                                                   │  └─────────────────────────┘  │
                                                   │  /mnt/scratch (50G EBS)       │
                                                   └───────────────────────────────┘
```

## 2. 用户身份传递

优先级从高到低：

| 来源 | 说明 |
|---|---|
| Header `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` | 由 `InvokeAgentRuntime` 的 `runtimeUserId` 参数注入，服务端可信来源 |
| `payload.user_id` | JSON 请求体字段，作为兜底 |

user_id 必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，否则直接 400 拒绝。

> 注意：调用方使用 `runtimeUserId` 时，IAM 需要同时具备
> `bedrock-agentcore:InvokeAgentRuntime` 和 `bedrock-agentcore:InvokeAgentRuntimeForUser`。

## 3. 隔离设计（容器内，应用层）

| 层 | 机制 |
|---|---|
| 文件系统 | 每用户目录 `/mnt/scratch/users/<slug>-<sha256(user_id)[:12]>`，作为 Claude 的 `cwd`；目录名带哈希，防止 user_id 构造碰撞/注入 |
| 工具面 | `allowed_tools` 仅 `Read / Write / Edit / Glob / Grep / LS / TodoWrite`；显式禁用 `Bash / WebFetch / WebSearch / Task`——没有 shell 就没有绕过路径检查的通用出口 |
| 路径守卫 | `PreToolUse` hook 对每次工具调用做参数审查：所有路径参数 `realpath` 归一化后必须落在该用户工作区内，否则返回 `permissionDecision=deny`（`..` 穿越、绝对路径、symlink 逃逸都会被拒） |
| 会话记忆 | 每用户独立 Claude session（`resume=<该用户上次 session_id>`），存储在各自工作区 `.session_meta.json`；A 的对话历史对 B 不可见 |
| 并发 | 每用户 `asyncio.Lock`（同一用户串行，避免 resume 冲突），跨用户并行；全局信号量限制并发 Claude 进程数，保护 2 vCPU 实例 |
| Claude 配置 | 每个 Claude 子进程 `HOME` 指向该用户工作区，CLI 的 transcript/配置也天然按用户隔离 |

### 已知边界（生产化需要补齐）

- 这是**应用层**隔离：容器内所有用户共享同一 OS 用户。若被授予 Bash 或存在
  SDK 逃逸漏洞，隔离即失效。注意：AgentCore 会为每个 `runtimeSessionId` 分配
  独立的 microVM/EC2（当前 EC2 Capacity Provider 模式下即独立 EC2），**不会**
  把多个 session 装箱到同一台实例——因此"每用户独立 session"意味着每用户独占
  一台 EC2，隔离最强但丧失共享算力池的成本优势。若要在同一实例内继续共享，
  只能在容器内叠加加固：每用户独立 UID 降权、或 nsjail/bubblewrap 等沙箱。
- `runtimeUserId` 的可信度取决于调用方（通常是你的 API 网关/BFF 层做完认证后注入）。
  终端用户绝不能直接持有 InvokeAgentRuntime 权限，否则可伪造任意 user_id。
- 同一 session 的并发调用行为以实测为准（见 `results/`）：若服务端对同 session
  串行化，客户端会自动降级为重试，多用户仍复用同一实例，只是排队执行。

## 4. 目录结构

```text
15-shared-runtime-instance/
├── README.md                 # 本文档
├── pyproject.toml            # 容器内依赖 (uv)
├── docker/Dockerfile         # linux/arm64, Node22 + claude-code CLI + uv
├── app/
│   ├── isolation.py          # 纯函数：user_id 校验 / 工作区推导 / 路径守卫（可单测）
│   └── server.py             # FastAPI: POST /invocations (SSE), GET /ping
├── tests/test_isolation.py   # 单元测试（仅标准库）
├── scripts/
│   ├── deploy.sh             # 构建镜像 → 推 ECR → create-agent-runtime → 等 READY
│   ├── invoke_multiuser.py   # 多用户并发测试客户端（共享 session）
│   ├── load_test.py          # 短任务并发爬坡 + EC2 资源采样
│   ├── load_test_longrun.py  # 5～10 分钟 Web 项目长任务并发爬坡
│   └── cleanup.sh            # 删除 runtime
└── results/                  # 汇总报告（原始 JSON/日志为本地测试产物，不提交）
```

## 5. 请求/响应协议

请求（`POST /invocations`）：

```json
{"prompt": "...", "user_id": "alice", "reset": false}
```

响应为 SSE 流，逐行 `data: {...}`：

```json
{"event": "delta", "text": "..."}
{"event": "tool", "name": "Write", "input": {"file_path": "..."}}
{"event": "denied", "reason": "path outside workspace"}
{"event": "complete", "result": "...", "user_id": "alice",
 "workspace": "/mnt/scratch/users/alice-xxxx", "claude_session_id": "...",
 "instance": {"boot_id": "...", "server_run_id": "...", "pid": 123, "hostname": "..."}}
```

`instance.boot_id`（宿主机内核 boot id）+ `server_run_id`（服务进程启动时生成的
UUID）用于向客户端证明多个用户确实命中了同一台 EC2 上的同一个容器进程。

## 6. 部署与测试

```bash
cd 15-shared-runtime-instance
python3 -m unittest discover -s tests -v     # 本地单测
bash scripts/deploy.sh                       # 构建/推送/创建 runtime
python3 scripts/invoke_multiuser.py          # 多用户并发验证
LEVELS='[2,4,8]' python3 scripts/load_test.py
LEVELS='[2,4,8]' python3 scripts/load_test_longrun.py
bash scripts/cleanup.sh                      # 清理
```

测试客户端验证四件事：

1. **同实例复用**：3 个用户并发调用，所有响应的 `boot_id` 与 `server_run_id` 一致；
2. **写入隔离**：每个用户写入自己的 `secret.txt`（唯一 token），各自读回正确；
3. **越权拒绝**：bob 试图用相对/绝对路径读 alice 的 secret，被 PreToolUse 守卫拒绝；
4. **记忆隔离**：alice 追问"我刚才写的 token 是什么"能答对，bob 问同样问题得到的
   是 bob 自己的 token。

### 长程并发 Load Test

`load_test_longrun.py` 中每个虚拟用户会连续完成两阶段任务：

1. 创建 4 个文件的离线项目看板主体；
2. 在同一 Claude session 中增加 about 页面，做全量 QA，并写入带唯一
   run token 的 `loadtest.json`。

不同用户并发执行，同一用户的两个阶段串行并验证 session resume。默认任务目标为
每用户约 5～10 分钟。脚本通过 SSM 在 EC2 宿主机上逐项检查 6 个文件、关键引用、
响应式断点、XSS 安全写法和 run token，分别输出 Agent 自报成功率和产物验证成功率。
每轮 user id 都包含随机 run id，不会复用旧工作区。

常用参数：

```bash
# 单用户 smoke
LEVELS='[1]' python3 scripts/load_test_longrun.py

# 并发爬坡；任一级端到端成功率低于 75% 时停止
LEVELS='[2,4,8]' SUCCESS_FLOOR=0.75 python3 scripts/load_test_longrun.py

# 单阶段最长响应等待时间（秒），默认 1200
TASK_READ_TIMEOUT_S=1800 LEVELS='[4]' python3 scripts/load_test_longrun.py
```

结果写入 `results/load_test_longrun_<UTC 时间>.json`，包含每阶段耗时、tool call 数、
session 续接状态、精确文件校验结果，以及测试窗口内的 CPU、内存、load average 和
Agent 进程数。

详细报告入口见本文开头的 TL;DR。
