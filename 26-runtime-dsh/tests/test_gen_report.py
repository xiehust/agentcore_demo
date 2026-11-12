"""Offline checks for the report generator; no AWS calls."""
import ast
import contextlib
import io
import json
import re
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from fakes import (DEPLOYMENTS, PLATFORM_DEPLOYMENTS, ROOT, FakeClock, FakeRuntime, fake_factory,
                   write_deployments)
import coldstart_test as ct
import gen_report

NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

LOCAL = {
    "host": {"machine": "aarch64", "cpus": 16, "mem_gb": 61.8, "kernel": "6.12.113", "docker": "25.0.14"},
    "image": "agentcore-dsh-coldstart:dsh-test", "image_size_bytes": 1105337345,
    "region": "us-east-1", "model_id": "deepseek.test-model", "dsh_version": "9.9.9",
    "tested_at": "2026-11-12T05:10:00+00:00", "rounds": 2, "page_cache": "warm",
    "records": [
        {"round": rnd, "sequence": seq, "phase": phase, "ok": True, "container_start_ms": start,
         "client_total_ms": total, "dsh_boot_ms": boot, "first_text_ms": text, "dsh_total_ms": dsh,
         "server": {"prewarm": {"enabled": False}}}
        for rnd, seq, phase, start, total, boot, text, dsh in (
            (1, "ping", "cold", 410.0, 3.1, None, None, None),
            (1, "ping", "warm", None, 1.2, None, None, None),
            (1, "agent", "cold", 400.0, 2100.0, 900.0, 1500.0, 2050.0),
            (1, "agent", "warm", None, 1200.0, 300.0, 800.0, 1150.0),
            (2, "ping", "cold", 430.0, 2.9, None, None, None),
            (2, "ping", "warm", None, 1.1, None, None, None),
            (2, "agent", "cold", 420.0, 2300.0, 1000.0, 1700.0, 2250.0),
            (2, "agent", "warm", None, 1300.0, 320.0, 900.0, 1250.0))
    ],
    # Only some keys, so both the summary path and the records fallback are exercised.
    "summary": {
        "ping.cold.container_start_ms": {"n": 2, "failures": 0, "min": 410.0, "p50": 420.0, "p90": 428.0,
                                         "max": 430.0, "mean": 420.0},
        "agent.cold.dsh_boot_ms": {"n": 2, "failures": 0, "min": 900.0, "p50": 950.0, "p90": 990.0,
                                   "max": 1000.0, "mean": 950.0},
    },
}


def assert_same_structure(case, en: str, zh: str) -> None:
    """EN and ZH must match line by line: headings, table shape and figures."""
    case.assertEqual(len(en.splitlines()), len(zh.splitlines()))
    for line_en, line_zh in zip(en.splitlines(), zh.splitlines()):
        case.assertEqual(line_en.startswith("#"), line_zh.startswith("#"))
        case.assertEqual(line_en.count("|"), line_zh.count("|"), line_en)
        case.assertEqual(NUMBER.findall(line_en), NUMBER.findall(line_zh), line_en)
    case.assertGreater(len(re.findall(r"[一-鿿]", zh)), 500)
    case.assertEqual(len(re.findall(r"[一-鿿]", en)), 0)


