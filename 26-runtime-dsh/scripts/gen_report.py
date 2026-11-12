"""Generate results/REPORT.md (English) and results/REPORT.zh.md (Chinese)
from results/summary.json, the raw files under results/raw, deployments.json
and, when present, results/local_baseline.json (scripts/local_baseline.py).
The page-pin sample code is copied from app/pinner.py and app/server.mjs at
generation time, so the report always shows the code that was measured.

`--platforms V2` limits the report to runtimes on those platform versions; the
header then names what was measured but left out.

The report covers two things only: the cold start of a new session and its
first message. Later messages on a session are sent by the benchmark but not
reported, because they measure DSH and the model rather than a session start.

Both reports come from one renderer, so they always have the same structure,
and every figure is read from (or computed from) those data files: nothing is
typed in by hand. Runtimes, platform versions, sequences and concurrency
levels are discovered from the data, so cells added by a later run appear
automatically.

summary.json decides WHICH raw files count (the latest one per cell) and
supplies the totals; the statistics are recomputed here from those raw
requests, joined with each runtime's platform_version from deployments.json.
That join is what turns a "warm_pool" record on a platform V2 runtime into a
snapshot restore, including in raw files written before that label existed.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SEQUENCES = ("ping", "agent")
BOOT_KINDS = ("real_boot", "warm_pool", "snapshot_restore")
SNAPSHOT_PLATFORMS = {"V2"}  # platform versions that restore sessions from a snapshot
PERCENTILES = {"p50": 50, "p90": 90, "p99": 99}
MAX_FAILURE_ROWS = 20
# Cold agent turns restored from one snapshot, grouped by order of use: (first, last).
TURN_BUCKETS = ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 10), (11, None))
DASH = "—"
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class ReportError(Exception):
    """The data files needed for the report are missing or empty."""


def fmt(value, digits: int = 0) -> str:
    return DASH if value is None else f"{value:,.{digits}f}"


def megabytes(size) -> str:
    return DASH if size is None else f"{size / 2**20:,.0f} MB"


def code_list(values) -> str:
    items = sorted({str(v) for v in values if v is not None})
    return ", ".join(f"`{v}`" for v in items) if items else DASH


def env_text(env) -> str:
    """Runtime env from deployments.json; values of secret-looking keys are hidden."""
    if not isinstance(env, dict) or not env:
        return DASH
    return ", ".join(
        f"`{k}={'<hidden>' if any(h in k.upper() for h in SECRET_HINTS) else v}`"
        for k, v in sorted(env.items()))


def image_text(uri) -> str:
    """Image reference without the registry host, which carries the account id."""
    return str(uri or DASH).split("/", 1)[-1]


def percentile(values: list, pct: float):
    """Linear-interpolated percentile, the same rule coldstart_test.py uses,
    rounded the way summary.json rounds it."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def spread(values: list, names=("p50", "p90", "max")) -> str:
    """'p50 / p90 / max (n)' for a list of samples."""
    vals = [v for v in values if v is not None]
    if not vals:
        return DASH
    parts = [fmt(max(vals)) if name == "max" else fmt(percentile(vals, PERCENTILES[name]))
             for name in names]
    return " / ".join(parts) + f" ({len(vals)})"


def p50_n(values: list) -> str:
    return spread(values, ("p50",))


def fanout(meta: dict) -> tuple:
    """(mode, client processes, most threads in one process) of a raw cell file.
    Files written before --processes existed are one process with N threads."""
    threads = meta.get("threads_per_process") or [meta["concurrency"]]
    return meta.get("process_mode", "inline"), meta.get("processes", 1), max(threads)


def round_spreads(requests: list, value) -> list:
    """Per round: max - min of value(record) over the cold requests that were
    sent, for rounds with at least two of them. `value` may return None."""
    rounds: dict[int, list] = {}
    for r in requests:
        if r["phase"] == "cold" and r["attempted"] and value(r) is not None:
            rounds.setdefault(r["round"], []).append(value(r))
    return [max(v) - min(v) for v in rounds.values() if len(v) > 1]


def t0_spread_ms(requests: list) -> list:
    """How far apart the cold requests of a round started, from their send timestamps."""
    return [spread * 1000.0 for spread in round_spreads(requests, lambda r: r.get("send_epoch"))]


def small(values: list, names=("p50", "p90", "max")) -> str:
    """Like spread(), with one decimal and no count: for millisecond-scale client delays."""
    vals = [v for v in values if v is not None]
    if not vals:
        return DASH
    return " / ".join(fmt(max(vals), 1) if name == "max" else fmt(percentile(vals, PERCENTILES[name]), 1)
                      for name in names)


def table_text(text) -> str:
    flat = " ".join(str(text or "").split()).replace("|", "\\|")
    return flat if len(flat) <= 120 else flat[:117] + "..."


