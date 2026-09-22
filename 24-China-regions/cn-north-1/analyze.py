#!/usr/bin/env python3
"""Recompute Beijing statistics from downloaded evidence and write the report."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results/20260922"
DATA = OUT / "ec2/collected/results"


def read(path):
    return json.loads(path.read_text())


def rank(values, p):
    return sorted(values)[math.ceil(len(values) * p / 100) - 1]


def linear(values, p):
    values = sorted(values)
    pos = (len(values) - 1) * p / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def main():
    env = read(DATA / "ec2_environment.json")
    instance = env["instance_identity_document"]
    assert instance["region"] == "cn-north-1"
    infra = read(OUT / "ec2/resources.json")
    assert instance["instanceId"] == infra["instance"]["InstanceId"]
    assert instance["instanceId"] in env["sts"]["Arn"]
    source_hashes = read(OUT / "ec2/benchmark-source-hashes.json")
    for name, digest in source_hashes.items():
        assert hashlib.sha256((DATA / "sources" / name).read_bytes()).hexdigest() == digest
    rt = read(OUT / "runtime/benchmark_results.json")
    rows = rt["requests"]
    assert len(rows) == 651 and len(rt["sessions"]) == 151
    assert rows == [json.loads(line) for line in (OUT / "runtime/requests.jsonl").read_text().splitlines()]
    for phase, count in [("cold", 100), ("warmup", 1), ("warm", 500), ("scale", 50)]:
        selected = [r for r in rows if r["phase"] == phase]
        assert len(selected) == count
        success = [r for r in selected if r["status"] == "PASS"]
        for p in (50, 99):
            assert math.isclose(rank([r["latency_ms"] for r in success], p),
                                rt["summary"]["phases"][phase]["latency"][f"p{p}_ms"])
        assert all(r["metadata"]["RetryAttempts"] == 0 for r in success)
        if phase in ("cold", "scale"):
            assert len({r["body"]["instance_id"] for r in success}) == len(success)
            assert all(r["body"]["request_index"] == 1 for r in success)
    assert all(r["stop_confirmed"] for r in rt["sessions"].values())
    warmup = next(r for r in rows if r["phase"] == "warmup")
    warm_rows = sorted((r for r in rows if r["phase"] == "warm"), key=lambda r: r["index"])
    assert all(r["body"]["instance_id"] == warmup["body"]["instance_id"]
               and r["body"]["request_index"] == r["index"] + 2 for r in warm_rows)
    assert set(rt["sessions"]) == {r["session_id"] for r in rows}
    for key, phase, field, threshold in [("7.1","cold","p50_ms",3000),
                                        ("7.2","cold","p99_ms",5000),
                                        ("7.3","warm","p99_ms",200)]:
        actual = rt["summary"]["phases"][phase]
        expected = "PASS" if actual["failures"] == 0 and actual["latency"][field] < threshold else "FAIL"
        assert rt["summary"]["tests"][key]["status"] == expected
    warm_over = sum(r["latency_ms"] >= 200 for r in warm_rows)
    warm_margin = 200 - rt["summary"]["tests"]["7.3"]["observed_ms"]
    ci_dir = OUT / "code_interpreter"
    batches = read(ci_dir / "concurrency.json")
    serial = read(ci_dir / "serial_summary.json")
    serial_rows = read(ci_dir / "serial_rows.json")
    assert [b["concurrency"] for b in batches] == [1, 10, 50]
    for batch in batches:
        good = [r for r in batch["rows"] if r["status"] == "PASS"]
        assert len(good) == batch["success"]
        for p in (50, 95):
            assert math.isclose(linear([r["end_to_end_s"] for r in good], p),
                                batch["end_to_end"][f"p{p}_s"])
    assert len(serial_rows) == 100
    for p in (50, 99):
        assert math.isclose(rank([r["end_to_end_ms"] for r in serial_rows if r["status"] == "PASS"], p),
                            serial["end_to_end"][f"p{p}_ms"])
    default = read(ci_dir / "summary.json")
    public = read(OUT / "code_interpreter_public/summary.json")
    efs = read(OUT / "efs/result.json")
    assert efs["region"] == "cn-north-1" and len(set(efs["session_ids"])) == 3
    assert efs["session_c"]["sha256"] == efs["session_b"]["sha256"]
    assert hashlib.sha256(efs["session_c"]["content"].encode()).hexdigest() == efs["session_c"]["sha256"]
    session_counts = {}
    for name in ("code_interpreter", "code_interpreter_public"):
        sessions = read(OUT / name / "sessions.json")
        assert all(r["stopped"] for r in sessions.values())
        session_counts[name] = len(sessions)
    efs_sessions = read(OUT / "efs/resources.json")["sessions"]
    assert all(s["stopped"] for s in efs_sessions)
    session_counts["efs"] = len(efs_sessions)
    cpu = [json.loads(line) for line in (DATA / "cpu_samples.jsonl").read_text().splitlines()]
    cpu_by_phase = {}
    for phase in sorted({r["phase"] for r in cpu}):
        selected = [r for r in cpu if r["phase"] == phase and "cpu_busy_percent" in r]
        if selected:
            cpu_by_phase[phase] = {"n": len(selected),
                "mean_busy": statistics.mean(r["cpu_busy_percent"] for r in selected),
                "max_busy": max(r["cpu_busy_percent"] for r in selected),
                "max_steal": max(r["cpu_steal_percent"] for r in selected)}
    audit = {"at": datetime.now(timezone.utc).isoformat(), "region": instance["region"],
             "instance": instance["instanceId"], "source_hashes_verified": True,
             "runtime_rows": len(rows), "runtime_sessions": 151, "ci_sessions": session_counts,
             "quantiles_recomputed": True, "efs_hash_verified": True, "cpu": cpu_by_phase}
    (OUT / "evidence_audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    summary = {"runtime": rt["summary"]["tests"], "ci_performance": {
        "concurrency": [{k:v for k,v in b.items() if k != "rows"} for b in batches], "serial": serial},
        "ci_functional": {key: default[key] for key in ["2.1","2.2","2.3","2.4","2.5","2.6","2.9"]},
        "ci_dependencies": {"default": default["2.10"], "public": public["2.10"]}, "efs": efs["status"]}
    (OUT / "final_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    previous_rt = read(ROOT.parent / "runtime/results/20260922-ec2/benchmark_summary.json")
    previous_ci = read(ROOT.parent / "code_interpreter/results/20260922-ec2/concurrency.json")
    rt_lines = []
    for key, name in [("7.1","冷请求 P50"),("7.2","冷请求 P99"),("7.3","暖请求 P99")]:
        test = rt["summary"]["tests"][key]
        rt_lines.append(f"| {key} {name} | {test['observed_ms']:.3f} ms | "
                        f"< {test['strict_target_ms']} ms | {previous_rt['tests'][key]['observed_ms']:.3f} ms | {test['status']} |")
    scale = rt["summary"]["tests"]["7.4"]
    rt_lines.append(f"| 7.4 50 并发 | {scale['errors']}/50 失败 | 0% | 0/50 失败 | {scale['status']} |")
    ci_lines = []
    for b, prev in zip(batches, previous_ci):
        ci_lines.append(f"| {b['concurrency']} | {b['success']}/{b['concurrency']} | "
                        f"{b['start']['p50_s']:.3f} | {b['end_to_end']['p50_s']:.3f} | "
                        f"{b['end_to_end']['p95_s']:.3f} | {b['end_to_end']['max_s']:.3f} | "
                        f"{prev['end_to_end']['p95_s']:.3f} |")
    functional_names = {"2.1":"Python / 标准库","2.2":"客户端宿主与跨会话隔离探针",
        "2.3":"stdout / stderr / 异常透传至 SDK 工具入口","2.4":"pandas / numpy / matplotlib",
        "2.5":"文本与二进制上传下载","2.6":"执行超时","2.9":"多轮状态"}
    fn_lines = [f"| {key} | {label} | {default[key]['status']} |"
                for key,label in functional_names.items()]
    fn_lines += [f"| 2.10 | 默认沙箱拉包 | {default['2.10']['status']} |",
                 f"| 2.10 | PUBLIC 清华镜像拉包与运行 | {public['2.10']['status']} |",
                 f"| EFS | 挂载与三会话持久化 | {efs['status']} |"]
    cpu_lines = [f"| {p} | {v['n']} | {v['mean_busy']:.2f}% | {v['max_busy']:.2f}% | {v['max_steal']:.2f}% |"
                 for p,v in cpu_by_phase.items()]
    timeout = default["2.6"]["details"]
    clean_path = OUT / "cleanup_audit.json"
    cleanup = read(clean_path) if clean_path.exists() else {"status":"not_yet_audited"}
    text = f"""# 北京区 AgentCore 验证报告

