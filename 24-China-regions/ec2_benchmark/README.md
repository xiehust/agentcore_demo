# 宁夏 EC2 同区域性能复测

在账号 `447150580482` 的 `cn-northwest-1` 创建小型 CPU EC2，
直接从实例测量 AgentCore Runtime 与 Code Interpreter。
见 [计划](PLAN.md) 和 [测试报告](REPORT.md)。

## 文件与证据

| 文件 | 用途 |
| --- | --- |
| `ec2_lab.py` | 创建 EC2、SSM 运行、结果传输、停止并保留实例 |
| `run_benchmarks.py` | EC2 内执行两组件测试、IMDSv2 区域校验、CPU 采样和结果归档 |
| `analyze.py` | 从原始请求重算统计并生成总报告与两个组件报告 |
| `requirements.txt` | Python 3.12 环境使用的固定 SDK 版本 |
| `results/20260922/` | EC2 / IAM / 网络配置、SSM 响应、CPU 和身份文档等证据 |
| `../runtime/results/20260922-ec2/` | Runtime 部署、151 个会话、651 次请求和汇总 |
| `../code_interpreter/results/20260922-ec2/` | 1/10/50 并发及额外 100 次串行新会话测试 |

Runtime 直接复用 [benchmark.py](../runtime/benchmark.py) 和
[app.py](../runtime/app.py)，包括首次业务请求生成的实例标记。
Code Interpreter 并发直接复用 [verify_code_interpreter.py](../code_interpreter/verify_code_interpreter.py)。
原有 2026-09-21 结果保持不变。

## 执行

以下为本次执行顺序，所有本地命令使用 `agentcore_cn`，所有 AWS client 指定宁夏区。
从仓库根目录运行；创建资源会产生使用费用。

```bash
# 不再创建 Code Interpreter 作为 Runtime 的压测客户端。
python3 24-China-regions/runtime/lab.py deploy --runtime-only \
  --output 24-China-regions/runtime/results/20260922-ec2

python3 24-China-regions/ec2_benchmark/ec2_lab.py deploy
python3 24-China-regions/ec2_benchmark/ec2_lab.py run
python3 24-China-regions/ec2_benchmark/ec2_lab.py status

# final_status 显示完成后下载，再清理测试服务并停止 EC2。
python3 24-China-regions/ec2_benchmark/ec2_lab.py collect
python3 24-China-regions/runtime/lab.py cleanup \
  --output 24-China-regions/runtime/results/20260922-ec2
python3 24-China-regions/ec2_benchmark/ec2_lab.py stop
```

`run` 通过 SSM 在实例上安装 Python 3.12 并创建独立 venv。
Amazon Linux 默认 Python 3.9 不满足此处 boto3 版本的要求。
每轮使用新结果目录；脚本拒绝覆盖已产生请求样本的远端目录。
上面的固定日期路径用于记录本次执行，不可直接当作已清理资源的再次部署入口。

用户选择测试后停止并保留 EC2。保留其 EBS、实例角色、instance profile 和无入站规则的
安全组，以便以后通过 SSM 使用；删除测试专用数据面授权、Runtime、ECR、测试日志组和
私有传输 bucket。再次做性能测试需要新建测试资源并更新实例的临时调用权限，
不要复用已删除的 Runtime ARN。

## 口径

Runtime：100 个串行冷请求、一个会话的 500 次暖请求、从未调用的独立 Runtime 的 50 并发。
Code Interpreter：按原脚本执行 1/10/50 并发，另加 100 个串行新会话。
SDK 不自动重试。Runtime 及新增串行基线使用 nearest-rank，
Code Interpreter 原有并发统计保持线性插值；报告注明这一差别。

冷启动指新用户会话的首次完整响应，不能识别或强制清空服务内部预热池。
Code Interpreter 端到端包含 StartSession 和首次 Invoke 两次 API，
Runtime 则在首次 Invoke 中完成会话分配，两者并非完全相同的 API 链路。

上一轮 Runtime 的客户端本来就在宁夏区的 Code Interpreter 中；
上一轮 Code Interpreter 并发测试由 us-west-2 工作站发起。
本轮统一为宁夏 EC2。跨天、客户端规格、CPU 架构和 SDK 版本等差异仍存在，
不将延迟变化全部归因于跨区网络。