def section(text: str, heading: str) -> str:
    """The body of one '## heading' section."""
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.results = root / "results"
        cls.dep = write_deployments(root)
        clock = FakeClock()
        # Sessions in run order: dsh ping x3, dsh agent x3, dsh_prewarm ping x3, dsh_prewarm agent x3.
        plan = [{}, {"boot": "pool"}, {"boot": "pool"},
                {}, {"boot": "pool"}, {"boot": "pool", "fail": "agent_fail"},
                {}, {"boot": "pool"}, {"fail": "throttle"},
                {}, {"boot": "pool"}, {"boot": "pool"}]
        base = ["--out", str(cls.results), "--deployments", str(cls.dep)]
        with patch.object(ct, "time", clock), contextlib.redirect_stdout(io.StringIO()):
            ct.main(["--smoke", "--runtimes", "dsh", "--sequences", "ping", *base],
                    client=FakeRuntime(clock), sleep=clock.sleep)
            ct.main(["--full", "--processes", "1", "--rounds", "3", "--settle-seconds", "45", *base],
                    client=FakeRuntime(clock, plan), sleep=clock.sleep)
        cls.summary = json.loads((cls.results / "summary.json").read_text())
        cls.cells = {(c["runtime"], c["sequence"], c["phase"], c["concurrency"], c["boot_kind"]): c
                     for c in cls.summary["cells"]}

    def generate(self, local=None):
        path = self.results / "local_baseline.json"
        if local is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(local))
        with contextlib.redirect_stdout(io.StringIO()):
            code = gen_report.main(["--results", str(self.results), "--deployments", str(self.dep)])
        self.assertEqual(code, 0)
        return (self.results / "REPORT.md").read_text(), (self.results / "REPORT.zh.md").read_text()

    def test_both_languages_have_the_same_structure_and_figures(self):
        for local in (None, LOCAL):
            with self.subTest(local=bool(local)):
                assert_same_structure(self, *self.generate(local))

    def test_header_states_date_region_image_and_model_from_the_data(self):
        en, zh = self.generate()
        started, finished = self.summary["test_started_iso"], self.summary["test_finished_iso"]
        when = f"{started[:10]} {started[11:16]}–{finished[11:16]} UTC"
        for text, date_label in ((en, "**Test date:**"), (zh, "**测试日期:**")):
            self.assertIn(f"{date_label} {when}", text)
            self.assertIn("us-east-1", text)
            self.assertIn("…3333", text)
            self.assertNotIn(DEPLOYMENTS["account"], text)
            self.assertIn("`deepseek.test-model`", text)
            self.assertIn("`@deepseek-ai/dsh` `9.9.9`", text)
            self.assertIn("`dsh_base` (`dsh`) — platform `—` — 1,054 MB / 279 MB — `dsh:test` — env: "
                          "`DSH_PREWARM=0`" if text is en else
                          "`dsh_base` (`dsh`) — 平台 `—` — 1,054 MB / 279 MB — `dsh:test` — env: "
                          "`DSH_PREWARM=0`", text)
            self.assertIn("`DSH_PREWARM=1`", text)
            self.assertIn("`EXAMPLE_API_KEY=<hidden>`", text)
            self.assertNotIn("must-not-be-printed", text)
            self.assertIn("image tag `dsh-test`" if text is en else "镜像 tag `dsh-test`", text)

    def test_summary_section_leads_the_report(self):
        en, zh = self.generate(LOCAL)
        self.assertLess(en.index("## Summary"), en.index("## Cold start and first message"))
        self.assertLess(zh.index("## 结论摘要"), zh.index("## 各并发档位的冷启动与第一条消息"))
        body = section(en, "Summary")
        # cold start, first message and its DSH boot; one failed agent turn on dsh: 5 of 6 cold requests ok
        self.assertIn("| `dsh` | — | `DSH_PREWARM=0`, `EXAMPLE_API_KEY=—` | real boot 6,007 (1) · warm-pool hit 157 (2) | "
                      "real boot 9,020 (1) · warm-pool hit 3,170 (1) | real boot 1,200 (1) · warm-pool hit 1,200 (1) | "
                      "5 / 6 |", body)
        self.assertIn("| `dsh_prewarm` | — | `DSH_PREWARM=1`, `EXAMPLE_API_KEY=<hidden>` |", body)
        self.assertIn("by concurrency: c=1: 40% (4/10).", body)
        self.assertIn("- **Local Docker floor:** container start + first answer p50 2,610 ms (n=2)", body)
        self.assertIn("- **Reliability:** 2 failed out of 23 requests sent; sessions stopped after measurement: "
                      "12/12.", body)
        self.assertNotIn("snapshot", body)
        self.assertNotIn("Local Docker floor", section(self.generate()[0], "Summary"))
        # the report is about the start of a session: nothing about later messages, nothing model-timed
        for absent in ("## Warm baseline", "follow-up", "first text", "DSH total", "1,550", "Page pin"):
            self.assertNotIn(absent, en)
        for absent in ("Warm 基线", "| 后续消息", "首段文本", "DSH 总耗时"):  # the intro may say they are left out
            self.assertNotIn(absent, zh)

    def test_tables_carry_the_summary_figures(self):
        en, _ = self.generate()
        f = gen_report.fmt
        ping = self.cells[("dsh", "ping", "cold", 1, "all")]["metrics"]["total_ms"]
        agent = self.cells[("dsh", "agent", "cold", 1, "all")]["metrics"]["total_ms"]
        self.assertEqual((ping["n"], agent["n"]), (3, 2))  # the failed agent turn is not a sample
        self.assertIn(f"| **dsh · ping** | {f(ping['p50'])} / {f(ping['p90'])} / {f(ping['p99'])} / "
                      f"{f(ping['max'])} (3) |", en)
        self.assertIn(f"| **dsh · agent** | {f(agent['p50'])} / {f(agent['p90'])} / {f(agent['p99'])} / "
                      f"{f(agent['max'])} (2) |", en)
        # two populations: 1 real boot (6,007 ms) and 2 warm-pool hits (157 ms) for dsh ping
        self.assertIn("| dsh · ping · c=1 | 1 | 6,007 | 6,007 | 2 | 157 | 157 | 33% | 0 |", en)
        self.assertIn("| dsh_prewarm · ping · c=1 | 1 | 6,007 | 6,007 | 1 | 157 | 157 | 50% | 1 |", en)
        # the first message: end-to-end, platform part, DSH boot
        made_of = section(en, "What the first message is made of")
        self.assertIn("| dsh · c=1 · real boot | 1 | 9,020 | 6,005 | 1,200 |", made_of)
        self.assertIn("| dsh · c=1 · warm-pool hit | 1 | 3,170 | 155 | 1,200 |", made_of)

    def test_variants_are_compared_side_by_side(self):
        en, zh = self.generate()
        self.assertIn("## Variant comparison\n", en)
        self.assertIn("## 变体对比\n", zh)
        self.assertIn("pooled over every concurrency level run (c=1)", en)
        self.assertIn("| metric | `dsh` | `dsh_prewarm` |", en)
        self.assertIn("| cold start (ping) · real boot | 6,007 (1) | 6,007 (1) |", en)
        self.assertIn("| first message · warm-pool hit | 3,170 (1) | 3,170 (2) |", en)
        self.assertIn("| first message · DSH boot · warm-pool hit | 1,200 (1) | 1,200 (2) |", en)

    def test_failures_are_reported_not_dropped(self):
        en, zh = self.generate()
        totals = self.summary["totals"]
        self.assertEqual((totals["attempted"], totals["failures"], totals["skipped"]), (23, 2, 1))
        self.assertIn("23 sent on 12 sessions, 2 failed, 1 not sent", en)
        self.assertIn("2 of 23 requests failed", en)
        self.assertIn("2 个请求失败(共发送 23 个", zh)
        self.assertIn("Sessions stopped after measurement: 12/12", en)
        self.assertIn("| dsh · agent · c=1 | cold | 3 | 2 | 1 | 0 | agent_failed: 1 |", en)
        self.assertIn("| dsh_prewarm · ping · c=1 | warm | 2 | 2 | 0 | 1 | — |", en)
        self.assertRegex(en, r"\| dsh_prewarm · ping · c=1 \| cold \| `\w{8}` \| 429 \| throttle \|")
        self.assertIn("model access \\| denied", en)  # pipes in messages cannot break the table
        self.assertIn("- **Reliability:** 2 failed out of 23 requests sent.", en)

    def test_findings_are_computed(self):
        en, zh = self.generate()
        self.assertIn("- **`dsh` · cold start (ping, c=1):** real boot p50 6,007 ms (n=1); "
                      "warm-pool hit p50 157 ms (n=2).", en)
        self.assertIn("- **`dsh` · inside the first message (real boot, c=1):** platform part "
                      "p50 6,005 ms (n=1); DSH boot p50 1,200 ms (n=1).", en)
        self.assertIn("- **`dsh_prewarm` · 第一条消息(c=1):**真实启动 p50 9,020 ms (n=1);"
                      "预热命中 p50 3,170 ms (n=2)。", zh)
        self.assertIn("- **`dsh` · real boot across every concurrency level (c=1), end-to-end:** "
                      "ping p50 6,007 ms, max 6,007 ms (n=1); agent p50 9,020 ms, max 9,020 ms (n=1).", en)
        data = gen_report.load_inputs(self.results, self.dep)
        ping_only = [dict(raw, requests=[r for r in raw["requests"] if r["boot_kind"] != "real_boot"])
                     for raw in data["raws"] if raw["meta"]["sequence"] == "ping"]
        text = gen_report.render(DEPLOYMENTS, self.summary, ping_only, [], None, "en")
        self.assertIn("real boot no samples; warm-pool hit p50 157 ms (n=2)", text)
        self.assertNotIn("## What the first message is made of", text)
        self.assertNotIn("· first message (c=", text)
        self.assertNotIn("inside the first message (real boot", text)

    def test_methodology_and_smoke_come_from_the_run(self):
        en, _ = self.generate()
        self.assertIn("fresh 40-char `runtimeSessionId`", en)
        self.assertIn("Pause between rounds and between cells (seconds, every value used): 45.", en)
        self.assertIn("dsh ping c=1: 3/3; dsh agent c=1: 3/3; dsh_prewarm ping c=1: 3/3; "
                      "dsh_prewarm agent c=1: 3/3.", en)
        self.assertIn("Read timeout 300 s", en)
        self.assertIn("- Agent prompt: `Reply with exactly: pong`.", en)
        self.assertIn("## Smoke probes", en)
        self.assertRegex(en, r"\| dsh \| ping \| cold \| yes \| 6,007 \| real_boot \| — \| — \|")
        for absent in ("Platform", "snapshot"):  # no platform version, no V2 runtime here
            self.assertNotIn(absent, en)

    def test_local_baseline_section(self):
        en, zh = self.generate()
        self.assertNotIn("## Local Docker vs AgentCore Runtime", en)
        self.assertIn("`results/local_baseline.json` not found", en)
        self.assertIn("未找到 `results/local_baseline.json`", zh)

        en, zh = self.generate(LOCAL)
        self.assertIn("## Local Docker vs AgentCore Runtime", en)
        self.assertIn("## 本地 Docker vs AgentCore Runtime", zh)
        self.assertNotIn("not found", en)
        self.assertIn("aarch64, 16 CPUs, 61.8 GB RAM, kernel `6.12.113`, Docker `25.0.14`", en)
        self.assertIn("tested 2026-11-12T05:10:00+00:00, 2 rounds, host page cache: warm, "
                      "DSH pre-warm enabled: false", en)
        self.assertIn("| metric | local Docker | `dsh` · real boot | `dsh` · warm-pool hit | "
                      "`dsh_prewarm` · real boot | `dsh_prewarm` · warm-pool hit |", en)
        rows = {line.split(" | ")[0].lstrip("| "): [c.strip(" |") for c in line.split(" | ")[1:]]
                for line in section(en, "Local Docker vs AgentCore Runtime").splitlines()
                if line.startswith("| ") and "---" not in line}
        self.assertEqual(len(rows), 4)  # header + cold start, DSH boot, first message
        start = next(v for k, v in rows.items() if k.startswith("cold start"))
        self.assertEqual(start, ["420 (2)", "6,007 (1)", "157 (2)", "6,007 (1)", "157 (1)"])  # from summary
        self.assertEqual(rows["first message · DSH boot"],
                         ["950 (2)", "1,200 (1)", "1,200 (1)", "1,200 (1)", "1,200 (2)"])
        first = next(v for k, v in rows.items() if k.startswith("first message (local"))
        self.assertEqual(first, ["2,610 (2)", "9,020 (1)", "3,170 (1)", "9,020 (1)", "3,170 (2)"])  # from records
        self.assertIn("image `agentcore-dsh-coldstart:dsh-test` (1,054 MB)", en)
        self.assertIn("so it is a floor, not a like-for-like cold start", en)
        self.assertIn("只是下限", zh)

    def test_missing_inputs(self):
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gen_report.main(["--results", empty, "--deployments", str(self.dep)]), 2)
            self.assertIn("summary.json not found", err.getvalue())
            self.assertEqual(list(Path(empty).iterdir()), [])

    def test_no_measurement_is_typed_into_the_generator(self):
        """Every figure must come from the data files: the report text in
        gen_report.py may only contain digits that are part of a name."""
        allowed = {"50", "90", "99",  # p50 / p90 / p99 column names
                   "200"}             # "non-200 response"
        tree = ast.parse((ROOT / "scripts" / "gen_report.py").read_text())
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)}
        found = {number for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and id(node) not in docstrings
                 for number in re.findall(r"\d+", node.value)}
        self.assertEqual({n for n in found if len(n) > 1} - allowed, set())