日期：2026-09-22；账号 `447150580482`；区域 **cn-north-1**。
实际容器检查和全部服务验证均在北京 EC2 `{instance['instanceId']}` 上运行；
当前工作站只管理资源、传输既有基础镜像与测试代码、下载和分析结果。

## 环境与口径

| 项目 | 实测值 |
| --- | --- |
| EC2 区域 / AZ | {instance['region']} / {instance['availabilityZone']} |
| 规格 | {instance['instanceType']}，ARM64，2 vCPU / 2 GiB |
| Python / boto3 / botocore | {env['python']} / {env['boto3']} / {env['botocore']} |
| endpoint | {env['target_endpoint']} |
| EC2 状态 | {infra['instance']['State']['Name']} |

区域由 IMDSv2 身份文档与 STS 实例角色交叉验证；没有向 EC2 复制本地静态凭证。
无 SSH 入站，通过 SSM 运行。CPU 采样覆盖测试过程。
SDK 自动重试关闭；冷请求按全新用户会话首次完整响应计量。
该口径无法确认服务内部没有预热池，也无法强制从 0 个物理实例启动。

## Runtime

100 次串行新会话、1 次暖准备调用、500 次同会话暖调用、独立 Runtime 的 50 并发，
共 651 次请求。分位数为 nearest-rank，阈值严格小于。

