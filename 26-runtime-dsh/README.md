# DeepSeek Harness（DSH）Agent 在 AgentCore Runtime 上的冷启动测试

把 [DeepSeek Harness](https://www.deepseek.com/en/harness/)（npm `@deepseek-ai/dsh`，`dsh headless` 一次性模式）
原样装进容器，部署到 Amazon Bedrock AgentCore Runtime，测量两件事：**冷启动**（新 session 的平台启动耗时）
和**第一条消息**（新 session 上第一次真实对话要等多久）。测试对象是平台版本 V2（每个新 session 从快照恢复），
与同一镜像的本地 Docker 对比，并验证 page pin 优化（示例代码见下文）。模型为 Bedrock 上的 DeepSeek V3.2
（`deepseek.v3.2`）。

不在范围内的两件事：同一 session 上的后续消息（主要反映 DSH 和模型，而不是 session 的启动），以及平台版本 V1
（两个 V1 runtime 测完后已删除并从部署脚本中移除，其原始数据保留在 `results/raw/`，但本文和报告都不讨论）。

完整数据见 [results/REPORT.zh.md](results/REPORT.zh.md) · [English](results/REPORT.md)。

## 核心结论（2026-11-12，us-east-1，平台 V2，4 个 runtime，1,600 个请求，0 失败）

p50，毫秒；合并并发 1 / 10 / 30 的样本。

| 指标 | 本地 Docker | V2 · 原样 DSH | V2 + page pin |
|---|---|---|---|
| 冷启动：新 session 的 ping（只有平台） | 598 | 2,014 | 1,938 |
| 第一条消息端到端 | 4,413 | 6,437 | 4,678 |
| 其中 DSH 进程启动（spawn → 首个输出，不含模型） | 2,588 | 3,552 | 1,999 |
| 第一条消息：新版本上线后同一快照的第 1 次使用 | — | 61,068 | 15,202 |

「冷启动」是新 session 的首次调用只返回时间戳，不跑 agent；「第一条消息」是把一次真实的 `dsh headless` 对话作为
首次调用，计时到完整回答返回（包含一次模型调用，提示词很短）。本地 Docker 一列的冷启动是 `docker run` 到
`/ping` 健康，第一条消息是容器启动加首次回答。
V2 两列的「DSH 进程启动」取同一快照被用过 10 次之后的样本；「第 1 次使用」每列只有 2 个样本。

1. **每个新 session 都从快照恢复，冷启动稳定在约 2.0 秒，与并发无关**：并发 1、10、30 的 p50 分别是
   2,014、1,995、2,027 毫秒。日志里每个 runtime 只在创建时启动过两次容器，之后的 session 都来自这两个快照。
2. **原样的 DSH 有一个大坑：新版本上线后的头几个 session，第一条消息要 40–60 秒。**
   时间全在 `dsh` 进程启动上（约 52 秒）。它随同一快照被使用的次数下降
   （第 1 次 61 秒，第 2 次 44 秒，第 3 次 28 秒，第 4 次 15 秒，第 5 次 12 秒），第 6 次起稳定在约 6.4 秒，
   与 runtime 存在多久无关。稳定之后 DSH 启动仍要约 3.5 秒，比本地 Docker 的约 2.6 秒慢。
3. **page pin 解决了大部分问题**：同一镜像、同时部署的一组对照，pin 之后第 1 次使用从 64.4 秒降到 15.2 秒，
   第 3 次起约 4.9 秒；稳定后第一条消息约 4.7 秒（对照 6.5 秒），DSH 启动降到约 2.0 秒，比本地 Docker 还快。
   代价是监听前多花约 0.1 秒、锁住 147 MB 内存（153,966,238 字节）直到首个回合结束。每个快照最初一两次使用仍要约 15 秒。
4. **`DSH_PREWARM=1`（容器启动后先跑一次 `dsh headless --help`）没有可测量的收益。**
   首次使用的 DSH 启动仍是约 53 秒。

结论：在 V2 上部署 DSH 应当带上 page pin。不带时，每次发布新版本后的头几个 session 会遇到 40–60 秒的第一条消息。

## V2 上为什么慢，page pin 做了什么

V2 在容器第一次 `/ping` 返回 200 时打快照，之后每个新 session 从快照恢复、不再启动容器。
`agentcore-e-layer-best-practices` 的解释是：打快照前页缓存会被清空，首个请求要执行的程序文件不在快照里，
恢复后要从按需加载的快照盘上冷读。`dsh headless` 每个回合新起一个 Node 进程，一个回合要打开
3,630 个文件（146.8 MB，其中 Node 二进制 118 MB），正好撞上这一点。只把文件读进页缓存的预热因此无效。

page pin（[`app/pinner.py`](app/pinner.py)，改写自该知识库 §6.4 的做法）：

1. `scripts/gen_pin_manifest.sh` 在成品镜像里用 strace 追踪一次真实回合，生成
   [`app/pin-manifest.txt`](app/pin-manifest.txt)（按首次打开顺序，按 inode 去重）。
2. `DSH_PREWARM=3` 时，包装器在监听 8080 **之前**启动 pinner：对清单里每个文件先读入，再
   `mmap(MAP_SHARED | MAP_POPULATE)` 并一直持有。被活进程映射的页清不掉，会随内存进入快照。
3. 首个 agent 回合结束后包装器 kill 掉 pinner。
4. 另外，包装器在启动阶段先走一遍 AWS SDK 凭证链以加载其模块，但丢弃取到的凭证；凭证在每个请求里重新获取
   （启动阶段取到的值会被写进快照、被所有 session 共用，而且可能已过期）。

实测 pinner 在 AgentCore 里 82–113 毫秒锁住 3,629 个文件、147 MB（1 个文件被跳过）。上限默认 1 GiB
（`PIN_MAX_BYTES`），期限 60 秒（`PIN_DEADLINE_S`）；V2 要求容器 120 秒内报告健康。

局限：

- pin 只保得住文件内容，保不住目录项和 inode（知识库的说法）；实测每个快照最初一两次使用的 DSH 启动仍要 6–9 秒。
- 清单只覆盖一次 `Reply with exactly: pong` 回合读到的文件；用到 git、ripgrep 等工具的回合会读到清单之外的文件。
- 清单依赖基础镜像和 DSH 版本，升级任一个都要重新生成。
- 「打快照前清空页缓存」来自知识库的实测结论，AWS 的
  [runtime-v2-optimize](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)
  文档（2026-11-12 核对）没有写这一点，也没有提 page pin；本测试的对照结果与它一致。

### 示例代码

下面的代码与仓库文件一致（[`app/pinner.py`](app/pinner.py)、[`app/server.mjs`](app/server.mjs)）；
测试报告在生成时也会从这两个文件复制同样的内容。

**1. pinner（`app/pinner.py`，完整文件）** — 读入清单里的每个文件并 `mmap` 住，打印一行 JSON 结果，然后一直持有映射：

```python
"""Page pinner for AgentCore Runtime V2 (snapshot start).

V2 snapshots the container after its first healthy /ping, and the page cache is
dropped before the snapshot is taken. Files the first request needs are then
read cold from the on-demand snapshot disk. Pages that a live process has
mapped cannot be dropped, so this process maps every file in the manifest and
blocks; the pages go into the snapshot with it.

Protocol with the wrapper: print exactly one JSON line on stdout, then hold the
mappings until stdin closes or the process is killed.

Adapted from the page-pin recipe in agentcore-e-layer-best-practices (§6.4);
the byte cap is raised because DSH reads far more than that recipe's example.
"""
import json
import mmap
import os
import sys
import time

MANIFEST = sys.argv[1] if len(sys.argv) > 1 else "/opt/dsh/pin-manifest.txt"
DEADLINE_S = float(os.environ.get("PIN_DEADLINE_S", "60"))
MAX_BYTES = int(os.environ.get("PIN_MAX_BYTES", str(1024 << 20)))
CHUNK = 1 << 20


def pin(paths, deadline):
    maps, total, skipped, buf = [], 0, 0, bytearray(CHUNK)  # one reused buffer
    seen = set()
    for path in paths:
        if time.monotonic() >= deadline:
            skipped += 1
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            skipped += 1
            continue
        try:
            st = os.fstat(fd)
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            if st.st_size == 0 or total + st.st_size > MAX_BYTES:  # never pin part of a file
                skipped += 1
                continue
            off = 0
            while off < st.st_size:
                n = os.preadv(fd, [buf], off)  # read into the page cache first
                if n == 0:
                    break
                off += n
            maps.append(mmap.mmap(fd, off, prot=mmap.PROT_READ,
                                  flags=mmap.MAP_SHARED | mmap.MAP_POPULATE))
            seen.add(key)
            total += off
        except (OSError, ValueError):
            skipped += 1
        finally:
            os.close(fd)
    return maps, total, skipped


def main():
    started = time.monotonic()
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    except OSError:
        paths = []
    maps, total, skipped = pin(paths, started + DEADLINE_S)
    print(json.dumps({"files": len(maps), "bytes": total, "skipped": skipped, "listed": len(paths),
                      "ms": round((time.monotonic() - started) * 1000), "max_bytes": MAX_BYTES}), flush=True)
    sys.stdin.buffer.read()  # hold the mappings until the wrapper lets go


if __name__ == "__main__":
    main()
```

**2. 包装器如何使用它（`app/server.mjs` 节选）** — 监听之前启动并等它完成，第一次 agent 对话结束后释放；
pin 失败只记录，不阻止服务启动：

```javascript
// Page pin (level 3). pinner.py reads and mmaps every file in the manifest,
// prints one JSON line, then blocks holding the mappings. Pages mapped by a
// live process survive the page-cache drop that precedes the V2 snapshot, so a
// restored session finds DSH's files in memory instead of reading them cold.
// Best effort: any failure is logged and the server starts anyway.
let pinner = null;
function startPinner() {
  prewarm.start_ts = now();
  return new Promise((resolve) => {
    let settled = false;
    let out = '';
    const finish = (detail) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      prewarm.detail = detail;
      prewarm.end_ts = now();
      resolve();
    };
    const timer = setTimeout(() => finish({ error: `no result within ${PIN_WAIT_MS} ms` }), PIN_WAIT_MS);
    try {
      pinner = spawn('python3', [path.join(APP_DIR, 'pinner.py'), PIN_MANIFEST], { stdio: ['pipe', 'pipe', 'inherit'] });
    } catch (err) {
      return finish({ error: `spawn failed: ${err.code || err.name}` });
    }
    pinner.on('error', (err) => { pinner = null; finish({ error: `spawn failed: ${err.code || err.name}` }); });
    pinner.on('close', (code) => { pinner = null; prewarm.exit_code = code; finish({ error: `pinner exited with ${code}` }); });
    pinner.stdout.on('data', (chunk) => {
      out += chunk.toString('utf8');
      const idx = out.indexOf('\n');
      if (idx < 0) return;
      try { finish(JSON.parse(out.slice(0, idx))); } catch { finish({ error: 'pinner printed something other than its result line' }); }
    });
  });
}
// Called after the first turn: the files are in the page cache now, so the
// mappings are no longer needed.
function releasePinner() {
  if (!pinner) return;
  pinner.kill('SIGKILL');
  pinner = null;
  prewarm.released_ts = now();
}
```

调用位置：

```javascript
// 启动阶段，server.listen() 之前
if (PREWARM_LEVEL === 3) await startPinner();

// /invocations 处理函数里，第一次 agent 对话结束后
try {
  agent = await runAgent(payload.prompt);
} finally {
  releasePinner();
}
```

**3. 镜像（`docker/Dockerfile`）** — 装上 `python3`，并把 pinner 和清单放进镜像（放在最后一层，重新生成清单只重建这一层）：

```dockerfile
COPY app/pinner.py app/pin-manifest.txt /opt/dsh/
```

**4. 生成清单并启用**

```bash
TAG=dsh-0.2.2-rc.1-pin bash scripts/build_image.sh       # 先构建一次镜像
bash scripts/gen_pin_manifest.sh                         # 在镜像里用 strace 追踪一次真实对话 -> app/pin-manifest.txt
TAG=dsh-0.2.2-rc.1-pin bash scripts/build_image.sh       # 带上清单重新构建
DEPLOY_KEYS="dsh_pin_v2" bash scripts/deploy.sh          # 该 runtime 的环境变量为 DSH_PREWARM=3
```

可调参数（环境变量）：`PIN_MANIFEST`（清单路径）、`PIN_MAX_BYTES`（pin 的总字节上限）、`PIN_DEADLINE_S`
（pinner 自身的期限）、`PIN_WAIT_MS`（包装器等待 pinner 的上限）。

## 架构

```
coldstart_test.py ──InvokeAgentRuntime(全新 40 字符 sessionId, SigV4)──▶ AgentCore Runtime
  (boto3, 关闭重试,                                                      └─ 每 session 一个微VM
   threading.Barrier 同步并发)                                               └─ app/server.mjs (Node, :8080)
                                                                               ├─ {"mode":"ping"}  → 只返回时间戳
scripts/local_baseline.py ──HTTP 127.0.0.1──▶ 本地 Docker 中的同一镜像         └─ {"mode":"agent"} → spawn `dsh headless --json`
                                                                                    └─ Bedrock Converse: deepseek.v3.2
```

包装器返回 `proc_start_ts`、`request_ts`、`response_ts` 以及 DSH 的 `spawn_ts` / `first_output_ts` /
`first_text_ts` / `exit_ts`，客户端据此把第一条消息拆成平台部分和 DSH 进程启动（两者都不含模型时间），并确认每个探测
都是**快照恢复**（包装器进程早于请求启动，且许多 session 共用同一个进程启动时间）。

| key | runtime 名称 | 平台 | 镜像 tag | `DSH_PREWARM` | 说明 |
|---|---|---|---|---|---|
| `dsh_v2` | `dsh_coldstart_base_v2` | V2 | `dsh-0.2.2-rc.1` | 0 | 原样 DSH |
| `dsh_prewarm_v2` | `dsh_coldstart_prewarm_v2` | V2 | `dsh-0.2.2-rc.1` | 1 | 启动后先跑一次 `dsh headless --help` |
| `dsh_pinctl_v2` | `dsh_coldstart_diag_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 0 | page pin 的对照组 |
| `dsh_pin_v2` | `dsh_coldstart_deepwarm_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 3 | page pin |

最后两个 runtime 沿用了最初创建时的名称，名称与现在的用途不对应。`-pin` 镜像比基础镜像多了 pinner、
清单和 python3（1,089 MB，ECR 压缩后 291 MB）；ping / agent 的处理逻辑相同。

## 运行

前置条件：支持 ARM64 的 Docker、[uv](https://docs.astral.sh/uv/)、Python ≥ 3.11，以及具备 ECR、IAM、
`bedrock-agentcore*` 权限且已开通 `deepseek.v3.2` 的 AWS 凭证。脚本固定使用 `us-east-1`
（可用 `REGION=...` 覆盖），不受 shell 里 `AWS_REGION` 的影响。

```bash
bash scripts/build_image.sh                              # 基础镜像（约 1.5 分钟）
TAG=dsh-0.2.2-rc.1-pin bash scripts/build_image.sh       # pin 镜像
bash scripts/gen_pin_manifest.sh                         # 追踪一个回合 -> app/pin-manifest.txt
TAG=dsh-0.2.2-rc.1-pin bash scripts/build_image.sh       # 带上清单重新构建
bash scripts/test_local.sh                               # 本地合约测试：/ping、ping、两次 agent 对话
V2="dsh_v2 dsh_prewarm_v2 dsh_pinctl_v2 dsh_pin_v2"      # 本文范围内的 4 个 runtime
DEPLOY_KEYS="$V2" bash scripts/deploy.sh                 # ECR + IAM 角色 + 这 4 个 runtime -> deployments.json（幂等）
DEPLOY_KEYS="dsh_pin_v2" bash scripts/deploy.sh          # 只创建/更新指定 runtime，不打扰其他的
uv sync
RT="${V2// /,}"                                          # coldstart_test.py 的 --runtimes 用逗号分隔
uv run python coldstart_test.py --smoke --runtimes "$RT" # 每个 runtime 各一次 ping 序列 + agent 序列
python3 scripts/local_baseline.py --rounds 5             # 本地 Docker 基线 -> results/local_baseline.json
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 1
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 10 --rounds 3 --settle-seconds 60
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 30 --rounds 2 --settle-seconds 0
uv run python scripts/gen_report.py --platforms V2       # 生成 results/REPORT.md 与 REPORT.zh.md（只含 V2）
uv run python -m unittest discover -s tests              # 客户端与报告生成器的离线测试
```

逐请求原始数据在 `results/raw/*.json`，聚合统计在 `results/summary.json`。
`results/raw/` 里还留有两个已删除的 V1 runtime（`dsh`、`dsh_prewarm`）的数据，不带 `--platforms V2`
生成报告会把它们也写进去。
runtime 每次创建或更新都会重新打快照，「同一快照的第几次使用」从头计数；创建或更新约需 3.5 分钟。

测试客户端是单进程多线程：同一轮的请求由 `threading.Barrier` 一起放行，并发 30 时各请求开始计时的时间
相差 p50 26.9 毫秒、最大 33.5 毫秒。`--processes auto` 可改用多个 worker 进程（并发 100 以上建议开启），
本次测试没有使用。

## 值得了解的事实

- DSH 的 headless profile 不读 `settings.yaml`，模型要通过 profile 的 `cordis.patch.yml` 配置
  （覆盖 `llm-pi-ai` 与 `agent-default-model` 两个条目）；包装器在启动时根据 `DSH_MODEL_ID` 和
  `AWS_REGION` 写入该文件。
- DSH 的 Bedrock 路由只在环境变量里看到凭证线索（`AWS_ACCESS_KEY_ID`、`AWS_PROFILE`、
  `AWS_CONTAINER_CREDENTIALS_*` 等）时才签名。实测 AgentCore 微VM里没有这些变量（全部 agent 对话的
  凭证来源都是 `sdk-default-chain`），所以包装器先用 AWS SDK 默认凭证链解析执行角色凭证，再以环境变量
  传给 `dsh` 子进程（凭证不记录、不返回）。直接在 AgentCore 里跑 `dsh headless` 而不做这一步，会缺凭证。
- 一次 `Reply with exactly: pong` 对话的输入约 8,361 token（DSH 系统提示词），输出 3 token。
- 基础镜像未压缩 1,054 MB，其中 DSH 的 `node_modules` 约 740 MB、69,477 个文件；压缩后 279 MB。
  一次对话实际只读其中 3,613 个文件、约 25 MB。
- 本地 Docker 的页缓存已热、无需拉镜像，只有 5 轮，是下限而不是同条件对比。
- 矩阵之外的冒烟探测里有 1 次失败：`dsh_prewarm_v2` 在一次 68.6 秒的首回合之后，第二回合返回 502，原因未查明。

## 安全

- Runtime 使用默认入站认证（SigV4 / IAM）；未签名的调用实测返回 `403 Missing Authentication Token`。
- 容器内监听 `0.0.0.0:8080` 是 AgentCore 的要求，不对外发布端口；本地测试只发布到 `127.0.0.1`，
  包装器默认绑定 `127.0.0.1`。
- 执行角色只能拉取本仓库镜像、写自己的日志、调用 `deepseek.v3.2` 一个模型；信任策略限定本账号本区域的
  AgentCore 资源。ECR 仓库为私有，镜像和环境变量中没有任何密钥。
- 任何能通过 IAM 调用该 runtime 的主体都能让 DSH 在微VM里执行工具（包括 shell），请勿把它开放给不可信调用方。

## 清理

Runtime 空闲时不计费（按微VM活跃秒数计费，session 空闲 60 秒后回收），ECR 镜像有少量存储费用。

```bash
bash scripts/cleanup.sh --dry-run   # 列出将删除的资源
bash scripts/cleanup.sh --yes       # 删除 4 个 runtime、ECR 仓库、IAM 角色
```
