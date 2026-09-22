#!/usr/bin/env python3
"""Validate EC2 evidence and generate the shared and component reports, without AWS calls."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
CHINA = ROOT.parent
OUT = ROOT / "results/20260922"
DATA = OUT / "collected/results"
RT = CHINA / "runtime/results/20260922-ec2"
CI = CHINA / "code_interpreter/results/20260922-ec2"


def read(path):
    return json.loads(path.read_text())


def rank(values, p):
    return sorted(values)[math.ceil(len(values) * p / 100) - 1]


def linear(values, p):
    values = sorted(values)
    i = (len(values) - 1) * p / 100
    a, b = math.floor(i), math.ceil(i)
    return values[a] + (values[b] - values[a]) * (i - a)


def main():
    env = read(DATA / "ec2_environment.json")
    identity = env["instance_identity_document"]
    infra = read(OUT / "resources.json")
    cleanup = read(OUT / "final_cleanup_audit.json")
    assert cleanup["ec2_state"] == infra["instance"]["State"]["Name"] == "stopped"
    assert all(row["absent"] for row in cleanup["deleted_resources"])
    assert identity["region"] == "cn-northwest-1"
    assert identity["accountId"] == "447150580482"
    assert identity["instanceId"] == infra["instance"]["InstanceId"]
    assert identity["instanceType"] == "t3.small"
    assert identity["instanceId"] in env["sts"]["Arn"]
    assert env["cpu_count"] == 2
    for name, digest in read(OUT / "input_hashes.json").items():
        assert hashlib.sha256((DATA / "sources" / name).read_bytes()).hexdigest() == digest
    rt = read(RT / "benchmark_results.json")
    requests = rt["requests"]
    assert requests == [json.loads(line) for line in (RT / "requests.jsonl").read_text().splitlines()]
    assert len(requests) == 651
    assert len(rt["sessions"]) == 151
    assert all(s["stop_confirmed"] for s in rt["sessions"].values())
    runtime_tests = rt["summary"]["tests"]
    for phase, n in [("cold", 100), ("warm", 500), ("warmup", 1), ("scale", 50)]:
        rows = [r for r in requests if r["phase"] == phase]
        assert len(rows) == n
        good = [r for r in rows if r["status"] == "PASS"]
        assert len(good) == rt["summary"]["phases"][phase]["successes"]
        for p in (50, 99):
            assert math.isclose(rank([r["latency_ms"] for r in good], p),
                                rt["summary"]["phases"][phase]["latency"][f"p{p}_ms"])
        assert all(r["metadata"]["RetryAttempts"] == 0 for r in good)
        if phase in ("cold", "scale"):
            assert len({r["body"]["instance_id"] for r in good}) == n
            assert all(r["body"]["request_index"] == 1 for r in good)
    warm = sorted([r for r in requests if r["phase"] == "warm"], key=lambda r: r["index"])
    warmup = next(r for r in requests if r["phase"] == "warmup")
    assert all(r["body"]["instance_id"] == warmup["body"]["instance_id"]
               and r["body"]["request_index"] == i + 2 for i, r in enumerate(warm))
    for key, target in [("7.1", 3000), ("7.2", 5000), ("7.3", 200)]:
        row = runtime_tests[key]
        assert row["status"] == ("PASS" if row["sample_integrity"] and row["observed_ms"] < target else "FAIL")
    batches = read(CI / "concurrency.json")
    assert [b["concurrency"] for b in batches] == [1, 10, 50]
    all_ci_ids = set()
    for batch in batches:
        n = batch["concurrency"]
        assert len(batch["rows"]) == n
        good = [r for r in batch["rows"] if r["status"] == "PASS"]
        assert len(good) == batch["success"]
        assert batch["failure"] == n - len(good)
        for metric, field in [("start", "start_s"), ("end_to_end", "end_to_end_s")]:
            for percentile in (50, 90, 95):
                assert math.isclose(linear([r[field] for r in good], percentile),
                                    batch[metric][f"p{percentile}_s"])
        ids = {r["session_id"] for r in good}
        assert not all_ci_ids.intersection(ids)
        all_ci_ids.update(ids)
    serial = read(CI / "serial_summary.json")
    serial_rows = read(CI / "serial_rows.json")
    good_serial = [r for r in serial_rows if r["status"] == "PASS"]
    assert len(serial_rows) == 100
    assert serial["success"] == len(good_serial)
    for metric, field in [("start", "start_ms"), ("first_execute", "first_execute_ms"),
                           ("end_to_end", "end_to_end_ms")]:
        for percentile in (50, 99):
            assert math.isclose(rank([r[field] for r in good_serial], percentile),
                                serial[metric][f"p{percentile}_ms"])
    serial_ids = {r["session_id"] for r in good_serial}
    assert not serial_ids.intersection(all_ci_ids)
    all_ci_ids.update(serial_ids)
    ledger = read(CI / "sessions.json")
    assert len(ledger) == 161 and all(row["stopped"] for row in ledger.values())
    assert set(ledger) == all_ci_ids
    assert len(all_ci_ids) == 161
    for path in (CI / "api").glob("*.json"):
        response = read(path)
        assert response["response"]["ResponseMetadata"]["RetryAttempts"] == 0
    cpu = [json.loads(line) for line in (DATA / "cpu_samples.jsonl").read_text().splitlines()]
    measured = [x for x in cpu if "cpu_busy_percent" in x]
    cpu_report = {}
    for phase in sorted({r["phase"] for r in measured}):
        values = [r for r in measured if r["phase"] == phase]
        cpu_report[phase] = {"samples": len(values),
                            "mean_busy_percent": statistics.mean(r["cpu_busy_percent"] for r in values),
                            "p95_busy_percent": rank([r["cpu_busy_percent"] for r in values], 95),
                            "max_busy_percent": max(r["cpu_busy_percent"] for r in values),
                            "max_steal_percent": max(r["cpu_steal_percent"] for r in values)}
    audit = {"at": datetime.now(timezone.utc).isoformat(), "region_verified_by_imdsv2": identity["region"],
             "instance_id": identity["instanceId"], "source_hashes_match": True,
             "runtime_requests_verified": len(requests), "runtime_sessions_stopped": 151,
             "code_interpreter_sessions_stopped": 161, "percentiles_recomputed": True, "cpu": cpu_report}
    audit["image_comparison"] = read(OUT / "image_comparison.json")
    (OUT / "evidence_audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    old_rt = read(CHINA / "runtime/results/20260921-runtime-v2/benchmark_summary.json")
    old_ci = read(CHINA / "code_interpreter/results/main-20260921T155804Z/concurrency.json")
    rt_rows = []
    labels = {"7.1": "冷请求 P50，100 个样本", "7.2": "冷请求 P99，100 个样本",
              "7.3": "暖请求 P99，500 个样本"}
    for key, limit in [("7.1", "3,000"), ("7.2", "5,000"), ("7.3", "200")]:
        test = runtime_tests[key]
        rt_rows.append(f"| {key} {labels[key]} | < {limit} ms | {test['observed_ms']:.3f} ms | "
                       f"{old_rt['tests'][key]['observed_ms']:.3f} ms | **{test['status']}** |")
    scale = runtime_tests["7.4"]
    rt_rows.append(f"| 7.4 从 0 个用户会话到 50 并发 | 错误率 0% | {scale['errors']}/50 失败 | 0/50 失败 | **{scale['status']}** |")
    ci_rows = []
    for b, old in zip(batches, old_ci):
        ci_rows.append(f"| {b['concurrency']} | {b['success']}/{b['concurrency']} | "
                       f"{b['start']['p50_s']:.3f} | {b['start']['p95_s']:.3f} | "
                       f"{b['end_to_end']['p50_s']:.3f} | {b['end_to_end']['p95_s']:.3f} | "
                       f"{b['end_to_end']['max_s']:.3f} | {old['end_to_end']['p95_s']:.3f} |")
    serial_table = []
    for key, label in [("start", "创建会话"), ("first_execute", "首次执行"),
                       ("end_to_end", "创建到首次执行完成")]:
        s = serial[key]
        serial_table.append(f"| {label} | {s['p50_ms']:.3f} | {s['p95_ms']:.3f} | "
                            f"{s['p99_ms']:.3f} | {s['max_ms']:.3f} |")
    cpu_rows = [f"| {phase} | {v['samples']} | {v['mean_busy_percent']:.2f}% | "
                f"{v['p95_busy_percent']:.2f}% | {v['max_busy_percent']:.2f}% | {v['max_steal_percent']:.2f}% |"
                for phase, v in cpu_report.items()]
    stopped = infra["instance"]["State"]["Name"]
    retained = infra.get("retained", {})
    runtime_section = f"""## Runtime：相同 100 / 500 / 50 测试