class PlatformReportTests(unittest.TestCase):
    """Two variants on platform V1 and the same two on V2 (snapshot restore)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.results = root / "results"
        cls.dep = write_deployments(root, PLATFORM_DEPLOYMENTS)
        clock = FakeClock()
        boot, pool, snap = {}, {"boot": "pool"}, {"boot": "snapshot"}
        slow = [{"boot": "snapshot", "cold_dsh_boot": seconds} for seconds in (50, 45, 60, 40, 20, 5)]
        # One fake for all three runs, so every dsh_v2 session restores the same snapshot.
        fake = FakeRuntime(clock, [
            slow[0],                                   # smoke: dsh_v2 agent
            slow[1], slow[2],                          # early run of dsh_v2 agent, later superseded
            boot, pool, pool, boot, pool, pool,        # dsh: ping x3, agent x3
            boot, pool, pool, boot, pool, pool,        # dsh_prewarm: ping x3, agent x3
            snap, snap, snap, slow[3], slow[4], slow[5],              # dsh_v2: ping x3, agent x3
            snap, snap, snap, *[{"boot": "snapshot", "cold_dsh_boot": 30}] * 3])  # dsh_prewarm_v2
        base = ["--out", str(cls.results), "--deployments", str(cls.dep)]
        with patch.object(ct, "time", clock), contextlib.redirect_stdout(io.StringIO()):
            ct.main(["--smoke", "--runtimes", "dsh_v2", "--sequences", "agent", *base],
                    client=fake, sleep=clock.sleep)
            ct.main(["--full", "--processes", "1", "--runtimes", "dsh_v2", "--sequences", "agent",
                     "--rounds", "2", *base],
                    client=fake, sleep=clock.sleep)
            early = next((cls.results / "raw").glob("*_dsh_v2_agent_c1.json"))
            early.rename(early.with_name("20200101T000000Z_dsh_v2_agent_c1.json"))
            ct.main(["--full", "--processes", "1", "--rounds", "3", *base], client=fake, sleep=clock.sleep)
        # dsh_v2's files as a client from before the snapshot label would have written them.
        for path in (cls.results / "raw").glob("*.json"):
            data = json.loads(path.read_text())
            if "prewarm" in path.name or not any(r["runtime"] == "dsh_v2" for r in data["requests"]):
                continue
            data["meta"].pop("platform_version", None)
            for record in data["requests"]:
                del record["platform_version"]
                record["boot_kind"] = record["boot_kind"].replace("snapshot_restore", "warm_pool")
            path.write_text(json.dumps(data))

    def generate(self, local=None, deployments=None):
        path = self.results / "local_baseline.json"
        if local is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(local))
        dep = self.dep
        if deployments is not None:
            (self.results.parent / "alt").mkdir(exist_ok=True)
            dep = write_deployments(self.results.parent / "alt", deployments)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gen_report.main(["--results", str(self.results), "--deployments", str(dep)]), 0)
        return (self.results / "REPORT.md").read_text(), (self.results / "REPORT.zh.md").read_text()

    def test_both_languages_have_the_same_structure_and_figures(self):
        for local in (None, LOCAL):
            with self.subTest(local=bool(local)):
                assert_same_structure(self, *self.generate(local))

    def test_summary_compares_platforms_and_the_probe_list_is_an_appendix(self):
        en, zh = self.generate()
        headings = [line for line in en.splitlines() if line.startswith("## ")]
        self.assertEqual(headings[0], "## Summary")
        self.assertEqual(headings[-1], "## Appendix · Platform V2: every cold agent probe in time order")
        self.assertEqual([line for line in zh.splitlines() if line.startswith("## ")][0], "## 结论摘要")
        body = section(en, "Summary")
        for text in (
                "| `dsh` | V1 | `DSH_PREWARM=0` | real boot 6,007 (1) · warm-pool hit 157 (2) | "
                "real boot 9,020 (1) · warm-pool hit 3,170 (2) | real boot 1,200 (1) · warm-pool hit 1,200 (2) | "
                "6 / 6 |",
                "| `dsh_v2` | V2 | `DSH_PREWARM=0` | snapshot restore 1,907 (3) | snapshot restore 23,720 (3) | "
                "snapshot restore 20,000 (3) | 6 / 6 |",
                "real boots on `dsh`, `dsh_prewarm`, by concurrency: c=1: 33% (4/12).",
                "- **`dsh` (V1) vs `dsh_v2` (V2), cold start:** real boot p50 6,007 ms (n=1); warm-pool hit "
                "p50 157 ms (n=2); snapshot restore p50 1,907 ms (n=3).",
                "- **`dsh_prewarm` (V1) vs `dsh_prewarm_v2` (V2), first message:** real boot p50 9,020 ms (n=1); "
                "warm-pool hit p50 3,170 ms (n=2); snapshot restore p50 33,720 ms (n=3).",
                "- **`dsh_v2` (V2): the first message is slow until its snapshot has been used a few times.** "
                "Cold agent turn number 1 served by the same snapshot: p50 53,720 ms (n=1); turn 6–10: "
                "p50 8,720 ms (n=1)",
                "- **Reliability:** 0 failed out of 48 requests sent; sessions stopped after measurement: 24/24."):
            self.assertIn(text, body)
        self.assertIn("- **`dsh` (V1) 对比 `dsh_v2` (V2),冷启动:**真实启动 p50 6,007 ms (n=1);预热命中 "
                      "p50 157 ms (n=2);快照恢复 p50 1,907 ms (n=3)。", section(zh, "结论摘要"))
        self.assertNotIn("1,550", en)  # no latency of a later message anywhere in the report

    def test_report_can_be_limited_to_one_platform_version(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = gen_report.main(["--results", str(self.results), "--deployments", str(self.dep),
                                    "--platforms", "V2"])
        en, zh = (self.results / "REPORT.md").read_text(), (self.results / "REPORT.zh.md").read_text()
        self.assertEqual(code, 0)
        assert_same_structure(self, en, zh)
        # the header says what was left out, and V1 appears nowhere else
        scope = ("- **Scope:** platform V2 only. Also measured, and left out of this report on purpose: "
                 "`dsh` (V1), `dsh_prewarm` (V1); their raw data is still in `results/raw/`.")
        self.assertIn(scope, en)
        self.assertNotIn("V1", en.replace(scope, ""))
        self.assertIn("- **范围:**仅平台 V2。", zh)
        for absent in ("real boot", "warm-pool", "warm pool", "| `dsh` |", "`dsh_prewarm` ", "shown alone",
                       "## The two populations"):
            self.assertNotIn(absent, en.replace(scope, ""))
        # totals and dates follow the scope: 2 runtimes x 6 sessions x 2 requests
        self.assertIn("- **Requests:** 24 sent on 12 sessions, 0 failed, 0 not sent", en)
        self.assertIn("- **Reliability:** 0 failed out of 24 requests sent; sessions stopped after measurement: "
                      "12/12.", section(en, "Summary"))
        self.assertEqual(len([line for line in section(en, "Summary").splitlines() if line.startswith("| `")]), 2)
        self.assertIn("| `dsh_v2` | V2 | `DSH_PREWARM=0` | snapshot restore 1,907 (3) | snapshot restore 23,720 (3) | "
                      "snapshot restore 20,000 (3) | 6 / 6 |", en)
        body = section(en, "Platform V2: every new session is a snapshot restore")
        self.assertRegex(body, r"\| `dsh_v2` \| V2 \| 6 \| 1 \|")
        alone = body.split("### `dsh_v2` (V2)\n")[1].split("### ")[0]
        self.assertIn("| metric | `dsh_v2` (V2) · snapshot restore |", alone)
        self.assertIn("| first message | 23,720 / 39,720 / 43,720 (3) |", alone)
        self.assertIn("The same figures by concurrency level.", alone)
        self.assertIn("| cold start (ping) · c=1 | 1,907 / 1,907 / 1,907 (3) |", alone)
        # superseded runs and smoke probes of the kept runtimes are still listed
        rows = [line for line in section(en, "Appendix · Platform V2: every cold agent probe in time order").splitlines()
                if line.startswith("| 20")]
        self.assertEqual(len(rows), 9)
        self.assertIn("## 平台 V2:每个新 session 都是快照恢复", zh)
        # a platform nobody measured is an error, not an empty report
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(gen_report.main(["--results", str(self.results), "--deployments", str(self.dep),
                                              "--platforms", "V9"]), 2)
        self.assertIn("no measured runtime is on platform V9", err.getvalue())

    def test_pin_sample_code_is_copied_from_the_repository(self):
        en, zh = self.generate()
        body = section(en, "Page pin on a snapshot-restore platform: effect and sample code")
        pinner = (ROOT / "app" / "pinner.py").read_text().rstrip("\n")
        wrapper = (ROOT / "app" / "server.mjs").read_text()
        self.assertIn("```python\n" + pinner + "\n```", body)           # the whole file, verbatim
        hooks = body.split("```javascript\n")[1].split("\n```")[0]
        self.assertIn(hooks, wrapper)                                    # an exact excerpt of the wrapper
        self.assertIn("function startPinner()", hooks)
        self.assertTrue(hooks.rstrip().endswith("}") and "function releasePinner()" in hooks)
        calls = body.split("```javascript\n")[2].split("\n```")[0].splitlines()
        self.assertTrue(calls and all(call in wrapper for call in calls))
        manifest = sum(1 for line in (ROOT / "app" / "pin-manifest.txt").read_text().splitlines()
                       if line.strip() and not line.startswith("#"))
        self.assertIn(f"`app/pin-manifest.txt`, {manifest:,} files", body)
        self.assertIn("### 示例代码:pinner(`app/pinner.py`)", zh)
        self.assertIn("```python\n" + pinner + "\n```", zh)
        self.assertNotIn("| metric |", body)   # no runtime in this fixture reported a pinner
        # without the source files the section is left out, and nothing else changes
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stdout(io.StringIO()):
            gen_report.main(["--results", str(self.results), "--deployments", str(self.dep), "--app", empty])
            bare = (self.results / "REPORT.md").read_text()
        self.assertNotIn("Page pin", bare)
        self.assertEqual(bare, en.replace("## Page pin" + en.split("## Page pin")[1].split("\n## ")[0] + "\n## ", "## "))

    def test_pin_effect_is_reported_against_its_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, dep, clock = Path(tmp) / "results", write_deployments(tmp, PLATFORM_DEPLOYMENTS), FakeClock()
            detail = {"files": 3629, "bytes": 153966238, "skipped": 1, "listed": 3630, "max_bytes": 1 << 30}
            fake = FakeRuntime(clock, [
                {"boot": "snapshot", "cold_dsh_boot": 40}, {"boot": "snapshot", "cold_dsh_boot": 20},
                {"boot": "snapshot", "cold_dsh_boot": 9, "pin": {**detail, "ms": 82}},
                {"boot": "snapshot", "cold_dsh_boot": 2, "pin": {**detail, "ms": 113}}])
            with patch.object(ct, "time", clock), contextlib.redirect_stdout(io.StringIO()):
                ct.main(["--full", "--processes", "1", "--runtimes", "dsh_v2,dsh_prewarm_v2", "--sequences", "agent",
                         "--rounds", "2", "--out", str(out), "--deployments", str(dep)],
                        client=fake, sleep=clock.sleep)
                gen_report.main(["--results", str(out), "--deployments", str(dep)])
            en, zh = (out / "REPORT.md").read_text(), (out / "REPORT.zh.md").read_text()
        assert_same_structure(self, en, zh)
        body = section(en, "Page pin on a snapshot-restore platform: effect and sample code")
        for row in ("| metric | `dsh_v2` (control) | `dsh_prewarm_v2` (page pin) |",
                    "| files pinned | — | 3,629 |",
                    "| memory pinned, MB | — | 147 |",
                    "| time to pin before listening, ms | — | 82–113 |",
                    "| first message, p50 / p90 / max | 33,720 / 41,720 / 43,720 (2) | 9,220 / 12,020 / 12,720 (2) |",
                    "| first message · DSH boot, p50 / p90 / max | 30,000 / 38,000 / 40,000 (2) | "
                    "5,500 / 8,300 / 9,000 (2) |",
                    "| first message, turn 1 on the same snapshot, p50 / p90 / max | 43,720 / 43,720 / 43,720 (1) | "
                    "12,720 / 12,720 / 12,720 (1) |",
                    "| first message, turn 2 on the same snapshot, p50 / p90 / max | 23,720 / 23,720 / 23,720 (1) | "
                    "5,720 / 5,720 / 5,720 (1) |"):
            self.assertIn(row, body)
        self.assertIn("- **Page pin (`dsh_prewarm_v2` against its control `dsh_v2`, same image):** first message "
                      "p50 33,720 ms (n=2) without it and p50 9,220 ms (n=2) with it; DSH boot p50 30,000 ms (n=2) "
                      "without it and p50 5,500 ms (n=2) with it.", section(en, "Summary"))
        self.assertIn("| 指标 | `dsh_v2` (对照组) | `dsh_prewarm_v2` (page pin) |", zh)

    def test_header_and_existing_sections_know_the_platform(self):
        en, zh = self.generate(LOCAL)
        self.assertIn("`dsh_base_v2` (`dsh_v2`) — platform `V2` — 1,054 MB / 279 MB — `dsh:test` — env: "
                      "`DSH_PREWARM=0`", en)
        self.assertIn("`dsh_base` (`dsh`) — 平台 `V1` —", zh)
        # the real-boot / warm-pool split is for V1 only
        split = section(en, "The two populations")
        self.assertIn("applies only to runtimes whose sessions start a container (`dsh`, `dsh_prewarm`). "
                      "Sessions on `dsh_prewarm_v2`, `dsh_v2` are restored from a snapshot", split)
        self.assertIn("| dsh · ping · c=1 | 1 | 6,007 | 6,007 | 2 | 157 | 157 | 33% | 0 |", split)
        self.assertNotIn("| dsh_v2 ·", split)
        self.assertIn("| dsh_v2 · c=1 · snapshot restore | 3 | 23,720 | 1,905 | 20,000 |",
                      section(en, "What the first message is made of"))
        self.assertIn("| **dsh_v2 · ping** | 1,907 / 1,907 / 1,907 / 1,907 (3) |",
                      section(en, "Cold start and first message by concurrency"))
        self.assertRegex(section(en, "Smoke probes"), r"\| dsh_v2 \| agent \| cold \| yes \| 53,720 \| "
                                                      r"snapshot_restore \| 50,000 \| — \|")
        self.assertIn("| metric | local Docker | `dsh` (V1) · real boot | `dsh` (V1) · warm-pool hit | "
                      "`dsh_prewarm` (V1) · real boot | `dsh_prewarm` (V1) · warm-pool hit | "
                      "`dsh_v2` (V2) · snapshot restore | `dsh_prewarm_v2` (V2) · snapshot restore |", en)
        self.assertNotIn("warm_pool", section(en, "Smoke probes"))
        self.assertNotIn("| warm |", section(en, "Smoke probes"))  # only first invokes (and failures)

    def test_variants_are_compared_within_a_platform_version(self):
        en, zh = self.generate()
        variants = section(en, "Variant comparison")
        v1, v2 = variants.split("### Platform V2")
        self.assertIn("### Platform V1", v1)
        self.assertIn("| metric | `dsh` | `dsh_prewarm` |", v1)
        self.assertIn("| cold start (ping) · warm-pool hit | 157 (2) | 157 (2) |", v1)
        self.assertNotIn("snapshot", v1.split("### Platform V1")[1])
        self.assertIn("| metric | `dsh_v2` | `dsh_prewarm_v2` |", v2)
        self.assertIn("| cold start (ping) · snapshot restore | 1,907 (3) | 1,907 (3) |", v2)
        self.assertIn("| first message · snapshot restore | 23,720 (3) | 33,720 (3) |", v2)
        self.assertIn("| first message · DSH boot · snapshot restore | 20,000 (3) | 30,000 (3) |", v2)
        self.assertNotIn("real boot", v2)
        self.assertIn("### 平台 V2", zh)

    def test_platform_comparison_tables(self):
        en, zh = self.generate()
        body = section(en, "Platform V1 vs V2")
        self.assertIn("## 平台 V1 vs V2", zh)
        # evidence: V1 probes each meet their own process, V2 probes share the snapshot's
        self.assertRegex(body, r"\| `dsh` \| V1 \| 6 \| 6 \|")
        self.assertRegex(body, r"\| `dsh_v2` \| V2 \| 6 \| 1 \|")
        pair = body.split("### `dsh` (V1) vs `dsh_v2` (V2)")[1].split("### ")[0]
        self.assertIn("| metric | `dsh` (V1) · real boot | `dsh` (V1) · warm-pool hit | "
                      "`dsh_v2` (V2) · snapshot restore |", pair)
        for row in (
                "| cold start (ping) | 6,007 / 6,007 / 6,007 (1) | 157 / 157 / 157 (2) | 1,907 / 1,907 / 1,907 (3) |",
                "| first message | 9,020 / 9,020 / 9,020 (1) | 3,170 / 3,170 / 3,170 (2) | "
                "23,720 / 39,720 / 43,720 (3) |",
                "| first message · DSH boot | 1,200 / 1,200 / 1,200 (1) | 1,200 / 1,200 / 1,200 (2) | "
                "20,000 / 36,000 / 40,000 (3) |",
                "| first message · platform part | 6,005 / 6,005 / 6,005 (1) | 155 / 155 / 155 (2) | "
                "1,905 / 1,905 / 1,905 (3) |",
                # per concurrency, with V1's real-boot share
                "| cold start (ping) · c=1 | 6,007 / 6,007 / 6,007 (1) | 157 / 157 / 157 (2) | "
                "1,907 / 1,907 / 1,907 (3) | 33% |",
                "| first message · c=1 | 9,020 / 9,020 / 9,020 (1) | 3,170 / 3,170 / 3,170 (2) | "
                "23,720 / 39,720 / 43,720 (3) | 33% |",
                "| first message · DSH boot · c=1 | 1,200 / 1,200 / 1,200 (1) | 1,200 / 1,200 / 1,200 (2) | "
                "20,000 / 36,000 / 40,000 (3) | 33% |"):
            self.assertIn(row, pair)
        for absent in ("warm agent", "first text", "DSH total"):
            self.assertNotIn(absent, pair)
        self.assertIn("### `dsh_prewarm` (V1) vs `dsh_prewarm_v2` (V2)", body)
        self.assertNotIn("shown alone", body)

    def test_every_v2_cold_agent_probe_is_listed_in_time_order(self):
        en, _ = self.generate()
        rows = [line.strip("| ").split(" | ") for line in
                section(en, "Appendix · Platform V2: every cold agent probe in time order").splitlines()
                if line.startswith("| 20")]
        self.assertEqual([row[0] for row in rows], sorted(row[0] for row in rows))
        dsh_v2 = [row for row in rows if row[1] == "dsh_v2"]
        # superseded and smoke probes are listed although the summary tables ignore them
        self.assertEqual([row[3] for row in dsh_v2], ["smoke", "superseded", "superseded", "matrix", "matrix", "matrix"])
        self.assertEqual([row[5] for row in dsh_v2], ["53,720", "48,720", "63,720", "43,720", "23,720", "8,720"])
        self.assertEqual([row[6] for row in dsh_v2], ["50,000", "45,000", "60,000", "40,000", "20,000", "5,000"])
        self.assertEqual([row[8] for row in dsh_v2], ["1", "2", "3", "4", "5", "6"])  # turn on its snapshot
        self.assertEqual(len({row[7] for row in dsh_v2}), 1)                          # one wrapper start
        self.assertEqual(rows[0][:3] + rows[0][9:], ["2027-01-15T08:16:40", "dsh_v2", "1", "10.0"])
        ages = [float(row[9]) for row in rows]
        self.assertEqual(ages, sorted(ages))
        self.assertEqual(len([row for row in rows if row[1] == "dsh_prewarm_v2"]), 3)
        order = section(en, "Platform V2: cold agent turns by order of use of the same snapshot")
        self.assertIn("| runtime | turn on the same snapshot | first message end-to-end | DSH boot | platform part |",
                      order)
        self.assertIn("| `dsh_v2` | 1 | 53,720 / 53,720 / 53,720 (1) | 50,000 / 50,000 / 50,000 (1) | "
                      "1,905 / 1,905 / 1,905 (1) |", order)
        self.assertIn("| `dsh_v2` | 6–10 | 8,720 / 8,720 / 8,720 (1) |", order)
        self.assertIn("| `dsh_prewarm_v2` | 3 | 33,720 / 33,720 / 33,720 (1) |", order)

    def test_findings_compare_the_platforms(self):
        en, zh = self.generate()
        findings = section(en, "Detailed findings")
        for line in (
                "- **`dsh_v2` (V2) · cold start (ping, c=1):** snapshot restore p50 1,907 ms (n=3).",
                "- **`dsh_v2` (V2) · inside the first message (snapshot restore, c=1):** platform part "
                "p50 1,905 ms (n=3); DSH boot p50 20,000 ms (n=3).",
                "- **`dsh_v2` (V2) · snapshot restore across every concurrency level (c=1), end-to-end:** "
                "ping p50 1,907 ms, max 1,907 ms (n=3); agent p50 23,720 ms, max 43,720 ms (n=3).",
                "- **Platform comparison · cold start (ping), end-to-end (c=1 pooled):** `dsh` (V1) · real boot "
                "p50 6,007 ms (n=1); `dsh` (V1) · warm-pool hit p50 157 ms (n=2); `dsh_v2` (V2) · snapshot "
                "restore p50 1,907 ms (n=3).",
                "- **Platform comparison · DSH boot inside the first message (c=1 pooled):** `dsh_prewarm` (V1) · "
                "real boot p50 1,200 ms (n=1); `dsh_prewarm` (V1) · warm-pool hit p50 1,200 ms (n=2); "
                "`dsh_prewarm_v2` (V2) · snapshot restore p50 30,000 ms (n=3).",
                "- **`dsh_v2` (V2) · first message by order of use of the same snapshot (every raw "
                "file):** turn 1 p50 53,720 ms (n=1); turn 2 p50 48,720 ms (n=1); turn 3 p50 63,720 ms (n=1); "
                "turn 4 p50 43,720 ms (n=1); turn 5 p50 23,720 ms (n=1); turn 6–10 p50 8,720 ms (n=1)."):
            self.assertIn(line, findings)
        self.assertNotIn("follow-up", findings)
        self.assertIn("- **平台对比 · 冷启动(ping),端到端(合并 c=1):**`dsh` (V1) · 真实启动 p50 6,007 ms (n=1);"
                      "`dsh` (V1) · 预热命中 p50 157 ms (n=2);`dsh_v2` (V2) · 快照恢复 p50 1,907 ms (n=3)。", zh)

    def test_a_v2_runtime_without_a_v1_twin_is_shown_alone(self):
        deployments = json.loads(json.dumps(PLATFORM_DEPLOYMENTS))
        deployments["runtimes"]["dsh_prewarm_v2"]["env"] = {"DSH_PREWARM": "2"}
        deployments["runtimes"]["dsh_prewarm_v2"]["deployed_at"] = "2027-01-15T08:11:40+00:00"
        en, zh = self.generate(deployments=deployments)
        assert_same_structure(self, en, zh)
        body = section(en, "Platform V1 vs V2")
        alone = body.split("### `dsh_prewarm_v2` (V2)\n")[1]
        self.assertIn("shown alone", alone)
        self.assertIn("| metric | `dsh_prewarm_v2` (V2) · snapshot restore |", alone)
        self.assertIn("| cold start (ping) · c=1 | 1,907 / 1,907 / 1,907 (3) |", alone)
        self.assertIn("### `dsh` (V1) vs `dsh_v2` (V2)", body)
        self.assertNotIn("`dsh_prewarm` (V1) · real boot p50", section(en, "Detailed findings").split("Platform comparison", 1)[1])
        # a runtime's own deployed_at wins over the file-level one: 5 minutes later, so 5 minutes younger
        rows = [line.strip("| ").split(" | ") for line in
                section(en, "Appendix · Platform V2: every cold agent probe in time order").splitlines()
                if line.startswith("| 20")]
        original = [line.strip("| ").split(" | ") for line in
                    section(self.generate()[0], "Appendix · Platform V2: every cold agent probe in time order").splitlines()
                    if line.startswith("| 20")]
        for new, old in zip(rows, original):
            shift = 5.0 if new[1] == "dsh_prewarm_v2" else 0.0
            self.assertAlmostEqual(float(old[9]) - float(new[9]), shift, places=1)

    def test_rows_per_concurrency_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, dep = Path(tmp) / "results", write_deployments(tmp, PLATFORM_DEPLOYMENTS)
            fake = FakeRuntime(plan=[{}, {"boot": "pool"}, {"boot": "pool"},
                                     {"boot": "snapshot"}, {"boot": "snapshot"}, {"boot": "snapshot"}])
            with contextlib.redirect_stdout(io.StringIO()):
                ct.main(["--full", "--processes", "1", "--runtimes", "dsh,dsh_v2", "--sequences", "ping",
                         "--concurrency", "1,2", "--rounds", "1", "--out", str(out), "--deployments", str(dep)],
                        client=fake, sleep=lambda seconds: None)
                self.assertEqual(gen_report.main(["--results", str(out), "--deployments", str(dep)]), 0)
            en, zh = (out / "REPORT.md").read_text(), (out / "REPORT.zh.md").read_text()
        self.assertEqual(len(en.splitlines()), len(zh.splitlines()))
        body = section(en, "Platform V1 vs V2")
        self.assertIn("All concurrency levels pooled (c=1, c=2).", body)
        self.assertRegex(body, r"\| cold start \(ping\) · c=1 \| [\d,]+ / [\d,]+ / [\d,]+ \(1\) \| — \| "
                               r"[\d,]+ / [\d,]+ / [\d,]+ \(1\) \| 100% \|")
        self.assertRegex(body, r"\| cold start \(ping\) · c=2 \| — \| [\d,]+ / [\d,]+ / [\d,]+ \(2\) \| "
                               r"[\d,]+ / [\d,]+ / [\d,]+ \(2\) \| 0% \|")
        self.assertNotIn("| first message", body)  # only the sequences that were run


class ClientReportTests(unittest.TestCase):
    """One cell run with a single process and again with worker processes, plus a
    raw file in the format from before the client-side measurements existed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.results, cls.dep = root / "results", write_deployments(root)
        base = ["--full", "--sequences", "ping", "--out", str(cls.results), "--deployments", str(cls.dep)]
        pool = {"boot": "pool", "connect": 5.0}
        with contextlib.redirect_stdout(io.StringIO()):
            ct.main([*base, "--runtimes", "dsh_prewarm", "--rounds", "2", "--processes", "1"],
                    client=FakeRuntime(default=pool), sleep=lambda seconds: None)
            ct.main([*base, "--runtimes", "dsh", "--concurrency", "4", "--rounds", "2", "--processes", "1"],
                    client=FakeRuntime(default=pool, client_delay=1.0), sleep=lambda seconds: None)
            single = next((cls.results / "raw").glob("*_dsh_ping_c4.json"))
            single.rename(single.with_name("20200101T000000Z_dsh_ping_c4.json"))
            # an old client: no process fields in the meta, no client-side fields in the records
            old = next((cls.results / "raw").glob("*_dsh_prewarm_ping_c1.json"))
            data = json.loads(old.read_text())
            for key in ("process_mode", "processes", "threads_per_process", "start_method", "client_warmed",
                        "cpu_count", "worker_failures"):
                del data["meta"][key]
            for record in data["requests"]:
                for key in ("pid", "worker", "release_to_start_ms", "start_to_send_ms", "connect_ms"):
                    del record[key]
            old.write_text(json.dumps(data))
            cls.code = ct.main([*base, "--runtimes", "dsh", "--concurrency", "4", "--rounds", "2", "--processes", "2"],
                               client_factory=partial(fake_factory, default=pool, client_delay=1.0),
                               sleep=lambda seconds: None)
            gen_report.main(["--results", str(cls.results), "--deployments", str(cls.dep)])
        cls.en = (cls.results / "REPORT.md").read_text()
        cls.zh = (cls.results / "REPORT.zh.md").read_text()

    def test_report_renders_in_both_languages(self):
        self.assertEqual(self.code, 0)
        assert_same_structure(self, self.en, self.zh)
        summary = json.loads((self.results / "summary.json").read_text())
        self.assertEqual(len(summary["sources"]), 2)  # the single-process c=4 run was superseded
        old = next(c for c in summary["cells"] if c["runtime"] == "dsh_prewarm")
        self.assertEqual((old["process_mode"], old["processes"], old["threads_per_process"]), ("inline", 1, [1]))
        self.assertEqual(old["metrics"]["start_to_send_ms"]["n"], 0)

    def test_client_overhead_table(self):
        body = section(self.en, "Client-side overhead")
        # the old file still loads: one process, and a dash for everything it never recorded
        self.assertIn("| c=1 | single process, 1 thread | 1 | 2 | — | — | — | — |", body)
        number = r"\d[\d,]*\.\d"
        row = re.search(rf"\| c=4 \| 2 spawned processes × up to 2 threads \| 1 \| 8 \| ({number}) / ({number}) \| "
                        rf"({number}) / ({number}) / ({number}) \| ({number}) / ({number}) / ({number}) \| "
                        rf"({number}) / ({number}) / ({number}) \|", body)
        self.assertIsNotNone(row, body)
        values = [float(v.replace(",", "")) for v in row.groups()]
        self.assertGreaterEqual(values[5], 2.0)    # t0 -> before-send p50: the fake's 2 ms of signing
        self.assertGreaterEqual(values[8], 10.0)   # new connection p50: the fake's 10 ms
        self.assertIn("| 并发 | 客户端 | 格子数 | 冷请求数 |", section(self.zh, "客户端自身的开销"))
        self.assertIn("| c=4 | 2 个 spawn 进程 × 每个最多 2 个线程 | 1 | 8 |", self.zh)

    def test_single_and_multi_process_runs_of_one_cell_are_shown_side_by_side(self):
        body = section(self.en, "Single-process vs multi-process client on the same cell")
        rows = [line.strip("| ").split(" | ") for line in body.splitlines() if line.startswith("| dsh ·")]
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("dsh · ping · c=4", "single process, 4 threads"),
                          ("dsh · ping · c=4", "2 spawned processes × up to 2 threads")])
        self.assertEqual([row[3] for row in rows], ["8 / 8", "8 / 8"])
        self.assertIn("| cold ok / sent | cold · warm-pool hit | t0 spread per round p50 / max |", body)
        self.assertNotIn("real boot", body)        # only the boot kinds that occurred get a column
        for row in rows:
            self.assertRegex(row[4], r"^[\d,]+ / [\d,]+ / [\d,]+ \(8\)$")   # cold warm-pool hits
            self.assertRegex(row[5], r"^[\d,.]+ / [\d,.]+$")
            for delay in row[6:9]:                 # release -> t0, t0 -> before-send, new connection
                self.assertRegex(delay, r"^[\d,.]+ / [\d,.]+ / [\d,.]+$")
            self.assertEqual(len(row), 9)
        self.assertNotIn("dsh_prewarm", body)  # that cell has only one kind of run
        self.assertIn("## 同一格子上单进程与多进程客户端的对比", self.zh)

    def test_method_and_findings_name_the_client(self):
        self.assertIn("Client per concurrency level (number of cells): c=1: single process, 1 thread (1); "
                      "c=4: 2 spawned processes × up to 2 threads (1).", section(self.en, "Methodology"))
        self.assertRegex(section(self.en, "Detailed findings"),
                         r"- \*\*Client side at c=4 \(2 spawned processes × up to 2 threads\):\*\* t0 spread per "
                         r"round p50 / max [\d.]+ / [\d.]+ ms; release → t0 p50 / p90 / max")
        self.assertIn("各并发档位使用的客户端(格子数):c=1: 单进程,1 个线程 (1); c=4: 2 个 spawn 进程 × "
                      "每个最多 2 个线程 (1)。", self.zh)

    def test_probes_a_failed_worker_never_sent_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, dep = Path(tmp) / "results", write_deployments(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                code = ct.main(["--full", "--runtimes", "dsh", "--sequences", "ping", "--concurrency", "4",
                                "--rounds", "1", "--processes", "2", "--out", str(out), "--deployments", str(dep)],
                               client_factory=partial(fake_factory, default={"boot": "pool"}, fail_worker=1),
                               sleep=lambda seconds: None)
                gen_report.main(["--results", str(out), "--deployments", str(dep)])
            en, zh = (out / "REPORT.md").read_text(), (out / "REPORT.zh.md").read_text()
        self.assertEqual(code, 1)
        assert_same_structure(self, en, zh)
        self.assertIn("4 sent on 4 sessions, 0 failed, 4 not sent", en)
        self.assertIn("| dsh · ping · c=4 | cold | 2 | 2 | 0 | 2 | not_sent: 2 |", en)
        self.assertIn("| dsh · ping · c=4 | warm | 2 | 2 | 0 | 2 | — |", en)
        self.assertIn("| c=4 | 2 spawned processes × up to 2 threads | 1 | 2 |", en)


if __name__ == "__main__":
    unittest.main()
