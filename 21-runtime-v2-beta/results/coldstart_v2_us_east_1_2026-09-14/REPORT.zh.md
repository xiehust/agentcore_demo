# us-east-1 Runtime V2 冷启动测试（2026-09-14）

**500mb ping-pong 镜像的新 session 首调用：串行 p50 2.467 秒，并发 10 的合并 p50 2.661 秒；30 次正式请求全部成功，无限流。** 同 session 热调用 p50 约 0.20 秒。

## 配置与口径

- 时间：2026-09-14 05:52:33–06:00:23 UTC；区域 `us-east-1`，账号 `434444145045`。
- 创建明确指定 `platformVersion=V2`，GetAgentRuntime 回显 `READY` / `V2`。
- 复用 `agentcore-coldstart-pingpong:500mb`，按 digest 固定：`sha256:8fce75a892c741d4712f9c3bdd2d0a8f429e69bebdd0393ea96c82f461029d87`；ECR 压缩大小 401,204,053 bytes。
- 复用 `AgentCoreColdstartRole`，PUBLIC/HTTP；idle 60 秒、maxLifetime 600 秒。没有修改 IAM、镜像或配额。
- 一个临时 Runtime，先执行一次 smoke（不纳入正式统计），静置 180 秒；再执行 10 次串行新 session（间隔 5 秒），最后两轮并发 10（每轮前暂停 5 秒）。每个 session 首调用后立即热调用，再停止。
- 并发轮使用现有多进程客户端：8 个 spawn 进程、独立客户端、全局屏障。SDK 自动重试关闭。串行轮复用客户端连接；并发轮各自新建客户端，因此不能把两种负载的差值全归因于服务端并发。
- 计时覆盖 InvokeAgentRuntime 开始至完整响应体读取，包含客户端、网络与服务端。新 session 首调用不等于已确认的 microVM 冷启动。

## 结果

所有耗时单位为毫秒。p50 为中位数，p90 使用 inclusive 线性插值。样本少，不报告 p99 或性能 SLA。

| 负载 | 成功/尝试 | 首调用 p50 | p90 | 最大值 | 平均值 | 热调用 p50 |
|---|---:|---:|---:|---:|---:|---:|
| 串行，10 次 | 10/10 | 2467.2 | 3219.0 | 3930.0 | 2629.4 | 206.3 |
| 并发 10，第 1 轮 | 10/10 | 2727.1 | 3092.9 | 3374.5 | 2764.9 | 198.2 |
| 并发 10，第 2 轮 | 10/10 | 2591.0 | 2936.7 | 3524.8 | 2661.3 | 209.5 |
| 并发 10，两轮合并 | 20/20 | 2660.8 | 3092.9 | 3524.8 | 2713.1 | 204.4 |

- 两轮并发首调用的 SDK `before-send` 跨度分别为 6.502 ms、6.605 ms；这是传输前 hook，不是实际网络发包时刻。
- 部署至 Runtime 与 DEFAULT endpoint READY 共 186.03 秒，与上表首调用耗时分开计算。
- 单次 smoke 首调用 2755.8 ms，同 session 热调用 251.6 ms，未纳入上表。
- 30 个正式样本只包含 2 个不同的 `proc_start_ts`，请求时间减进程时间约 286.59–378.49 秒。该字段不能证明每个请求都执行了一次新的进程或 microVM 启动，也不能据此认定不同 session 共用同一隔离实例。因此这里只报告新 session 首调用端到端延迟。
- 本轮仅测 500mb，不覆盖 1gb/2gb、高并发或跨时段重复，也不是 us-west-2 的同期配对实验。

## 核验与资源清理

- 实测运行器退出码 0。含 smoke 共 31 个唯一 session；62 次调用均 HTTP 200 / pong，31 次 StopRuntimeSession 均 HTTP 200，无 SDK 重试。
- 临时 Runtime `coldstart_v2_east1_679e168f-84fYyV9rx4` 于 06:00:22 UTC 确认删除。随后独立再次调用 GetAgentRuntime，得到 `ResourceNotFoundException`。原有 Runtime 未修改，自动生成的日志未删除。
- `verify.py` 独立核验源文件哈希、区域与 V2 回显、镜像 digest、原始响应及耗时、同 session 冷热响应配对、请求唯一性、并发进程正常退出、统计值和删除记录，退出码 0。
- 运行前语法检查通过；`test_multiprocess_coldstart` 的 5 项离线测试全部通过。

证据位于本目录：`run_test.py`、`run.log`、`run.json`、`create_request.json`、`images.json`、`deployments.json`、`smoke.json`、`serial.json`、两轮 `c10_round*/`、`summary.json` 和 `cleanup.json`。历史测试脚本保持不变；`run_test.py` 拒绝覆盖已有证据。

离线复核命令（在项目目录执行，不调用 AWS）：

```bash
.venv/bin/python -B results/coldstart_v2_us_east_1_2026-09-14/verify.py
```
