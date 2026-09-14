# us-east-1 Runtime V2 可用性复测

- 时间：2026-09-14 05:26:33–05:29:48 UTC；账号：`434444145045`；区域：`us-east-1`。
- 结论：**当前账号已开通 V2，创建、READY 版本回显和实际调用均通过。**
- SDK：现有 `.venv` 私有 boto3/botocore 1.43.87；每个 API 禁用自动重试。
- 镜像：`agentcore-coldstart-pingpong`，按 digest `sha256:8fce75a892c741d4712f9c3bdd2d0a8f429e69bebdd0393ea96c82f461029d87` 固定部署；ECR 大小 401,204,053 bytes。
- 复用 `AgentCoreColdstartRole`；PUBLIC/HTTP；idle 60 秒、maxLifetime 600 秒；未修改 IAM、镜像或配额。
- 临时 Runtime ID：`rtv2_east1_check_08c56d3d-wcxYIY5qJy`。
- 创建指定 `platformVersion="V2"`，请求获准；等待 Runtime 和 DEFAULT endpoint 就绪约 185.34 秒，GetAgentRuntime 明确返回 `status=READY`、`platformVersion=V2`。

| 调用 | Session | HTTP | 响应 | E2E |
|---|---|---:|---|---:|
| 首次 | `92334a09-0ccc-4b1b-8334-d68e5df70abd` | 200 | pong | 2.744 秒 |
| 同 session 第二次 | 同上 | 200 | pong | 0.201 秒 |

- Invoke Request ID：`eabdb6f5-d47a-4e10-a813-06e4ee8fc83a`、`f215b4a3-963e-4e09-9ad3-74c51fa76482`。
- StopRuntimeSession 返回 HTTP 200；DeleteAgentRuntime 返回 HTTP 202，随后 GetAgentRuntime 返回 ResourceNotFoundException，确认删除。验证进程退出码 0。
- 本文件是执行结果摘要；原始输出保留于本次会话的后台任务 `bg-b2402ad0-9b13-4306-88c7-3f5db6791f68`。
- 限制：这是单次可用性检查，不是并发或冷启动性能实验；延迟包含客户端与网络，不能作为 SLA。本次未重新检查其他区域，不代表所有账号或区域均已开通。自动生成的日志未删除。