| 验证项 | 目标 | 本轮宁夏 EC2 | 上轮宁夏 Code Interpreter 客户端 | 结果 |
| --- | --- | --- | --- | --- |
{chr(10).join(rt_rows)}

651 次请求包括 100 次串行新会话请求、1 次暖会话准备请求、500 次暖请求、50 次扩容请求。
冷请求的首次调用标记各不相同，业务序号均为 1；暖请求维持同一标记，序号连续。
50 并发的客户端区间峰值重叠 {scale['client_call_peak_overlap']}，
应用处理区间峰值重叠 {scale['handler_peak_overlap']}；
扩容阶段沿用 5 秒处理停留，仅检查错误率和重叠，不混入空载冷/暖时延。

Runtime 应用和基础镜像 digest 沿用上轮；新镜像与资源标识记录在本轮 resources.json。
独立比较确认根文件系统 layer 列表、容器 Config 和 app 源码均相同；
镜像 manifest digest 有变化，因此原始 digest 也一并记录，不把它们写成同一个 digest。
分位数为 nearest-rank，目标严格小于。
"""
    ci_section = f"""## Code Interpreter：相同 1 / 10 / 50 并发

下表单位为秒。每档使用新会话，全部首次执行完成后才统一停止该批会话。
统计沿用原脚本的线性插值，1 样本档位的分位数只代表一个观测值。

