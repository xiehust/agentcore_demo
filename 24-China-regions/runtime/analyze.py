#!/usr/bin/env python3
"""Recompute acceptance from saved requests and render the Chinese report; no AWS calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from benchmark import max_overlap, stats

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Recorded result directory")
    args = parser.parse_args()
    folder = Path(args.output).resolve()
    data = read(folder / "benchmark_results.json")
    resources = read(folder / "resources.json")
    summary = data["summary"]
    raw = data["requests"]
    groups = {p: sorted([r for r in raw if r["phase"] == p], key=lambda r: r["index"])
              for p in ("cold", "warmup", "warm", "scale")}
    checks = {}
    settings = data["environment"]["settings"]
    for phase, expected in [("cold", settings["cold_samples"]), ("warm", settings["warm_samples"]),
                            ("warmup", 1), ("scale", settings["concurrency"])]:
        rows = groups[phase]
        assert len(rows) == expected, (phase, len(rows), expected)
        good = [r for r in rows if r["status"] == "PASS"]
        computed = stats([r["latency_ms"] for r in good])
        assert computed == summary["phases"][phase]["latency"]
        assert all(r["metadata"]["HTTPStatusCode"] == 200 and r["metadata"]["RetryAttempts"] == 0
                   and r["body"]["echo"] == r["nonce"] for r in good)
        checks[phase] = {"attempts": len(rows), "successful": len(good), "statistics_recomputed": True}
    for phase in ("cold", "scale"):
        good = [r for r in groups[phase] if r["status"] == "PASS"]
        checks[phase]["unique_instances"] = len({r["body"]["instance_id"] for r in good})
        checks[phase]["all_first_request"] = all(r["body"]["request_index"] == 1 for r in good)
        assert checks[phase]["unique_instances"] == len(good)
        assert checks[phase]["all_first_request"]
    warmup = groups["warmup"][0]["body"]
    assert all(r["body"]["instance_id"] == warmup["instance_id"]
               and r["body"]["request_index"] == r["index"] + 2
               for r in groups["warm"] if r["status"] == "PASS")
    checks["warm"]["same_instance_contiguous_sequence"] = True
    tests = summary["tests"]
    for key, phase, metric, limit in [("7.1", "cold", "p50_ms", 3000),
                                     ("7.2", "cold", "p99_ms", 5000),
                                     ("7.3", "warm", "p99_ms", 200)]:
        computed = summary["phases"][phase]["latency"][metric]
        expected = "PASS" if (tests[key]["sample_integrity"] and computed < limit) else "FAIL"
        assert tests[key]["status"] == expected
    assert len(data["sessions"]) == 151
    assert all(s["stop_confirmed"] for s in data["sessions"].values())
    assert len({r["session_id"] for r in raw}) == 151
    assert len(raw) == 651
    assert [json.loads(line) for line in (folder / "requests.jsonl").read_text().splitlines()] == raw
    assert set(data["sessions"]) == {row["session_id"] for row in raw}
    assert all(row["stop_response"]["ResponseMetadata"]["HTTPStatusCode"] == 200
               for row in data["sessions"].values())
    scale_good = [r for r in groups["scale"] if r["status"] == "PASS"]
    scale_peak = max_overlap([(r["client_start_unix_s"], r["call_end_unix_s"])
                              for r in groups["scale"]])
    scale_expected = ("PASS" if len(scale_good) == 50 and scale_peak == 50
                      and len({r["body"]["instance_id"] for r in scale_good}) == 50
                      and all(r["body"]["request_index"] == 1 for r in scale_good) else "FAIL")
    assert tests["7.4"]["status"] == scale_expected
    assert tests["7.4"]["errors"] == 50 - len(scale_good)
    assert hashlib.sha256((folder / "benchmark.py").read_bytes()).hexdigest() == read(
        folder / "benchmark-source.json")["sha256"]
    if (folder / "app.executed.py").exists():
        checks["app_sha256"] = hashlib.sha256((folder / "app.executed.py").read_bytes()).hexdigest()
    checks.update(total_requests=651, runtime_sessions=151, all_stop_responses_received=True,
                  benchmark_source_hash_verified=True, jsonl_matches_full_result=True,
                  scale_acceptance_recomputed=True, at=datetime.now(timezone.utc).isoformat())
    (folder / "evidence_audit.json").write_text(json.dumps(checks, indent=2) + "\n")
    rel = folder.relative_to(ROOT).as_posix()
    latency = summary["phases"]
    table = []
    names = {"7.1": "单并发首次请求 P50", "7.2": "单并发首次请求 P99",
             "7.3": "同一热实例 P99", "7.4": "0 → 50 并发请求错误率"}
    targets = {"7.1": "< 3,000 ms", "7.2": "< 5,000 ms", "7.3": "< 200 ms", "7.4": "0%"}
    for key in ("7.1", "7.2", "7.3", "7.4"):
        row = tests[key]
        value = (f"{row['observed_ms']:.3f} ms" if key != "7.4"
                 else f"{row['errors']}/{row['samples']}，{row['error_rate']:.0%}")
        table.append(f"| {key} | P0 | {names[key]} | {targets[key]} | {value} | **{row['status']}** |")
    phase_table = []
    for phase, title in [("cold", "新会话首次请求"), ("warm", "同实例热请求"), ("scale", "50 并发，含 5 秒处理")]:
        row = latency[phase]
        values = row["latency"]
        phase_table.append(f"| {title} | {row['successes']}/{row['attempts']} | "
                           + " | ".join(f"{values[k]:.3f}" for k in ("p50_ms", "p95_ms", "p99_ms", "max_ms"))
                           + " |")
    scale_rows = groups["scale"]
    peak = max_overlap([(r["body"]["handler_started_unix_s"], r["body"]["handler_finished_unix_s"])
                        for r in scale_rows if r["status"] == "PASS"])
    skew = max(r["launch_offset_ms"] for r in scale_rows) - min(r["launch_offset_ms"] for r in scale_rows)
    warm_over = sum(r["latency_ms"] >= 200 for r in groups["warm"])
    first_at = datetime.fromtimestamp(min(r["client_start_unix_s"] for r in raw), timezone.utc).isoformat()
    last_at = datetime.fromtimestamp(max(r["call_end_unix_s"] for r in raw), timezone.utc).isoformat()
    all_pass = all(t["status"] == "PASS" for t in tests.values())
    clean_path = folder / "cleanup.json"
    clean = read(clean_path) if clean_path.exists() else None
    cleaned = (clean is not None and resources.get("cleanup_completed_at")
               and not any(a["status"] == "ERROR" for a in clean["actions"]))
    cleanup_text = ("本轮 151 个 Runtime 会话的 Stop 均成功；压测 Code Interpreter 会话已停止，"
                    "2 个 Runtime、1 个自定义 Code Interpreter、2 个专用 IAM 角色、1 个 ECR 仓库"
                    "和本次 Runtime 日志组已删除。"
                    if cleaned else "Runtime 会话已发出成功的 Stop；其他资源清理结果尚未完整确认。")
    text = f"""# AgentCore Runtime 中国区实测报告

