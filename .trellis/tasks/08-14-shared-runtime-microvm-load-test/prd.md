# 实现 microVM 共享 Runtime 多用户并发测试

## Goal

参考 `15-shared-runtime-instance/` 的共享 Runtime Session 多用户短程与长程任务并发测试，在新目录 `16-shared-runtime-microvm/` 中提供可独立部署、执行和复验的 microVM Runtime 版本，并以 `InvokeAgentRuntimeCommand` 替代 SSM 完成同一 active session 内的资源采样和产物校验。

## Requirements

1. 复用基准测试的身份传递、用户工作区隔离、每用户 Claude session resume、同用户串行/跨用户并发和实例指纹机制，但不修改 `15-shared-runtime-instance/`。
2. microVM Runtime 部署不得配置 EC2 Capacity Provider 或宿主机文件系统；用户工作区位于 session 生命周期内的临时文件系统。
3. 所有虚拟用户共享一个随机且长度合法的 `runtimeSessionId`，真实用户通过 `runtimeUserId` 和 payload `user_id` 传入。
4. 提供多用户隔离 smoke、短程并发爬坡和两阶段长程任务并发爬坡；每档记录成功率、p50/p90/max、错误、工作区、session resume 和单一实例指纹。
5. 不依赖 SSM、EC2、ASG 或托管宿主内部实现。warmup 激活 session 后，使用 `InvokeAgentRuntimeCommand` 在同一容器内启动轻量 `/proc` 采样器、读取采样结果并验证长程产物。
6. 命令客户端必须解析 `contentStart`、`contentDelta`、`contentStop`，检查 API status、stream exception、command status 和 exit code，并对 session provisioning/teardown 的 409 冲突做有限退避重试。
7. 压测期间逐档原子写 checkpoint；监控或产物校验失败时保留 agent 请求结果并明确标记验证不可用。
8. 所有测试脚本默认在 `finally` 中调用 `StopRuntimeSession`，避免等待 idle timeout 继续计费；允许显式配置保留 session 仅用于调试，并给出风险提示。
9. 文档明确 microVM 的 session 隔离边界、临时文件系统和 8 小时 compute lifecycle；不得把未执行的 AWS 结果写成实测结论。
10. 依赖版本固定；客户端所需 boto3 版本必须包含 `invoke_agent_runtime_command`。

## Authorized Cloud Execution

用户已于 2026-08-14 明确授权实际执行。本任务现在包括：构建并推送 ECR 镜像、创建或更新独立的默认 microVM Runtime、执行计费的隔离/短程/长程调用、使用 `InvokeAgentRuntimeCommand` 采样和验收、停止每个测试 session，并将真实数据写成中文报告。

执行边界：

- 复用已验证 Runtime 的 execution role，不修改 IAM、VPC、Capacity Provider、ASG 或其他现有 Runtime。
- 目标 Runtime 使用独立名称 `shared_runtime_microvm` 和独立镜像 tag。
- 先执行三用户 smoke、短程 `2/4/8`、长程 `1` 和 `2/4`；只有在成功率、内存和清理结果健康时才追加长程 `8`。
- 每个测试脚本必须确认 `StopRuntimeSession` 成功；报告完成后删除测试 Runtime，但默认保留 ECR 镜像以便复验。

## Out of Scope

- 修改现有 EC2 Capacity Provider Runtime、IAM role、VPC、ASG 或托管宿主。
- 证明不可信用户在同一 microVM session 内互相隔离；同 session 仍是共享容器/进程信任边界，应用层守卫只适用于弱威胁模型。
- 在资源余量不足或失败阈值触发后继续盲目扩大并发。
- 生成、补齐或推测缺失的云端容量数字。

## Acceptance Criteria

- [x] `16-shared-runtime-microvm/` 包含独立的 app、Dockerfile、部署/清理脚本、隔离单测和使用说明。
- [x] microVM Runtime 实际部署为 READY，且没有 Capacity Provider 和 filesystem configuration。
- [x] 多用户 smoke 实际验证同 session/同进程复用、独立工作区、文件/记忆无串扰，并通过 command API 核对 boot ID/hostname。
- [x] 短程 `2/4/8` 并发实际记录成功率、p50/p90/max 和 command API CPU/内存/load/进程指标。
- [x] 长程至少完成 `1` 和 `2/4` 档两阶段 resume、6 文件精确校验与资源采样；是否执行 `8` 由前序余量决定并在报告说明。
- [x] 每个测试 JSON 均记录 cleanup success，所有创建的 Runtime sessions 均已停止。
- [x] `results/REPORT.md` 改写为中文，所有表格和结论均可追溯到真实 JSON，不复用目录 15 的容量数字。
- [x] command API/SSE/监控/隔离单测、Python compile、shell syntax、lint 和类型检查通过。
- [x] 测试 Runtime 在报告完成后删除，ECR 镜像保留并记录；`15-shared-runtime-instance/` 无改动。

## Key Decisions

- 将共同的 Runtime invocation、command event-stream、监控和原子结果写入逻辑集中到一个客户端工具模块，短程与长程脚本复用。
- 监控器是通过 command API 启动的单个后台 Python 进程，直接读取 `/proc` 和可用的 cgroup v2 文件，避免每次采样派生大量 shell 进程。
- 每次测试只使用一个新 session，warmup 后复用；在产物与监控采集结束后 stop。
- 本地验证只证明脚本和解析契约正确；真实容量结论必须来自后续 AWS 执行生成的 JSON。
