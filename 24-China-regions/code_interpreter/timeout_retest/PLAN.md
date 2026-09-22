# Code Interpreter 2.6 补充验证

Owner：River。区域：宁夏 `cn-northwest-1`、北京 `cn-north-1`。
复用两区保留的 EC2，从同区发起测试，结束后再次停止并保留。

## 新的契约证据

当前 boto3 1.43.87 的 `StartCodeInterpreterSession.sessionTimeoutSeconds` 文档
与 AWS CLI 文档明确写明：

> The duration in seconds (time-to-live) after which the session automatically
> terminates, regardless of ongoing activity.

此前“180 秒任务在 900 秒会话内完成”没有跨越配置期限，不能证明超时无效。
本轮分别验证会话级原生 TTL 和保留会话的命令级自动期限，不混同两者。

## 验证矩阵

1. **原生 TTL / executeCode**：PUBLIC 会话 TTL 60 秒，代码计划运行 180 秒。
2. **原生 TTL / startCommandExecution**：相同 TTL，异步命令计划运行 180 秒；
   到期前定期 getTask，以确认有活动也不会重置绝对 TTL。
3. **外部存活证据**：两个原生 TTL 任务每秒向独立私有 S3 bucket 写心跳，
   结束时写完成标记。使用由 EC2 角色签发的短期预签名 PUT URL；
   其有效期独立于 Code Interpreter 会话，不依赖被终止会话的角色凭证。
   预签名 URL 不进入报告、代码日志、结果归档或模型上下文。
4. **超时后检查**：持续观察到原定 180 秒任务结束之后，确认心跳停止、
   完成标记未出现、session 为 TERMINATED；用独立探针确认同批签名授权仍有效。
   观察完成前不调用 stopTask / StopSession。
5. **命令自动超时**：默认系统沙箱执行 GNU `timeout --kill-after=2s 5s ...`，
   对普通父子进程和忽略 SIGTERM 的父子进程分别验证自动 TERM / KILL。
   检查退出码、父子 PID、心跳及完成标记；在原计划完成时间之后再次检查，
   并确认同会话后续代码仍可执行。无超时包装的短任务作为正常完成对照。

客户端读取超时设为大于测试任务长度，不能把网络读取超时当作执行终止。
所有任务有有限运行时长，会话也有有限 TTL。

## 判定边界

- 会话 TTL 通过只能证明整个会话及其中任务在期限到达后被终止。
  它从会话创建时计时，不能宣称是独立的每次 executeCode deadline。
- GNU timeout 通过属于沙箱内的自动任务终止方案，不是 API 新增了 timeout 参数。
  终端子进程不与 executeCode 的 notebook 直接共享 Python 变量。
- 不预设本轮通过；依据会话状态、外部心跳和进程证据更新报告，保留旧结果。

参考：
https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/start-code-interpreter-session.html