2026-09-21，账号 `447150580482`，区域 `cn-northwest-1`。
**最终一轮四项验收{'全部通过' if all_pass else '未全部通过'}**。
这里的冷启动和从 0 扩容采用“全新用户会话”的可观测口径，不能据此声称已清空平台内部预热池。

## 验收结果

| 编号 | 优先级 | 验证项 | 目标 | 实测 | 结论 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(table)}

主测试共有 **651 次请求，151 个 Runtime 会话**，包含 1 次不计入热请求统计的准备调用。
样本窗口：`{first_at}` 至 `{last_at}`。
P50/P99 使用 nearest-rank；SDK 自动重试关闭；所有请求均保留。

## 测量环境

| 项目 | 配置 |
| --- | --- |
| 管理端 profile | `agentcore_cn` |
| 压测客户端 | 宁夏 `cn-northwest-1` 的 PUBLIC Code Interpreter，专用 IAM 执行角色 |
| 客户端 boto3 / botocore | {data['environment']['boto3']} / {data['environment']['botocore']} |
| 本地管理端 | us-west-2 工作站；不使用该工作站的网络延迟进行验收 |
| 被测服务 | Python 标准库 HTTP echo，无 LLM、无外部业务依赖 |
| 镜像架构 | linux/arm64 |
| ECR 压缩镜像大小 | {resources['image']['ecr']['imageSizeInBytes']:,} bytes，{resources['image']['ecr']['imageSizeInBytes']/1024**2:.3f} MiB |
| 镜像 digest | `{resources['image']['ecr']['imageDigest']}` |
| Runtime 网络 | PUBLIC，默认 IAM 签名认证 |
| 生命周期 | idle 60 秒，max lifetime 1,800 秒；测试结束主动 Stop |
| baseline Runtime | `{resources['runtimes']['baseline']['agentRuntimeId']}`，版本 1 |
| scale Runtime | `{resources['runtimes']['scale']['agentRuntimeId']}`，版本 1 |

完整响应延迟从 SDK Invoke 调用前开始，到响应体完整读取结束，包含区内网络、服务转发、
实例准备及应用执行开销；不包含本地结果文件写入和 Stop 调用。
压测脚本经 Code Interpreter 在中国区内执行，管理工作站只负责部署、发起任务与下载结果。
没有向沙箱复制本地静态凭证。

## 延迟明细

单位均为毫秒。扩容阶段有意运行 5 秒工作负载，不能拿其延迟与空载冷/热请求直接比较。