| 验证项 | 北京实测 | 目标 | 宁夏 EC2 实测 | 结果 |
| --- | --- | --- | --- | --- |
{chr(10).join(rt_lines)}

暖请求 P99 距 200 ms 阈值仅 **{warm_margin:.3f} ms**，属于临界通过。
500 次暖请求中有 {warm_over} 次达到或超过 200 ms；本轮样本通过不代表长期 P99 保证。

50 并发返回 {scale['unique_instances']} 个首次调用实例标记，
客户端请求区间峰值重叠 {scale['client_call_peak_overlap']}，
应用处理区间峰值重叠 {scale['handler_peak_overlap']}。
该阶段保留原来的 5 秒处理停留，不与空载冷/暖请求延迟混算。

## Code Interpreter 冷启动

下表单位为秒，沿用 1/10/50 并发原脚本的线性插值分位数。
每批全部首次执行完成后才停止会话。

| 并发 | 成功 | Start P50 | 端到端 P50 | 端到端 P95 | max | 宁夏 EC2 端到端 P95 |
| --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(ci_lines)}

额外 100 次串行新会话：成功 {serial['success']}/100。
创建至首次执行完成的 P50 **{serial['end_to_end']['p50_ms']:.3f} ms**、
P99 **{serial['end_to_end']['p99_ms']:.3f} ms**（nearest-rank）。
其中创建会话 P50 {serial['start']['p50_ms']:.3f} ms，首次执行 P50
{serial['first_execute']['p50_ms']:.3f} ms。
Code Interpreter 端到端包含 Start + Invoke 两次调用，与 Runtime 的单次 Invoke 链路不同。

## Code Interpreter 功能与 EFS

