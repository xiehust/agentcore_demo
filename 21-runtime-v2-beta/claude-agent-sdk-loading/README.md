# Claude Agent SDK on AgentCore Runtime V2 · 分阶段延迟探针

日期：2026-10-14（UTC），us-west-2，账号 434444145045。
Agent 用 Claude Agent SDK 0.2.166（自带 Claude Code CLI 2.1.299）+ Bedrock `global.anthropic.claude-haiku-5-5`，
部署为 `platformVersion="V2"` 的 Runtime，`GetAgentRuntime` 回显 `V2`。

## 结论

**V2 的新会话不冷启动容器，而是从创建 Runtime 时拍的快照恢复。** 因此"runtime 启动"分成两段：
创建/更新时的一次性构建（约 217 秒到 READY），以及每个新会话约 **1.9 秒**的快照恢复。

| 证据 | 观测值 |
|---|---|
| 容器在会话之前就存在 | 新会话请求到达时，进程已运行 140–900 秒；进程启动时间早于 READY |
| 多个会话是同一个进程的副本 | eager 串行 10 个会话：boot_id、`listening_ns`、CLI 启动时间戳各只有 **1 个**取值 |
| guest 被暂停过 | wall − monotonic 在启动与请求之间增大 68–830 秒，本地容器对照为 245 ns |
| 快照前丢弃了 page cache | 构建时 page cache 约 981 MB，会话中只剩约 201–315 MB |
| 容器 stdout 在构建期写入 | 日志流 `build-logs-<uuid>`，每个版本有 2 次完整启动（对应 2 份快照，会话会落到其中任一份） |

**Claude CLI 何时启动决定了冷请求的长尾。**

- **lazy**（首个请求时 `ClaudeSDKClient.connect()`）：CLI 二进制 265 MB，快照恢复后要从镜像存储重新读入
  约 134 MB、触发约 990 次主缺页。I/O 量固定，耗时却在 **0.9–48 秒**之间。两次部署后的头几个会话都很慢
  （v1 冒烟 42 秒；v2 前 3 个会话 19–48 秒），之后降到 1–3 秒；落在较少使用的第二份快照上的 3 个会话也都慢。
- **eager**（HTTP 开始监听后立即在后台启动 CLI）：CLI 在构建期启动（0.51–0.65 秒），随快照一起恢复，
  请求路径里没有 CLI 启动。代价是恢复后的首次 query 偏慢：TTFT **2.1–6.1 秒**，热请求约 0.85 秒。