| 阶段 | 成功 / 总数 | P50 | P95 | P99 | max |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(phase_table)}

冷请求是串行 100 个新 session，全部请求序号为 1，并获得
{latency['cold']['unique_instances']} 个不同的首次调用实例标记。
热请求是同一 session 的 500 次后续调用，实例标记保持不变，请求序号从 2 连续至 501。
有 {warm_over} 次热请求达到或超过 200 ms；验收对象为 P99，而不是最大值。

镜像构建、上传和 Runtime 的 CREATING → READY 等待单独发生在压测前，
没有混入首次 Invoke 的延迟。镜像规模、应用依赖、客户端位置和采样数量均会影响结果，
本次数据不代表任意业务 Agent 的性能，也不是长期 P99 保证。

## 0 → 50 并发扩容

scale 使用独立、从未调用过的 Runtime。测试前该 Runtime 没有本实验创建的用户 session。
50 个工作线程通过屏障同时发出不同 session 的首次调用，客户端发起时间跨度约 {skew:.3f} ms。
SDK 无自动重试，{tests['7.4']['samples'] - tests['7.4']['errors']}/50 首次调用成功。

返回 {latency['scale']['unique_instances']} 个不同的首次调用实例标记，业务请求序号均为 1。
客户端请求区间峰值重叠 **{latency['scale']['client_call_peak_overlap']}**，
应用处理区间峰值重叠 **{peak}**，证明这批请求确实并发处理；
应用区间计算依赖各 guest 时钟基本同步。

“0”指用户会话数，不是服务内部预热实例数。没有公开 API 让客户端清空预热池，
本次也没有取得底层物理实例计数，所以不将这项扩大为“确认从 0 个物理实例启动”。

## 首轮发现的实例识别问题

首轮目录 `results/20260921-runtime/` 保留完整原始数据。
651 次调用均成功，冷请求 P50 1,709.498 ms、P99 2,354.795 ms，
热请求 P99 157.937 ms，50 并发错误率 0%，处理区间峰值重叠 50。
但 100 个新会话只返回两组启动时 UUID / guest boot ID，且业务请求序号全部为 1。

这与预初始化状态被复用的现象一致，启动时随机 ID 和 guest boot ID
不能可靠标识恢复后的独立会话。首轮脚本因此将 7.1、7.2、7.4 的样本完整性判为失败；
这些失败是测量假设不成立，不能解释为服务请求失败。

最终探针在首次业务请求时生成并保留 `instance_id`，核验跨会话唯一、
同会话保持不变及请求计数连续。启动时字段仍保留：
本轮冷请求的启动 UUID 只有 {latency['cold']['unique_snapshot_process_ids']} 种，
首次请求实例标记却有 {latency['cold']['unique_instances']} 种。
该标记用于识别会话执行状态，不用于识别 AWS 物理宿主。
使用新镜像和新的 baseline / scale Runtime 完整复测，没有改变阈值、样本量、统计方法或计时范围，
也没有覆盖首轮的失败标记。

## 清理与复核

{cleanup_text}
首轮资源也已清理。两轮合计 302 个 Runtime 会话、2 个压测 Code Interpreter 会话，
4 个 Runtime、2 个自定义解释器、4 个 IAM 角色、2 个 ECR 仓库。
`StopRuntimeSession` 成功是 API 确认；本报告未将它描述为逐实例操作系统层的退出观测。
删除 Runtime 后再次查询得到不存在响应。

原账号已有的 Runtime 不属于本实验，未修改或删除。
结果文件保留资源标识、请求 ID、完整响应和清理响应；没有保存 AWS 密钥。

## 证据与复现

- [计划](PLAN.md)与[运行说明](README.md)。
- [最终汇总]({rel}/benchmark_summary.json)。
- [完整请求、响应与会话台账]({rel}/benchmark_results.json)。
- [逐请求 JSONL]({rel}/requests.jsonl)。
- [压测环境与执行身份]({rel}/benchmark_environment.json)。
- [镜像与 Runtime 配置]({rel}/resources.json)。
- [独立重算与完整性检查]({rel}/evidence_audit.json)。
- [清理响应]({rel}/cleanup.json)。
- [两轮资源独立清理复核](results/final_cleanup_audit.json)。
- [首轮原始汇总](results/20260921-runtime/benchmark_summary.json)。

在仓库根目录执行以下命令可从保存的数据重新计算并生成本报告：

```bash
python3 24-China-regions/runtime/analyze.py --output 24-China-regions/runtime/{rel}
```

协议背景参考：[AgentCore HTTP contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)。
中国区可用性和性能结论以本次真实调用为依据。
"""
    (ROOT / "REPORT.md").write_text(text)
    print(json.dumps({"tests": tests, "evidence_audit": checks}, indent=2))


if __name__ == "__main__":
    main()