| 编号 | 验证项 | 结果 |
| --- | --- | --- |
{chr(10).join(fn_lines)}

隔离项仅验证北京 EC2 客户端宿主标记和跨会话私有文件，不是 AWS 底层宿主逃逸审计。
stdout / stderr 验证到 Python SDK 工具入口，没有调用额外 LLM。

180 秒原生执行观察的耗时为 {timeout['native_elapsed_s']:.3f} 秒，
原生自动执行超时证据：`{timeout['native_timeout_proven']}`；
调用端期限 + stopTask 的终止证据：`{timeout['cancel_termination_proven']}`。
单次执行没有 SDK timeout 字段；不能把主动取消或会话过期替代原生执行超时验收。

依赖安装保留默认沙箱与 PUBLIC 两种配置的结果。PUBLIC 测试固定
`pytimeparse==1.1.8`、禁用缓存、独立安装目录，并执行导入后的真实函数。

EFS 使用北京区独立文件系统和 access point，VPC 挂载 `/mnt/efs`。
从北京 EC2 调用三个独立会话：A 写入后停止，B 读取并追加后停止，C 验证追加内容及 SHA-256。
最终哈希 `{efs['session_c']['sha256']}`。
用户原有 EFS / Runtime 不属于本次测试资源。

## CPU 与比较限制

| 阶段 | 样本 | busy 平均 | busy max | steal max |
| --- | --- | --- | --- | --- |
{chr(10).join(cpu_lines)}

宁夏客户端是 t3.small（x86_64），北京是 t4g.small（ARM64），两者都在被测区域内。
跨区域比较还包含客户端架构、服务状态和测试时间差异，不能把全部差异归因于地域。
所有异常值和失败都保留；没有为通过阈值而剔除样本。

北京 EC2 访问 Docker Hub 超时，使用经北京 S3 传输的固定基础镜像离线构建。
压缩包哈希、镜像 ID、RootFS、运行配置在 EC2 上验证，再原生运行容器检查并推送北京 ECR。
构建准备和服务 CREATING → READY 不计入冷启动延迟。
两次准备阶段失败（Docker Hub 超时、Docker inspect 可选字段差异）保留日志，
均发生在正式性能采样之前。

## 清理

Runtime 会话 151 个；Code Interpreter 会话：{json.dumps(session_counts,ensure_ascii=False)}，
全部取得成功 Stop 响应。
EC2 按已确认偏好停止并保留，保留其 EBS、SSM 角色/profile 和安全组；
临时被测资源及传输 bucket 的独立复核如下：

```json
{json.dumps(cleanup,ensure_ascii=False,indent=2)}
```

## 证据

- [执行计划](PLAN.md)与[运行方法](README.md)。
- [结构化最终结果](results/20260922/final_summary.json)。
- [实例身份和客户端环境](results/20260922/ec2/collected/results/ec2_environment.json)。
- [原始 Runtime 请求](results/20260922/runtime/benchmark_results.json)。
- [CI 并发结果](results/20260922/code_interpreter/concurrency.json)。
- [CI 功能结果](results/20260922/code_interpreter/summary.json)。
- [PUBLIC 包源结果](results/20260922/code_interpreter_public/summary.json)。
- [EFS 结果](results/20260922/efs/result.json)。
- [CPU 原始采样](results/20260922/ec2/collected/results/cpu_samples.jsonl)。
- [独立重算与完整性检查](results/20260922/evidence_audit.json)。
- [清理独立复核](results/20260922/cleanup_audit.json)。
- [EFS 网络清理重试状态](results/20260922/efs/cleanup-retry.json)。
"""
    (ROOT / "REPORT.md").write_text(text)
    print(json.dumps({"runtime":rt["summary"]["tests"],"ci_serial":serial["end_to_end"],
                      "functional":{k:v["status"] for k,v in default.items()},"public":public["2.10"]["status"],
                      "efs":efs["status"],"sessions":session_counts},indent=2))


if __name__ == "__main__":
    main()
