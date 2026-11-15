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
             reading it, so the read really goes to the disk: the platform's
             disk warmup call is meant to see those reads.
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
