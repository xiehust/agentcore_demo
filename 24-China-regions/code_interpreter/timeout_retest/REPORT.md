# Code Interpreter 2.6 超时补测报告

日期：2026-09-22；Owner：**River**。  
账号：`447150580482`。宁夏和北京均从各自区域内保留的 EC2 发起测试。

**结论：2.6 可更新为“✅ 条件通过（会话 TTL / 沙箱命令自动超时）”。**
本次验证了两种真实的自动终止机制，不再仅以人工调用 stopTask 作为依据。
这不表示 `executeCode` 新增了独立 timeout 参数，也不保证在指定秒数准点返回。

## 1. 为什么上次未验证出来

上次任务执行 180 秒，而会话 TTL 为 900 秒，没有越过配置的会话期限。
正常完成的结果不能用于否定会话超时机制。

当前 boto3 1.43.87 的字段说明和 AWS CLI 文档明确规定：
`sessionTimeoutSeconds` 是绝对会话 TTL，即使有正在执行的任务也会自动终止。
它从**会话创建**开始计时，包含启动、文件准备和执行时间，不是从每次 Invoke 开始计时。

参考：[StartCodeInterpreterSession CLI 文档](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/start-code-interpreter-session.html)。
实际 SDK 字段说明也保存在两区 `evidence/environment.json` 中。

## 2. 原生会话 TTL：同步代码和异步任务均通过

配置和方法：

- 创建 PUBLIC Code Interpreter 会话，TTL 设为 **60 秒**。
- 分别通过 `executeCode` 和 `startCommandExecution` 启动计划运行 **180 秒**的任务。
- 任务每秒写独立私有 S3 心跳，正常完成时写完成标记。
- 观察持续到约 210 秒，覆盖原定任务结束之后；观察完成前不调用 stopTask 或 StopSession。
- 异步任务在到期前定期调用 getTask，验证持续活动没有重置绝对 TTL。
- 心跳使用 EC2 角色签发的短期预签名授权，独立于被终止的 Code Interpreter 会话。
  到期后用同批独立探针授权成功 PUT，排除“会话凭证失效导致心跳停止”的混淆。
  预签名 URL 未写入报告、代码日志或结果归档。
- 客户端 socket 读取超时为 260 秒，避免把客户端读取超时误当成服务端终止。

下表中的终态、S3 更新和采样时间均相对于会话 `createdAt`，单位为秒：

| 区域 | 入口 | 配置 TTL | 首次观察到 TERMINATED | S3 最后一次心跳写入 | 末次观察 | 完成标记 |
| --- | --- | --- | --- | --- | --- | --- |
| 宁夏 | executeCode | 60 | 62.117 | 63.992 | 209.461 | 未出现 |
| 宁夏 | startCommandExecution | 60 | 62.459 | 62.985 | 209.803 | 未出现 |
| 北京 | executeCode | 60 | 62.204 | 63.152 | 209.956 | 未出现 |
| 北京 | startCommandExecution | 60 | 62.640 | 63.152 | 210.199 | 未出现 |

四组任务均先产生连续心跳，随后停止更新，在观察结束前没有恢复，
也没有在原定 180 秒结束时产生完成标记。同步调用均收到服务端
**HTTP 409 / ConflictException**，错误明确包含：

> ended before the request could complete, as it exceeded the configured session timeout.

这次证据包含会话终态、独立外部活动记录和明确的服务端超时错误，
不只是客户端断连或一条错误文本。

### 终止与返回延迟必须保留

原生 TTL 不是严格实时截止：

- 配置 60 秒，最后一次外部心跳写入出现在创建后约 **63–64 秒**。
- S3 最后一次更新比首次观察到 TERMINATED 晚约 **0.5–1.9 秒**。
  这包含服务收尾、网络写入和观测精度的影响，不能仅凭终态标记认定立即没有活动。
- 同步 `executeCode` 的调用总耗时：宁夏 **98.828 秒**，北京 **71.881 秒**，
  均晚于外部心跳停止。调用总耗时从 Invoke 开始计算，与表中时间原点不同。
- 会话状态约每 5 秒采样，S3 LastModified 为秒级记录，不能据此推断毫秒级进程退出时刻。

因此本项支持“会话 TTL 会自动结束运行中的任务”的结论，
不支持“配置 60 秒就一定在 60.000 秒停止并返回”的承诺。

## 3. 同一会话的命令级自动超时：通过

在默认系统沙箱内执行：

```bash
timeout --signal=TERM --kill-after=2s 5s python3 job.py
```

测试任务包含父、子两个进程，各自写 PID、进程启动标识、心跳和完成标记。
任务原计划运行 40 秒。普通任务收到 TERM 退出；另一组父子进程都忽略 SIGTERM，
由两秒后的 KILL 终止。没有调用 stopTask。

