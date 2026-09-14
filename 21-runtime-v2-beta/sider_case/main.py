"""
最小化 AgentCore Runtime - 测试 Session Storage 持久化 + Snapshot + S3 Files 挂载
+ WebSocket 双向流 (/ws 与 /invocations 共用同一容器的 8080 端口)
"""
import os
import time
import json
import hashlib
import asyncio
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# 容器进程启动时刻 (用于冷启动分析: 容器启动 -> 首个 WS 消息的耗时)
PROCESS_START_TS = time.time()
# 镜像构建标记 (每次重建时更新, 用于确认容器跑的是新镜像)
IMAGE_BUILD_TAG = os.getenv("IMAGE_BUILD_TAG", "v13-wsv2-memstat-20260912")

app = BedrockAgentCoreApp()
log = app.logger

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# ---------------------------------------------------------------- 资源采样
# AgentCore Runtime 按 microVM 层面的 host 内存/vCPU 计费, 容器内 /proc/meminfo
# 和 /proc/stat 看到的就是 guest(microVM) 视角的整机资源 —— 正是计费口径。
# cgroup 文件是容器视角, 两者可能不同, 都采下来用于和 USAGE_LOGS 比对。

def _read_meminfo() -> dict:
    """/proc/meminfo -> {key: kB}"""
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                parts = v.split()
                if parts:
                    out[k] = int(parts[0])          # 单位 kB
    except Exception:
        pass
    return out


def _read_first(path: str):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _cpu_busy_seconds() -> float:
    """/proc/stat 第一行 cpu 累计 jiffies -> 已消耗的 CPU 秒数 (排除 idle/iowait)"""
    line = _read_first("/proc/stat")
    if not line:
        return None
    f = [int(x) for x in line.split()[1:11]]
    # user nice system idle iowait irq softirq steal guest guest_nice
    total = sum(f)
    idle = f[3] + f[4]
    return round((total - idle) / CLK_TCK, 4)


