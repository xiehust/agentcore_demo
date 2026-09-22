# 宁夏 EC2 同区域 Runtime / Code Interpreter 复测

日期：2026-09-22；profile：`agentcore_cn`；账号：`447150580482`；
区域：`cn-northwest-1`。

## 目标与范围

新建一台小型 CPU EC2，以 EC2 为唯一测量客户端，复测两个组件的首次调用和并发。
记录 EC2 Instance Identity Document、可用区、实例规格、SDK 版本、目标 endpoint、
实例角色身份及原始响应，直接证明客户端和被测资源都位于宁夏区。
本地工作站只负责部署、SSM 发起任务与下载结果，不参与延迟计时。

优先 `t3.small`（2 vCPU / 2 GiB）、Amazon Linux、加密 EBS、IMDSv2，
不开放 SSH 入站，通过 Systems Manager 执行。压测使用实例角色，不复制静态凭证。
记录 CPU 使用和积分相关数据，避免把小实例资源不足误判为服务端延迟。

## 测试矩阵

| 组件 | 测试 | 样本 / 并发 | 口径 |
| --- | --- | --- | --- |
| Runtime | 串行新会话首次请求 | 100 个新会话，并发 1 | 完整响应 P50 < 3s，P99 < 5s |
| Runtime | 同会话暖请求 | 准备请求 1 次，随后 500 次 | 完整响应 P99 < 200ms |
| Runtime | 从 0 个用户会话到 50 并发 | 独立、从未调用的 Runtime，50 个新会话 | 0 失败；沿用 5 秒处理停留，核验并发区间 |
| Code Interpreter | 沿用上一轮并发档位 | 1、10、50，各一批 | 分别统计创建会话、首次执行、端到端耗时及成功率 |
| Code Interpreter | 补充单并发基线 | 100 个新会话，串行执行并停止 | 给出更有代表性的 P50/P99，便于与 Runtime 对照 |

Runtime 沿用上一轮相同 app、镜像基础 digest、首次业务请求生成的 instance_id、
100/500/50 样本量及 nearest-rank 分位数。Code Interpreter 并发测试沿用原脚本、
原有线性插值分位数；新增串行基线额外给出 nearest-rank P50/P99。
所有 SDK 自动重试关闭，失败保留，不将客户端超时当作成功。

“冷启动”指新用户会话首次请求；无法通过客户端清空或观测服务内部预热池。
同区复测排除了跨区域链路，但仍包含同区域网络、客户端 SDK、平台和应用处理时间。
跨天、不同客户端规格及 SDK 的差异会列出，不将两次数据差异全部归因于网络。

## 执行步骤

1. 检查宁夏区 AMI、t3.small、子网公网路由、SSM；先保存资源创建意图和名称。
2. 创建最小入站权限的测试安全组、EC2 专用角色/实例配置文件、传输结果用的临时私有 S3 bucket。
3. 创建 EC2，确认 SSM 在线、IMDS 区域正确；部署全新 baseline/scale Runtime。
4. 上传测试代码，通过 SSM 在 EC2 上安装 SDK 并串行执行两组件测试，保留原始日志。
5. 下载结果到 `runtime/results/20260922-ec2/`、
   `code_interpreter/results/20260922-ec2/` 与本目录结果文件夹，生成对照报告。
6. 核验会话停止和资源清理。EC2 完成后的保留方式已向用户询问，
   在没有额外回复时采用停止并保留；测试 Runtime、ECR、临时传输 bucket 等清理。
   EC2 保留时保留其启动所需角色、实例配置文件和安全组，并说明残留资源。

## 交付

- 本目录：计划、EC2 管理脚本、同区执行入口、总报告和 EC2/SSM 证据。
- `runtime/`：本轮原始数据及 Runtime EC2 复测报告。
- `code_interpreter/`：本轮原始数据及 Code Interpreter EC2 复测报告。
- 保留 2026-09-21 原报告和原始结果。

用户已确认：完成测试后停止并保留 EC2，便于后续复测。

## 完成记录

- EC2 `i-0a685957c9b9355d7`，`cn-northwest-1a`，`t3.small`；
  IMDSv2 与 STS 实例角色身份验证通过。
- 正式测量：2026-09-22 00:29:58–00:35:53 UTC。
  Runtime 四项均通过；Code Interpreter 1/10/50 并发及 100 次串行均成功。
- Runtime 冷 P50 1,623.420 ms、冷 P99 2,008.168 ms、暖 P99 154.920 ms。
  Code Interpreter 100 次串行端到端 P50 917.005 ms、P99 1,443.306 ms。
- 保留早期 CPU steal 的全部样本与准备阶段 Python 3.9 安装失败记录。
- 151 个 Runtime 会话和 161 个 Code Interpreter 会话均停止；
  临时 Runtime/ECR/服务角色/S3 bucket 已删除，EC2 已停止并保留其启动所需资源。