| 区域 | 任务类型 | 调用耗时 | 退出码 | 父子进程 | 原计划结束之后的复查 |
| --- | --- | --- | --- | --- | --- |
| 宁夏 | 普通父子进程 | 5.191 s | 124 | 均不再运行 | 心跳不变，完成标记不存在 |
| 北京 | 普通父子进程 | 5.306 s | 124 | 均不再运行 | 心跳不变，完成标记不存在 |
| 宁夏 | 父子进程均忽略 TERM | 7.132 s | 137 | 均不再运行 | 心跳不变，完成标记不存在 |
| 北京 | 父子进程均忽略 TERM | 7.186 s | 137 | 均不再运行 | 心跳不变，完成标记不存在 |

PID 检查结合 `/proc/<pid>/stat` 的进程启动标识，避免把 PID 复用误认为原任务存活。
在最晚一组任务启动至少 45 秒后再次复查。两个区域中，同一会话的 notebook 标记变量
仍可读取为 42，说明命令超时没有终止整个会话。

这里的 124/137 是已知 GNU timeout 包装下的实测结果；
不能把任意程序返回 137 都解释成超时。此方案适用于终端子进程，
子进程不与 `executeCode` 的 notebook 直接共享 Python 全局变量。
本次没有验证主动脱离进程组等对抗性任务。

### 对照修正

首轮正常完成对照的父进程没有等待子进程，导致子进程未留下完成标记。
该分支记录为 ERROR，未改写为成功。
修正为父进程显式等待子进程后，仅重跑命令分支：
正常对照的父子进程均完成；两类自动超时任务均通过。
原生 TTL 分支没有因这次修正而重跑或覆盖。

## 4. 建议采用的验收记录

| # | 验证项 | 优先级 | 状态 | Owner | 备注 |
| --- | --- | --- | --- | --- | --- |
| 2.6 | 执行超时机制有效（超时后任务正确终止） | 🔴 P0 | ✅ 条件通过 | River | 两区验证了会话 TTL 自动终止同步/异步任务，以及默认沙箱 GNU timeout 自动终止父子进程。TTL 60s 的外部活动约 63–64s 停止；命令期限 5s、KILL 宽限 2s 的实测约 5.2s/7.2s。不是 executeCode 独立原生 deadline，也不是硬实时保证。 |

选择方式：

| 接入方式 | 已验证能力 | 代价 / 边界 |
| --- | --- | --- |
| 每个任务独立会话，设置 sessionTimeoutSeconds | 原生 TTL 自动结束会话及运行任务 | 从会话创建计时，整个会话终止，存在收尾和错误返回延迟 |
| 同一会话通过 executeCommand + GNU timeout 运行任务 | 自动 TERM / KILL，父子进程结束，会话仍可用 | 终端子进程不直接共享 notebook 全局变量 |

如果项目要求“在同一多轮 notebook 会话内，为每次 executeCode 设置独立原生 deadline”，
当前公开 SDK 没有对应字段，本次也没有证明该模式。
不能把本项写成对所有执行入口、所有任务形态的无条件通过。

## 5. 环境、收尾与证据

- 宁夏：`i-0a685957c9b9355d7`，t3.small。
- 北京：`i-0ab621bda76e7f0af`，t4g.small。
- SDK：boto3 / botocore 1.43.87；区域由 EC2 IMDSv2 与 STS 验证。
- 本轮每区 4 个测试会话：两个 TTL、一个首轮命令对照、一个修正后的命令分支。
- 结果已下载；本轮临时 PUBLIC 解释器、S3 bucket 和临时 IAM 内联授权已清理，
  两台 EC2 再次停止并保留。既有 EFS 清理遗留不属于本次资源，未在本轮修改。

主要文件：

- [验证计划](PLAN.md)、[管理脚本](manage.py)、[EC2 测试脚本](verify_timeout.py)。
- [独立核验后的汇总](results/20260922/verified_summary.json)。
- [宁夏同步 TTL](results/20260922/cn-northwest-1/evidence/ttl-executeCode-result.json)、
  [宁夏异步 TTL](results/20260922/cn-northwest-1/evidence/ttl-async-result.json)。
- [北京同步 TTL](results/20260922/cn-north-1/evidence/ttl-executeCode-result.json)、
  [北京异步 TTL](results/20260922/cn-north-1/evidence/ttl-async-result.json)。
- [宁夏命令超时复测](results/20260922/cn-northwest-1/command-retry/evidence/command-deadlines-result.json)、
  [北京命令超时复测](results/20260922/cn-north-1/command-retry/evidence/command-deadlines-result.json)。
- [宁夏清理](results/20260922/cn-northwest-1/cleanup.json)、
  [北京清理](results/20260922/cn-north-1/cleanup.json)。
- [清理独立复核](results/20260922/cleanup_audit.json)。

同目录的 `*-observations.json` 保存完整观察序列；`api/` 保存脱敏 API 证据。
`submitted_source/` 保留实际提交的首轮与修正后代码，哈希已核对。
旧 180 秒测试、首轮命令对照失败及历史报告均保留。