- 冷请求端到端 p50 两者接近（串行 5.2 / 5.0 秒），**p90 差距大：lazy 35.6 秒，eager 8.4 秒**。
  需要稳定的首请求延迟时选 eager，实现与注意事项见[eager 与 lazy 的差异](#eager-与-lazy-的差异)。

同一会话的后续请求（复用同一 microVM 和同一 CLI 进程）两种模式一样：请求送达约 0.10 秒、TTFT 约 0.8–0.9 秒、
端到端约 **1.2–1.3 秒**。

## 时间线

```text
CreateAgentRuntime / UpdateAgentRuntime（每个版本一次，约 217 s 到 READY）
  +85 s   构建 microVM 内容器启动：进程 → import 0.85 s → HTTP 监听；约 1.1 s 后收到首个 /ping
          eager：此时启动 Claude CLI，0.51–0.65 s
  ...     快照（丢弃 page cache）
  +217 s  Runtime 与 DEFAULT endpoint READY

新会话首个请求
  client send ─(快照恢复+转发 ~1.9 s)→ /invocations ─┬─ lazy : CLI 启动 0.9–48 s → query ─(TTFT 0.9–2.8 s)→ 首 token ─(~0.35 s)→ 结束
                                                     └─ eager: ─────────────────→ query ─(TTFT 2.1–6.1 s)→ 首 token ─(~0.4 s)→ 结束
同一会话后续请求
  client send ─(~0.10 s)→ /invocations → query ─(TTFT ~0.85 s)→ 首 token → 结束（端到端 ~1.25 s）
```

## eager 与 lazy 的差异

两种模式的代码只差一行：要不要在服务启动时就调用 `ClaudeSDKClient.connect()`。
在 V2 上，这一行决定了 CLI 进程是否进入构建期快照。下表中的耗时出自本次实测（串行 / 并发 10）。

| 维度 | lazy | eager |
|---|---|---|
| CLI 启动时机 | 会话首个 `/invocations` 到达时 | 服务启动时在后台启动，即 V2 构建期、拍快照之前 |
| 快照里有什么 | Python 服务；CLI 二进制的 page cache 已被丢弃 | Python 服务 + 已完成 initialize 握手的 CLI 进程 |
| 首个请求的路径 | 快照恢复 → 启动 CLI（读入约 134 MB、约 990 次主缺页）→ query | 快照恢复 → query（CLI 已就绪，等待 0 ms） |
| CLI 启动耗时 | 在请求内：p50 1.7 / 2.6 s，p90 30.0 / 4.2 s，最大 48.0 / 13.2 s | 在构建期：0.51–0.65 s；请求内为 0 |
| 首次 query → 首 token | p50 1.2 / 1.1 s（全新进程） | p50 2.4 / 3.6 s（恢复出的进程首次响应慢） |
| 冷请求端到端 p50 / p90 | 5.2 / 35.6 s；6.1 / 8.3 s | 5.0 / 8.4 s；5.9 / 8.1 s |
| 部署或更新后的头几个会话 | 长尾明显（19–48 s） | 未见 20 s 以上长尾（部署后首个会话 9.0 s） |
| CLI 进程与 Claude `session_id` | 每个会话各自启动，`session_id` 各不相同 | 所有会话都是同一进程的快照副本，**`session_id` 相同** |
| 同一会话后续请求 | 约 1.3 s | 约 1.3 s |
| 适用场景 | 本地开发；不使用快照的平台；很少调用 agent 的路径 | V2 生产环境；需要稳定的首请求延迟 |

### eager 实现核心代码

下面是去掉探针后的最小实现。`app/main.py` 为了记录监听时刻，是在 uvicorn `Server.startup()` 之后启动 CLI；
这里改用 FastAPI `lifespan`，启动时机只早几毫秒，行为相同。这段代码已在本地容器中验证：
`/ping` 立即可用（1.4 ms），首个请求不含 CLI 启动（1.29 s），第二轮复用同一 CLI 进程并带上下文。
运行所需的环境变量（`CLAUDE_CODE_USE_BEDROCK=1`、可写的 `HOME`、`AWS_REGION`）见 `Dockerfile` 与 `deploy.py`。

```python
import asyncio
import json
import time
from contextlib import asynccontextmanager

import uvicorn
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

OPTIONS = ClaudeAgentOptions(
    model="global.anthropic.claude-haiku-5-5",
    system_prompt="You are a concise assistant.",
    tools=[],
    setting_sources=[],
    include_partial_messages=True,
)


class Agent:
    """每个 microVM 一个 Claude CLI 进程：启动一次，本会话所有轮次复用。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.turn_lock = asyncio.Lock()  # 一个 ClaudeSDKClient 同一时刻只处理一轮

    def start(self) -> asyncio.Task:
        failed = self._task is not None and self._task.done() and (
            self._task.cancelled() or self._task.exception() is not None)
        if self._task is None or failed:  # 首次启动，或上次启动失败后重试
            self._task = asyncio.create_task(self._connect())
        return self._task

    async def _connect(self) -> ClaudeSDKClient:
        client = ClaudeSDKClient(options=OPTIONS)
        await client.connect()  # 拉起 CLI 子进程并完成 initialize 握手
        return client

    async def client(self) -> ClaudeSDKClient:
        # 已就绪立即返回，仍在启动则等待；shield 避免请求断开时连带取消启动任务
        return await asyncio.shield(self.start())


agent = Agent()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    agent.start()  # eager：只调度不 await，CLI 在后台启动，/ping 不受阻塞
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/ping")
async def ping():
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/invocations")
async def invocations(request: Request):
    prompt = (await request.json())["prompt"]

    async def stream():
        async with agent.turn_lock:
            client = await agent.client()
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent) and msg.event.get("type") == "content_block_delta":
                    delta = msg.event["delta"]
                    if delta.get("type") == "text_delta":
                        yield f"data: {json.dumps({'text': delta['text']}, ensure_ascii=False)}\n\n"
                elif isinstance(msg, ResultMessage):
                    yield f"data: {json.dumps({'done': True, 'is_error': msg.is_error})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

改成 lazy 只需删掉 `lifespan` 里的 `agent.start()`。此时首个请求中的 `agent.client()` 才触发 `connect()`。

### eager 注意事项

- **不要在启动阶段 `await` CLI 启动。** 后台调度能让 `/ping` 立即响应。实测 CLI 在 HTTP 开始监听后 0.51–0.65 秒就绪，
  早于首个 `/ping`（约 1.1 秒）。AWS 没有公开快照与健康检查的先后关系；如果 CLI 初始化更重（挂了 MCP server、插件），
  可能来不及在快照前完成。那样恢复后会继续完成启动，首个请求等它完成即可，功能不受影响，只是延迟会计入请求（本次未测）。
- **快照前生成的状态会被所有会话共享。** 实测 eager 串行 10 个会话的 Claude `session_id` 只有 1 个取值，
  并发轮有 2 个（对应两份快照），两轮测试还出现了同一个 ID。外部持久化（对话记录、`session_store`、日志、计费）要按
  请求头 `x-amzn-bedrock-agentcore-runtime-session-id` 区分会话，不能用 Claude `session_id`。
  `ClaudeAgentOptions.session_id` 只在 CLI 启动时生效，eager 模式下这时会话还不存在，无法使用。
  启动阶段生成的随机值、令牌、临时文件名同理。
- **不要在启动阶段获取并缓存短期凭证。** 快照可能在构建后很久才被恢复（本次为构建后约 4.5–14 分钟），缓存的凭证可能已过期。
  本次 20 次 eager 冷请求的 Bedrock 调用全部成功，但未单独验证 CLI 的凭证获取时机。
- **eager 不能消除首次 query 的额外耗时。** 恢复后首次 query 仍比热请求慢 1.5–2.8 秒（p50）。
  构建期先发一次预热 query 是否有效，本次未测。

## 探针

所有服务端探针都是 microVM 上的 `time.time_ns()`，随响应最后一个 SSE `done` 事件返回，
同时以 JSON 行写入 stdout（CloudWatch runtime 日志组）。客户端记录发送、首字节、首 token、流结束时刻。

| 位置 | 探针 | 含义 |
|---|---|---|
| 进程（构建期） | `kernel_boot` / `proc_start` | `/proc/uptime`、`/proc/self/stat` 推算的内核与进程启动时刻 |
| | `module` → `sdk_import` → `imports` | `main.py` 第一行、`import claude_agent_sdk` 完成、全部 import 完成 |
| | `listening` / `first_ping` | uvicorn 开始接受连接（`Server.startup` 之后）；首个 `/ping` |
| | `wall_minus_mono`、`vm` | wall − monotonic（只在 guest 暂停时增大）；`Cached`、`pgmajfault` 等 |
| Agent | `connect_start` → `connect_done` | `ClaudeSDKClient.connect()`：拉起 CLI 子进程并完成 initialize 握手 |
| 请求 | `request_received` | `/invocations` 入口；随即先发出 `accepted` 事件，客户端首字节即送达时间 |
| | `agent_ready` / `query_sent` | 拿到可用 CLI；`client.query()` 写入完成 |
| | `cli_init_msg` / `message_start` / `first_token` | CLI 的 init 消息；模型流的 `message_start`；首个 `text_delta` |
| | `result` / `done_sent` | `ResultMessage`；`done` 事件发出 |

跨时钟阶段（客户端时刻与服务端时刻相减）在报告中标 `*`。客户端 chrony 偏差小于 1 µs；
120 次调用全部满足"客户端发出 ≤ 服务端收到 ≤ 客户端收到 accepted"，未发现时钟顺序违例。

## 结果

两轮测试，每轮每种模式 10 个全新 `runtimeSessionId`，每个会话 3 轮对话（第 1 轮冷，第 2–3 轮热），
结束后 `StopRuntimeSession`。**120 次调用全部成功，0 错误、0 限流。**

- `serial_c1`：串行，13:14:55–13:19:35，紧接 v2 部署 READY（13:14:45）之后开始。
- `burst_c10`：10 个会话由 barrier 同时发起，13:27:05–13:27:37。

### 新会话首个请求（ms，p50 / p90 / max，n=10）

| 阶段 | lazy 串行 | eager 串行 | lazy 并发10 | eager 并发10 |
|---|---:|---:|---:|---:|
| 发出 → 收到请求（快照恢复）* | 1982 / 2519 / 2790 | 1955 / 2244 / 2556 | 1931 / 2099 / 2286 | 1860 / 2171 / 2232 |
| 请求内 CLI 启动 `connect()` | 1714 / 30014 / 48004 | — | 2604 / 4215 / 13166 | — |
| query → CLI init 消息 | 34 / 289 / 363 | 935 / 2050 / 2943 | 28 / 93 / 472 | 1471 / 2730 / 2907 |
| query → 首 token | 1186 / 2295 / 2847 | 2390 / 5323 / 6136 | 1147 / 1289 / 1888 | 3641 / 5228 / 5437 |
| 首 token → ResultMessage | 353 / 410 / 506 | 409 / 552 / 571 | 348 / 563 / 1546 | 427 / 505 / 714 |
| 客户端：发出 → 首 token | 4770 / 35391 / 53575 | 4439 / 7851 / 8357 | 5684 / 7231 / 17360 | 5485 / 7457 / 7605 |
| 客户端：发出 → 流结束 | **5152 / 35560 / 54081** | **4986 / 8357 / 9012** | **6091 / 8313 / 17725** | **5919 / 8132 / 8155** |
| CLI 启动期间读入 page cache（MB） | 134 | — | 134 | — |
| CLI 启动期间主缺页次数 | 992 | — | 992 | — |

lazy 串行各会话的 CLI 启动耗时（按发起顺序，秒）：23.0、48.0、19.5、1.6、1.5、1.5、0.9、28.0、1.2、1.8。
两轮中共有 3 个 lazy 会话恢复自第二份快照（boot_id `6011a6b7`：串行第 1、8 个和并发轮 1 个），
CLI 启动分别为 23.0、28.0、13.2 秒，均为慢样本；其余并发轮会话为 2.3–3.2 秒。

### 同一会话后续请求（ms，p50 / p90 / max，n=20）

| 阶段 | lazy 串行 | eager 串行 | lazy 并发10 | eager 并发10 |
|---|---:|---:|---:|---:|
| 发出 → 收到请求 * | 102 / 114 / 204 | 109 / 219 / 409 | 104 / 110 / 160 | 97 / 114 / 146 |
| query → 首 token | 916 / 1004 / 1060 | 849 / 955 / 2247 | 782 / 923 / 1013 | 848 / 961 / 1020 |
| 客户端：发出 → 流结束 | 1302 / 1440 / 1491 | 1338 / 1613 / 2720 | 1238 / 1410 / 2526 | 1206 / 1337 / 1469 |

### 构建期（每个 Runtime 版本一次，v2）

每个版本在构建期完整启动了两次，形成快照 A（boot_id `39de4891`）和 B（`6011a6b7`），表内为 A / B。

| 阶段 | lazy | eager |
|---|---:|---:|
| 提交 UpdateAgentRuntime → 构建 VM 中 HTTP 监听（A） | 87 s | 84 s |
| 进程启动 → `main.py` 第一行（解释器） | 31 / 30 ms | 24 / 39 ms |
| `import claude_agent_sdk` | 612 / 770 ms | 642 / 811 ms |
| 全部 import | 809 / 1020 ms | 846 / 1071 ms |
| 进程启动 → HTTP 监听 | 852 / 1065 ms | 883 / 1126 ms |
| HTTP 监听 → 首个 `/ping` | 1187 / 963 ms | 1135 / 905 ms |
| eager：CLI `connect()`（page cache 全热，0 次主缺页） | — | 509 / 647 ms |
| HTTP 监听（A）→ READY | 132 s | 133 s |

本地 8 vCPU Docker 对照：CLI `connect()` 约 1.9–2.1 秒，热请求 TTFT 约 0.75–1.3 秒。

## 口径与局限

- 每格 10 个冷样本、单轮、单区域、单账号，p90 只描述本次样本，不是 SLA。模型延迟随 Bedrock 负载波动。
- "快照恢复"由上表证据推断，AWS 未公开 V2 实现细节。构建 VM 中 Python 启动时内核 uptime 已有 25–27 秒，
  且不同 Runtime 的构建 VM boot_id 相同，内核本身也来自共享的基础快照；"内核启动 → 进程启动"的 25 秒不可解释为本应用的启动耗时，未列入结论。
- lazy 慢启动集中在"部署后最先的几个会话"和"较少使用的快照 B"（3/3），只来自两次部署。原因推断为底层镜像存储
  按快照的缓存未热（I/O 量一致而耗时差 50 倍），未直接证实。并发 10 的那一轮距部署 12 分钟，最大值 13.2 秒（快照 B），
  没有复现 30 秒以上的长尾。
- eager 首次 query 偏慢（CLI init 消息 0.66–2.9 秒），推测与快照恢复后 CLI 内存按需换入、到 Bedrock 的连接与凭证首次建立有关，未拆分验证。
- Agent 刻意精简：自定义 system prompt、`tools=[]`、`setting_sources=[]`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`，
  输入约 400 token。启用内置工具或 MCP 会增加 CLI 初始化和首 token 时间。
- CLI 的 `duration_api_ms` 在会话内是累计值，未用作单轮指标；原始值保留在 `records.jsonl`。

## 运行

```bash
cd 21-runtime-v2-beta/claude-agent-sdk-loading
../.venv/bin/python deploy.py                                      # 构建 arm64 镜像、建角色、创建/更新两个 V2 Runtime
../.venv/bin/python bench.py --sessions 10 --out results/<dir>     # 串行
../.venv/bin/python bench.py --sessions 10 --concurrency 10 --out results/<dir>
../.venv/bin/python bench.py --report results/serial_c1            # 离线重算汇总，不调用 AWS
../.venv/bin/python -B -m unittest test_stages -v                  # 离线测试
```

`serial_c1/meta.json` 是运行后补写的（首版汇总写盘时 datetime 序列化失败，`records.jsonl` 已先落盘，未受影响），已在文件中注明。

## 文件

| 文件 | 说明 |
|---|---|
| `app/main.py` | 带探针的 Agent：FastAPI `/ping` + `/invocations`（SSE），`AGENT_START=lazy\|eager` |
| `Dockerfile`、`requirements.txt` | `python:3.13-slim` + 固定版本依赖；SDK wheel 自带 CLI，无需 Node.js |
| `deploy.py` | ECR、IAM 角色 `ClaudeSdkV2ProbeRole`、Runtime `casdk_v2_lazy` / `casdk_v2_eager`，写 `deployment.json` |
| `bench.py`、`stages.py` | 测试客户端；阶段计算为纯函数 |
| `test_stages.py` | 阶段计算与 SSE 解析的离线测试 |
| `cleanup.py` | 删除 Runtime、角色、ECR 仓库 |
| `results/serial_c1`、`results/burst_c10` | `records.jsonl` 原始数据、`summary.json`、`report.txt` |

## 资源与清理

仍在部署：Runtime `casdk_v2_lazy-gFFK95CiPF`、`casdk_v2_eager-axDR2vBa8N`（V2，version 2），
IAM 角色 `ClaudeSdkV2ProbeRole`，ECR 仓库 `claude-sdk-v2-probe`。空闲 Runtime 不产生会话费用，测试会话均已停止。

```bash
../.venv/bin/python cleanup.py          # 预览
../.venv/bin/python cleanup.py --yes    # 删除并确认
```
