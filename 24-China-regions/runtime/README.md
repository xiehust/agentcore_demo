# AgentCore Runtime 中国区性能验证

使用 `agentcore_cn`、`cn-northwest-1`、账号 `447150580482`，
验证最小 HTTP Runtime 的冷启动、热请求和 50 并发扩容。

验证计划见 [PLAN.md](PLAN.md)，实测结论见 [REPORT.md](REPORT.md)。

## 文件

| 文件 | 用途 |
| --- | --- |
| `app.py` / `Dockerfile` | 仅使用 Python 标准库的 ARM64 HTTP 服务，返回进程、guest boot ID 和请求序号 |
| `benchmark.py` | 中国区内运行的 100 次新会话、500 次热请求、50 并发客户端 |
| `lab.py` | 创建测试资源、启动压测、下载证据、删除本次资源 |
| `analyze.py` | 从逐请求数据独立重算、校验源代码哈希并生成 REPORT.md |
| `requirements.txt` | 本地管理脚本依赖 |
| `results/20260921-runtime-v2/` | 最终实测资源配置、逐请求数据、汇总和清理响应 |
| `results/20260921-runtime/` | 首轮数据；启动时实例标记被预初始化状态复用，已保留测量假设修正依据 |

## 复现

需要本地 Docker、ARM64 构建能力，以及可创建本次 ECR、IAM、Runtime、Code Interpreter
资源的中国区 AWS profile。默认账号有保护检查，其他账号需先修改相应账号配置。
部署和测试会产生使用费用；以下所有资源仅供此次实验。

```bash
cd 24-China-regions/runtime
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 使用新目录，避免混用已清理资源或覆盖原始记录。
.venv/bin/python lab.py deploy --output results/my-run
.venv/bin/python lab.py run --output results/my-run

# 异步压测通常需要数分钟；重复 collect 查看日志，直到 completed 或 failed。
.venv/bin/python lab.py collect --output results/my-run

# 确认 benchmark_results.json、runtime_sessions.json 等已下载后清理。
.venv/bin/python lab.py cleanup --output results/my-run
```

所有命令默认 `--profile agentcore_cn --region cn-northwest-1`。
`deploy` 的镜像构建和控制面等待不计入 Runtime 请求延迟。
`run` 启动异步任务；`collect` 的退出码不表示验收通过，请查看
`benchmark_summary.json` 中各项 `status`。远端 benchmark 任一验收失败会退出 1，
但已经完成的样本和错误仍会保存并下载。

主客户端运行在同区域的 PUBLIC Code Interpreter，会话绑定专用 IAM 角色，
权限仅覆盖本次两个 Runtime 的 Invoke / Stop。远端不接收本地 AWS profile 或静态密钥。
镜像推送使用临时 Docker auth 目录，推送后退出登录并删除该目录。

## 指标口径

- 冷请求：并发 1，100 个全新用户 session 各调用一次；
  验证首次业务请求生成的实例标记各不相同且请求序号为 1。
- 热请求：一个新 session 的首次准备请求不计入统计，随后连续 500 次；
  验证相同进程和连续请求序号。
- 扩容：从未调用的独立 Runtime，同时发出 50 个不同 session 的首次请求。
  每个处理函数停留 5 秒，以观察处理区间是否实际重叠。
- 延迟：调用端 `InvokeAgentRuntime` 发出到完整响应读取完毕，
  包含中国区网络和服务开销；另外记录响应头与应用处理耗时。
- P50/P99：nearest-rank，严格小于阈值才通过；SDK 不自动重试。
- “冷”与“从 0”指用户会话维度，不能通过客户端强制清除或观察服务内部预热池。
  此外，跨 guest 的处理区间重叠依赖各实例时钟基本同步。

不要用启动时 UUID 或 guest boot ID 直接计数实例：本次首轮观察到这些字段
在多个独立会话中重复，符合预初始化状态被复用的现象。
最终探针在首次业务请求时生成并保存 `instance_id`；首轮数据不覆盖、不修改。

Runtime 的 idle timeout 为 60 秒，max lifetime 为 1,800 秒。
测试代码主动停止所有 Runtime 会话；压测沙箱及自定义解释器、
测试 Runtime、镜像仓库、执行角色、测试日志组由 `cleanup` 删除。
只清理当前结果目录台账记录的资源。

若在资源创建成功、响应尚未落盘的极短窗口被强制终止，可用已保存的创建请求名称
核对资源并补齐台账。`cleanup` 要求远端任务已进入终态；未知任务状态时先执行 `collect`。