| 并发 | 成功 | Start P50 | Start P95 | 端到端 P50 | 端到端 P95 | 端到端 max | 上轮 us-west-2 客户端端到端 P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(ci_rows)}

### 补充：100 个串行新会话

成功 {serial['success']}/100，失败 {serial['failure']}，唯一 session 数 {serial['unique_sessions']}。
此表单位为毫秒，使用 nearest-rank。

| 指标 | P50 | P95 | P99 | max |
| --- | --- | --- | --- | --- |
{chr(10).join(serial_table)}

Code Interpreter 的端到端包含 StartSession 和首次 Invoke 两次调用；
Runtime 在首次 Invoke 内完成用户会话分配，不能把两者数值直接当作完全相同 API 的性能比较。
"""
    report = f"""# 宁夏 EC2 同区域冷启动与并发复测

日期：2026-09-22。两组件均直接从宁夏区 EC2 发起调用，
本地 us-west-2 工作站只负责 SSM 调度与结果下载，不在计时链路内。

## 实例与实测环境

| 项目 | 实测值 |
| --- | --- |
| AWS 账号 / profile | 447150580482 / agentcore_cn |
| EC2 | `{identity['instanceId']}` |
| 区域 / 可用区 | `{identity['region']}` / `{identity['availabilityZone']}` |
| 实例规格 | `{identity['instanceType']}`，2 vCPU / 2 GiB，x86_64 |
| AMI | `{identity['imageId']}` |
| Python / boto3 / botocore | {env['python']} / {env['boto3']} / {env['botocore']} |
| 目标 endpoint | `{env['target_endpoint']}` |
| 最终实例状态 | **{stopped}** |

区域与实例 ID 由实例内 IMDSv2 Identity Document 验证，并与 EC2 DescribeInstances、
STS 实例角色身份交叉检查。安全组没有入站规则，通过 SSM 执行；
API 使用实例角色，没有复制本地静态密钥。两组件串行测试，SDK 自动重试均关闭。

{runtime_section}
{ci_section}
## CPU 与测量边界

每秒读取 `/proc/stat` 和内存信息，busy 包含非 idle/iowait CPU tick，
steal 单列。以下为两组件测试期间采样，不含安装 Python / SDK 的启动准备。

| 阶段 | 样本数 | busy 平均 | busy P95 | busy max | steal max |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(cpu_rows)}

