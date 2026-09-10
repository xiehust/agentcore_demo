"""Dependency-free AgentCore HTTP memory probe; no LLM calls or credentials logged."""
import gc
import json
import mmap
import os
from pathlib import Path
import resource
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIB = 1024 ** 2
BUSY = threading.Lock()

def read_text(path, errors):
    try:
        return Path(path).read_text()
    except OSError as exc:
        errors[str(path)] = str(exc)
        return ""


def parse_kib(text):
    """Keep only byte-valued proc fields; VmFlags/Name are not numbers."""
    result = {}
    for line in text.splitlines():
        parts = line.replace(":", " ", 1).split()
        if len(parts) == 3 and parts[2] == "kB":
            result[parts[0]] = int(parts[1]) * 1024
    return result


def cgroup_snapshot(errors):
    membership = read_text("/proc/self/cgroup", errors)
    # Handle both a cgroup namespace (mount root is self) and host-visible paths.
    candidates = []
    for line in membership.splitlines():
        _, controllers, relative = line.split(":", 2)
        if not controllers:
            candidates += [(Path("/sys/fs/cgroup") / relative.lstrip("/"), 2),
                           (Path("/sys/fs/cgroup"), 2)]
        elif "memory" in controllers.split(","):
            candidates += [(Path("/sys/fs/cgroup/memory") / relative.lstrip("/"), 1),
                           (Path("/sys/fs/cgroup/memory"), 1)]
    for directory, version in candidates:
        current = "memory.current" if version == 2 else "memory.usage_in_bytes"
        if not (directory / current).exists():
            continue
        result = {"version": version, "path": str(directory), "membership": membership}
        names = [current, "memory.stat"]
        names += (["memory.peak", "memory.max", "memory.events"] if version == 2 else
                  ["memory.max_usage_in_bytes", "memory.limit_in_bytes"])
        for name in names:
            text = read_text(directory / name, errors).strip()
            if not text:
                result[name] = None
            elif "\n" in text or " " in text:
                result[name] = {k: int(v) for k, v in (s.split() for s in text.splitlines())}
            else:
                result[name] = int(text) if text.isdigit() else text
        return result
    errors["cgroup"] = "No readable memory controller at the resolved paths"
    return {"membership": membership}


def snapshot(phase):
    errors = {}
    status = parse_kib(read_text("/proc/self/status", errors))
    smaps = parse_kib(read_text("/proc/self/smaps_rollup", errors))
    meminfo = parse_kib(read_text("/proc/meminfo", errors))
    return {"timestamp": time.time(), "phase": phase, "pid": os.getpid(),
            "process": status, "smaps_rollup": smaps,
            "ru_maxrss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "cgroup": cgroup_snapshot(errors), "guest_meminfo": meminfo, "errors": errors}


def environment():
    errors = {}
    processes = []
    for directory in Path("/proc").glob("[0-9]*"):
        status = read_text(directory / "status", errors)
        name = next((s.split(":", 1)[1].strip() for s in status.splitlines()
                     if s.startswith("Name:")), "?")
        processes.append({"pid": int(directory.name), "name": name,
                          "memory": parse_kib(status)})
    return {"uname": list(os.uname()), "processes": processes,
            "mountinfo": read_text("/proc/self/mountinfo", errors),
            "boot_id": read_text("/proc/sys/kernel/random/boot_id", errors).strip(),
            "errors": errors}

def experiment(payload):
    kind = payload.get("kind", "baseline")
    size = payload.get("mib", 256)
    duration = payload.get("phase_seconds", 30)
    if kind not in {"baseline", "anonymous", "file_cache", "image_read"}:
        raise ValueError("Unknown experiment kind")
    if type(size) is not int or not 1 <= size <= 512:
        raise ValueError("mib must be an integer in [1, 512]")
    if type(duration) not in {int, float} or not 0.1 <= duration <= 45:
        raise ValueError("phase_seconds must be in [0.1, 45]")
    run_id = payload.get("run_id", "local")
    if not isinstance(run_id, str) or len(run_id) > 100:
        raise ValueError("run_id must be a string of at most 100 characters")
    samples, phases = [], []
    allocation = None
    file = None

    def hold(label):
        phase = {"phase": label, "start": time.time()}
        end = time.monotonic() + duration
        while True:
            sample = snapshot(label)
            sample["run_id"] = run_id
            samples.append(sample)
            print(json.dumps({"type": "memory_sample", **sample}), flush=True)
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))
        phase["end"] = time.time()
        phases.append(phase)

    result = {"run_id": run_id, "kind": kind, "mib": size,
              "environment": environment(), "started": time.time()}
    try:
        hold("baseline")
        if kind == "anonymous":
            # MAP_PRIVATE anonymous pages, explicitly dirtied. close() returns them to OS.
            allocation = mmap.mmap(-1, size * MIB,
                                   flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            for offset in range(0, size * MIB, mmap.PAGESIZE):
                allocation[offset] = 1
            hold("allocated")
            allocation.close()
            allocation = None
            gc.collect()
            hold("released")
        elif kind in {"file_cache", "image_read"}:
            if kind == "file_cache":
                file = tempfile.TemporaryFile(dir="/tmp")
                # Reuse a 1 MiB buffer, so file size is not mirrored in Python RSS.
                block = b"x" * MIB
                for _ in range(size):
                    file.write(block)
                file.flush()
                os.fsync(file.fileno())
                del block
                file.seek(0)
            else:
                file = open("/opt/image-pad.bin", "rb")
            while file.read(MIB):
                pass
            hold("file_cached")
            # Advisory, per-file eviction only. Never use global drop_caches.
            advice = "POSIX_FADV_DONTNEED"
            try:
                os.posix_fadvise(file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                result["eviction_advice"] = advice + " accepted (not guaranteed)"
            except OSError as exc:
                result["eviction_advice"] = str(exc)
            hold("after_fadvise")
            file.close()
            file = None
            hold("file_closed")
        else:
            hold("idle_control")
    finally:
        if allocation is not None:
            allocation.close()
        if file is not None:
            file.close()
    result.update(phases=phases, samples=samples, ended=time.time())
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, body):
        encoded = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/ping":
            self.send_json(200, {"status": "HealthyBusy" if BUSY.locked() else "Healthy"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/invocations":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Body must be 1..4096 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
        except (ValueError, UnicodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        if not BUSY.acquire(blocking=False):
            self.send_json(409, {"error": "Experiment already running"})
            return
        try:
            self.send_json(200, experiment(payload))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
        finally:
            BUSY.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

