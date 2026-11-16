# DeepSeek Harness（DSH）Agent 在 AgentCore Runtime 上的冷启动测试

把 [DeepSeek Harness](https://www.deepseek.com/en/harness/)（npm `@deepseek-ai/dsh`，`dsh headless` 一次性模式）
原样装进容器，部署到 Amazon Bedrock AgentCore Runtime 平台版本 V2（每个新 session 从快照恢复），测量**新 session 的
第一条消息要等多久**，并比较几种缩短它的做法：拷进 `/dev/shm`、[build warmup lifecycle hook](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-hooks.html)、page pin，
以及它们的组合。模型为 Bedrock 上的 DeepSeek V3.2（`deepseek.v3.2`），区域 us-east-1。

不在范围内：同一 session 上的后续消息（主要反映 DSH 和模型），以及平台版本 V1（原始数据留在 `results/raw/`，不讨论）。

## 结论

**推荐 `/dev/shm` + lifecycle hook。** 下表是 11-16 第二轮里 5 种做法同时测的结果，DSH 启动
（spawn → 首个输出，不含模型），p50：

| | 不优化 | page pin | lifecycle hook | /dev/shm（只放 JS） | **/dev/shm + hook** |
|---|---|---|---|---|---|
| 新版本第 1 次使用 | 48.1 秒 | 7.4 秒 | 5.0 秒 | 11.7 秒 | **3.1 秒** |
| 稳定态（同一快照用过 10 次后） | 3.4 秒 | **2.0 秒** | 3.8 秒 | **1.9 秒** | 2.5 秒 |
| 稳定态第一条消息端到端（含模型） | 6.2 秒 | 4.9 秒 | 6.6 秒 | **4.6 秒** | 5.3 秒 |
| 代价 | — | 常驻 pinner，锁住 147 MB 直到首个回合结束 | 每次创建/更新最多多 120 秒 | 每个 session 在内存盘里持有 25 MB | 两者之和 |

- **/dev/shm + hook**：第 1 次使用 3.1 秒，之后每次 2.5–2.9 秒，没有「刚上线很慢」的阶段。`/dev/shm` 让 DSH 的 JS 文件
  随快照在内存里恢复，hook 预读仍留在镜像上的 Node 二进制和原生插件，两者各管一半。
- **只用 /dev/shm**：稳定态最好（1.9 秒），但第 1 次使用要 12 秒（Node 二进制放不进 AgentCore 的 `/dev/shm`）。
- **只用 hook**：只解决「刚上线」，稳定态与不优化相同。
- **page pin**：四轮里三轮稳定态 2.0 秒、一轮 4.1 秒（配置相同，原因未查明），依赖没有文档的平台行为，不作首选。
  不要与 hook 同时用（pin 的稳定态收益会消失）。

怎么选：发布频繁、流量小，在意新版本上线后头几个用户 → **/dev/shm + hook**；只看大流量下的稳定态 → **只用 /dev/shm**。
两者只差一个 hook 配置。

各做法在多轮实验里的复现情况（DSH 启动 p50，秒；稳定态 / 第 1 次使用）：

| 做法 | 11-12 | 11-14 | 11-16 第一轮 | 11-16 第二轮 |
|---|---|---|---|---|
| 不优化 | 3.5 / 59.3 | 3.3 / 47.6 | 3.3 / 52.2 | 3.4 / 48.1 |
| page pin | 2.0 / 9.3 | 2.0 / 7.3 | **4.1** / 8.0 | 2.0 / 7.4 |
| lifecycle hook | — | 3.4 / 4.9 | 3.9 / 5.5 | 3.8 / 5.0 |
| /dev/shm | — | — | 2.0 / 12.0 | 1.9 / 11.7 |
| /dev/shm + hook | — | — | — | 2.5 / 3.1 |

第 1 次使用每格只有 2–5 个样本，按量级看；稳定态每格约 80 个。冷启动 ping（只有平台）各轮各做法都在 1.8–2.1 秒，
优化不影响它；同一 session 的第二条消息（热）DSH 启动都约 1.9 秒。

## 背景：V2 怎么启动 session，DSH 为什么慢

V2 在创建或更新 runtime 时启动一次容器，等第一次 `/ping` 返回 200（配置了 lifecycle hook 时是 `memory` 调用之后）
就打快照；之后每个新 session 从快照恢复出一个新微VM，不再启动容器。AWS 的
[fast session startup blog](https://aws.amazon.com/blogs/machine-learning/architecture-patterns-for-fast-session-startup-on-amazon-bedrock-agentcore-runtime/) 写明了恢复后的三条规律：

- **内存完整恢复**：进程持有的东西、以及 `/dev/shm` 这类内存盘里的文件，从第一次访问起就是内存速度。
- **文件读取不会带过去**：打快照前文件缓存会被清空，所以启动阶段读一遍文件，对恢复后的 session 没有帮助。
- **镜像文件按需拉取**：每个块第一次被读时从快照存储拉取，「and is then cached closer to the host」。
  这就是同一快照越用越快、用过几次后进入稳定态的原因。

`dsh headless` 每个回合新起一个 Node 进程（blog 点名的反模式：handler 里起的子进程每个新 session 都要从镜像重新读
二进制和库），一个回合要打开 3,630 个文件（146.8 MB，其中 Node 二进制 118 MB），正好撞上后两条。

本文用到的几个说法：

- **冷启动 / 第一条消息**：新 session 的第一个请求。前者只返回时间戳（只有平台耗时），后者跑一次真实的 `dsh headless` 对话。
- **第 1 次使用 / 稳定态**：同一个快照被新 session 用的次数。每次创建或更新 runtime 都打新快照（实测每个版本 2 个），
  计数从头开始；用到第 11 次起叫稳定态。稳定态的 session 仍然是冷启动，只是快照已经「用熟了」。
- **热请求**：同一 session 的后续请求，落在同一个微VM上，不再恢复快照。

## 优化做法

### 1. /dev/shm：启动阶段把 DSH 的文件拷进内存盘

依据 blog：「Anything in a RAM-backed file system such as `/dev/shm`, is available at memory speed from the first
access」，对 `tsc` 这类每次单独起进程的工具链建议「Copy them to `/dev/shm` or a ramfs mount during init」。
`DSH_PREWARM=4` 时，包装器在监听之前把清单里的文件拷进 `/dev/shm`，之后每个回合从那里运行 DSH。

AgentCore 微VM里的实际情况：

- `/dev/shm` 是 64 MB 的 tmpfs，挂载选项 `nosuid,nodev,noexec`。其他 tmpfs（`/dev`、`/run/containerd` 等）
  也都是 64 MB，挂载权限 `mode=755`，按此推断以普通用户 `node` 运行的容器写不进去（未实测）；容器也不能自己挂载。
- 因为 `noexec`，Node 二进制和 2 个原生插件（`.node`）放进去也无法以可执行方式映射（本地复现：
  `failed to map segment from shared object`），只能留在镜像上。实际放进去的是 3,613 个 JS 等文件、25 MB，
  启动阶段耗时 0.3–0.7 秒。
- 本地对照（清掉镜像文件的页缓存后跑一个回合）：从镜像读 4.4–4.5 秒；`/dev/shm` 只放 JS 2.5 秒；
  `/dev/shm` 可执行、连 Node 和库一起放 2.1–2.2 秒（AgentCore 上做不到）。

#### 核心代码

[`app/server.mjs`](app/server.mjs) 在监听之前调用 `stageToShm()`。清单里 `/opt/dsh` 下的文件按原目录结构拷进
`/dev/shm/dsh/app`，这些目录里的其他条目建成指回镜像的符号链接，所以清单之外的代码路径照样能用（只是从镜像读）。
挂载允许执行时连 Node 和它的库一起拷；`noexec` 时 ELF 文件留在镜像上。任何一步失败都退回从镜像运行：

```javascript
// RAM-disk staging (level 4). AWS's fast-session-startup blog: memory,
// including RAM-backed file systems such as /dev/shm, comes back whole with a
// V2 snapshot, while files read from the image are fetched again by every new
// session. Before listening, mirror APP_DIR under SHM_DIR: files in the
// manifest are copied, every other entry of the directories on their paths
// becomes a symlink back to the image, so code paths outside the manifest
// still resolve (from the image, at image speed). The node binary and the
// libraries it loads are copied too when SHM_DIR is not mounted noexec; on a
// noexec mount, ELF files (native addons) stay on the image, because they
// cannot be mapped executable from there.
// Best effort: on any failure DSH runs from the image as usual. The copies
// stay in memory for the whole session (pin releases its pages after turn 1).
const shm = { staged: false, node: null, entry: null, libdir: null };
function mountOf(dir) {
  let best = null;
  for (const line of readFileSync('/proc/self/mounts', 'utf8').split('\n')) {
    const [, mnt, type, opts] = line.split(' ');
    if (!mnt) continue;
    const inside = dir === mnt || dir.startsWith(mnt === '/' ? '/' : `${mnt}/`);
    if (inside && (!best || mnt.length > best.mnt.length)) best = { mnt, type, opts };
  }
  return best;
}
function isElf(file) {
  const fd = openSync(file, 'r');
  try {
    const head = Buffer.alloc(4);
    return readSync(fd, head, 0, 4, 0) === 4 && head.equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]));
  } finally {
    closeSync(fd);
  }
}
function stageToShm() {
  prewarm.start_ts = now();
  const detail = { copied: 0, bytes: 0, links: 0, dirs: 0, elf_on_image: 0, exec: false, ms: null,
                   mount: null, free_bytes: null, tmpfs: [], error: null };
  try {
    const listed = readFileSync(PIN_MANIFEST, 'utf8').split('\n')
      .map((line) => line.trim()).filter((line) => line && !line.startsWith('#'));
    const hot = new Set(listed.filter((p) => p.startsWith(`${APP_DIR}/`)));
    const dirs = new Set([APP_DIR]);
    for (const p of hot) for (let d = path.dirname(p); d.length > APP_DIR.length; d = path.dirname(d)) dirs.add(d);
    detail.tmpfs = readFileSync('/proc/self/mounts', 'utf8').split('\n')  // RAM-backed mounts, for the record
      .map((line) => line.split(' ')).filter((f) => f[2] === 'tmpfs' || f[2] === 'ramfs')
      .map((f) => `${f[1]} ${f[3]}`);
    mkdirSync(SHM_DIR, { recursive: true });
    const mnt = mountOf(SHM_DIR);
    detail.mount = mnt ? `${mnt.mnt} ${mnt.type} ${mnt.opts}` : null;
    const fsStat = statfsSync(SHM_DIR);
    detail.free_bytes = fsStat.bavail * fsStat.bsize;
    detail.exec = !(mnt && mnt.opts.split(',').includes('noexec'));
    const mirror = (p) => path.join(SHM_DIR, 'app', path.relative(APP_DIR, p));
    const stageable = (p) => detail.exec || !isElf(p) || (detail.elf_on_image += 1, false);
    const size = (p) => { try { return statSync(p).size; } catch { return 0; } };
    const libs = listed.filter((f) => /^\/(usr\/)?lib\//.test(f) && f.includes('.so'));
    const need = [...hot].filter((p) => detail.exec || !isElf(p)).reduce((sum, p) => sum + size(p), 0)
      + (detail.exec ? size(process.execPath) + libs.reduce((sum, p) => sum + size(p), 0) : 0);
    if (need * 1.1 > detail.free_bytes) throw new Error(`needs ${need} bytes, ${detail.free_bytes} free`);
    const copy = (from, to, mode) => {
      copyFileSync(from, to);
      chmodSync(to, mode);
      detail.copied += 1;
      detail.bytes += statSync(to).size;
    };
    const build = (dir) => {
      mkdirSync(mirror(dir), { recursive: true });
      detail.dirs += 1;
      for (const ent of readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, ent.name);
        if (dirs.has(p) && ent.isDirectory()) build(p);
        else if (hot.has(p) && ent.isFile() && stageable(p)) copy(p, mirror(p), statSync(p).mode & 0o777);
        else { symlinkSync(p, mirror(p)); detail.links += 1; }
      }
    };
    build(APP_DIR);
    if (detail.exec) {
      const bin = path.join(SHM_DIR, 'bin');
      const lib = path.join(SHM_DIR, 'lib');
      mkdirSync(bin, { recursive: true });
      mkdirSync(lib, { recursive: true });
      copy(process.execPath, path.join(bin, 'node'), 0o755);
      for (const p of libs) {
        const soname = path.basename(p).match(/^(.*?\.so(?:\.\d+)?)/)[1];  // libstdc++.so.6.0.30 -> libstdc++.so.6
        copy(p, path.join(lib, soname), 0o755);
      }
      shm.node = path.join(bin, 'node');
      shm.libdir = lib;
    }
    shm.entry = mirror(realpathSync(DSH_BIN));
    statSync(shm.entry);  // the entry script must be a real copy
    shm.staged = true;
  } catch (err) {
    detail.error = String(err.message || err).slice(0, 300);
    shm.staged = false;
    shm.node = shm.libdir = shm.entry = null;
  }
  prewarm.end_ts = now();
  detail.ms = Math.round((prewarm.end_ts - prewarm.start_ts) * 1000);
  prewarm.detail = detail;
}
```

每个回合用拷好的入口脚本启动 DSH（`noexec` 时 Node 仍是镜像上的那个）：

```javascript
    const dshArgs = ['headless', '--json', '--approval-policy', 'never', '-'];
    let cmd = DSH_BIN;
    let args = dshArgs;
    if (shm.staged) {  // run the staged copy: node and DSH from the RAM disk
      cmd = shm.node || process.execPath;
      args = [shm.entry, ...dshArgs];
      if (shm.node) env.PATH = `${path.dirname(shm.node)}:${env.PATH || ''}`;
      if (shm.libdir) env.LD_LIBRARY_PATH = shm.libdir;
      result.ran_from = shm.node ? 'shm' : 'shm-js';
    }
```

### 2. lifecycle hook：让平台预取 session 要读的文件

hook 是容器里的一个端点，AgentCore 在创建/更新 runtime 时调用（[文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-hooks.html)）。
本实现的端点是 `POST /warmup`，平台调用两次，用请求头 `X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type` 区分
`memory` 和 `disk`：

- **`memory`（打快照之前）：什么都不做，只记一个时间戳就返回 200**（实测 0.0 秒）。该进快照的启动工作
  （写 DSH 的 profile、加载 AWS SDK 凭证链模块、`DSH_PREWARM=3` 时启动 pinner）在 `server.listen()` 之前
  就做完了，平台第一次 `/ping` 成功时它们已经在内存里。这与
  [AWS 的这篇 blog](https://aws.amazon.com/blogs/machine-learning/architecture-patterns-for-fast-session-startup-on-amazon-bedrock-agentcore-runtime/)
  一致：hook 是 init 的补充，能放进 init 的就放进 init。记下的 `hook.memory_ts` 只用作证据：恢复出来的 session
  里它有值、`hook.disk_calls` 为 0，说明快照打在 `memory` 之后、`disk` 之前。
- **`disk`（在一个从快照恢复的副本上）：按清单把 DSH 一个回合要读的 3,630 个文件（147 MB）冷读一遍**，读完才返回
  200。这次调用留在内存里的东西不会进快照，它的作用是让平台知道恢复后的 session 先拉哪些块。

`memory` 调用是「`/ping` 健康之后、冻结之前」的最后一次机会，做在这里的事会进快照。本实现没有用到它：
pinner 和 `/dev/shm` 拷贝（`DSH_PREWARM=3` / `4`）都在监听之前做完，效果与放在 `memory` 调用里相同。
在 `/dev/shm` + hook 组合里，hook 也只做 `disk` 调用这一件事。

#### 实测到的 hook 行为

以下是从构建日志（`/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` 中 `build-logs-*`）和包装器返回的时间戳
看到的，前两条与 AWS 的 [fast session startup blog](https://aws.amazon.com/blogs/machine-learning/architecture-patterns-for-fast-session-startup-on-amazon-bedrock-agentcore-runtime/)（2026-11-13）写的一致：

- **快照取在 `memory` 调用之后、`disk` 调用之前**：恢复出来的 session 里 `hook.memory_ts` 有值，
  `hook.disk_calls` 都是 0。blog 的说法是：`memory` 调用在构建用的微VM上、冻结之前执行，「Anything your agent
  loads here and keeps in memory is part of the snapshot」。
- **`disk` 调用在一个从快照恢复的副本上执行，不进快照**：blog 的说法是它跑在「a separate microVM that restores
  the snapshot to check it. Nothing from this call is snapshotted」，作用是「shapes what restored sessions fetch
  first」。实测它在 `memory` 之后约 86–100 秒开始，读 147 MB 用了 12–25 秒（冷读快照盘）。所以在 `disk` 调用里
  pin 没有意义。
- 如果文件已被 pin，`disk` 调用的读全部命中内存（3,629 个文件约 100 毫秒），平台看不到这些读。
- 每次创建/更新，平台起 **2 个**构建实例，各自走一遍 `/ping` → `memory` → `disk`，各有完整的 120 秒预算；
  session 也来自这两个快照（`proc_start_ts` 相差约 5 秒），「第几次使用」要按快照分别计数。

#### 为什么 `memory` 什么都不做，hook 和 pin 的效果还差这么多

hook 的效果全部来自 `disk` 调用。它和 pin 作用在不同的地方：pin 把数据放进内存，hook 让平台提前把磁盘块拉到近处。

| | page pin | hook（`disk` 调用） |
|---|---|---|
| 作用在哪 | 内存：打快照前把文件页 mmap 住，这些页随快照一起恢复 | 磁盘：平台在测试恢复的副本上记下 session 要读哪些块，之后恢复 session 时提前把这些块拉到近处 |
| 每个新 session 还要读盘吗 | 被 pin 住的文件内容不用读，直接在内存里 | 要读，只是读的块已经在离主机近的缓存里，不用再去远端快照存储拉 |
| 管不到的部分 | 目录项、inode、清单之外的文件，以及 Node 解析模块时对不存在路径的大量 `stat`，这些仍要冷读快照盘 | 盘上的数据每个 session 都要再读进页缓存一次（打快照前缓存被清空） |

这正好解释了数字（11-16 第二轮，DSH 启动 p50）：

- **hook 的第 1 次使用（5.0 秒）接近对照组的稳定态（3.4 秒）**：对照组越用越快，是因为被读过的块逐渐缓存到离主机近的地方；
  hook 提前做了同一件事，新快照一上来就接近对照组「用熟了」之后的状态。
- **hook 的稳定态（3.8 秒）≈ 对照组的稳定态（3.4 秒）**：块都在近处之后 hook 没有额外好处，每个 session 照样要从盘上读约 147 MB。
- **pin 的稳定态（2.0 秒）更低**：数据本来就在内存里，省掉了每个 session 的这次读盘。/dev/shm 的 1.9 秒同理。
- **pin 的第 1 次使用（7.4 秒）反而比 hook 慢**：pin 管不到的那部分在新快照上一开始全是冷的。

/dev/shm + hook 两头都好，是两者各补一块：/dev/shm 把 JS 文件放进内存（和 pin 一样），hook 把留在镜像上的 Node 二进制等
提前拉到近处。「`disk` 调用让平台提前拉取这些块」是根据 blog 的说法（「shapes what restored sessions fetch first」）和上面的
数据推断的，平台内部怎么做没有文档。

#### 核心代码

整体分三块：包装器提供 `POST /warmup`（[`app/server.mjs`](app/server.mjs)），`disk` 调用复用 pinner 冷读清单
（[`app/pinner.py`](app/pinner.py) 的 `--evict --no-hold`），部署时给 runtime 加上 `hooksConfiguration`
（[`scripts/deploy.sh`](scripts/deploy.sh)）。下面的代码与仓库文件一致。

**1. 开关（`app/server.mjs`）** — `DSH_HOOK` 决定 `disk` 调用做什么，不设置时没有 `/warmup` 路由：

```javascript
// DSH_HOOK: what the build warmup lifecycle hook (POST /warmup) does in its
// `disk` call. '' = no hook route; 'read' = read the manifest's files cold
// (cache dropped per file first) and let go; 'pin' = the same cold reads by the
// pinner, which then keeps the files mapped. The `memory` call has nothing left
// to do: startup work already runs before listening.
// Measured on AgentCore (2026-11-14): sessions are restored from the state
// after the `memory` call; the `disk` call runs ~90 s later on a restored copy,
// so what it leaves in memory (pin mode's mappings) never reaches a session.
// To pin into the snapshot, combine DSH_PREWARM=3 with DSH_HOOK=read.
const HOOK_MODE = process.env.DSH_HOOK || '';
const WARMUP_PATH = '/warmup';
const WARMUP_TYPE_HEADER = 'x-amzn-bedrock-agentcore-runtime-warmup-type';
if (!['', 'read', 'pin'].includes(HOOK_MODE)) throw new Error(`DSH_HOOK must be "read" or "pin", got ${JSON.stringify(HOOK_MODE)}`);
if (HOOK_MODE === 'pin' && PREWARM_LEVEL === 3) throw new Error('DSH_HOOK=pin and DSH_PREWARM=3 both pin; set one');
```

**2. hook 处理函数（`app/server.mjs`）** — `memory` 直接返回；`disk` 在子进程里冷读清单，读完才返回 200，
失败返回 500 让创建/更新失败。子进程不阻塞事件循环，所以 hook 运行期间 `/ping` 照常响应（实测约 1 毫秒）：

```javascript
// Build warmup lifecycle hook. AgentCore calls POST /warmup twice while it
// prepares a V2 runtime (create/update), `memory` first, then `disk`. Sessions
// start from the state after `memory`; `disk` runs later on a restored copy and
// only tells the platform which files to make available faster (measured
// 2026-11-14). A 2xx means the work is done; anything else fails the
// create/update, which is what a broken variant of this benchmark should do.
// The work runs in a child process, so /ping keeps answering meanwhile.
// runPinner() is the page-pin helper above.
const hook = { mode: HOOK_MODE || null, memory_ts: null, disk_calls: 0, disk_start_ts: null, disk_end_ts: null, detail: null };
async function handleWarmup(type, res) {
  if (type === 'memory') {
    hook.memory_ts = now();
    console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE }));
    return send(res, 200, { type });
  }
  if (type !== 'disk') {
    console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE, ignored: true }));
    return send(res, 200, { type, ignored: true });
  }
  hook.disk_calls += 1;
  if (HOOK_MODE === 'pin' && pinner) return send(res, 200, { type, reused: true, detail: hook.detail });
  hook.disk_start_ts = now();
  const hold = HOOK_MODE === 'pin';
  const detail = await runPinner(hold ? ['--evict'] : ['--evict', '--no-hold'], hold);
  hook.disk_end_ts = now();
  hook.detail = detail;
  if (hold) {  // so the report's page-pin sections see this runtime as pinned
    prewarm.detail = detail;
    prewarm.start_ts = hook.disk_start_ts;
    prewarm.end_ts = hook.disk_end_ts;
  }
  const ok = !detail.error && (detail.read ?? 0) > 0;
  console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE, ok, ms: Math.round((hook.disk_end_ts - hook.disk_start_ts) * 1000), detail }));
  return send(res, ok ? 200 : 500, { type, detail });
}
```

路由（在 `/invocations` 之前判断；平台的 `GET /ping`、`POST /invocations` 不受影响）：

```javascript
  if (HOOK_MODE && req.method === 'POST' && url === WARMUP_PATH) {
    return handleWarmup(String(req.headers[WARMUP_TYPE_HEADER] || '').toLowerCase(), res);
  }
```

**3. 冷读（`app/pinner.py`，完整文件见「4. page pin」的示例代码）** — `--no-hold` 读完就退出，不做 `mmap`。
`--evict` 在读每个文件之前先 `fadvise(DONTNEED)`；`disk` 调用所在的恢复副本上文件缓存本来就是空的，所以它在
AgentCore 上不起作用，只在本地测试（文件还在缓存里）时有区别，保留它是为了与已测的变体一致：

```python
EVICT = "--evict" in sys.argv[1:]
HOLD = "--no-hold" not in sys.argv[1:]
```

```python
            if EVICT:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
```

**4. runtime 配置（`scripts/deploy.sh`）** — `UpdateAgentRuntime` 会整体替换 hook 配置，所以每次更新都重新带上；
创建/更新走 boto3（[`scripts/put_runtime.py`](scripts/put_runtime.py)）：

```bash
runtime_config() {   # $1 = key
    local env="\"DSH_MODEL_ID\": \"$MODEL_ID\", \"DSH_PREWARM\": \"${PREWARM[$1]}\""
    local hooks=""
    if [ -n "${HOOK[$1]}" ]; then
        env+=", \"DSH_HOOK\": \"${HOOK[$1]}\""
        # UpdateAgentRuntime replaces the whole hook configuration, so every
        # update of a hooked runtime sends it again.
        hooks=', "hooksConfiguration": {"build": {"warmup": {"action": {"httpPost": {"path": "/warmup", "port": 8080}}}}}'
    fi
    cat <<JSON
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "$ECR_URI:${TAGOF[$1]}"}},
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "protocolConfiguration": {"serverProtocol": "HTTP"},
  "roleArn": "$ROLE_ARN",
  "platformVersion": "${PLATFORM[$1]}",
  "environmentVariables": {$env},
  "lifecycleConfiguration": {"idleRuntimeSessionTimeout": $IDLE_TIMEOUT}$hooks
JSON
}
```

各取值：`DSH_HOOK=read` 是结论表里的 hook；`DSH_HOOK=pin` 在 `disk` 调用里冷读后 pin（实测无效，保留作对照）；
`DSH_HOOK=read` 可与 `DSH_PREWARM=3` 组合，`DSH_HOOK=pin` 不行。没有设置 `DSH_HOOK` 时 `/warmup` 返回 404。
hook 只支持平台 V2 + microVM；调用不重试，失败或超过 120 秒都会让创建/更新失败。

### 3. 组合

#### /dev/shm + hook（推荐）

不需要新代码：同一个包装器同时打开两个开关，`DSH_PREWARM=4`（启动阶段拷进 `/dev/shm`）和 `DSH_HOOK=read`
（`disk` 调用按清单读文件）。两部分在不同时刻起作用：

| 时刻 | 在哪个微VM上 | 做了什么 | 作用 |
|---|---|---|---|
| 启动，`server.listen()` 之前 | 构建用的微VM | `stageToShm()`：3,613 个 JS 等文件（25 MB）拷进 `/dev/shm` | 这些文件随快照在内存里恢复 |
| `memory` 调用 | 同上，冻结之前 | 只记时间戳 | — |
| 打快照 | — | 内存（含 `/dev/shm`）和磁盘一起冻结 | — |
| `disk` 调用 | 从快照测试恢复的另一个微VM | 按清单冷读 3,629 个文件（154 MB，从镜像路径读，13.7–15.3 秒） | 平台据此预取块；真正有用的是仍在镜像上的 Node 二进制、共享库和 2 个原生插件 |
| 每个回合 | 每个 session 的微VM | 用 `/dev/shm` 里的入口脚本起 DSH，Node 仍是镜像上的那个 | JS 从内存读，Node 二进制从已预取的块读 |

**1. 启动顺序（`app/server.mjs`）** — 拷贝在监听之前同步做完，所以平台第一次 `/ping` 成功时 `/dev/shm` 已经就绪：

```javascript
mkdirSync(WORKDIR, { recursive: true });
writeProfilePatch();
await warmCredentialChain();
if (PREWARM_LEVEL === 3) await startPinner();
if (PREWARM_LEVEL === 4) stageToShm();
server.listen(PORT, HOST, () => {
  console.log(JSON.stringify({
    event: 'listening', host: HOST, port: PORT, dsh_version: dshVersion, model: MODEL_ID,
    region: REGION, prewarm: PREWARM_LEVEL, prewarm_detail: prewarm.detail, hook: HOOK_MODE || null,
    boot_ms: Math.round((now() - PROC_START_TS) * 1000),
  }));
  if (PREWARM_LEVEL === 1) runPrewarm();
});
```

**2. 允许的组合（`app/server.mjs`）** — `DSH_PREWARM=4` 可以和 `DSH_HOOK=read` 一起用；只有 `DSH_HOOK=pin` 与
`DSH_PREWARM=3` 互斥：

```javascript
if (!['', 'read', 'pin'].includes(HOOK_MODE)) throw new Error(`DSH_HOOK must be "read" or "pin", got ${JSON.stringify(HOOK_MODE)}`);
if (HOOK_MODE === 'pin' && PREWARM_LEVEL === 3) throw new Error('DSH_HOOK=pin and DSH_PREWARM=3 both pin; set one');
if (![0, 1, 3, 4].includes(PREWARM_LEVEL)) throw new Error(`DSH_PREWARM must be 0, 1, 3 or 4, got ${process.env.DSH_PREWARM}`);
```

**3. `disk` 调用与每个回合**：`disk` 调用走「2. lifecycle hook」核心代码里的 `handleWarmup()`，`DSH_HOOK=read` 时执行的是

```javascript
  const detail = await runPinner(hold ? ['--evict'] : ['--evict', '--no-hold'], hold);
```

每个回合怎么从 `/dev/shm` 起 DSH，见「1. /dev/shm」核心代码的最后一段。

**4. runtime 配置** — `scripts/deploy.sh` 为 `dsh_shmhook_v2` 生成；下面是 `get-agent-runtime` 读回的实际配置（节选）：

```json
{
    "platformVersion": "V2",
    "environmentVariables": {
        "DSH_HOOK": "read",
        "DSH_MODEL_ID": "deepseek.v3.2",
        "DSH_PREWARM": "4"
    },
    "hooksConfiguration": {
        "build": {
            "warmup": {
                "action": {
                    "httpPost": {
                        "path": "/warmup",
                        "port": 8080
                    }
                }
            }
        }
    }
}
```

稳定态比只用 /dev/shm 慢约 0.5 秒（2.5 vs 1.9 秒），原因未查明。只读 Node 二进制、库和原生插件（不读已在 `/dev/shm`
里的 JS）可以缩短 `disk` 调用，但没有测过。

#### pin + hook（不要用）

在 `disk` 调用里 pin 无效（那次调用不进快照）；监听前 pin 再加 hook，第 1 次使用与 pin 相当，但 pin 的稳定态收益消失。

### 4. page pin（不作首选）

page pin（[`app/pinner.py`](app/pinner.py)，改写自 `agentcore-e-layer-best-practices` 知识库 §6.4 的做法）的步骤：

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
- 「打快照前清空页缓存」最初来自知识库的实测结论；AWS 的 [blog](https://aws.amazon.com/blogs/machine-learning/architecture-patterns-for-fast-session-startup-on-amazon-bedrock-agentcore-runtime/)（2026-11-13）已写明这一点。
  AWS 的文档和 blog 都没有提 page pin，blog 对放不进进程的文件推荐的是 init 时拷到 `/dev/shm`，或用 build hook。

#### 示例代码

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

Options (used by the build warmup lifecycle hook, see server.mjs):
  --evict    drop each file's cached pages (posix_fadvise DONTNEED) right before
             reading it. On AgentCore the disk warmup call runs on a freshly
             restored microVM whose file cache is already empty, so this is a
             no-op there; it only matters where the files are still cached
             (local tests). Kept so the measured hook variants stay unchanged.
  --no-hold  read only: print the result line and exit without mapping.

Adapted from the page-pin recipe in agentcore-e-layer-best-practices (§6.4);
the byte cap is raised because DSH reads far more than that recipe's example.
"""
import json
import mmap
import os
import sys
import time

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
EVICT = "--evict" in sys.argv[1:]
HOLD = "--no-hold" not in sys.argv[1:]
MANIFEST = ARGS[0] if ARGS else "/opt/dsh/pin-manifest.txt"
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
            if EVICT:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            off = 0
            while off < st.st_size:
                n = os.preadv(fd, [buf], off)  # read into the page cache first
                if n == 0:
                    break
                off += n
            if HOLD:
                maps.append(mmap.mmap(fd, off, prot=mmap.PROT_READ,
                                      flags=mmap.MAP_SHARED | mmap.MAP_POPULATE))
            seen.add(key)
            total += off
        except (OSError, ValueError):
            skipped += 1
        finally:
            os.close(fd)
    return maps, total, skipped, len(seen)


def main():
    started = time.monotonic()
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    except OSError:
        paths = []
    maps, total, skipped, read = pin(paths, started + DEADLINE_S)
    result = {"files": len(maps), "bytes": total, "skipped": skipped, "listed": len(paths),
              "ms": round((time.monotonic() - started) * 1000), "max_bytes": MAX_BYTES}
    if EVICT or not HOLD:  # keys added with the hook options; the default line is unchanged
        result.update(read=read, evict=EVICT, hold=HOLD)
    print(json.dumps(result), flush=True)
    if HOLD:
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
// Runs pinner.py and resolves with its result line. `hold` keeps the process
// (and its mappings) as `pinner`; otherwise it is a read-only pass that exits.
function runPinner(extraArgs = [], hold = true) {
  return new Promise((resolve) => {
    let settled = false;
    let out = '';
    let child;
    const finish = (detail) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(detail);
    };
    const timer = setTimeout(() => finish({ error: `no result within ${PIN_WAIT_MS} ms` }), PIN_WAIT_MS);
    try {
      child = spawn('python3', [path.join(APP_DIR, 'pinner.py'), ...extraArgs, PIN_MANIFEST], { stdio: ['pipe', 'pipe', 'inherit'] });
    } catch (err) {
      return finish({ error: `spawn failed: ${err.code || err.name}` });
    }
    if (hold) pinner = child;
    const drop = () => { if (pinner === child) pinner = null; };
    child.on('error', (err) => { drop(); finish({ error: `spawn failed: ${err.code || err.name}` }); });
    child.on('close', (code) => {
      drop();
      if (hold) prewarm.exit_code = code;
      finish({ error: `pinner exited with ${code}` });
    });
    child.stdout.on('data', (chunk) => {
      out += chunk.toString('utf8');
      const idx = out.indexOf('\n');
      if (idx < 0) return;
      try { finish(JSON.parse(out.slice(0, idx))); } catch { finish({ error: 'pinner printed something other than its result line' }); }
    });
  });
}
async function startPinner() {
  prewarm.start_ts = now();
  prewarm.detail = await runPinner();
  prewarm.end_ts = now();
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

### 无效的做法

- `DSH_PREWARM=1`（监听之后跑一次 `dsh headless --help`）：没有可测量的收益（11-12）。
- 在 hook 的 `disk` 调用里 pin（`DSH_HOOK=pin`）：与只用 hook 相同（11-14）。

## 实验记录

每次实验的做法相同：同一镜像的多个 runtime 只在环境变量和 hook 配置上不同；每个 runtime 创建/更新 3 次（3 个版本），
每个版本先跑 smoke 测第 1 次使用，最后一个版本上跑并发 1 / 10 / 30 的矩阵。runtime 每次实验前重建过，不同实验之间
按量级比较，同一张表内可直接比较。下列表格都是 p50。

### 2026-11-12：page pin（4 个 runtime，1,600 个请求，0 失败）

与本地 Docker 基线对比，毫秒；合并并发 1 / 10 / 30 的样本。

| 指标 | 本地 Docker · 页缓存已热 | 本地 Docker · 页缓存清空 | V2 · 原样 DSH | V2 + page pin |
|---|---|---|---|---|
| 冷启动：新 session 的 ping（只有平台） | 418 | — | 2,014 | 1,938 |
| DSH 进程启动（spawn → 首个输出，不含模型） | 2,151 | 4,618 | 3,552 | 1,999 |
| 第一条消息端到端（平台启动 + DSH 进程启动 + 一次模型调用，到完整回答返回） | 3,712 | 6,410 | 6,437 | 4,678 |
| 第一条消息：新版本上线后同一快照的第 1 次使用 | — | — | 61,068 | 15,202 |

「冷启动」是新 session 的首次调用只返回时间戳；「第一条消息」是首次调用就跑一次真实的 `dsh headless` 对话。
本地「页缓存清空」一列在第一条消息前清掉了 DSH 要读的 3,630 个文件的缓存，它才与快照恢复后的 session 可比。
V2 的「DSH 进程启动」取同一快照用过 10 次之后的样本；「第 1 次使用」每列只有 2 个样本。

1. **冷启动稳定在约 2.0 秒，与并发无关**：每个新 session 都从快照恢复。
2. **不带 page pin 时，新版本上线后的头几个 session 第一条消息要 40–60 秒**，时间都在 `dsh` 进程启动上；
   同一快照用到第 6 次起稳定在约 6.4 秒。
3. **带上 page pin 后**，第 1 次使用从 64.4 秒降到 15.2 秒，稳定后约 4.7 秒；DSH 启动约 2.0 秒，
   与本地页缓存已热时相当。代价是锁住 147 MB 内存，直到首个回合结束。
4. **`DSH_PREWARM=1`（启动后先跑一次 `dsh headless --help`）没有可测量的收益。**

### 2026-11-14：lifecycle hook（5 个 runtime，2,056 个请求，0 失败）

DSH 启动，按「同一快照的第几次冷 agent 调用」分组，毫秒：

| 做法 | 第 1 次 | 第 2 次 | 第 3–5 次 | 第 6–10 次 | 第 11 次起（稳定态） |
|---|---|---|---|---|---|
| 对照：不 pin、无 hook（`dsh_pinctl_v2`） | 47,582 | 32,200 | 8,640 | 7,672 | 3,316 |
| page pin（`dsh_pin_v2`） | 7,330 | 6,240 | 4,916 | 2,268 | **1,971** |
| hook：`disk` 调用冷读清单（`dsh_hook_v2`） | **4,917** | 3,510 | 3,685 | 4,023 | 3,398 |
| hook：`disk` 调用里 pin（`dsh_hookpin_v2`） | 5,536 | 3,542 | 4,391 | 3,500 | 3,670 |
| 监听前 pin + `disk` 调用读（`dsh_pinhook_v2`） | 6,655 | 4,236 | 3,208 | 4,050 | 3,432 |

第 1 次使用每组 3–4 个样本，稳定态每组 81 个。冷启动 ping 五组都在 1.8–1.9 秒，hook 不影响平台启动。

完整数据见 [results/HOOKS.zh.md](results/HOOKS.zh.md)。

### 2026-11-16：/dev/shm（两轮，3,720 个请求，0 失败）

第二轮（5 个 runtime 同时测，2,060 个请求），DSH 启动，按「同一快照的第几次冷 agent 调用」分组，秒（样本数）：

| 第几次 | 不优化 | page pin | hook | /dev/shm | /dev/shm + hook |
|---|---|---|---|---|---|
| 1 | 48.1 (4) | 7.4 (4) | 5.0 (4) | 11.7 (4) | **3.1** (4) |
| 2 | 33.6 (2) | 5.2 (2) | 4.6 (2) | 2.7 (2) | 2.6 (2) |
| 3–5 | 6.8 (6) | 3.4 (6) | 4.8 (6) | 2.3 (6) | 2.9 (6) |
| 6–10 | 3.4 (10) | 3.3 (10) | 4.0 (10) | 2.3 (10) | 2.9 (10) |
| 11–20 | 3.4 (20) | 2.6 (20) | 3.8 (20) | 2.3 (20) | 2.7 (20) |
| 21–40 | 3.5 (39) | 2.0 (26) | 4.2 (33) | 2.0 (33) | 2.5 (28) |
| 41 起 | 3.3 (22) | **1.9** (35) | 3.4 (28) | **1.9** (28) | 2.5 (33) |

`dsh_shmhook_v2` 的 `disk` 调用读清单里 3,629 个文件（154 MB，从镜像路径读）用 13.7–15.3 秒。冷启动 ping 五组都在
1.8–1.9 秒；同一 session 的第二个回合（热）DSH 启动五组都是 1.9 秒。

第一轮（4 个 runtime，1,660 个请求）：

| 第几次 | 不优化 | page pin | hook | /dev/shm |
|---|---|---|---|---|
| 1 | 52.2 (5) | 8.0 (5) | **5.5** (5) | 12.0 (4) |
| 2 | 40.0 (2) | 5.1 (2) | 5.8 (2) | 9.3 (2) |
| 3–5 | 10.9 (6) | 3.4 (6) | 4.0 (6) | 4.2 (6) |
| 6–10 | 6.3 (10) | 3.4 (10) | 4.2 (10) | 5.8 (10) |
| 11–20 | 3.5 (20) | 3.3 (20) | 3.9 (20) | 2.6 (20) |
| 21–40 | 3.4 (32) | 4.1 (34) | 4.0 (39) | **2.0** (36) |
| 41 起 | 3.1 (29) | 4.1 (27) | 3.9 (22) | **1.9** (25) |

第一轮第一次部署时 `/dev/shm` 拷贝因大小检查有误而失败（DSH 实际从镜像运行），这 4 个请求未计入；原始文件保留。

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

| key | runtime 名称 | 平台 | 镜像 tag | `DSH_PREWARM` | `DSH_HOOK` | 说明 |
|---|---|---|---|---|---|---|
| `dsh_v2` | `dsh_coldstart_base_v2` | V2 | `dsh-0.2.2-rc.1` | 0 | — | 原样 DSH（11-12） |
| `dsh_prewarm_v2` | `dsh_coldstart_prewarm_v2` | V2 | `dsh-0.2.2-rc.1` | 1 | — | 启动后先跑一次 `dsh headless --help`（11-12） |
| `dsh_pinctl_v2` | `dsh_coldstart_diag_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 0 | — | page pin 与 hook 的对照组 |
| `dsh_pin_v2` | `dsh_coldstart_deepwarm_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 3 | — | page pin |
| `dsh_hook_v2` | `dsh_coldstart_hook_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 0 | `read` | lifecycle hook（11-14） |
| `dsh_hookpin_v2` | `dsh_coldstart_hookpin_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 0 | `pin` | hook 的 `disk` 调用里 pin（11-14） |
| `dsh_pinhook_v2` | `dsh_coldstart_pinhook_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 3 | `read` | 监听前 pin + hook（11-14） |
| `dsh_shm_v2` | `dsh_coldstart_shm_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 4 | — | 监听前把清单文件拷进 `/dev/shm`（11-16） |
| `dsh_shmhook_v2` | `dsh_coldstart_shmhook_v2` | V2 | `dsh-0.2.2-rc.1-pin` | 4 | `read` | `/dev/shm` + hook（11-16） |

`dsh_pinctl_v2`、`dsh_pin_v2` 沿用了最初创建时的名称，名称与现在的用途不对应。
`dsh_v2`、`dsh_prewarm_v2` 只在 11-12 测过，11-14 时已不存在、没有重建（记在 `deployments.json` 的
`removed_runtimes` 里）。11-14 的 5 个 runtime 用同一个 `-pin` 镜像（带 `/warmup` 处理逻辑；
不设 `DSH_HOOK` 时行为与 11-12 相同）。`-pin` 镜像比基础镜像多了 pinner、
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
V2="dsh_v2 dsh_prewarm_v2 dsh_pinctl_v2 dsh_pin_v2"      # 11-12 的 4 个 runtime
# 11-14 的 hook 测试：V2="dsh_pinctl_v2 dsh_pin_v2 dsh_hook_v2 dsh_hookpin_v2 dsh_pinhook_v2"
# 11-16 的 /dev/shm 测试：V2="dsh_pinctl_v2 dsh_pin_v2 dsh_hook_v2 dsh_shm_v2 dsh_shmhook_v2"
DEPLOY_KEYS="$V2" bash scripts/deploy.sh                 # ECR + IAM 角色 + 这些 runtime -> deployments.json（幂等）
DEPLOY_KEYS="dsh_pin_v2" bash scripts/deploy.sh          # 只创建/更新指定 runtime，不打扰其他的
FORCE_UPDATE=1 DEPLOY_KEYS="$V2" bash scripts/deploy.sh  # 镜像没变也更新，让每个 runtime 打新快照（用于测第 1 次使用）
uv sync
RT="${V2// /,}"                                          # coldstart_test.py 的 --runtimes 用逗号分隔
uv run python coldstart_test.py --smoke --runtimes "$RT" # 每个 runtime 各一次 ping 序列 + agent 序列
python3 scripts/local_baseline.py --rounds 10            # 本地 Docker 基线（页缓存已热）-> results/local_baseline.json
python3 scripts/local_baseline.py --rounds 6 --evict-page-cache   # 页缓存清空 -> results/local_baseline_coldcache.json
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 1
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 10 --rounds 3 --settle-seconds 60
uv run python coldstart_test.py --full --runtimes "$RT" --concurrency 30 --rounds 2 --settle-seconds 0
uv run python scripts/gen_report.py --platforms V2       # 生成 results/REPORT.md 与 REPORT.zh.md（只含 V2）
uv run python -m unittest discover -s tests              # 客户端与报告生成器的离线测试
```

逐请求原始数据在 `results/raw/*.json`，聚合统计在 `results/summary.json`。
`results/raw/` 里还留有两个已删除的 V1 runtime（`dsh`、`dsh_prewarm`）的数据，不带 `--platforms V2`
生成报告会把它们也写进去。
runtime 每次创建或更新都会重新打快照，「同一快照的第几次使用」从头计数；创建或更新约需 3.5 分钟，
带 hook 时最多再多 120 秒。`deploy.sh` 只管理目标 runtime 和仍然存在的 runtime；创建/更新走
[`scripts/put_runtime.py`](scripts/put_runtime.py)（boto3），因为 2.38.0 及更早的 AWS CLI 不认 `hooksConfiguration`
（2.40.3 已支持）。

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
- 本地 Docker 两组数据都无需拉镜像，样本少（10 轮和 6 轮），并且受这台开发机上其他负载影响：
  同一镜像在负载未记录的第一次测量里 DSH 启动是 2,588 毫秒，重测为 2,151 毫秒（重测时 1 分钟负载约 2.2–2.9）。
  「页缓存清空」用 `dd iflag=nocache` 逐个文件丢弃缓存（用 `fincore` 验证过），只影响这些文件，不需要主机 root。
  清缓存的循环会把主机 1 分钟负载抬到约 7.9，但它在计时开始前已经结束；同一轮里紧接着的第二条消息
  DSH 启动为 2,164 毫秒，与页缓存已热的那组一致，所以 4.6 秒来自读盘而不是负载。
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
bash scripts/cleanup.sh --yes       # 删除 deployments.json 里记录的 runtime、ECR 仓库、IAM 角色
```