实例使用 T3 unlimited，CPU 积分模式与 CloudWatch 指标另存证据；
单次高 CPU 峰值不自动等于持续瓶颈，低 CPU 也不代表没有任何客户端开销。
本轮最初有 4 个约一秒采样区间的 steal 超过 10%，最高 34.40%，
与前三个 Runtime 冷请求重叠；这些请求全部保留在分位数计算中，没有剔除。
因此虽然已排除跨区域客户端链路，也不能把测试描述为完全没有客户端调度噪声。
本轮消除了“客户端与被测区域不同”的跨区链路，仍包含同区域网络、SDK、平台与应用时间。

上一轮 Runtime 已经由宁夏区 Code Interpreter 客户端运行；
这次替换为独立 EC2。上一轮 Code Interpreter 则由 us-west-2 工作站运行，
这次才改为同区域 EC2。跨天、CPU 架构、SDK 版本、客户端配置等也不同，
不能把数值变化全部归因于网络。此次“冷”仍指新用户会话首次响应，
不能观察或强制清空平台内部预热池。

## 清理与后续使用

用户选择停止并保留 EC2。当前状态为 `{stopped}`，保留资源如下：

```json
{json.dumps(retained, ensure_ascii=False, indent=2)}
```

151 个 Runtime 会话、161 个 Code Interpreter 会话均取得成功 Stop 响应。
测试 Runtime、ECR 镜像/仓库、测试日志组和临时传输 S3 bucket 的删除结果见清理证据。
保留 EC2 的 EBS、角色、instance profile 和安全组；测试临时数据面授权已删除，
角色保留 SSM 托管权限。原有业务资源未修改。

重新启动实例：

```bash
aws ec2 start-instances --instance-ids {identity['instanceId']} \\
  --profile agentcore_cn --region cn-northwest-1
```

再次压测需要重新部署被测 Runtime 并更新临时调用权限。
EC2 上 `/opt/cn-agentcore-benchmark/` 保留代码和结果，避免重跑时覆盖本轮目录。

## 证据与代码

- [执行计划](PLAN.md)、[运行说明](README.md)。
- [EC2 身份文档和客户端环境](results/20260922/collected/results/ec2_environment.json)。
- [EC2 配置与保留资源](results/20260922/resources.json)。
- [Runtime 原始结果](../runtime/results/20260922-ec2/benchmark_results.json)。
- [Code Interpreter 并发明细](../code_interpreter/results/20260922-ec2/concurrency.json)。
- [Code Interpreter 串行明细](../code_interpreter/results/20260922-ec2/serial_rows.json)。
- [CPU 原始采样](results/20260922/collected/results/cpu_samples.jsonl)。
- [CPU 积分模式与 CloudWatch 数据](results/20260922/ec2_cpu_metrics.json)。
- [短时 CPU steal 与请求重叠](results/20260922/cpu_spike_analysis.json)。
- [镜像文件系统与配置对照](results/20260922/image_comparison.json)。
- [独立重算与完整性审计](results/20260922/evidence_audit.json)。
- [停止与删除资源的独立复核](results/20260922/final_cleanup_audit.json)。

准备阶段首次安装因默认 Python 3.9 不满足固定 SDK 要求而失败，没有产生测量样本；
改用 Python 3.12 后执行正式测试。SSM 首次失败及后续执行响应均保留。
"""
    (ROOT / "REPORT.md").write_text(report)
    (CHINA / "runtime/REPORT.ec2.md").write_text(
        "# Runtime 宁夏 EC2 复测\n\n" + runtime_section +
        "\n实例、CPU、测量边界与清理详见[完整报告](../ec2_benchmark/REPORT.md)。\n"
        "\n[本轮原始结果](results/20260922-ec2/benchmark_results.json)。\n")
    (CHINA / "code_interpreter/REPORT.ec2.md").write_text(
        "# Code Interpreter 宁夏 EC2 复测\n\n" + ci_section +
        "\n实例、CPU、测量边界与清理详见[完整报告](../ec2_benchmark/REPORT.md)。\n"
        "\n[并发原始结果](results/20260922-ec2/concurrency.json)，"
        "[串行原始结果](results/20260922-ec2/serial_rows.json)。\n")
    print(json.dumps({"runtime": runtime_tests, "ci_serial": serial, "cpu": cpu_report}, indent=2))


if __name__ == "__main__":
    main()
