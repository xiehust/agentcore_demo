"""
拉取 AgentCore Runtime 的 USAGE_LOGS, 与压测客户端观测做三方比对

三方数据源:
  A. 平台计费口径  USAGE_LOGS (session 级 1 秒粒度)
       event_timestamp, session.id, elapsed_time_seconds,
       agent.runtime.vcpu.hours.used, agent.runtime.memory.gb_hours.used
  B. 容器自述      test_ws_coldstart_v2.py 采到的 memory_samples.jsonl
       microVM /proc/meminfo MemTotal / MemTotal-MemAvailable, /proc/stat 差分 vCPU
  C. 账号聚合      CloudWatch 顶层指标 CPUUsed-vCPUHours / MemoryUsed-GBHours (1 分钟)

核心要回答的问题:
  1. gb_hours.used 跟的是 microVM 的**分配值**(MemTotal) 还是**实际用量**(MemTotal-MemAvailable)?
  2. vcpu.hours.used 跟的是分配的核数还是实际 CPU 占用?
  3. elapsed_time_seconds 是否从 session 创建(含冷启动等待)开始计时 —— 冷启动的
     十几秒到底算不算钱?
  4. 字段是累计值还是每条增量?

注意: 文档明确 "Resource usage data may be delayed by up to 60 minutes"。
压测结束后立刻跑通常查不到数据, 用 --wait-min 让脚本轮询等待。

用法:
  python collect_usage_logs.py --run-dir results/ws_coldstart_v2_xxx
  python collect_usage_logs.py --run-dir <dir> --wait-min 60          # 轮询等数据
  python collect_usage_logs.py --run-dir <dir> --log-group /my/usage  # 手动指定
  python collect_usage_logs.py --discover                             # 只找投递配置
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config

USAGE_FIELDS = ("elapsed_time_seconds",
                "agent.runtime.vcpu.hours.used",
                "agent.runtime.memory.gb_hours.used")


# ------------------------------------------------------------------ 发现日志位置
def discover(logs) -> dict:
    """找出 USAGE_LOGS 的 vended-log 投递配置和候选 CloudWatch 日志组"""
    out = {"deliveries": [], "candidate_log_groups": [], "usage_log_groups": []}
    try:
        srcs = {s["name"]: s for s in logs.describe_delivery_sources().get("deliverySources", [])}
        dests = {d["name"]: d for d in logs.describe_delivery_destinations().get("deliveryDestinations", [])}
        for d in logs.describe_deliveries().get("deliveries", []):
            src = srcs.get(d.get("deliverySourceName"), {})
            dst = dests.get(d.get("deliveryDestinationName"), {})
            item = {
                "source": d.get("deliverySourceName"),
                "log_type": src.get("logType"),
                "resource_arns": src.get("resourceArns"),
                "destination_type": d.get("deliveryDestinationType") or dst.get("deliveryDestinationType"),
                "destination_arn": d.get("deliveryDestinationArn") or dst.get("arn"),
            }
            out["deliveries"].append(item)
            if (item["log_type"] or "").upper() == "USAGE_LOGS" and \
               (item["destination_type"] or "").upper() in ("CWL", "CLOUDWATCH_LOGS"):
                arn = item["destination_arn"] or ""
                if ":log-group:" in arn:
                    out["usage_log_groups"].append(arn.split(":log-group:")[1].split(":")[0])
                else:
                    # destination_arn 指向 delivery-destination, 再解一层拿真正的日志组
                    try:
                        dd = logs.get_delivery_destination(
                            name=d.get("deliveryDestinationName")
                                 or arn.rsplit(":", 1)[-1])["deliveryDestination"]
                        tgt = (dd.get("deliveryDestinationConfiguration") or {}
                               ).get("destinationResourceArn", "")
                        item["target_log_group_arn"] = tgt
                        if ":log-group:" in tgt:
                            out["usage_log_groups"].append(
                                tgt.split(":log-group:")[1].split(":")[0])
                    except Exception as e:
                        item["resolve_error"] = str(e)[:120]
    except Exception as e:
        out["delivery_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    try:
        p = logs.get_paginator("describe_log_groups")
        for page in p.paginate(logGroupNamePattern="agentcore"):
            for g in page.get("logGroups", []):
                out["candidate_log_groups"].append(g["logGroupName"])
    except Exception as e:
        out["scan_error"] = str(e)[:160]
    return out


# ------------------------------------------------------------------ 启用 USAGE_LOGS
def enable_usage_logs(region, runtime_arn, log_group, retention_days=30) -> dict:
    """为指定 runtime 开启 USAGE_LOGS 并投递到 CloudWatch 日志组。

    ⚠️ 这是**写操作**, 会在账号里创建 delivery source / destination / delivery
    以及日志组。只在显式传 --enable-usage-logs 时执行。
    压测前必须先做这一步, 否则 USAGE_LOGS 根本不产生, 事后无法回溯。
    """
    logs = boto3.client("logs", region_name=region)
    acct = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    lg_arn = f"arn:aws:logs:{region}:{acct}:log-group:{log_group}:*"
    out = {"log_group": log_group, "runtime_arn": runtime_arn}

    try:
        logs.create_log_group(logGroupName=log_group)
        out["log_group_created"] = True
    except logs.exceptions.ResourceAlreadyExistsException:
        out["log_group_created"] = False
    try:
        logs.put_retention_policy(logGroupName=log_group, retentionInDays=retention_days)
    except Exception as e:
        out["retention_error"] = str(e)[:120]

    rid = runtime_arn.split("/")[-1][:40]
    src_name = f"agentcore-usage-{rid}"
    dst_name = f"agentcore-usage-dst-{rid}"

    out["source"] = logs.put_delivery_source(
        name=src_name, resourceArn=runtime_arn, logType="USAGE_LOGS")["deliverySource"]
    dst = logs.put_delivery_destination(
        name=dst_name, outputFormat="json", deliveryDestinationType="CWL",
        deliveryDestinationConfiguration={"destinationResourceArn": lg_arn},
    )["deliveryDestination"]
    out["destination"] = dst
    try:
        out["delivery"] = logs.create_delivery(
            deliverySourceName=src_name,
            deliveryDestinationArn=dst["arn"])["delivery"]
    except logs.exceptions.ConflictException:
        out["delivery"] = "already exists"
    return out


# ------------------------------------------------------------------ Insights 查询
def query_usage_logs(logs, log_groups, prefix, t0, t1, wait_min, poll_s=60) -> list:
    """按 session.id 前缀捞 USAGE_LOGS。直接取 @message 自行解析 JSON,
    避免对带点号字段名的 Insights 转义方式做假设。"""
    q = (f'fields @timestamp, @message\n'
         f'| filter @message like /{prefix}/\n'
         f'| sort @timestamp asc\n'
         f'| limit 10000')
    deadline = time.time() + wait_min * 60
    attempt = 0
    while True:
        attempt += 1
        rows = _run_insights(logs, log_groups, q, t0, t1)
        if rows:
            print(f"  ✅ 第 {attempt} 次查询命中 {len(rows)} 条")
            return rows
        if time.time() >= deadline:
            print(f"  ❌ 等待 {wait_min} 分钟仍无数据。可能原因: "
                  f"(a) USAGE_LOGS 未启用; (b) 投递到了 S3/Firehose 而非 CWL; "
                  f"(c) 平台延迟仍未到(文档称最长 60 分钟)")
            return []
        left = int(deadline - time.time())
        print(f"  … 暂无数据, {poll_s}s 后重试 (剩余等待 {left//60}m{left%60}s)")
        time.sleep(min(poll_s, max(5, left)))


def _run_insights(logs, log_groups, q, t0, t1) -> list:
    try:
        r = logs.start_query(logGroupNames=log_groups,
                             startTime=int(t0.timestamp()),
                             endTime=int(t1.timestamp()),
                             queryString=q, limit=10000)
    except Exception as e:
        print(f"  start_query 失败: {type(e).__name__}: {str(e)[:200]}")
        return []
    qid = r["queryId"]
    while True:
        res = logs.get_query_results(queryId=qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    if res["status"] != "Complete":
        print(f"  查询状态 {res['status']}")
        return []
    out = []
    for row in res.get("results", []):
        d = {f["field"]: f["value"] for f in row}
        msg = d.get("@message")
        if not msg:
            continue
        try:
            out.append(flatten(json.loads(msg)))
        except Exception:
            out.append({"_unparsed": msg, "@timestamp": d.get("@timestamp")})
    return out


# ------------------------------------------------------------------ 归并分析
def flatten(rec: dict) -> dict:
    """USAGE_LOGS 实际是嵌套结构(文档写的是平铺, 与实测不符), 这里拉平:
         {resource_arn, event_timestamp,
          resource:{service.name, cloud.region, ...},
          attributes:{session.id, time_elapsed_seconds, agent.name, ...},
          metrics:{agent.runtime.vcpu.hours.used, agent.runtime.memory.gb_hours.used}}
    """
    out = {}
    for k, v in rec.items():
        if k in ("resource", "attributes", "metrics") and isinstance(v, dict):
            out.update(v)
        else:
            out[k] = v
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _elapsed(rec):
    """文档写 elapsed_time_seconds, 实测字段名是 time_elapsed_seconds; 两个都接"""
    for k in ("time_elapsed_seconds", "elapsed_time_seconds"):
        v = _num(rec.get(k))
        if v is not None:
            return v
    return None


def _sid(rec):
    for k in ("session.id", "session_id"):
        if rec.get(k):
            return rec[k]
    return None


def _ts(rec):
    for k in ("event_timestamp", "@timestamp", "timestamp"):
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return v / 1000.0 if v > 1e11 else float(v)
        try:                                     # ISO 字符串
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return None


def analyze_session(recs, client_rec, client_samples=None) -> dict:
    """解读单 session 的 USAGE_LOGS 序列, 并与客户端逐秒采样按时间轴对齐比对。

    实测 schema: 每条记录代表一个 ~1 秒区间, time_elapsed_seconds=1.0,
    metrics 是该区间内的增量(X-hours)。因此:
        该区间的瞬时用量 = 增量 * 3600 / time_elapsed_seconds
    """
    recs = [r for r in recs if _ts(r) is not None]
    recs.sort(key=_ts)
    ts = [_ts(r) for r in recs]
    el = [_elapsed(r) for r in recs]
    vc = [_num(r.get("agent.runtime.vcpu.hours.used")) for r in recs]
    gb = [_num(r.get("agent.runtime.memory.gb_hours.used")) for r in recs]

    a = {"session_id": _sid(recs[0]) if recs else None,
         "records": len(recs),
         "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
         "wall_span_s": round(ts[-1] - ts[0], 3) if len(ts) > 1 else 0.0}

    # 采样粒度
    if len(ts) > 1:
        gaps = [round(b - x, 3) for x, b in zip(ts, ts[1:])]
        a["ts_gap_s"] = {"median": statistics.median(gaps), "min": min(gaps), "max": max(gaps)}
        a["granularity_is_1s"] = abs(statistics.median(gaps) - 1.0) < 0.25

    # 累计还是增量: time_elapsed_seconds 恒为 ~1 => 每条是一个 1 秒区间的增量
    el_ok = [x for x in el if x is not None]
    a["time_elapsed_seconds_median"] = statistics.median(el_ok) if el_ok else None
    cumulative = bool(len(el_ok) > 2 and el_ok[-1] > el_ok[0] * 1.5)
    a["interpretation"] = "cumulative" if cumulative else "per_interval"

    if cumulative:
        a["vcpu_hours_total"] = round(vc[-1], 8) if vc and vc[-1] is not None else None
        a["memory_gb_hours_total"] = round(gb[-1], 8) if gb and gb[-1] is not None else None
        a["billed_seconds"] = el_ok[-1] if el_ok else None
    else:
        a["vcpu_hours_total"] = round(sum(x for x in vc if x is not None), 8)
        a["memory_gb_hours_total"] = round(sum(x for x in gb if x is not None), 8)
        a["billed_seconds"] = round(sum(el_ok), 3) if el_ok else a["wall_span_s"]

    # ---- 逐条反推瞬时用量, 形成时间序列 ----
    series = []
    for r, t, e, v, g in zip(recs, ts, el, vc, gb):
        iv = e or 1.0
        series.append({"ts": t,
                       "vcpu": round(v * 3600 / iv, 4) if v is not None else None,
                       "gb": round(g * 3600 / iv, 4) if g is not None else None})
    a["implied_series_len"] = len(series)
    gser = [x["gb"] for x in series if x["gb"] is not None]
    vser = [x["vcpu"] for x in series if x["vcpu"] is not None]
    if gser:
        a["implied_memory_gb"] = round(statistics.median(gser), 4)
        a["implied_memory_gb_peak"] = round(max(gser), 4)
        a["implied_memory_gb_min"] = round(min(gser), 4)
    if vser:
        a["implied_vcpu"] = round(statistics.median(vser), 4)
        a["implied_vcpu_peak"] = round(max(vser), 4)
        a["implied_vcpu_min"] = round(min(vser), 4)

    if not client_rec:
        return a

    # ---- 与容器自述比对 ----
    alloc_gb = (client_rec.get("vm_mem_total_mb") or 0) / 1024 or None
    avg_gb = (client_rec.get("vm_mem_used_mb_avg") or 0) / 1024 or None
    peak_gb = (client_rec.get("vm_mem_used_mb_peak") or 0) / 1024 or None
    a["client_vm_mem_total_gb"] = round(alloc_gb, 3) if alloc_gb else None
    a["client_vm_mem_used_gb_avg"] = round(avg_gb, 3) if avg_gb else None
    a["client_vm_mem_used_gb_peak"] = round(peak_gb, 3) if peak_gb else None
    a["client_vcpu_count"] = client_rec.get("vm_cpu_count")
    a["client_cgroup_vcpu_limit"] = client_rec.get("cgroup_vcpu_limit")
    a["client_vcpu_avg_observed"] = client_rec.get("vcpu_avg_observed")
    a["client_e2e_ms"] = client_rec.get("e2e_ms")
    a["client_fresh_boot"] = client_rec.get("fresh_boot")
    a["client_spin_cpu_busy_delta_s"] = client_rec.get("spin_cpu_busy_delta_s")

    im = a.get("implied_memory_gb")
    if im:
        cands = {"分配值 MemTotal": alloc_gb, "峰值用量": peak_gb, "平均用量": avg_gb}
        cands = {k: v for k, v in cands.items() if v}
        if cands:
            a["memory_basis_verdict"] = min(cands, key=lambda k: abs(im - cands[k]))
            a["memory_basis_gap_gb"] = {k: round(abs(im - v), 4) for k, v in cands.items()}
    iv = a.get("implied_vcpu")
    if iv:
        cands = {"microVM 核数 nproc": client_rec.get("vm_cpu_count"),
                 "容器 cgroup 配额": client_rec.get("cgroup_vcpu_limit"),
                 "实际 CPU 占用": client_rec.get("vcpu_avg_observed")}
        cands = {k: v for k, v in cands.items() if isinstance(v, (int, float)) and v}
        if cands:
            a["vcpu_basis_verdict"] = min(cands, key=lambda k: abs(iv - cands[k]))
            a["vcpu_basis_gap"] = {k: round(abs(iv - v), 4) for k, v in cands.items()}

    # 计费秒 vs 客户端观测: 冷启动等待/空闲是否也在计费
    if a.get("billed_seconds") and client_rec.get("sample_window_s"):
        a["billed_minus_sample_window_s"] = round(
            a["billed_seconds"] - client_rec["sample_window_s"], 2)
    if a.get("billed_seconds") and client_rec.get("e2e_ms"):
        a["billed_minus_client_e2e_s"] = round(
            a["billed_seconds"] - client_rec["e2e_ms"] / 1000, 3)

    # ---- 按 phase 对齐: 尖峰-释放形态下平台是否跟着降? ----
    if client_samples:
        cs = sorted(client_samples, key=lambda s: s["ts"])
        by_phase = {}
        for x in series:
            near = min(cs, key=lambda s: abs(s["ts"] - x["ts"]))
            if abs(near["ts"] - x["ts"]) > 3:      # 时间对不上就不归类
                continue
            ph = near.get("_phase", "?")
            by_phase.setdefault(ph, {"platform_gb": [], "client_gb": [],
                                     "platform_vcpu": []})
            if x["gb"] is not None:
                by_phase[ph]["platform_gb"].append(x["gb"])
            if x["vcpu"] is not None:
                by_phase[ph]["platform_vcpu"].append(x["vcpu"])
            if near.get("vm_mem_used_mb"):
                by_phase[ph]["client_gb"].append(near["vm_mem_used_mb"] / 1024)
        a["by_phase"] = {
            ph: {"n": len(d["platform_gb"]),
                 "platform_gb_mean": round(statistics.mean(d["platform_gb"]), 4) if d["platform_gb"] else None,
                 "client_gb_mean": round(statistics.mean(d["client_gb"]), 4) if d["client_gb"] else None,
                 "platform_vcpu_mean": round(statistics.mean(d["platform_vcpu"]), 4) if d["platform_vcpu"] else None}
            for ph, d in by_phase.items()}
    return a



# ------------------------------------------------------------------ 账号级指标
def fetch_vended_metrics(region, runtime_arn, t0, t1) -> dict:
    """CPUUsed-vCPUHours / MemoryUsed-GBHours (1 分钟分辨率), 作为第三方交叉验证"""
    cw = boto3.client("cloudwatch", region_name=region)
    out = {"namespaces_tried": [], "series": {}}
    candidates = ["AWS/Bedrock-AgentCore", "AWS/BedrockAgentCore", "AWS/Bedrock-AgentCore-Runtime"]
    found_ns = None
    for ns in candidates:
        out["namespaces_tried"].append(ns)
        try:
            ms = cw.list_metrics(Namespace=ns, MetricName="MemoryUsed-GBHours").get("Metrics", [])
            if ms:
                found_ns = ns
                out["dimension_sets_seen"] = [
                    sorted(d["Name"] for d in m.get("Dimensions", [])) for m in ms[:10]]
                break
        except Exception as e:
            out.setdefault("errors", []).append(f"{ns}: {str(e)[:120]}")
    if not found_ns:
        out["note"] = ("未在候选 namespace 找到 MemoryUsed-GBHours。用 "
                       "`aws cloudwatch list-metrics --namespace <ns>` 确认真实 namespace, "
                       "或直接看 CloudWatch AgentCore Observability 控制台 Runtime 页。")
        return out
    out["namespace"] = found_ns
    for mn in ("CPUUsed-vCPUHours", "MemoryUsed-GBHours"):
        try:
            r = cw.get_metric_data(
                MetricDataQueries=[{
                    "Id": "m1",
                    "MetricStat": {
                        "Metric": {"Namespace": found_ns, "MetricName": mn,
                                   "Dimensions": [{"Name": "Service", "Value": "AgentCore.Runtime"},
                                                  {"Name": "Resource", "Value": runtime_arn}]},
                        "Period": 60, "Stat": "Sum"},
                    "ReturnData": True}],
                StartTime=t0, EndTime=t1, ScanBy="TimestampAscending")
            res = r["MetricDataResults"][0]
            out["series"][mn] = {
                "points": len(res.get("Values", [])),
                "sum": round(sum(res.get("Values", [])), 8),
                "values": [round(v, 8) for v in res.get("Values", [])[:120]],
                "timestamps": [t.isoformat() for t in res.get("Timestamps", [])[:120]],
            }
        except Exception as e:
            out["series"][mn] = {"error": str(e)[:200]}
    return out


# ------------------------------------------------------------------ 输出
def print_report(rows, agg):
    print(f"\n{'='*118}")
    print("USAGE_LOGS × 容器自述 逐 session 比对")
    print(f"{'='*118}")
    print(f"{'session(尾8)':<14}{'条数':>5}{'粒度1s':>8}{'计费秒':>9}"
          f"{'vCPU-h':>11}{'GB-h':>11}{'反推vCPU':>10}{'反推GB':>9}"
          f"{'容器GB(分配/实用)':>20}{'内存计费基准':>16}")
    print("-" * 118)
    for a in rows:
        sid = (a.get("session_id") or "?")[-8:]
        g = "✓" if a.get("granularity_is_1s") else ("✗" if a.get("ts_gap_s") else "-")
        f = lambda v, n=3: f"{v:.{n}f}" if isinstance(v, (int, float)) else "N/A"
        alloc = a.get("client_vm_mem_total_gb")
        used = a.get("client_vm_mem_used_gb_avg")
        print(f"{sid:<14}{a['records']:>5}{g:>8}{f(a.get('billed_seconds'),1):>9}"
              f"{f(a.get('vcpu_hours_total'),6):>11}{f(a.get('memory_gb_hours_total'),6):>11}"
              f"{f(a.get('implied_vcpu'),2):>10}{f(a.get('implied_memory_gb'),2):>9}"
              f"{(f(alloc,2)+'/'+f(used,2)):>20}"
              f"{(a.get('memory_basis_verdict') or '-'):>16}")
    print("-" * 118)

    # 尖峰-释放形态: 平台是否跟着降? 这是区分峰值/平均计费最直接的证据
    ph_rows = [a for a in rows if a.get("by_phase") and len(a["by_phase"]) > 1]
    if ph_rows:
        print(f"\n{'='*118}")
        print("负载形态对齐 (phase peak = 高位撑住, released = 释放后)")
        print(f"{'='*118}")
        print(f"{'session(尾8)':<14}{'phase':<12}{'条数':>5}"
              f"{'平台反推GB':>13}{'容器自述GB':>13}{'平台反推vCPU':>15}")
        print("-" * 118)
        for a in ph_rows:
            for ph in ("peak", "released", "flat"):
                d = a["by_phase"].get(ph)
                if not d:
                    continue
                f = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "N/A"
                print(f"{(a.get('session_id') or '?')[-8:]:<14}{ph:<12}{d['n']:>5}"
                      f"{f(d['platform_gb_mean']):>13}{f(d['client_gb_mean']):>13}"
                      f"{f(d['platform_vcpu_mean']):>15}")
        print("-" * 118)
        print("平台反推GB 若在 released 阶段明显下降 => 按实际用量计费; 若不降 => 峰值/分配计费")

    print(f"\n{'='*118}")
    print("汇总结论")
    print(f"{'='*118}")
    for k, v in agg.items():
        print(f"  {k:<38} {v}")


def main():
    ap = argparse.ArgumentParser(description="拉取 USAGE_LOGS 并与压测观测比对")
    ap.add_argument("--run-dir", help="test_ws_coldstart_v2.py 的产出目录")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--log-group", action="append", default=[],
                    help="USAGE_LOGS 所在 CloudWatch 日志组, 可多次传; 不传则自动发现")
    ap.add_argument("--wait-min", type=int, default=0,
                    help="查不到数据时轮询等待的分钟数(平台延迟最长 60 分钟)")
    ap.add_argument("--window-pad-min", type=int, default=20,
                    help="查询时间窗在压测起止两端各扩多少分钟")
    ap.add_argument("--discover", action="store_true", help="只打印投递配置后退出")
    ap.add_argument("--enable-usage-logs", metavar="RUNTIME_ARN",
                    help="⚠️写操作: 为该 runtime 开启 USAGE_LOGS 投递到 CloudWatch(压测前必须先做)")
    ap.add_argument("--enable-log-group", default="/aws/vendedlogs/bedrock-agentcore/usage",
                    help="配合 --enable-usage-logs 使用的目标日志组")
    args = ap.parse_args()

    logs = boto3.client("logs", region_name=args.region,
                        config=Config(retries={"max_attempts": 3}))

    if args.enable_usage_logs:
        print(f"为 {args.enable_usage_logs}\n开启 USAGE_LOGS -> {args.enable_log_group}")
        r = enable_usage_logs(args.region, args.enable_usage_logs, args.enable_log_group)
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
        print("\n✅ 已配置。注意: 只对**此后**产生的 session 生效, 历史无法回溯。"
              "\n   随后压测, 再用 --log-group 指定该日志组做比对。")
        return

    if args.discover or not args.run_dir:
        d = discover(logs)
        print(json.dumps(d, indent=2, ensure_ascii=False))
        if not args.run_dir:
            print("\n(未传 --run-dir, 仅做发现)")
        return

    run_dir = Path(args.run_dir)
    sess = json.loads((run_dir / "sessions.json").read_text())
    prefix = sess["session_id_prefix"]
    t0 = datetime.fromisoformat(sess["started"]) - timedelta(minutes=args.window_pad_min)
    t1 = datetime.fromisoformat(sess["ended"]) + timedelta(minutes=args.window_pad_min)
    client_by_sid = {s["session_id"]: s for s in sess["sessions"] if s.get("session_id")}

    print("=" * 78)
    print(f"run_id={sess['run_id']}  session 前缀={prefix}")
    print(f"时间窗 {t0.isoformat()} ~ {t1.isoformat()}")
    print(f"客户端记录 {len(client_by_sid)} 个 session "
          f"(成功 {sum(1 for s in client_by_sid.values() if s.get('ok'))})")
    print("=" * 78)

    groups = args.log_group
    if not groups:
        d = discover(logs)
        groups = d.get("usage_log_groups") or d.get("candidate_log_groups") or []
        (run_dir / "log_discovery.json").write_text(json.dumps(d, indent=2, ensure_ascii=False))
        print(f"自动发现日志组: {groups or '（无）'}")
        if not groups:
            print("❌ 没找到日志组。先按 observability-configure 文档为该 agent 启用 "
                  "USAGE_LOGS 并投递到 CloudWatch, 或用 --log-group 手动指定。")
            sys.exit(2)
    groups = groups[:20]                      # Insights 单次最多 20 个日志组

    recs = query_usage_logs(logs, groups, prefix, t0, t1, args.wait_min)
    (run_dir / "usage_logs_raw.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    if not recs:
        sys.exit(1)

    # 按 session 分组
    by_sid = {}
    for r in recs:
        sid = _sid(r) or "<unknown>"
        by_sid.setdefault(sid, []).append(r)
    print(f"USAGE_LOGS 覆盖 {len(by_sid)} 个 session")

    # 客户端逐秒采样, 用于按时间轴对齐 phase
    samples_by_sid = {}
    sp = run_dir / "memory_samples.jsonl"
    if sp.exists():
        for line in sp.open():
            m = json.loads(line)
            samples_by_sid.setdefault(m.get("_session_id"), []).append(m)
        print(f"客户端采样 {sum(len(v) for v in samples_by_sid.values())} 条, "
              f"覆盖 {len(samples_by_sid)} 个 session")

    rows = []
    for k in sorted(by_sid):
        a = analyze_session(by_sid[k], client_by_sid.get(k), samples_by_sid.get(k))
        a.setdefault("session_id", k)
        rows.append(a)

    # 汇总
    def vals(k):
        return [a[k] for a in rows if isinstance(a.get(k), (int, float))]
    mem_verdicts = [a.get("memory_basis_verdict") for a in rows if a.get("memory_basis_verdict")]
    cpu_verdicts = [a.get("vcpu_basis_verdict") for a in rows if a.get("vcpu_basis_verdict")]
    gaps = vals("billed_minus_client_e2e_s")

    agg = {
        "USAGE_LOGS session 数": f"{len(by_sid)} / 客户端 {len(client_by_sid)}",
        "缺失的 session": len([s for s in client_by_sid if s not in by_sid]),
        "字段解读(累计 or 增量)": ", ".join(sorted({a["interpretation"] for a in rows})),
        "1 秒粒度符合率": f"{sum(1 for a in rows if a.get('granularity_is_1s'))}/{len(rows)}",
        "反推 vCPU 中位数": round(statistics.median(vals("implied_vcpu")), 3) if vals("implied_vcpu") else "N/A",
        "反推内存 GB 中位数": round(statistics.median(vals("implied_memory_gb")), 3) if vals("implied_memory_gb") else "N/A",
        "内存计费基准判定": max(set(mem_verdicts), key=mem_verdicts.count) if mem_verdicts else "N/A",
        "vCPU 计费基准判定": max(set(cpu_verdicts), key=cpu_verdicts.count) if cpu_verdicts else "N/A",
        "计费秒 - 客户端E2E 中位数": f"{statistics.median(gaps):.2f}s (>0 说明冷启动等待也在计费窗口内)" if gaps else "N/A",
        "vCPU-hours 合计": round(sum(vals("vcpu_hours_total")), 8),
        "GB-hours 合计": round(sum(vals("memory_gb_hours_total")), 8),
    }

    vended = fetch_vended_metrics(args.region, sess["runtime_arn"], t0, t1)
    if vended.get("series"):
        for mn, s in vended["series"].items():
            agg[f"CloudWatch {mn} 合计"] = s.get("sum", s.get("error"))

    print_report(rows, agg)
    (run_dir / "reconcile.json").write_text(json.dumps(
        {"run_id": sess["run_id"], "log_groups": groups, "aggregate": agg,
         "per_session": rows, "vended_metrics": vended},
        indent=2, ensure_ascii=False))
    print(f"\n产出: {run_dir/'usage_logs_raw.jsonl'}, {run_dir/'reconcile.json'}")


if __name__ == "__main__":
    main()