def epoch_of(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


def clock_text(epoch) -> str:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return DASH
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S")


def pin_sources(app_dir: Path) -> dict:
    """The page-pin sample code as it is in the repository: the pinner, the
    wrapper's hooks around it, where they are called, and the manifest size."""
    def read(name: str) -> str | None:
        path = app_dir / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    out = {"pinner": read("pinner.py"), "hooks": None, "calls": [], "manifest_files": None}
    wrapper = read("server.mjs")
    if wrapper:
        start, last = wrapper.find("// Page pin"), wrapper.find("function releasePinner(")
        end = wrapper.find("\n}\n", last) if last >= 0 else -1
        if 0 <= start < last and end >= 0:
            out["hooks"] = wrapper[start:end + 2]
        out["calls"] = [line.strip() for line in wrapper.splitlines()
                        if ("startPinner(" in line or "releasePinner(" in line)
                        and not line.lstrip().startswith("function")]
    manifest = read("pin-manifest.txt")
    if manifest:
        out["manifest_files"] = sum(1 for line in manifest.splitlines()
                                    if line.strip() and not line.startswith("#"))
    return out


def load_inputs(results_dir: Path, deployments_path: Path, app_dir: Path | None = None) -> dict:
    """Read every input. 'raws' are the raw cell files summary.json counts,
    'history' is every raw cell file on disk (including superseded ones)."""
    summary_path = results_dir / "summary.json"
    for path, hint in ((deployments_path, "run scripts/deploy.sh"),
                       (summary_path, "run coldstart_test.py --full")):
        if not path.exists():
            raise ReportError(f"{path} not found - {hint} first.")
    summary = json.loads(summary_path.read_text())
    if not summary.get("cells"):
        raise ReportError(f"{summary_path} has no cells - run coldstart_test.py --full first.")
    history, smokes = [], []
    for path in sorted((results_dir / "raw").glob("*.json")):
        data = json.loads(path.read_text())
        data["file"] = path.name
        (smokes if path.name.startswith("smoke") else history).append(data)
    by_name = {data["file"]: data for data in history}
    missing = [name for name in summary.get("sources", []) if name not in by_name]
    if missing:
        raise ReportError(f"{results_dir / 'raw' / missing[0]} is listed in summary.json but missing.")
    local_path = results_dir / "local_baseline.json"
    return {
        "dep": json.loads(deployments_path.read_text()),
        "summary": summary,
        "raws": [by_name[name] for name in summary.get("sources", [])],
        "history": history,
        "smokes": smokes,
        "local": json.loads(local_path.read_text()) if local_path.exists() else None,
        "pin": pin_sources(app_dir if app_dir is not None else HERE / "app"),
    }


def scope_inputs(data: dict, platforms: set | None) -> dict:
    """Keep only the runtimes on the given platform versions (None = all).
    Adds 'excluded': {runtime: platform version} for what was measured but dropped."""
    if not platforms:
        return {**data, "excluded": {}}
    conf = {**data["dep"].get("removed_runtimes", {}), **data["dep"].get("runtimes", {})}
    files = [*data["history"], *data["smokes"]]
    recorded = {r["runtime"]: r.get("platform_version") for raw in files for r in raw["requests"]}
    versions = {r: conf.get(r, {}).get("platform_version") or recorded.get(r) for r in recorded}
    keep = {r for r, version in versions.items() if version in platforms}
    if not keep:
        raise ReportError(f"no measured runtime is on platform {', '.join(sorted(platforms))} "
                          f"(found: {', '.join(sorted({str(v) for v in versions.values()}))}).")

    def narrowed(raw: dict) -> dict:
        requests = [r for r in raw["requests"] if r["runtime"] in keep]
        sessions = {r["session_id"] for r in requests}
        return {**raw, "requests": requests,
                "sessions": [s for s in raw.get("sessions", []) if s["session_id"] in sessions]}

    return {**data,
            "raws": [raw for raw in data["raws"] if raw["meta"]["runtime"] in keep],
            "history": [raw for raw in data["history"] if raw["meta"]["runtime"] in keep],
            "smokes": [narrowed(raw) for raw in data["smokes"]],
            "excluded": {r: versions[r] for r in sorted(versions) if r not in keep}}


class Report:
    def __init__(self, dep: dict, summary: dict, raws: list, smokes: list,
                 local: dict | None, lang: str, history: list | None = None,
                 pin: dict | None = None, excluded: dict | None = None):
        self.dep, self.summary, self.local, self.lang = dep, summary, local, lang
        self.conf = dep.get("runtimes", {})
        self.pin = pin or {}
        self.excluded = excluded or {}
        self.smokes = smokes
        self.history = raws if history is None else history
        self._raws = raws
        self.sources = {raw.get("file") for raw in raws}
        self.requests = [r for raw in raws for r in raw["requests"]]
        seen = {r["runtime"] for r in self.requests}
        self.runtimes = [r for r in self.conf if r in seen] + sorted(seen - set(self.conf))
        self.sequences = [s for s in SEQUENCES if any(r["sequence"] == s for r in self.requests)]
        self.concs = sorted({r["concurrency"] for r in self.requests})
        self.levels = ", ".join(f"c={c}" for c in self.concs)
        self.metas = sorted((raw["meta"] for raw in raws), key=lambda m: (
            self.runtimes.index(m["runtime"]), SEQUENCES.index(m["sequence"]), m["concurrency"]))
        # Counted here, not taken from summary.json, so they follow the report's scope.
        sent = [r for r in self.requests if r["attempted"]]
        sessions = [session for raw in raws for session in raw.get("sessions", [])]
        self.totals = {
            "attempted": len(sent), "failures": sum(1 for r in sent if not r["ok"]),
            "skipped": len(self.requests) - len(sent),
            "anomalies": sum(1 for r in self.requests if r.get("anomalies")),
            "sessions": len(sessions), "sessions_stopped": sum(1 for x in sessions if x["stopped"]),
        }
        recorded = {r["runtime"]: r.get("platform_version")
                    for raw in [*self.history, *smokes] for r in raw["requests"]}
        self.versions = {r: self.conf.get(r, {}).get("platform_version") or recorded.get(r)
                         for r in {*self.conf, *recorded}}
        self.kind_labels = {
            "real_boot": self.tr("real boot", "真实启动"),
            "warm_pool": self.tr("warm-pool hit", "预热命中"),
            "snapshot_restore": self.tr("snapshot restore", "快照恢复"),
        }
        self.L: list[str] = []

    # ------------------------------------------------------------ helpers
    def tr(self, en: str, zh: str) -> str:
        return en if self.lang == "en" else zh

    def add(self, en: str, zh: str) -> None:
        self.L.append(self.tr(en, zh))

    def version(self, runtime: str) -> str:
        return self.versions.get(runtime) or DASH

    def is_snapshot(self, runtime: str) -> bool:
        return self.versions.get(runtime) in SNAPSHOT_PLATFORMS

    def tag(self, runtime: str) -> str:
        version = self.versions.get(runtime)
        return f"`{runtime}` ({version})" if version else f"`{runtime}`"

    def kind(self, record: dict) -> str:
        """Boot kind joined with the runtime's platform version at report time."""
        kind = record["boot_kind"]
        return "snapshot_restore" if kind == "warm_pool" and self.is_snapshot(record["runtime"]) else kind

    def select(self, runtime=None, sequence=None, phase=None, conc=None, boot="all",
               records=None) -> list:
        """Successful requests matching the filters (None / "all" = any)."""
        return [r for r in (self.requests if records is None else records)
                if r["ok"]
                and runtime in (None, r["runtime"]) and sequence in (None, r["sequence"])
                and phase in (None, r["phase"]) and conc in (None, r["concurrency"])
                and boot in ("all", self.kind(r))]

    def vals(self, metric: str, **filters) -> list:
        return [r[metric] for r in self.select(**filters) if r.get(metric) is not None]

    @property
    def raws(self) -> list:
        return self._raws

    def sample(self, values: list) -> str:
        if not values:
            return self.tr("no samples", "无样本")
        return f"p50 {fmt(percentile(values, 50))} ms (n={len(values)})"

    def kinds_for(self, runtime: str) -> list:
        """Boot kinds a runtime can show: V1 boots or hits the warm pool, V2
        restores a snapshot (plus real boots if the data has any)."""
        if not self.is_snapshot(runtime):
            return ["real_boot", "warm_pool"]
        extra = ["real_boot"] if self.select(runtime=runtime, phase="cold", boot="real_boot") else []
        return ["snapshot_restore", *extra]

    def pairs(self) -> list:
        """(V1 runtime or None, V2 runtime): same image and env, different platform."""
        def signature(runtime):
            conf = self.conf.get(runtime, {})
            return conf.get("image_uri"), json.dumps(conf.get("env"), sort_keys=True)
        return [(next((r for r in self.runtimes if not self.is_snapshot(r)
                       and signature(r) == signature(v2)), None), v2)
                for v2 in self.runtimes if self.is_snapshot(v2)]

    def fanout_label(self, mode: str, processes: int, threads: int) -> str:
        def count(number: int, noun: str) -> str:
            return f"{number} {noun}" if number == 1 else f"{number} {noun}s"

        if mode == "inline":
            return self.tr(f"single process, {count(threads, 'thread')}",
                           f"单进程,{threads} 个线程")
        return self.tr(f"{count(processes, 'spawned process').replace('processs', 'processes')} × "
                       f"up to {count(threads, 'thread')}",
                       f"{processes} 个 spawn 进程 × 每个最多 {threads} 个线程")

    def client_cells(self, raws: list, with_count: bool = True) -> list:
        """Table cells for the client-side delays of the cold requests in `raws`."""
        cold = [r for raw in raws for r in raw["requests"] if r["phase"] == "cold" and r["attempted"]]
        spreads = [x for raw in raws for x in t0_spread_ms(raw["requests"])]
        cells = [small(spreads, ("p50", "max")),
                 small([r.get("release_to_start_ms") for r in cold]),
                 small([r.get("start_to_send_ms") for r in cold]),
                 small([r.get("connect_ms") for r in cold])]
        return ([str(len(cold))] if with_count else []) + cells

    def pin_details(self, runtime: str) -> list:
        """What the container reported about its page pinner, one entry per answer."""
        out = []
        for r in self.requests:
            server = r.get("server") if r["runtime"] == runtime else None
            prewarm = server.get("prewarm") if isinstance(server, dict) else None
            detail = prewarm.get("detail") if isinstance(prewarm, dict) else None
            if isinstance(detail, dict) and detail.get("files"):
                out.append(detail)
        return out

    def pin_pairs(self) -> list:
        """(control runtime or None, pinned runtime): same image, same platform kind."""
        pinned = [r for r in self.runtimes if self.pin_details(r)]
        return [(next((r for r in self.runtimes if r not in pinned and self.is_snapshot(r) == self.is_snapshot(p)
                       and self.conf.get(r, {}).get("image_uri") == self.conf.get(p, {}).get("image_uri")),
                      None), p) for p in pinned]

    def deployed_epoch(self, runtime: str) -> float | None:
        return epoch_of(self.conf.get(runtime, {}).get("deployed_at") or self.dep.get("deployed_at"))

    def timeline(self, runtime=None) -> list:
        """Every cold agent probe sent to a snapshot-platform runtime, in send
        order, from the counted raw files, superseded ones and smoke files, as
        (record, source, turn). `turn` counts the cold agent turns answered by
        the same snapshot (same runtime and wrapper process start) so far."""
        rows = []
        for raw, smoke in [*((h, False) for h in self.history), *((s, True) for s in self.smokes)]:
            source = (self.tr("smoke", "smoke") if smoke
                      else self.tr("matrix", "矩阵") if raw.get("file") in self.sources
                      else self.tr("superseded", "已被替代"))
            rows += [(r, source) for r in raw["requests"]
                     if r["sequence"] == "agent" and r["phase"] == "cold" and r["attempted"]
                     and self.is_snapshot(r["runtime"])]
        turns: dict[tuple, int] = {}
        ordered = []
        for r, source in sorted(rows, key=lambda row: row[0]["send_epoch"] or 0):
            start = r["server"].get("proc_start_ts") if isinstance(r.get("server"), dict) else None
            turn = None
            if start is not None:
                turn = turns[(r["runtime"], start)] = turns.get((r["runtime"], start), 0) + 1
            ordered.append((r, source, turn))
        return [row for row in ordered if runtime in (None, row[0]["runtime"])]

    @staticmethod
    def bucket_label(first: int, last: int | None) -> str:
        if last is None:
            return f"{first}+"
        return f"{first}" if first == last else f"{first}–{last}"

    def turn_buckets(self, runtime: str) -> list:
        """[(label, successful cold agent records)] by order of use of a snapshot."""
        rows = [(r, turn) for r, _, turn in self.timeline(runtime) if r["ok"] and turn]
        buckets = [(self.bucket_label(first, last),
                    [r for r, turn in rows if turn >= first and (last is None or turn <= last)])
                   for first, last in TURN_BUCKETS]
        return [(label, records) for label, records in buckets if records]

    def age_minutes(self, record: dict) -> float | None:
        deployed = self.deployed_epoch(record["runtime"])
        if deployed is None or record.get("send_epoch") is None:
            return None
        return (record["send_epoch"] - deployed) / 60.0

    # ------------------------------------------------------------- header
    def header(self) -> None:
        dep, totals = self.dep, self.totals
        started = min((m["started_iso"] for m in self.metas), default=None)
        finished = max((m["finished_iso"] for m in self.metas if m.get("finished_iso")), default=None)
        if started and finished:
            end = (finished[11:16] if finished[:10] == started[:10]
                   else f"{finished[:10]} {finished[11:16]}")
            when = f"{started[:10]} {started[11:16]}–{end} UTC"
        else:
            when = DASH
        served = [r["server"] for r in self.requests if isinstance(r.get("server"), dict)]
        agents = [s["agent"] for s in served if isinstance(s.get("agent"), dict)]
        account = str(dep.get("account") or "")
        self.add("# DeepSeek Harness (dsh) on AgentCore Runtime — cold-start benchmark results\n",
                 "# DeepSeek Harness(dsh)运行在 AgentCore Runtime 上 — 冷启动基准测试结果\n")
        self.add(f"- **Test date:** {when} · **Region:** {dep.get('region', DASH)} · "
                 f"**Account:** …{account[-4:]}",
                 f"- **测试日期:** {when} · **区域:** {dep.get('region', DASH)} · "
                 f"**账号:** …{account[-4:]}")
        self.add(f"- **Agent:** `dsh headless` (npm `@deepseek-ai/dsh` "
                 f"`{dep.get('dsh_version', DASH)}`) behind a Node HTTP wrapper · "
                 f"**Model:** `{dep.get('model_id', DASH)}` (both from `deployments.json`)",
                 f"- **Agent:** Node HTTP 包装器后面的 `dsh headless`(npm `@deepseek-ai/dsh` "
                 f"`{dep.get('dsh_version', DASH)}`) · "
                 f"**模型:** `{dep.get('model_id', DASH)}`(均来自 `deployments.json`)")
        self.add(f"- **Reported by the container during the test:** dsh "
                 f"{code_list(s.get('dsh_version') for s in served)}, model "
                 f"{code_list(a.get('model') for a in agents)}, image tag "
                 f"{code_list(s.get('image_tag') for s in served)}",
                 f"- **测试期间容器自报:** dsh "
                 f"{code_list(s.get('dsh_version') for s in served)},模型 "
                 f"{code_list(a.get('model') for a in agents)},镜像 tag "
                 f"{code_list(s.get('image_tag') for s in served)}")
        self.add("- **Runtimes** (platform version · uncompressed docker / compressed ECR image · "
                 "image · env):",
                 "- **Runtime**(平台版本 · 未压缩 docker / 压缩后 ECR 镜像 · 镜像 · 环境变量):")
        for runtime in self.runtimes:
            rt = self.conf.get(runtime, {})
            self.L.append(
                f"  - `{rt.get('name', runtime)}` (`{runtime}`) — "
                f"{self.tr('platform', '平台')} `{self.version(runtime)}` — "
                f"{megabytes(rt.get('docker_size_bytes'))} / {megabytes(rt.get('ecr_size_bytes'))}"
                f" — `{image_text(rt.get('image_uri'))}` — env: {env_text(rt.get('env'))}")
        self.add(f"- **Requests:** {totals['attempted']} sent on {totals['sessions']} sessions, "
                 f"{totals['failures']} failed, {totals['skipped']} not sent "
                 "(the second invoke of a session is skipped when its first one never reached the "
                 "container; a first invoke is not sent when its client worker process failed "
                 "before the release)",
                 f"- **请求:** 共发送 {totals['attempted']} 个,分布在 {totals['sessions']} 个 session 上,"
                 f"失败 {totals['failures']} 个,未发送 {totals['skipped']} 个"
                 "(首次调用未到达容器时,跳过该 session 的第二次调用;客户端 worker 进程在释放前失败时,"
                 "其首次调用不会发出)")
        if self.excluded:
            scope = ", ".join(sorted({self.version(r) for r in self.runtimes}))
            left_out = ", ".join(f"`{r}` ({version or DASH})" for r, version in self.excluded.items())
            self.add(f"- **Scope:** platform {scope} only. Also measured, and left out of this "
                     f"report on purpose: {left_out}; their raw data is still in `results/raw/`.",
                     f"- **范围:**仅平台 {scope}。另外测量过、但有意不纳入本报告的 runtime:{left_out};"
                     f"其原始数据仍保留在 `results/raw/` 中。")
        if self.local is None:
            self.add("- **Local Docker baseline:** `results/local_baseline.json` not found, so the "
                     "local Docker vs AgentCore Runtime section is omitted.",
                     "- **本地 Docker 基线:**未找到 `results/local_baseline.json`,因此省略"
                     "“本地 Docker vs AgentCore Runtime”一节。")
        if self.summary.get("interrupted"):
            self.add("- **Note:** the run was interrupted; some cells are partial.",
                     "- **注意:** 测试被中断,部分格子的数据不完整。")
        self.L.append("")

    # ------------------------------------------------------------ summary
    def variant_text(self, runtime: str) -> str:
        """What sets this runtime apart: env values and image tag, where runtimes differ."""
        confs = [self.conf.get(r, {}) for r in self.runtimes]
        envs = [c.get("env") if isinstance(c.get("env"), dict) else {} for c in confs]
        keys = sorted(k for k in {k for e in envs for k in e} if len({e.get(k) for e in envs}) > 1)
        mine = self.conf.get(runtime, {})
        env = mine.get("env") if isinstance(mine.get("env"), dict) else {}
        hidden = {k for k in keys if any(h in k.upper() for h in SECRET_HINTS)}
        parts = [f"{k}={DASH if k not in env else '<hidden>' if k in hidden else env[k]}" for k in keys]
        tags = {image_text(c.get("image_uri")).rsplit(":", 1)[-1] for c in confs}
        if len(tags) > 1:
            parts.append(image_text(mine.get("image_uri")).rsplit(":", 1)[-1])
        return ", ".join(f"`{part}`" for part in parts) or DASH

    def local_first_answer(self) -> list:
        """Local Docker: container start + cold agent request, per round."""
        records = (self.local or {}).get("records", [])
        return [r["container_start_ms"] + r["client_total_ms"] for r in records
                if r.get("ok") and r.get("sequence") == "agent" and r.get("phase") == "cold"
                and r.get("container_start_ms") is not None and r.get("client_total_ms") is not None]

    def summary_section(self) -> None:
        totals = self.totals
        sep = self.tr("; ", ";")
        self.add("## Summary\n", "## 结论摘要\n")
        self.add(f"Two things are measured. **Cold start**: the first invoke of a new session "
                 f"only returns timestamps (`ping`), so it is the platform alone. **First "
                 f"message**: the first invoke of a new session is a real `dsh headless` turn, "
                 f"timed until the complete answer is back; `DSH boot` is the part of it from "
                 f"spawning `dsh` to its first output, before any model call. p50 in ms, number "
                 f"of samples in brackets, pooled over every concurrency level run "
                 f"({self.levels}). Later messages on the same session are not reported: they "
                 f"measure DSH and the model, not the start of a session.\n",
                 f"本报告只看两件事。**冷启动**:新 session 的首次调用只返回时间戳(`ping`),因此只包含平台本身。"
                 f"**第一条消息**:新 session 的首次调用是一次真实的 `dsh headless` 对话,计时到完整回答返回为止;"
                 f"`DSH 启动` 是其中从创建 `dsh` 进程到它首次输出的部分,发生在调用模型之前。数值为 p50,单位毫秒,"
                 f"括号内为样本数,合并了所有已测并发档位({self.levels})。同一 session 上的后续消息不在报告范围内:"
                 f"它们反映的是 DSH 和模型,而不是 session 的启动。\n")
        self.add("| runtime | platform | variant | cold start (ping) | first message | "
                 "of which DSH boot | cold requests ok / sent |",
                 "| runtime | 平台 | 变体 | 冷启动(ping) | 第一条消息 | 其中 DSH 启动 | "
                 "冷请求 成功 / 已发送 |")
        self.L.append("|---|---|---|---|---|---|---|")
        for runtime in self.runtimes:
            def by_kind(sequence, metric="total_ms", runtime=runtime) -> str:
                cells = [(k, self.vals(metric, runtime=runtime, sequence=sequence,
                                       phase="cold", boot=k)) for k in self.kinds_for(runtime)]
                return " · ".join(f"{self.kind_labels[k]} {p50_n(v)}" for k, v in cells if v) or DASH
            sent = [r for r in self.requests
                    if r["runtime"] == runtime and r["phase"] == "cold" and r["attempted"]]
            self.L.append(
                f"| `{runtime}` | {self.version(runtime)} | {self.variant_text(runtime)} | "
                f"{by_kind('ping')} | {by_kind('agent')} | {by_kind('agent', 'dsh_boot_ms')} | "
                f"{sum(1 for r in sent if r['ok'])} / {len(sent)} |")
        self.L.append("")

        booting = [r for r in self.runtimes if not self.is_snapshot(r)]
        if booting:
            shares = []
            for c in self.concs:
                real = sum(len(self.select(runtime=r, phase="cold", conc=c, boot="real_boot"))
                           for r in booting)
                pool = sum(len(self.select(runtime=r, phase="cold", conc=c, boot="warm_pool"))
                           for r in booting)
                if real + pool:
                    shares.append(f"c={c}: {real / (real + pool) * 100:.0f}% ({real}/{real + pool})")
            self.add(f"- **A new session only boots a container when the warm pool is used up.** "
                     f"Share of cold requests that were real boots on {code_list(booting)}, by "
                     f"concurrency: {sep.join(shares)}.",
                     f"- **只有预热池被用完时,新 session 才会真正启动容器。**{code_list(booting)} 上冷请求中"
                     f"真实启动的占比,按并发:{sep.join(shares)}。")
        for v1, v2 in self.pairs():
            if not v1:
                continue
            columns = self.platform_columns(v1, v2)

            def line(sequence, columns=columns) -> str:
                return sep.join(f"{self.kind_labels[k]} " + self.sample(
                    self.vals("total_ms", runtime=r, sequence=sequence, phase="cold", boot=k))
                    for r, k, _ in columns)
            if "ping" in self.sequences:
                self.add(f"- **{self.tag(v1)} vs {self.tag(v2)}, cold start:** {line('ping')}.",
                         f"- **{self.tag(v1)} 对比 {self.tag(v2)},冷启动:**{line('ping')}。")
            if "agent" in self.sequences:
                self.add(f"- **{self.tag(v1)} vs {self.tag(v2)}, first message:** {line('agent')}.",
                         f"- **{self.tag(v1)} 对比 {self.tag(v2)},第一条消息:**{line('agent')}。")
        for runtime in [r for r in self.runtimes if self.is_snapshot(r)]:
            buckets = self.turn_buckets(runtime)
            if len(buckets) < 2:
                continue
            (first, early), (last, late) = buckets[0], buckets[-1]
            self.add(f"- **{self.tag(runtime)}: the first message is slow until its snapshot has "
                     f"been used a few times.** Cold agent turn number {first} served by the same "
                     f"snapshot: {self.sample([r['total_ms'] for r in early])}; turn {last}: "
                     f"{self.sample([r['total_ms'] for r in late])} (every raw file, including "
                     f"superseded runs and smoke probes).",
                     f"- **{self.tag(runtime)}:在快照被使用过几次之前,第一条消息很慢。**同一快照应答的第 "
                     f"{first} 次冷 agent 对话:{self.sample([r['total_ms'] for r in early])};第 {last} 次:"
                     f"{self.sample([r['total_ms'] for r in late])}(统计全部原始文件,包含被替代的运行和 "
                     f"smoke 探测)。")
        for control, pinned in self.pin_pairs():
            if not control or "agent" not in self.sequences:
                continue
            both = [self.sample(self.vals(metric, runtime=r, sequence="agent", phase="cold"))
                    for metric in ("total_ms", "dsh_boot_ms") for r in (control, pinned)]
            self.add(f"- **Page pin (`{pinned}` against its control `{control}`, same image):** "
                     f"first message {both[0]} without it and {both[1]} with it; DSH boot "
                     f"{both[2]} without it and {both[3]} with it. The sample code is in the page "
                     f"pin section.",
                     f"- **Page pin(`{pinned}` 与同镜像的对照组 `{control}`):**第一条消息,不加时 {both[0]},"
                     f"加上后 {both[1]};DSH 启动,不加时 {both[2]},加上后 {both[3]}。示例代码见 page pin 一节。")
        local_answer = self.local_first_answer()
        if local_answer:
            self.add(f"- **Local Docker floor:** container start + first answer "
                     f"{self.sample(local_answer)}, with a warm host page cache and no image pull.",
                     f"- **本地 Docker 下限:**容器启动 + 首次回答 {self.sample(local_answer)},"
                     f"条件是主机页缓存已预热且无需拉取镜像。")
        self.add(f"- **Reliability:** {totals['failures']} failed out of {totals['attempted']} "
                 f"requests sent; sessions stopped after measurement: "
                 f"{totals['sessions_stopped']}/{totals['sessions']}.\n",
                 f"- **可靠性:**失败 {totals['failures']} 个,共发送 {totals['attempted']} 个请求;"
                 f"测量后已停止的 session:{totals['sessions_stopped']}/{totals['sessions']}。\n")

    # ------------------------------------------------- latency by cell
    def latency_table(self, phase: str, names: tuple) -> None:
        self.L.append("| runtime · sequence | " + " | ".join(f"c={c}" for c in self.concs) + " |")
        self.L.append("|---|" + "---|" * len(self.concs))
        for runtime in self.runtimes:
            for sequence in self.sequences:
                cols = [spread(self.vals("total_ms", runtime=runtime, sequence=sequence,
                                         phase=phase, conc=c), names) for c in self.concs]
                self.L.append(f"| **{runtime} · {sequence}** | " + " | ".join(cols) + " |")
        self.L.append("")

    def cold_latency(self) -> None:
        self.add("## Cold start and first message by concurrency (end-to-end, ms)\n",
                 "## 各并发档位的冷启动与第一条消息(端到端,毫秒)\n")
        self.add("The first invoke of a new session, all successful samples. `ping` rows are the "
                 "cold start (the platform alone); `agent` rows are the first message (a real "
                 "`dsh headless` turn as that first invoke). Each cell is p50 / p90 / p99 / max "
                 "(number of samples).\n",
                 "每个新 session 首次调用的全部成功样本。`ping` 行是冷启动(只包含平台本身);`agent` 行是"
                 "第一条消息(把一次真实的 `dsh headless` 对话作为首次调用)。每格为 p50 / p90 / p99 / max"
                 "(样本数)。\n")
        self.latency_table("cold", ("p50", "p90", "p99", "max"))

    # ---------------------------------------------------- two populations
    def populations(self) -> None:
        booting = [r for r in self.runtimes if not self.is_snapshot(r)]
        if not booting:
            return
        self.add("## The two populations: real microVM boots vs warm-pool hits\n",
                 "## 两类群体:真实微VM启动 vs 预热池命中\n")
        self.add("A “cold” invoke (fresh `runtimeSessionId`) does **not** always boot a microVM: "
                 "AgentCore keeps a pool of pre-provisioned instances per runtime. The wrapper "
                 "returns its own process start time, so a probe is a *real boot* when the process "
                 "started after the request was sent (`response_ts − proc_start_ts` shorter than the "
                 "client-side time) and a *warm-pool hit* otherwise. Both timestamps are on the "
                 "microVM clock, so client clock skew does not matter. Latencies are end-to-end ms; "
                 "the share is real boots / classified samples.\n",
                 "使用全新 `runtimeSessionId` 的“冷”调用**并不一定**触发微VM启动:AgentCore 为每个 "
                 "runtime 维护一个预先创建的实例池。包装器会返回自身进程的启动时间,因此若进程是在请求发出"
                 "之后才启动的(`response_ts − proc_start_ts` 小于客户端耗时),该探测即为*真实启动*,"
                 "否则为*预热池命中*。两个时间戳都取自微VM时钟,不受客户端时钟偏差影响。延迟为端到端毫秒;"
                 "占比 = 真实启动数 / 已分类样本数。\n")
        if len(booting) < len(self.runtimes):
            snap = code_list(r for r in self.runtimes if self.is_snapshot(r))
            self.add(f"This split applies only to runtimes whose sessions start a container "
                     f"({code_list(booting)}). Sessions on {snap} are restored from a snapshot "
                     f"instead; they are covered in the platform comparison below.\n",
                     f"这种划分只适用于 session 会启动容器的 runtime({code_list(booting)})。"
                     f"{snap} 上的 session 是从快照恢复的,见后文的平台对比。\n")
        self.add("| cell | real boots | real-boot p50 | real-boot max | warm-pool hits | "
                 "warm-pool p50 | warm-pool max | real-boot share | unclassified |",
                 "| 格子 | 真实启动数 | 真实启动 p50 | 真实启动 max | 预热命中数 | "
                 "预热命中 p50 | 预热命中 max | 真实启动占比 | 未分类 |")
        self.L.append("|---|---|---|---|---|---|---|---|---|")
        for runtime in booting:
            for sequence in self.sequences:
                for c in self.concs:
                    where = {"runtime": runtime, "sequence": sequence, "phase": "cold", "conc": c}
                    sent = [r for r in self.requests if r["attempted"] and r["phase"] == "cold"
                            and (r["runtime"], r["sequence"], r["concurrency"]) == (runtime, sequence, c)]
                    if not sent:
                        continue
                    real = self.vals("total_ms", boot="real_boot", **where)
                    pool = self.vals("total_ms", boot="warm_pool", **where)
                    self.L.append(
                        f"| {runtime} · {sequence} · c={c} | {len(real)} | "
                        f"{fmt(percentile(real, 50))} | {fmt(max(real, default=None))} | {len(pool)} | "
                        f"{fmt(percentile(pool, 50))} | {fmt(max(pool, default=None))} | "
                        f"{self.share(real, pool)} | "
                        f"{sum(1 for r in sent if self.kind(r) == 'unknown')} |")
        self.L.append("")

    @staticmethod
    def share(real: list, pool: list) -> str:
        return f"{len(real) / (len(real) + len(pool)) * 100:.0f}%" if real or pool else DASH

    # ---------------------------------------------------- agent breakdown
    def agent_breakdown(self) -> None:
        if "agent" not in self.sequences:
            return
        self.add("## What the first message is made of (p50, ms)\n", "## 第一条消息的构成(p50,毫秒)\n")
        self.add("`platform part` = end-to-end − time inside the wrapper's request handler: "
                 "starting or restoring the session, routing and network. `DSH boot` = `dsh` "
                 "spawn → first stdout byte, before any model call. The remainder of the turn is "
                 "the model call and DSH finishing; it is not broken down here.\n",
                 "`平台部分` = 端到端 − 包装器请求处理函数内的耗时,即 session 的启动或恢复、路由和网络。"
                 "`DSH 启动` = `dsh` 进程创建 → 首个 stdout 字节,发生在调用模型之前。对话的其余部分是"
                 "模型调用和 DSH 收尾,这里不再细分。\n")
        self.add("| cell | samples | first message end-to-end | platform part | DSH boot |",
                 "| 格子 | 样本数 | 第一条消息端到端 | 平台部分 | DSH 启动 |")
        self.L.append("|---|---|---|---|---|")
        for runtime in self.runtimes:
            for c in self.concs:
                for kind in BOOT_KINDS:
                    where = {"runtime": runtime, "sequence": "agent", "phase": "cold",
                             "conc": c, "boot": kind}
                    count = len(self.select(**where))
                    if not count:
                        continue
                    cols = [fmt(percentile(self.vals(metric, **where), 50))
                            for metric in ("total_ms", "outside_handler_ms", "dsh_boot_ms")]
                    self.L.append(f"| {runtime} · c={c} · {self.kind_labels[kind]} | {count} | "
                                  + " | ".join(cols) + " |")
        self.L.append("")

    # ------------------------------------------------- variant comparison
    def variants(self) -> None:
        groups: dict[str, list] = {}
        for runtime in self.runtimes:
            groups.setdefault(self.version(runtime), []).append(runtime)
        groups = {version: members for version, members in groups.items() if len(members) > 1}
        if not groups:
            return
        cold_start = self.tr("cold start (ping)", "冷启动(ping)")
        first = self.tr("first message", "第一条消息")
        boot = self.tr("first message · DSH boot", "第一条消息 · DSH 启动")
        self.add("## Variant comparison\n", "## 变体对比\n")
        self.add(f"Runtime variants side by side, only within one platform version (their `env` is "
                 f"listed at the top). Cells are p50 ms (number of samples), pooled over every "
                 f"concurrency level run ({self.levels}).\n",
                 f"各 runtime 变体并排对比,仅在同一平台版本内比较(各自的 `env` 见报告开头)。"
                 f"每格为 p50 毫秒(样本数),合并了所有已测并发档位({self.levels})。\n")
        for version, members in groups.items():
            kinds = [k for k in BOOT_KINDS if any(k in self.kinds_for(r) for r in members)]
            if version != DASH:
                self.add(f"### Platform {version}\n", f"### 平台 {version}\n")
            self.L.append(self.tr("| metric | ", "| 指标 | ")
                          + " | ".join(f"`{r}`" for r in members) + " |")
            self.L.append("|---|" + "---|" * len(members))
            rows = [("ping", k, "total_ms", f"{cold_start} · {self.kind_labels[k]}") for k in kinds]
            rows += [("agent", k, "total_ms", f"{first} · {self.kind_labels[k]}") for k in kinds]
            rows += [("agent", k, "dsh_boot_ms", f"{boot} · {self.kind_labels[k]}") for k in kinds]
            for sequence, kind, metric, label in rows:
                if sequence in self.sequences:
                    self.L.append(f"| {label} | " + " | ".join(
                        p50_n(self.vals(metric, runtime=r, sequence=sequence, phase="cold", boot=kind))
                        for r in members) + " |")
            self.L.append("")

    # ------------------------------------------------ platform comparison
    def platform_columns(self, v1, v2) -> list:
        """[(runtime, boot kind, column title)] for one V1/V2 pair."""
        cols = [(v1, k) for k in (self.kinds_for(v1) if v1 else [])]
        cols += [(v2, k) for k in self.kinds_for(v2)]
        return [(r, k, f"{self.tag(r)} · {self.kind_labels[k]}") for r, k in cols]

    def platforms(self) -> None:
        pairs = self.pairs()
        if not pairs:
            return
        names = sorted({self.version(r) for r in self.runtimes})
        mixed = any(not self.is_snapshot(r) for r in self.runtimes)
        snap = code_list(v2 for _, v2 in pairs)
        if mixed:
            self.add(f"## Platform {' vs '.join(names)}\n", f"## 平台 {' vs '.join(names)}\n")
            self.add(f"On {snap} a new session does not start the container. Sessions there report a "
                     f"wrapper process that started long before the request, and many sessions share "
                     f"the same process start while each still sees its first invocation "
                     f"(`invocation_seq` = 1), so they are copies restored from a snapshot. The "
                     f"real-boot / warm-pool split therefore does not apply there: a cold probe on "
                     f"those runtimes is a *snapshot restore*. The table shows the evidence: on a "
                     f"container-per-session runtime almost every cold probe meets a different "
                     f"process.\n",
                     f"在 {snap} 上,新 session 不会启动容器。这些 runtime 上的 session 自报的包装器进程"
                     f"早在请求之前很久就已启动,并且许多 session 共享同一个进程启动时间、却各自都是第一次调用"
                     f"(`invocation_seq` = 1),说明它们是从快照恢复出来的副本。因此真实启动 / 预热池的划分"
                     f"在这些 runtime 上不适用:其冷探测属于*快照恢复*。下表给出证据:在每个 session 一个容器的 "
                     f"runtime 上,几乎每次冷探测遇到的都是不同的进程。\n")
        else:
            self.add(f"## Platform {' / '.join(names)}: every new session is a snapshot restore\n",
                     f"## 平台 {' / '.join(names)}:每个新 session 都是快照恢复\n")
            self.add(f"On {snap} a new session does not start the container. Sessions report a "
                     f"wrapper process that started long before the request, and many sessions "
                     f"share the same process start while each still sees its first invocation "
                     f"(`invocation_seq` = 1), so they are copies restored from a snapshot: every "
                     f"cold probe here is a *snapshot restore*. The table shows the evidence: a "
                     f"handful of process starts answered all the cold probes.\n",
                     f"在 {snap} 上,新 session 不会启动容器。session 自报的包装器进程早在请求之前很久就已启动,"
                     f"并且许多 session 共享同一个进程启动时间、却各自都是第一次调用(`invocation_seq` = 1),"
                     f"说明它们是从快照恢复出来的副本:这里的每次冷探测都是*快照恢复*。下表给出证据:"
                     f"全部冷探测只由寥寥几个进程启动时间应答。\n")
        self.add("| runtime | platform | cold probes answered | distinct wrapper process starts | "
                 "wrapper process age at the request, p50 (s) |",
                 "| runtime | 平台 | 得到应答的冷探测数 | 不同的包装器进程启动时间数 | "
                 "请求到达时包装器进程已运行时长 p50(秒) |")
        self.L.append("|---|---|---|---|---|")
        for runtime in self.runtimes:
            answered = [r for r in self.requests if r["runtime"] == runtime and r["phase"] == "cold"
                        and isinstance(r.get("server"), dict)]
            starts = {r["server"].get("proc_start_ts") for r in answered} - {None}
            ages = [r["uptime_at_request_ms"] / 1000.0 for r in answered
                    if r.get("uptime_at_request_ms") is not None]
            self.L.append(f"| `{runtime}` | {self.version(runtime)} | {len(answered)} | "
                          f"{len(starts)} | {fmt(percentile(ages, 50))} |")
        self.L.append("")

        metrics = [("ping", "cold", "total_ms", self.tr("cold start (ping)", "冷启动(ping)")),
                   ("agent", "cold", "total_ms", self.tr("first message", "第一条消息")),
                   ("agent", "cold", "dsh_boot_ms",
                    self.tr("first message · DSH boot", "第一条消息 · DSH 启动")),
                   ("agent", "cold", "outside_handler_ms",
                    self.tr("first message · platform part", "第一条消息 · 平台部分"))]
        metrics = [m for m in metrics if m[0] in self.sequences]
        for v1, v2 in pairs:
            columns = self.platform_columns(v1, v2)
            title = f"{self.tag(v1)} vs {self.tag(v2)}" if v1 else self.tag(v2)
            self.L.append(f"### {title}\n")
            if not v1 and mixed:
                self.add("No runtime on another platform version has the same image and env, so "
                         "this runtime is shown alone.\n",
                         "其他平台版本上没有镜像和环境变量都相同的 runtime,因此单独列出该 runtime。\n")
            self.add(f"All concurrency levels pooled ({self.levels}). Each cell is p50 / p90 / max "
                     f"(number of samples), end-to-end ms unless the row names a part. `DSH boot` "
                     f"= `dsh` spawn → first stdout byte; `platform part` = end-to-end − time "
                     f"inside the wrapper's request handler.\n",
                     f"合并所有并发档位({self.levels})。每格为 p50 / p90 / max(样本数),除标明构成部分的行外"
                     f"均为端到端毫秒。`DSH 启动` = `dsh` 进程创建 → 首个 stdout 字节;`平台部分` = 端到端 − "
                     f"包装器请求处理函数内的耗时。\n")
            self.L.append(self.tr("| metric | ", "| 指标 | ")
                          + " | ".join(title for _, _, title in columns) + " |")
            self.L.append("|---|" + "---|" * len(columns))
            for sequence, phase, metric, label in metrics:
                self.L.append(f"| {label} | " + " | ".join(
                    spread(self.vals(metric, runtime=r, sequence=sequence, phase=phase, boot=k))
                    for r, k, _ in columns) + " |")
            self.L.append("")

            if v1:
                self.add("By concurrency, because the container-per-session answer depends on "
                         "whether the warm pool is exhausted. Same cell format; the share is real "
                         "boots / classified cold samples.\n",
                         "按并发档位拆分,因为每个 session 一个容器的平台其结果取决于预热池是否被耗尽。"
                         "格式同上;占比 = 真实启动数 / 已分类冷样本数。\n")
            else:
                self.add("The same figures by concurrency level.\n", "同样的指标,按并发档位拆分。\n")
            share_col = [f"{self.tag(v1)} · {self.tr('real-boot share', '真实启动占比')}"] if v1 else []
            self.L.append(self.tr("| metric · concurrency | ", "| 指标 · 并发 | ")
                          + " | ".join([title for _, _, title in columns] + share_col) + " |")
            self.L.append("|---|" + "---|" * (len(columns) + len(share_col)))
            for sequence, phase, metric, label in metrics[:3]:
                for c in self.concs:
                    where = {"sequence": sequence, "phase": phase, "conc": c}
                    cells = [spread(self.vals(metric, runtime=r, boot=k, **where))
                             for r, k, _ in columns]
                    if v1:
                        cells.append(self.share(
                            self.select(runtime=v1, boot="real_boot", **where),
                            self.select(runtime=v1, boot="warm_pool", **where)))
                    self.L.append(f"| {label} · c={c} | " + " | ".join(cells) + " |")
            self.L.append("")

    def platform_turns(self) -> None:
        rows = self.timeline()
        if not rows:
            return
        names = " / ".join(sorted({self.version(r["runtime"]) for r, _, _ in rows}))
        self.add(f"## Platform {names}: cold agent turns by order of use of the same snapshot\n",
                 f"## 平台 {names}:按同一快照被使用的先后次序统计的冷 agent 对话\n")
        self.add("Sessions that report the same wrapper process start were restored from the same "
                 "snapshot. `turn` numbers the cold agent turns one snapshot has answered, in send "
                 "order, counted over every raw file on disk (superseded runs and smoke probes "
                 "included; only probes in these files are known). Each cell is p50 / p90 / max "
                 "(number of samples), ms.\n",
                 "自报包装器进程启动时间相同的 session 是从同一个快照恢复的。`次序` 是同一个快照按发送时间"
                 "先后应答的冷 agent 对话的序号,统计范围为磁盘上的全部原始文件(包含被替代的运行和 "
                 "smoke 探测;只能统计到这些文件里的探测)。每格为 p50 / p90 / max(样本数),毫秒。\n")
        self.add("| runtime | turn on the same snapshot | first message end-to-end | DSH boot | "
                 "platform part |",
                 "| runtime | 同一快照上的次序 | 第一条消息端到端 | DSH 启动 | 平台部分 |")
        self.L.append("|---|---|---|---|---|")
        for runtime in [r for r in self.runtimes if self.is_snapshot(r)]:
            for label, records in self.turn_buckets(runtime):
                cols = [spread([r.get(metric) for r in records])
                        for metric in ("total_ms", "dsh_boot_ms", "outside_handler_ms")]
                self.L.append(f"| `{runtime}` | {label} | " + " | ".join(cols) + " |")
        self.L.append("")
    def platform_timeline(self) -> None:
        rows = self.timeline()
        if not rows:
            return
        names = " / ".join(sorted({self.version(r["runtime"]) for r, _, _ in rows}))
        self.add(f"## Appendix · Platform {names}: every cold agent probe in time order\n",
                 f"## 附录 · 平台 {names}:按时间排序的全部冷 agent 探测\n")
        self.add("Every cold agent probe sent to a snapshot-restore runtime, oldest first, so a "
                 "trend with runtime age is visible. Unlike the tables above (latest raw file per "
                 "cell), this list also includes raw files that a later run superseded and the "
                 "smoke probes. `wrapper start` is the process start time the container reported "
                 "(it identifies the snapshot) and `turn` is the order defined in the turn-order "
                 "section; the last "
                 "column is the time between `deployed_at` in `deployments.json` and the probe.\n",
                 "发往快照恢复型 runtime 的每一次冷 agent 探测,按时间从早到晚排列,以便观察随 runtime "
                 "存续时间变化的趋势。与上面的表格(每个格子取最新原始文件)不同,这里还包含被后续运行"
                 "替代的原始文件以及 smoke 探测。`包装器启动` 是容器自报的进程启动时间(可用于区分快照),"
                 "`次序` 的定义见按次序统计的一节;最后一列是 `deployments.json` 中的 `deployed_at` 到该探测之间的时间。\n")
        self.add("| sent (UTC) | runtime | c | source | ok | end-to-end ms | DSH boot ms | "
                 "wrapper start (UTC) | turn | minutes since deployed_at |",
                 "| 发送时间(UTC) | runtime | c | 来源 | 成功 | 端到端毫秒 | DSH 启动毫秒 | "
                 "包装器启动(UTC) | 次序 | 距 deployed_at 的分钟数 |")
        self.L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r, source, turn in rows:
            server = r["server"] if isinstance(r.get("server"), dict) else {}
            self.L.append(
                f"| {(r['send_iso'] or DASH)[:19]} | {r['runtime']} | {r['concurrency']} | {source} | "
                f"{self.yes_no(r['ok'])} | {fmt(r['total_ms'])} | {fmt(r['dsh_boot_ms'])} | "
                f"{clock_text(server.get('proc_start_ts'))} | "
                f"{turn or DASH} | {fmt(self.age_minutes(r), 1)} |")
        self.L.append("")

    def yes_no(self, value) -> str:
        return self.tr("yes", "是") if value else self.tr("no", "否")

    # ------------------------------------------------------------ page pin
    def pin_section(self) -> None:
        pairs, pin = self.pin_pairs(), self.pin
        if not pairs and not (pin.get("pinner") and any(self.is_snapshot(r) for r in self.runtimes)):
            return
        self.add("## Page pin on a snapshot-restore platform: effect and sample code\n",
                 "## 快照恢复型平台上的 page pin:效果与示例代码\n")
        self.add("On a snapshot-restore platform a new session is a copy of the container as it "
                 "was when it first reported healthy. Files that were only read into the page "
                 "cache before that moment were not served from memory afterwards in this test: "
                 "`dsh` started slowly until the same snapshot had been used several times (see "
                 "the turn-order table). Page pin keeps those files resident: before the wrapper "
                 "starts listening, a helper process maps every file a first turn reads and holds "
                 "the mappings, so the pages belong to a live process when the snapshot is taken. "
                 "That pages mapped this way survive into the snapshot is the working explanation "
                 "behind this recipe, not something the platform documents; the measured effect "
                 "below is consistent with it.\n",
                 "在快照恢复型平台上,新 session 是容器首次报告健康那一刻的副本。在本测试中,那一刻之前仅仅被读入"
                 "页缓存的文件,恢复后并不能直接从内存取得:`dsh` 的启动一直很慢,直到同一快照被使用过若干次"
                 "(见按次序统计的表)。Page pin 让这些文件常驻内存:包装器开始监听之前,由一个辅助进程把第一次对话"
                 "要读的每个文件都映射进内存并一直持有,这样打快照时这些页属于一个存活的进程。"
                 "“这样映射的页会随快照保留下来”是这个做法背后的工作假设,并非平台文档中的说明;"
                 "下面测得的效果与它一致。\n")
        for control, pinned in pairs:
            details = self.pin_details(pinned)
            runtimes = [r for r in (control, pinned) if r]

            def stat(key, scale=1.0, digits=0, details=details) -> str:
                values = sorted({d[key] / scale for d in details if d.get(key) is not None})
                if not values:
                    return DASH
                low, high = fmt(values[0], digits), fmt(values[-1], digits)
                return low if low == high else f"{low}–{high}"

            self.L.append(self.tr("| metric | ", "| 指标 | ") + " | ".join(
                f"`{r}` ({self.tr('control', '对照组') if r == control else 'page pin'})"
                for r in runtimes) + " |")
            self.L.append("|---|" + "---|" * len(runtimes))
            for label, value in (
                    (self.tr("files pinned", "pin 住的文件数"), stat("files")),
                    (self.tr("memory pinned, MB", "pin 住的内存,MB"), stat("bytes", 2.0 ** 20)),
                    (self.tr("time to pin before listening, ms", "监听前 pin 的耗时,毫秒"), stat("ms"))):
                self.L.append(f"| {label} | " + " | ".join(
                    value if r == pinned else DASH for r in runtimes) + " |")
            for label, sequence, metric in (
                    (self.tr("cold start (ping), p50 / p90 / max", "冷启动(ping),p50 / p90 / max"),
                     "ping", "total_ms"),
                    (self.tr("first message, p50 / p90 / max", "第一条消息,p50 / p90 / max"),
                     "agent", "total_ms"),
                    (self.tr("first message · DSH boot, p50 / p90 / max",
                             "第一条消息 · DSH 启动,p50 / p90 / max"), "agent", "dsh_boot_ms")):
                if sequence in self.sequences:
                    self.L.append(f"| {label} | " + " | ".join(
                        spread(self.vals(metric, runtime=r, sequence=sequence, phase="cold"))
                        for r in runtimes) + " |")
            turns = {r: dict(self.turn_buckets(r)) for r in runtimes}
            for label in [label for label, _ in self.turn_buckets(pinned)]:
                self.L.append(
                    f"| {self.tr('first message, turn', '第一条消息,同一快照上的第')} {label} "
                    f"{self.tr('on the same snapshot, p50 / p90 / max', '次,p50 / p90 / max')} | "
                    + " | ".join(spread([x["total_ms"] for x in turns[r].get(label, [])])
                                 for r in runtimes) + " |")
            self.L.append("")
        if pin.get("pinner"):
            manifest = pin.get("manifest_files")
            self.add("### Sample code: the pinner (`app/pinner.py`)\n",
                     "### 示例代码:pinner(`app/pinner.py`)\n")
            self.add(f"Copied from the repository when this report was generated. The manifest it "
                     f"reads (`app/pin-manifest.txt`, {fmt(manifest)} files) is produced by "
                     f"`scripts/gen_pin_manifest.sh`, which traces one real first turn with strace "
                     f"inside the built image and lists the files in the order they are first "
                     f"opened; it depends on the base image and the DSH version, so regenerate it "
                     f"when either changes.\n",
                     f"生成本报告时从仓库中复制。它读取的清单(`app/pin-manifest.txt`,{fmt(manifest)} 个文件)由 "
                     f"`scripts/gen_pin_manifest.sh` 生成:该脚本在构建好的镜像里用 strace 追踪一次真实的首次对话,"
                     f"按首次打开的顺序列出文件;清单依赖基础镜像和 DSH 版本,任一变化都要重新生成。\n")
            self.L.append("```python\n" + pin["pinner"].rstrip("\n") + "\n```\n")
        if pin.get("hooks"):
            self.add("### Sample code: how the wrapper uses it (`app/server.mjs`, excerpt)\n",
                     "### 示例代码:包装器如何使用它(`app/server.mjs`,节选)\n")
            self.add("The pinner is started and awaited before the server listens, so the files "
                     "are mapped before the container can report healthy, and it is released "
                     "after the first agent turn. A failure to pin is logged and never stops the "
                     "server.\n",
                     "pinner 在服务开始监听之前启动并等待其完成,因此文件在容器能够报告健康之前就已映射好;"
                     "第一次 agent 对话结束后释放。pin 失败只记录日志,不会阻止服务启动。\n")
            self.L.append("```javascript\n" + pin["hooks"].rstrip("\n") + "\n```\n")
            if pin.get("calls"):
                self.add("Where the two functions are called:\n", "这两个函数的调用位置:\n")
                self.L.append("```javascript\n" + "\n".join(pin["calls"]) + "\n```\n")

    # ------------------------------------------------------- local docker
    def local_docker(self) -> None:
        local = self.local
        if local is None:
            return
        records = [r for r in local.get("records", []) if r.get("ok")]
        local_summary = local.get("summary") or {}

        def local_cell(sequence, phase, metric) -> str:
            entry = local_summary.get(f"{sequence}.{phase}.{metric}")
            if isinstance(entry, dict) and entry.get("n"):
                return f"{fmt(entry.get('p50'))} ({entry['n']})"
            return p50_n([r.get(metric) for r in records
                          if r.get("sequence") == sequence and r.get("phase") == phase])

        start = local_cell("ping", "cold", "container_start_ms")
        if start == DASH:
            start = p50_n([r.get("container_start_ms") for r in records if r.get("phase") == "cold"])
        first_answer = p50_n(self.local_first_answer())
        host = local.get("host") or {}
        prewarm = sorted({str(bool(r["server"]["prewarm"].get("enabled"))).lower()
                          for r in local.get("records", [])
                          if isinstance((r.get("server") or {}).get("prewarm"), dict)})
        prewarm_text = ", ".join(prewarm) if prewarm else DASH
        columns = [(r, k) for r in self.runtimes for k in self.kinds_for(r)]

        self.add("## Local Docker vs AgentCore Runtime\n", "## 本地 Docker vs AgentCore Runtime\n")
        self.add(f"- **Local host:** {host.get('machine', DASH)}, {host.get('cpus', DASH)} CPUs, "
                 f"{host.get('mem_gb', DASH)} GB RAM, kernel `{host.get('kernel', DASH)}`, "
                 f"Docker `{host.get('docker', DASH)}`",
                 f"- **本地主机:**{host.get('machine', DASH)},{host.get('cpus', DASH)} 个 CPU,"
                 f"{host.get('mem_gb', DASH)} GB 内存,内核 `{host.get('kernel', DASH)}`,"
                 f"Docker `{host.get('docker', DASH)}`")
        self.add(f"- **Local run:** image `{image_text(local.get('image'))}` "
                 f"({megabytes(local.get('image_size_bytes'))}), model "
                 f"`{local.get('model_id', DASH)}`, dsh `{local.get('dsh_version', DASH)}`, "
                 f"tested {local.get('tested_at', DASH)}, {local.get('rounds', DASH)} rounds, "
                 f"host page cache: {local.get('page_cache', DASH)}, DSH pre-warm enabled: "
                 f"{prewarm_text}",
                 f"- **本地测试:**镜像 `{image_text(local.get('image'))}`"
                 f"({megabytes(local.get('image_size_bytes'))}),模型 "
                 f"`{local.get('model_id', DASH)}`,dsh `{local.get('dsh_version', DASH)}`,"
                 f"测试时间 {local.get('tested_at', DASH)},{local.get('rounds', DASH)} 轮,"
                 f"主机页缓存:{local.get('page_cache', DASH)},DSH 预热开启:{prewarm_text}")
        self.add(f"- Locally, every round starts a brand-new container and sends it its first "
                 f"request. AgentCore figures pool every concurrency level run ({self.levels}). "
                 f"Cells are p50 ms (number of samples).\n",
                 f"- 本地每一轮都新建一个容器并向它发送第一个请求。AgentCore 数据合并了所有已测并发档位"
                 f"({self.levels})。每格为 p50 毫秒(样本数)。\n")
        self.L.append(self.tr("| metric | local Docker | ", "| 指标 | 本地 Docker | ")
                      + " | ".join(f"{self.tag(r)} · {self.kind_labels[k]}" for r, k in columns) + " |")
        self.L.append("|---|---|" + "---|" * len(columns))

        def compare(label, local_text, sequence, phase, metric) -> None:
            cols = [p50_n(self.vals(metric, runtime=r, sequence=sequence, phase=phase, boot=k))
                    for r, k in columns]
            self.L.append(f"| {label} | {local_text} | " + " | ".join(cols) + " |")

        compare(self.tr("cold start (local: `docker run` → first healthy `/ping`; AgentCore: "
                        "cold ping end-to-end)",
                        "冷启动(本地:`docker run` → 首次 `/ping` 健康;AgentCore:冷 ping 端到端)"),
                start, "ping", "cold", "total_ms")
        compare(self.tr("first message · DSH boot", "第一条消息 · DSH 启动"),
                local_cell("agent", "cold", "dsh_boot_ms"), "agent", "cold", "dsh_boot_ms")
        compare(self.tr("first message (local: container start + first agent request, per "
                        "round; AgentCore: first message end-to-end)",
                        "第一条消息(本地:每轮的容器启动 + 首个 agent 请求;AgentCore:第一条消息端到端)"),
                first_answer, "agent", "cold", "total_ms")
        self.L.append("")
        self.add("Local Docker runs with a warm host page cache and no image pull, so it is a "
                 "floor, not a like-for-like cold start.\n",
                 "本地 Docker 在主机页缓存已预热、且无需拉取镜像的条件下运行,因此它只是下限,"
                 "并非与 AgentCore 冷启动的同条件对比。\n")

    # ---------------------------------------------------- success / errors
    def errors(self) -> None:
        totals = self.totals
        self.add("## Success / errors\n", "## 成功率 / 错误\n")
        self.add("| cell | phase | sent | ok | failed | not sent | error types |",
                 "| 格子 | 阶段 | 已发送 | 成功 | 失败 | 未发送 | 错误类型 |")
        self.L.append("|---|---|---|---|---|---|---|")
        for runtime in self.runtimes:
            for sequence in self.sequences:
                for c in self.concs:
                    for phase in ("cold", "warm"):
                        recs = [r for r in self.requests
                                if (r["runtime"], r["sequence"], r["concurrency"], r["phase"])
                                == (runtime, sequence, c, phase)]
                        if not recs:
                            continue
                        sent = [r for r in recs if r["attempted"]]
                        bad = sorted(r["error_type"] for r in sent if not r["ok"])
                        unsent = [r["error_type"] for r in recs
                                  if not r["attempted"] and r["error_type"] != "skipped"]
                        kinds = ", ".join(
                            [f"{k}: {bad.count(k)}" for k in dict.fromkeys(bad)]
                            + [f"{k}: {unsent.count(k)}" for k in dict.fromkeys(unsent)]) or DASH
                        self.L.append(
                            f"| {runtime} · {sequence} · c={c} | {phase} | {len(sent)} | "
                            f"{len(sent) - len(bad)} | {len(bad)} | {len(recs) - len(sent)} | {kinds} |")
        self.L.append("")
        self.add(f"{totals['failures']} of {totals['attempted']} requests failed (non-200 response, "
                 f"unparseable body, or `agent.ok` not true); failed requests are counted here and "
                 f"excluded from every latency statistic. Sessions stopped after measurement: "
                 f"{totals['sessions_stopped']}/{totals['sessions']}. Requests flagged as anomalous "
                 f"(first invoke not the first invocation of its microVM, or second invoke served "
                 f"by a different process): {totals['anomalies']}.\n",
                 f"{totals['failures']} 个请求失败(共发送 {totals['attempted']} 个;失败指非 200 响应、"
                 f"响应体无法解析,或 `agent.ok` 不为 true);失败请求在此计数,并从所有延迟统计中剔除。"
                 f"测量后已停止的 session:{totals['sessions_stopped']}/{totals['sessions']}。"
                 f"被标记为异常的请求(首次调用不是其微VM的第一次调用,或第二次调用由另一个进程处理):"
                 f"{totals['anomalies']}。\n")
        failed = [r for r in self.requests if r["attempted"] and not r["ok"]]
        if failed:
            self.add("| runtime · sequence · c | phase | session | status | error type | message |",
                     "| runtime · sequence · c | 阶段 | session | 状态码 | 错误类型 | 信息 |")
            self.L.append("|---|---|---|---|---|---|")
            for r in failed[:MAX_FAILURE_ROWS]:
                self.L.append(
                    f"| {r['runtime']} · {r['sequence']} · c={r['concurrency']} | {r['phase']} | "
                    f"`{r['session_id'][:8]}` | {r['status_code'] or DASH} | {r['error_type']} | "
                    f"{table_text(r['error_msg'])} |")
            if len(failed) > MAX_FAILURE_ROWS:
                self.add(f"\n{len(failed) - MAX_FAILURE_ROWS} more failed requests are in the raw files.",
                         f"\n另有 {len(failed) - MAX_FAILURE_ROWS} 个失败请求见原始文件。")
            self.L.append("")

    # ------------------------------------------------------- client side
    def client_overhead(self) -> None:
        groups: dict[tuple, list] = {}
        for raw in self.raws:
            groups.setdefault((raw["meta"]["concurrency"], *fanout(raw["meta"])), []).append(raw)
        self.add("## Client-side overhead\n", "## 客户端自身的开销\n")
        self.add("How much of a number is the benchmark client. All probes of a round are released "
                 "together, and each request's clock starts at its own `t0`, just before the SDK "
                 "call. `t0 spread` is how far apart the cold requests of one round started (from "
                 "their send timestamps) and `release → t0` is each probe's delay after the "
                 "release: both are stagger before the clock starts, so they are **not** in any "
                 "latency, but they mean the requests did not all arrive at the same instant. "
                 "`t0 → before-send` (request serialized and signed, handed to the HTTP layer) and "
                 "`new connection` (TCP + TLS setup) **are** inside the measured latency. Cold "
                 "requests only, ms; a dash means the raw file predates these measurements.\n",
                 "基准测试客户端自身占了多少。同一轮的所有探测一起释放,每个请求在各自的 `t0`"
                 "(SDK 调用之前)开始计时。`t0 分散度` 是同一轮内各冷请求开始时间的最大间隔(取自发送时间戳),"
                 "`释放 → t0` 是每个探测在释放之后的延迟:两者都发生在计时开始之前,因此**不**计入任何延迟,"
                 "但说明请求并非在同一瞬间到达。`t0 → before-send`(请求已序列化并签名,交给 HTTP 层)"
                 "和 `新建连接`(TCP + TLS 建立)则**包含**在测得的延迟之内。仅统计冷请求,毫秒;"
                 "短横线表示原始文件早于这些测量项。\n")
        self.add("| concurrency | client | cells | cold requests | t0 spread per round p50 / max | "
                 "release → t0 p50 / p90 / max | t0 → before-send p50 / p90 / max | "
                 "new connection p50 / p90 / max |",
                 "| 并发 | 客户端 | 格子数 | 冷请求数 | 每轮 t0 分散度 p50 / max | "
                 "释放 → t0 p50 / p90 / max | t0 → before-send p50 / p90 / max | "
                 "新建连接 p50 / p90 / max |")
        self.L.append("|---|---|---|---|---|---|---|---|")
        for key in sorted(groups):
            conc, mode, processes, threads = key
            self.L.append(f"| c={conc} | {self.fanout_label(mode, processes, threads)} | "
                          f"{len(groups[key])} | " + " | ".join(self.client_cells(groups[key])) + " |")
        self.L.append("")

    def process_comparison(self) -> None:
        """Latest single-process and latest multi-process run of the same cell,
        from every raw file on disk: the summary tables keep only one of them."""
        latest: dict[tuple, dict] = {}
        for raw in sorted(self.history, key=lambda raw: raw.get("file") or ""):
            meta = raw["meta"]
            cell = (meta["runtime"], meta["sequence"], meta["concurrency"])
            latest.setdefault(cell, {})[fanout(meta)[0]] = raw
        both = {cell: runs for cell, runs in latest.items() if len(runs) > 1}
        if not both:
            return
        self.add("## Single-process vs multi-process client on the same cell\n",
                 "## 同一格子上单进程与多进程客户端的对比\n")
        self.add("The latest run of each kind found on disk for the same runtime, sequence and "
                 "concurrency (the tables above use only the newest raw file of a cell). The two "
                 "runs were not simultaneous, so the later one may meet a warm pool the earlier "
                 "one drained: compare the client columns directly, and the latencies only "
                 "between requests of the same boot kind. Latencies are p50 / p90 / max (number "
                 "of samples), ms.\n",
                 "对同一 runtime、sequence 和并发,取磁盘上每种客户端方式最近的一次运行(上面的表格只使用"
                 "每个格子最新的原始文件)。两次运行并非同时进行,后一次可能遇到被前一次耗尽的预热池:"
                 "客户端各列可以直接比较,延迟则只应在相同启动类型的请求之间比较。延迟为 "
                 "p50 / p90 / max(样本数),毫秒。\n")
        kinds = [k for k in BOOT_KINDS
                 if any(self.kind(r) == k for runs in both.values() for raw in runs.values()
                        for r in raw["requests"])]
        self.add("| cell | client | run started (UTC) | cold ok / sent | "
                 + " | ".join(f"cold · {self.kind_labels[k]}" for k in kinds)
                 + " | t0 spread per round p50 / max | release → t0 p50 / p90 / max | "
                 "t0 → before-send p50 / p90 / max | new connection p50 / p90 / max |",
                 "| 格子 | 客户端 | 运行开始(UTC) | 冷请求 成功 / 已发送 | "
                 + " | ".join(f"冷 · {self.kind_labels[k]}" for k in kinds)
                 + " | 每轮 t0 分散度 p50 / max | 释放 → t0 p50 / p90 / max | "
                 "t0 → before-send p50 / p90 / max | 新建连接 p50 / p90 / max |")
        self.L.append("|---|---|---|---|" + "---|" * len(kinds) + "---|---|---|---|")
        order = {r: i for i, r in enumerate(self.conf)}
        for cell in sorted(both, key=lambda c: (order.get(c[0], len(order)), SEQUENCES.index(c[1]), c[2])):
            for mode in ("inline", "spawn"):
                raw = both[cell][mode]
                records = raw["requests"]
                cold = [r for r in records if r["phase"] == "cold" and r["attempted"]]
                cols = [spread(self.vals("total_ms", phase="cold", boot=k, records=records))
                        for k in kinds]
                self.L.append(
                    f"| {cell[0]} · {cell[1]} · c={cell[2]} | {self.fanout_label(*fanout(raw['meta']))} | "
                    f"{(raw['meta'].get('started_iso') or DASH)[:19]} | "
                    f"{sum(1 for r in cold if r['ok'])} / {len(cold)} | " + " | ".join(cols) + " | "
                    + " | ".join(self.client_cells([raw], with_count=False)) + " |")
        self.L.append("")

    # -------------------------------------------------------- smoke probes
    def smoke(self) -> None:
        rows = [r for s in self.smokes for r in s["requests"]
                if r["attempted"] and (r["phase"] == "cold" or not r["ok"])]
        if not rows:
            return
        self.add("## Smoke probes (single invokes outside the matrix)\n",
                 "## Smoke 探测(矩阵之外的单次调用)\n")
        self.add("The first invoke of each smoke session, and any smoke request that failed.\n",
                 "每个 smoke session 的首次调用,以及所有失败的 smoke 请求。\n")
        self.add("| sent (UTC) | runtime | sequence | phase | ok | end-to-end ms | boot kind | "
                 "DSH boot ms | error |",
                 "| 发送时间(UTC) | runtime | sequence | 阶段 | 成功 | 端到端毫秒 | 启动类型 | "
                 "DSH 启动毫秒 | 错误 |")
        self.L.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            error = DASH if r["ok"] else f"{r['error_type']} {r['status_code'] or ''}".strip()
            self.L.append(f"| {(r['send_iso'] or DASH)[:19]} | {r['runtime']} | {r['sequence']} | "
                          f"{r['phase']} | {self.yes_no(r['ok'])} | {fmt(r['total_ms'])} | "
                          f"{self.kind(r)} | {fmt(r['dsh_boot_ms'])} | {error} |")
        self.L.append("")

    # --------------------------------------------------------- methodology
    def methodology(self) -> None:
        dep, metas = self.dep, self.metas
        id_lengths = ", ".join(str(n) for n in sorted({len(r["session_id"]) for r in self.requests})) or DASH
        timeouts = ", ".join(str(t) for t in sorted({m["read_timeout_seconds"] for m in metas})) or DASH
        settles = ", ".join(f"{s:g}" for s in sorted({m["settle_seconds"] for m in metas})) or DASH
        prompts = code_list(m["prompt"] for m in metas)
        rounds = "; ".join(f"{m['runtime']} {m['sequence']} c={m['concurrency']}: "
                           f"{m['rounds_completed']}/{m['rounds_planned']}" for m in metas) or DASH
        self.add("## Methodology\n", "## 测试方法\n")
        self.add(f"- Cold start = client-side wall time of `InvokeAgentRuntime` with a **fresh "
                 f"{id_lengths}-char `runtimeSessionId`** (a new session in AgentCore's session "
                 f"model), from the API call to the full response-body read; time to response "
                 f"headers is recorded separately. boto3, SigV4, {dep.get('region', DASH)}.",
                 f"- 冷启动 = 使用**全新 {id_lengths} 字符 `runtimeSessionId`**(按 AgentCore 的 session "
                 f"模型对应新 session)调用 `InvokeAgentRuntime`,从发起 API 调用到完整读取响应体的客户端"
                 f"墙钟时间;收到响应头的时间另行记录。boto3、SigV4、{dep.get('region', DASH)}。")
        self.add(f"- botocore retries **disabled** (`total_max_attempts=1`): throttles and errors "
                 f"are counted, never retried into the latency distribution. Read timeout "
                 f"{timeouts} s.",
                 f"- **关闭** botocore 重试(`total_max_attempts=1`):限流和错误被单独计数,"
                 f"绝不会被重试而混入延迟分布。读超时 {timeouts} 秒。")
        setups: dict[tuple, int] = {}
        for meta in metas:
            key = (meta["concurrency"], *fanout(meta))
            setups[key] = setups.get(key, 0) + 1
        fan = "; ".join(f"c={key[0]}: {self.fanout_label(*key[1:])} ({count})"
                        for key, count in sorted(setups.items())) or DASH
        self.add(f"- Concurrency-N: N probes released together. In one process, N threads wait on "
                 f"a `threading.Barrier`. With worker processes (spawn start method), each "
                 f"process builds its own client and resolves its credentials before the release, "
                 f"runs its share of the probes in its own thread pool, and one cross-process gate "
                 f"releases them all; no connection to the runtime is opened before the release. "
                 f"Client per concurrency level (number of cells): {fan}.",
                 f"- 并发 N:N 个探测同时释放。单进程方式下,N 个线程在 `threading.Barrier` 上等待。"
                 f"多进程方式下(spawn 启动方式),每个进程在释放前自行创建客户端并解析凭证,"
                 f"在各自的线程池中运行分到的探测,由一个跨进程的闸门统一释放;释放之前不会预先建立到 "
                 f"runtime 的连接。各并发档位使用的客户端(格子数):{fan}。")
        self.add(f"- Rounds completed/planned per cell: {rounds}. Pause between rounds and between "
                 f"cells (seconds, every value used): {settles}. A later run replaces a cell with "
                 f"its latest raw file.",
                 f"- 每个格子完成/计划的轮数:{rounds}。轮次之间和格子之间的暂停(秒,列出用过的全部取值):"
                 f"{settles}。后续运行会用最新的原始文件替换对应格子。")
        self.add("- After each first invoke: one more invoke on the same session, then "
                 "`StopRuntimeSession` (best effort). The second invoke only confirms that the "
                 "session stays on the same process (it is in the success and error counts); its "
                 "latency is not reported, because it measures DSH and the model, not a session "
                 "start.",
                 "- 每次首次调用之后:在同一 session 上再调用一次,然后(尽力而为地)调用 "
                 "`StopRuntimeSession`。第二次调用只用于确认 session 仍由同一个进程服务(它计入成功与错误统计);"
                 "其延迟不在报告中,因为它反映的是 DSH 和模型,而不是 session 的启动。")
        self.add("- Boot kind comes from the timestamps the container returns and from each "
                 "runtime's `platform_version` in `deployments.json`, joined when this report is "
                 "generated.",
                 "- 启动类型由容器返回的时间戳和 `deployments.json` 中各 runtime 的 `platform_version` "
                 "共同决定,在生成本报告时关联。")
        self.add(f"- Agent prompt: {prompts}.\n", f"- Agent 提示词:{prompts}。\n")

    def caveats(self) -> None:
        self.add("## Caveats\n", "## 注意事项\n")
        if any(not self.is_snapshot(r) for r in self.runtimes):
            self.add("- **The warm pool is opaque**: its size and refill speed may vary by runtime, "
                     "account, region and time, so the real-boot share is not a contract. Cells run "
                     "one after another, so later cells see a pool that earlier cells have drained.",
                     "- **预热池不透明**:其大小和补充速度可能随 runtime、账号、区域和时间变化,因此真实启动占比"
                     "并非平台承诺。各格子依次执行,靠后的格子面对的是被前面格子消耗过的预热池。")
        if any(self.is_snapshot(r) for r in self.runtimes):
            self.add("- **Snapshot-restore latency may depend on how often the same snapshot was "
                     "already used**: read the turn-order and time-ordered tables before quoting a "
                     "single figure. The other tables use only the latest raw file per cell, so "
                     "they describe the snapshots as they were during that run, and those runtimes "
                     "were tested at a different time than the others.",
                     "- **快照恢复的延迟可能与同一个快照此前被使用的次数有关**:引用单个数字之前,请先看按次序"
                     "和按时间排序的两张表。其余表格只使用每个格子最新的原始文件,反映的是那次运行时快照所处的"
                     "状态;另外这些 runtime 的测试时间与其他 runtime 不同。")
        self.add("- **The first message includes one model call**, which varies with model "
                 "load; the benchmark prompt is very short, so this is a lower bound for real "
                 "turns. Cold start (ping), the platform part and DSH boot contain no model time.",
                 "- **第一条消息包含一次模型调用**,其耗时随模型负载波动;基准测试的提示词很短,因此这是真实对话的"
                 "下限。冷启动(ping)、平台部分和 DSH 启动都不含模型时间。")
        self.add("- Boot-kind classification trusts the microVM wall clock; a clock step during "
                 "boot would distort it.",
                 "- 启动类型的判定依赖微VM的墙上时钟;启动过程中若发生时钟跳变,判定会失真。")
        self.add("- With few samples per cell, p90/p99 are interpolated and sit close to the max. "
                 "These are measurements from one region at one point in time, not an SLA.\n",
                 "- 每个格子样本较少,p90/p99 为插值结果,接近最大值。这只是单个区域在某一时刻的测量结果,"
                 "不构成 SLA。\n")

    # ------------------------------------------------------------ findings
    def findings(self) -> None:
        c0, totals = self.concs[0], self.totals
        sep = self.tr("; ", ";")
        self.add("## Detailed findings\n", "## 详细结论\n")
        for runtime in self.runtimes:
            kinds = self.kinds_for(runtime)
            tag = self.tag(runtime)

            def by_kind(sequence, metric="total_ms", runtime=runtime, kinds=kinds) -> str:
                return sep.join(
                    f"{self.kind_labels[k]} "
                    + self.sample(self.vals(metric, runtime=runtime, sequence=sequence,
                                            phase="cold", conc=c0, boot=k)) for k in kinds)

            if "ping" in self.sequences:
                self.add(f"- **{tag} · cold start (ping, c={c0}):** {by_kind('ping')}.",
                         f"- **{tag} · 冷启动(ping,c={c0}):**{by_kind('ping')}。")
            if "agent" in self.sequences:
                self.add(f"- **{tag} · first message (c={c0}):** {by_kind('agent')}.",
                         f"- **{tag} · 第一条消息(c={c0}):**{by_kind('agent')}。")
                for k in kinds:
                    where = {"runtime": runtime, "sequence": "agent", "phase": "cold",
                             "conc": c0, "boot": k}
                    if not self.select(**where):
                        continue
                    parts = [self.sample(self.vals(metric, **where))
                             for metric in ("outside_handler_ms", "dsh_boot_ms")]
                    self.add(f"- **{tag} · inside the first message ({self.kind_labels[k]}, "
                             f"c={c0}):** platform part {parts[0]}; DSH boot {parts[1]}.",
                             f"- **{tag} · 第一条消息的构成({self.kind_labels[k]},c={c0}):**"
                             f"平台部分 {parts[0]};DSH 启动 {parts[1]}。")
            headline = "snapshot_restore" if self.is_snapshot(runtime) else "real_boot"
            pooled = []
            for sequence in self.sequences:
                values = self.vals("total_ms", runtime=runtime, sequence=sequence,
                                   phase="cold", boot=headline)
                pooled.append(f"{sequence} " + (
                    f"p50 {fmt(percentile(values, 50))} ms, max {fmt(max(values))} ms "
                    f"(n={len(values)})" if values else self.tr("no samples", "无样本")))
            self.add(f"- **{tag} · {self.kind_labels[headline]} across every concurrency level "
                     f"({self.levels}), end-to-end:** {'; '.join(pooled)}.",
                     f"- **{tag} · 所有并发档位({self.levels})的{self.kind_labels[headline]},端到端:**"
                     f"{';'.join(pooled)}。")

        for v1, v2 in self.pairs():
            if not v1:
                continue
            columns = self.platform_columns(v1, v2)
            for sequence, metric, en, zh in (
                    ("ping", "total_ms", "cold start (ping), end-to-end", "冷启动(ping),端到端"),
                    ("agent", "total_ms", "first message, end-to-end", "第一条消息,端到端"),
                    ("agent", "dsh_boot_ms", "DSH boot inside the first message",
                     "第一条消息中的 DSH 启动")):
                if sequence not in self.sequences:
                    continue
                parts = sep.join(
                    f"{title} " + self.sample(self.vals(metric, runtime=r, sequence=sequence,
                                                         phase="cold", boot=k))
                    for r, k, title in columns)
                self.add(f"- **Platform comparison · {en} ({self.levels} pooled):** {parts}.",
                         f"- **平台对比 · {zh}(合并 {self.levels}):**{parts}。")
        for runtime in [r for r in self.runtimes if self.is_snapshot(r)]:
            buckets = self.turn_buckets(runtime)
            if not buckets:
                continue
            parts = sep.join(
                f"{self.tr('turn', '次序')} {label} "
                + self.sample([r["total_ms"] for r in records]) for label, records in buckets)
            self.add(f"- **{self.tag(runtime)} · first message by order of use of the same "
                     f"snapshot (every raw file):** {parts}.",
                     f"- **{self.tag(runtime)} · 按同一快照被使用的先后次序统计的第一条消息"
                     f"(全部原始文件):**{parts}。")
        top = self.concs[-1]
        setups = {}
        for raw in self.raws:
            if raw["meta"]["concurrency"] == top:
                setups.setdefault(fanout(raw["meta"]), []).append(raw)
        for key in sorted(setups):
            cells = self.client_cells(setups[key], with_count=False)
            names = ((self.tr("t0 spread per round p50 / max", "每轮 t0 分散度 p50 / max"), cells[0]),
                     (self.tr("release → t0 p50 / p90 / max", "释放 → t0 p50 / p90 / max"), cells[1]),
                     (self.tr("t0 → before-send p50 / p90 / max (inside the measured latency)",
                              "t0 → before-send p50 / p90 / max(计入测得的延迟)"), cells[2]),
                     (self.tr("new connection p50 / p90 / max (inside the measured latency)",
                              "新建连接 p50 / p90 / max(计入测得的延迟)"), cells[3]))
            parts = sep.join(f"{name} {value} ms" for name, value in names if value != DASH)
            if parts:
                self.add(f"- **Client side at c={top} ({self.fanout_label(*key)}):** {parts}.",
                         f"- **c={top} 时的客户端({self.fanout_label(*key)}):**{parts}。")
        self.add(f"- **Reliability:** {totals['failures']} failed out of {totals['attempted']} "
                 f"requests sent.\n",
                 f"- **可靠性:**失败 {totals['failures']} 个,共发送 {totals['attempted']} 个请求。\n")

    def render(self) -> str:
        for section in (self.header, self.summary_section, self.cold_latency, self.populations,
                        self.agent_breakdown, self.variants, self.platforms,
                        self.platform_turns, self.pin_section, self.local_docker, self.errors,
                        self.client_overhead,
                        self.process_comparison, self.smoke, self.methodology, self.caveats,
                        self.findings, self.platform_timeline):
            section()
        return "\n".join(self.L)


def render(dep: dict, summary: dict, raws: list, smokes: list, local: dict | None,
           lang: str, history: list | None = None, pin: dict | None = None,
           excluded: dict | None = None) -> str:
    return Report(dep, summary, raws, smokes, local, lang, history, pin, excluded).render()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("--results", default=str(HERE / "results"), help="results directory")
    p.add_argument("--deployments", default=str(HERE / "deployments.json"),
                   help="path to deployments.json")
    p.add_argument("--platforms", default=None,
                   help="comma-separated platform versions to report, e.g. V2 (default: all measured)")
    p.add_argument("--app", default=str(HERE / "app"),
                   help="directory holding pinner.py and server.mjs (the page-pin sample code)")
    args = p.parse_args(argv)
    results_dir = Path(args.results)
    try:
        platforms = {v.strip() for v in (args.platforms or "").split(",") if v.strip()}
        data = scope_inputs(load_inputs(results_dir, Path(args.deployments), Path(args.app)),
                            platforms or None)
    except ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for lang, name in (("en", "REPORT.md"), ("zh", "REPORT.zh.md")):
        text = render(data["dep"], data["summary"], data["raws"], data["smokes"],
                      data["local"], lang, data["history"], data["pin"], data["excluded"])
        (results_dir / name).write_text(text)
        print(f"wrote {results_dir / name} ({text.count(chr(10)) + 1} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
