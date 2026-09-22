# 北京区 Runtime / Code Interpreter 验证计划

日期：2026-09-22；profile：`agentcore_cn`；账号：`447150580482`；
目标区域：`cn-north-1`。所有被测服务调用和延迟计时均在新建的北京区 EC2 上执行。
当前工作站只编写/传输代码并管理 AWS 资源，不运行性能或功能测试。

## 实例和执行位置

使用 `t4g.small`（2 vCPU / 2 GiB、ARM64），以便在实例上原生构建并校验同一个
ARM64 Runtime 容器，不需要在当前工作站运行容器或使用跨架构模拟。
与宁夏 t3.small 的 CPU 架构差异会写入报告。
启用 IMDSv2、加密 EBS、无 SSH 入站规则，使用 SSM 和实例角色。
测试后沿用已确认的偏好：停止并保留 EC2，清理临时被测资源。

## 测试范围

| 组件 | 内容 | 数量 / 判定 |
| --- | --- | --- |
| Runtime | 串行新会话首次调用 | 100 次；P50 < 3s、P99 < 5s |
| Runtime | 同一会话暖请求 | 首次准备调用后 500 次；P99 < 200ms |
| Runtime | 从 0 个用户会话扩至 50 并发 | 独立、从未调用的 Runtime，50 次首次调用；0 失败，检查实例标记和处理重叠 |
| Code Interpreter | 冷启动 / 首次执行并发 | 1、10、50 各一批，记录 Start / Invoke / 端到端耗时 |
| Code Interpreter | 串行冷启动基线 | 100 个全新会话 |
| Code Interpreter | 2.1/2.2/2.3/2.4/2.5/2.9 | 基础执行、隔离探针、stdout/stderr、库、文件往返、多轮状态 |
| Code Interpreter | 2.6 | 180 秒原生执行观察、调用端期限 + stopTask；不把主动取消当作原生自动超时 |
| Code Interpreter | 2.10 | 默认沙箱和 PUBLIC 包源对照，清华镜像真实安装 |
| Code Interpreter | EFS | 北京区独立 EFS + access point + VPC interpreter；三个会话验证持久化 |

Runtime 和 100 次串行基线使用 nearest-rank；CI 1/10/50 并发沿用原脚本的线性插值。
保留所有失败，SDK 自动重试关闭。“冷”和“从 0”指用户会话维度，不声称清空平台预热池。

## 执行顺序

1. 查询北京区 AMI、子网和现有业务资源；记录并保护已有 Runtime。
2. 创建北京 EC2、私有传输 bucket、专用角色、安全组及 ECR。
3. 在北京 EC2 构建/校验/推送 Runtime 镜像；部署新的 baseline / scale Runtime。
4. 准备 PUBLIC 和 EFS VPC Code Interpreter；所有实际验证均由北京 EC2 发起。
5. 按阶段执行，收集 IMDS 身份文档、CPU 采样、API 原始事件、哈希与结果。
6. 下载原始结果，独立重算并生成中文报告；停止并保留 EC2，清理临时云资源。

若服务拥有的 ENI 延迟释放，不强制解绑；明确记录待清理资源并执行有界重试。
宁夏的历史结果和保留的 EC2 不改动。

## 执行记录

- 北京 EC2：`i-0ab621bda76e7f0af`，`cn-north-1a`，`t4g.small`。
- Docker Hub 访问超时；通过北京 S3 传输固定基础镜像，并在北京 EC2 校验、离线构建、
  运行容器检查、推送北京 ECR。首次构建失败日志及 Docker inspect 可选字段差异均保留。
- 正式测试于 2026-09-22 05:21:28–05:31:37 UTC 在北京 EC2 上完成。
- Runtime 冷 P50 1,737.471 ms、冷 P99 2,196.439 ms、暖 P99 199.440 ms，
  50 并发 0 失败；暖 P99 临界通过。
- Code Interpreter 并发 1/10/50 全部成功；100 次串行端到端 P50 968.555 ms、
  P99 1,493.453 ms。基础功能、PUBLIC 包源、EFS 通过，原生执行超时仍为 PARTIAL。
- 151 个 Runtime 会话及 168 个 Code Interpreter 会话均停止。EC2 已停止并保留。
  核心临时资源已删除，EFS 客户端安全组与执行角色等待 AWS 服务 ENI 释放，
  重试状态见结果目录。原有北京 Runtime 未修改。