def _cgroup_mem() -> dict:
    """cgroup v2 优先, 回落 v1"""
    cur = _read_first("/sys/fs/cgroup/memory.current")
    mx = _read_first("/sys/fs/cgroup/memory.max")
    if cur is None:
        cur = _read_first("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        mx = _read_first("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return {"cgroup_mem_current_bytes": int(cur) if cur and cur.isdigit() else None,
            "cgroup_mem_max": mx}


def _self_rss_kb() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def memstat() -> dict:
    """一次资源快照。所有内存字段单位统一为 MB(1024^2) 便于和 GB-Hours 换算。"""
    mi = _read_meminfo()
    total_kb = mi.get("MemTotal")
    avail_kb = mi.get("MemAvailable")
    now = time.time()
    s = {
        "ts": now,
        "uptime_s": round(now - PROCESS_START_TS, 3),
        # ---- microVM(host) 视角: 计费口径 ----
        "vm_mem_total_mb": round(total_kb / 1024, 2) if total_kb else None,
        "vm_mem_available_mb": round(avail_kb / 1024, 2) if avail_kb else None,
        "vm_mem_used_mb": round((total_kb - avail_kb) / 1024, 2) if total_kb and avail_kb else None,
        "vm_mem_free_mb": round(mi.get("MemFree", 0) / 1024, 2) if mi else None,
        "vm_mem_cached_mb": round(mi.get("Cached", 0) / 1024, 2) if mi else None,
        # ---- vCPU ----
        "vm_cpu_count": os.cpu_count(),
        "vm_cpu_busy_s": _cpu_busy_seconds(),        # 累计值, 取差分算平均 vCPU
        "cpu_max": _read_first("/sys/fs/cgroup/cpu.max"),
        # ---- 容器/进程视角 ----
        "proc_rss_mb": round(_self_rss_kb() / 1024, 2) if _self_rss_kb() else None,
        "loadavg": _read_first("/proc/loadavg"),
    }
    s.update(_cgroup_mem())
    if s.get("cgroup_mem_current_bytes"):
        s["cgroup_mem_current_mb"] = round(s["cgroup_mem_current_bytes"] / 1048576, 2)
    # cpu.max = "<quota> <period>" 或 "max <period>", 换算成核数配额
    try:
        q, p = (s.get("cpu_max") or "").split()
        s["cgroup_vcpu_limit"] = None if q == "max" else round(int(q) / int(p), 3)
    except Exception:
        s["cgroup_vcpu_limit"] = None
    return s


# 压测时用来制造已知负载, 反向验证 USAGE_LOGS 的 vcpu/memory 是否跟随真实用量
_BALLAST = []
_BALLAST_LOCK = threading.Lock()


def alloc_mb(mb: int) -> dict:
    """真实占用 mb MB 匿名内存(逐页写入, 防止只是保留地址空间)"""
    with _BALLAST_LOCK:
        for _ in range(max(0, mb)):
            b = bytearray(1048576)
            b[::4096] = b"\x01" * len(b[::4096])
            _BALLAST.append(b)
        held = len(_BALLAST)
    return {"held_mb": held, **memstat()}


def free_ballast() -> dict:
    with _BALLAST_LOCK:
        _BALLAST.clear()
    return {"held_mb": 0, **memstat()}


def spin_cpu(seconds: float, threads: int = 1) -> dict:
    """占满 threads 个核 seconds 秒, 用于验证 vcpu.hours.used"""
    before = _cpu_busy_seconds()
    t_end = time.time() + seconds

    def burn():
        x = 0
        while time.time() < t_end:
            for _ in range(20000):
                x += 1
    ts = [threading.Thread(target=burn, daemon=True) for _ in range(max(1, threads))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    after = _cpu_busy_seconds()
    return {"wall_s": round(seconds, 3), "threads": threads,
            "cpu_busy_delta_s": round((after or 0) - (before or 0), 3), **memstat()}

# Session Storage 挂载路径
STORAGE_PATH = Path(os.getenv("SESSION_STORAGE_PATH", "/mnt/workspace"))

def _session_file(session_id: str) -> Path:
    """获取 session 状态文件路径"""
    safe_id = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    d = STORAGE_PATH / "sessions" / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"

def _snapshot_dir(session_id: str) -> Path:
    """获取 session 快照目录"""
    safe_id = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    d = STORAGE_PATH / "snapshots" / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def save_state(session_id: str, state: dict):
    f = _session_file(session_id)
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps({"session_id": session_id, "saved_at": datetime.now(timezone.utc).isoformat(), "data": state}, ensure_ascii=False))
    tmp.rename(f)

def load_state(session_id: str) -> dict:
    f = _session_file(session_id)
    if not f.exists():
        return {"conversation_history": [], "turn_count": 0, "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        return json.loads(f.read_text()).get("data", {})
    except:
        return {"conversation_history": [], "turn_count": 0, "created_at": datetime.now(timezone.utc).isoformat()}

def create_snapshot(session_id: str, state: dict, name: str = None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_id = f"snap_{ts}"
    snap_dir = _snapshot_dir(session_id)
    snap_file = snap_dir / f"{snapshot_id}.json"
    snap_file.write_text(json.dumps({
        "snapshot_id": snapshot_id, "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": name or snapshot_id, "state": state
    }, ensure_ascii=False, indent=2))
    return snapshot_id

def list_snapshots(session_id: str) -> list:
    snap_dir = _snapshot_dir(session_id)
    result = []
    for f in sorted(snap_dir.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text())
            result.append({"snapshot_id": d["snapshot_id"], "name": d.get("name",""), "created_at": d["created_at"]})
        except:
            pass
    return result

def restore_snapshot(session_id: str, snapshot_id: str = None) -> dict:
    snap_dir = _snapshot_dir(session_id)
    if snapshot_id:
        f = snap_dir / f"{snapshot_id}.json"
    else:
        files = sorted(snap_dir.glob("*.json"), reverse=True)
        if not files: return None
        f = files[0]
    if not f.exists(): return None
    return json.loads(f.read_text()).get("state")

@app.entrypoint
async def invoke(payload: dict, context):
    session_id = getattr(context, 'session_id', None) or 'default'
    action = payload.get("action", "chat")
    
    # 加载状态
    state = load_state(session_id)
    
    if action == "chat":
        prompt = payload.get("prompt", "")
        state["turn_count"] = state.get("turn_count", 0) + 1
        state.setdefault("conversation_history", []).append({"role": "user", "content": prompt, "ts": datetime.now(timezone.utc).isoformat()})
        
        # 简单回复（展示持久化生效）
        reply = f"[Turn {state['turn_count']}] Session={session_id[:8]}... Storage={STORAGE_PATH}. Got: {prompt}"
        state["conversation_history"].append({"role": "assistant", "content": reply, "ts": datetime.now(timezone.utc).isoformat()})
        
        # 保存
        save_state(session_id, state)
        
        # 每5轮自动快照
        if state["turn_count"] % 5 == 0:
            create_snapshot(session_id, state, f"auto-turn-{state['turn_count']}")
        
        result = {"response": reply, "turn_count": state["turn_count"], "session_id": session_id}
    
    elif action == "snapshot":
        sid = create_snapshot(session_id, state, payload.get("name"))
        result = {"snapshot_id": sid, "status": "created"}
    
    elif action == "list_snapshots":
        result = {"snapshots": list_snapshots(session_id)}
    
    elif action == "restore":
        restored = restore_snapshot(session_id, payload.get("snapshot_id"))
        if restored:
            save_state(session_id, restored)
            result = {"status": "restored", "turn_count": restored.get("turn_count", 0)}
        else:
            result = {"error": "snapshot not found"}
    
    elif action == "status":
        # 返回 session storage 状态信息
        result = {
            "session_id": session_id,
            "storage_path": str(STORAGE_PATH),
            "storage_exists": STORAGE_PATH.exists(),
            "state": state,
            "snapshot_count": len(list_snapshots(session_id)),
        }

    elif action == "ping":
        # HTTP 冷启动测量: 返回容器进程时间戳 (与 WS ping 对齐)
        now = time.time()
        result = {
            "action": "pong",
            "session_id": session_id,
            "image_build_tag": IMAGE_BUILD_TAG,
            "process_start_ts": PROCESS_START_TS,
            "server_recv_ts": now,
            "uptime_s": round(now - PROCESS_START_TS, 3),
            "memstat": memstat(),
        }

    elif action == "memstat":
        result = {"action": "memstat", "session_id": session_id,
                  "image_build_tag": IMAGE_BUILD_TAG,
                  "process_start_ts": PROCESS_START_TS, **memstat()}

    elif action == "alloc":
        result = {"action": "alloc", "session_id": session_id,
                  **alloc_mb(int(payload.get("mb", 0)))}

    elif action == "free":
        result = {"action": "free", "session_id": session_id, **free_ballast()}

    elif action == "spin":
        result = {"action": "spin", "session_id": session_id,
                  **spin_cpu(float(payload.get("seconds", 1)), int(payload.get("threads", 1)))}

    elif action == "exec":
        # 执行 shell 命令 (用于验证挂载点读写/共享/隔离)
        commands = payload.get("commands", [])
        if isinstance(commands, str):
            commands = [commands]
        script = "\n".join(commands)
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", script],
                capture_output=True, text=True, timeout=120,
            )
            result = {
                "session_id": session_id,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except Exception as e:
            result = {"session_id": session_id, "error": f"exec failed: {e}"}

    else:
        result = {"error": f"unknown action: {action}"}

    yield json.dumps(result, ensure_ascii=False)


@app.websocket
async def websocket_handler(websocket, context):
    """
    WebSocket 双向流 handler (/ws, 与 /invocations 共用 8080)。

    协议 (JSON text frame):
      {"action": "ping"}            -> 时间戳 + memstat (冷启动测量)
      {"action": "memstat"}         -> 单次 microVM 资源快照
      {"action": "memwatch", "interval_s":1, "duration_s":30}
                                    -> 连续推送资源快照(与 USAGE_LOGS 1 秒粒度对齐)
      {"action": "alloc", "mb":512} -> 真实占用内存, 反向验证计费口径
      {"action": "spin", "seconds":10, "threads":2} -> 占 CPU, 验证 vcpu.hours
      {"action": "free"}            -> 释放 alloc 的内存
      {"action": "echo", ...}       -> 原样回显
      {"action": "exec", "commands": [...]} -> 执行 shell 命令
      其他                          -> 回显 + 提示
    连接保持 loop, 客户端关闭或异常时退出。
    """
    await websocket.accept()
    accept_ts = time.time()
    session_id = getattr(context, 'session_id', None) or 'default'
    msg_count = 0

    try:
        while True:
            data = await websocket.receive_json()
            msg_count += 1
            recv_ts = time.time()
            action = data.get("action", "echo") if isinstance(data, dict) else "echo"

            if action == "ping":
                await websocket.send_json({
                    "action": "pong",
                    "msg_index": msg_count,
                    "session_id": session_id,
                    "image_build_tag": IMAGE_BUILD_TAG,         # 确认镜像版本
                    # 冷启动分析用的服务端时间戳 (epoch 秒)
                    "process_start_ts": PROCESS_START_TS,      # 容器进程启动时刻
                    "ws_accept_ts": accept_ts,                  # 本连接 accept 时刻
                    "server_recv_ts": recv_ts,                  # 本消息到达时刻
                    "uptime_s": round(recv_ts - PROCESS_START_TS, 3),
                    "memstat": memstat(),                       # microVM 资源快照
                })
            elif action == "memwatch":
                # 服务端按 interval_s 连续推送资源快照, 时长 duration_s。
                # 与 USAGE_LOGS 的 1 秒粒度对齐, 便于逐秒比对 vcpu/memory。
                interval = float(data.get("interval_s", 1.0))
                duration = float(data.get("duration_s", 30.0))
                n = max(1, int(duration / interval))
                prev = None
                for i in range(n):
                    s = memstat()
                    if prev and s.get("vm_cpu_busy_s") is not None:
                        dt = s["ts"] - prev["ts"]
                        dcpu = s["vm_cpu_busy_s"] - prev["vm_cpu_busy_s"]
                        # 该区间的平均 vCPU 占用 (与 vcpu.hours.used 差分对照)
                        s["vcpu_avg"] = round(dcpu / dt, 4) if dt > 0 else None
                        s["interval_s"] = round(dt, 3)
                    await websocket.send_json({"action": "memsample", "seq": i,
                                               "session_id": session_id, **s})
                    prev = s
                    if i < n - 1:
                        await asyncio.sleep(interval)
                await websocket.send_json({"action": "memwatch_done", "samples": n,
                                           "session_id": session_id})
            elif action == "memstat":
                await websocket.send_json({"action": "memstat_result",
                                           "session_id": session_id, **memstat()})
            elif action == "alloc":
                # 放到线程里跑, 避免阻塞事件循环导致 WS 心跳超时
                r = await asyncio.to_thread(alloc_mb, int(data.get("mb", 0)))
                await websocket.send_json({"action": "alloc_result", **r})
            elif action == "free":
                await websocket.send_json({"action": "free_result", **free_ballast()})
            elif action == "spin":
                r = await asyncio.to_thread(spin_cpu, float(data.get("seconds", 1)),
                                            int(data.get("threads", 1)))
                await websocket.send_json({"action": "spin_result", **r})
            elif action == "exec":
                commands = data.get("commands", [])
                if isinstance(commands, str):
                    commands = [commands]
                try:
                    proc = subprocess.run(
                        ["/bin/sh", "-c", "\n".join(commands)],
                        capture_output=True, text=True, timeout=120,
                    )
                    await websocket.send_json({
                        "action": "exec_result",
                        "exit_code": proc.returncode,
                        "stdout": proc.stdout[-8000:],
                        "stderr": proc.stderr[-4000:],
                    })
                except Exception as e:
                    await websocket.send_json({"action": "exec_result", "error": str(e)})
            elif action == "close":
                await websocket.send_json({"action": "bye", "msg_count": msg_count})
                break
            else:
                await websocket.send_json({"echo": data, "msg_index": msg_count})
    except Exception as e:
        log.info(f"websocket loop end: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    app.run()
