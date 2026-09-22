# 北京区 AgentCore 验证报告

日期：2026-09-22；账号 `447150580482`；区域 **cn-north-1**。
实际容器检查和全部服务验证均在北京 EC2 `i-0ab621bda76e7f0af` 上运行；
当前工作站只管理资源、传输既有基础镜像与测试代码、下载和分析结果。

## 环境与口径

| 项目 | 实测值 |
| --- | --- |
| EC2 区域 / AZ | cn-north-1 / cn-north-1a |
| 规格 | t4g.small，ARM64，2 vCPU / 2 GiB |
| Python / boto3 / botocore | 3.12.14 / 1.43.87 / 1.43.87 |
| endpoint | https://bedrock-agentcore.cn-north-1.amazonaws.com.cn |
| EC2 状态 | stopped |

区域由 IMDSv2 身份文档与 STS 实例角色交叉验证；没有向 EC2 复制本地静态凭证。
无 SSH 入站，通过 SSM 运行。CPU 采样覆盖测试过程。
SDK 自动重试关闭；冷请求按全新用户会话首次完整响应计量。
该口径无法确认服务内部没有预热池，也无法强制从 0 个物理实例启动。

## Runtime

100 次串行新会话、1 次暖准备调用、500 次同会话暖调用、独立 Runtime 的 50 并发，
共 651 次请求。分位数为 nearest-rank，阈值严格小于。

| 验证项 | 北京实测 | 目标 | 宁夏 EC2 实测 | 结果 |
| --- | --- | --- | --- | --- |
| 7.1 冷请求 P50 | 1737.471 ms | < 3000 ms | 1623.420 ms | PASS |
| 7.2 冷请求 P99 | 2196.439 ms | < 5000 ms | 2008.168 ms | PASS |
| 7.3 暖请求 P99 | 199.440 ms | < 200 ms | 154.920 ms | PASS |
| 7.4 50 并发 | 0/50 失败 | 0% | 0/50 失败 | PASS |

暖请求 P99 距 200 ms 阈值仅 **0.560 ms**，属于临界通过。
500 次暖请求中有 5 次达到或超过 200 ms；本轮样本通过不代表长期 P99 保证。

50 并发返回 50 个首次调用实例标记，
客户端请求区间峰值重叠 50，
应用处理区间峰值重叠 50。
该阶段保留原来的 5 秒处理停留，不与空载冷/暖请求延迟混算。

## Code Interpreter 冷启动

下表单位为秒，沿用 1/10/50 并发原脚本的线性插值分位数。
每批全部首次执行完成后才停止会话。

| 并发 | 成功 | Start P50 | 端到端 P50 | 端到端 P95 | max | 宁夏 EC2 端到端 P95 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1/1 | 0.792 | 0.914 | 0.914 | 0.914 | 0.822 |
| 10 | 10/10 | 0.936 | 1.097 | 1.334 | 1.392 | 1.039 |
| 50 | 50/50 | 0.836 | 1.099 | 1.555 | 2.078 | 1.310 |

额外 100 次串行新会话：成功 100/100。
创建至首次执行完成的 P50 **968.555 ms**、
P99 **1493.453 ms**（nearest-rank）。
其中创建会话 P50 815.630 ms，首次执行 P50
152.923 ms。
Code Interpreter 端到端包含 Start + Invoke 两次调用，与 Runtime 的单次 Invoke 链路不同。

## Code Interpreter 功能与 EFS

| 编号 | 验证项 | 结果 |
| --- | --- | --- |
| 2.1 | Python / 标准库 | PASS |
| 2.2 | 客户端宿主与跨会话隔离探针 | PASS |
| 2.3 | stdout / stderr / 异常透传至 SDK 工具入口 | PASS |
| 2.4 | pandas / numpy / matplotlib | PASS |
| 2.5 | 文本与二进制上传下载 | PASS |
| 2.6 | 执行超时 | PARTIAL |
| 2.9 | 多轮状态 | PASS |
| 2.10 | 默认沙箱拉包 | FAIL |
| 2.10 | PUBLIC 清华镜像拉包与运行 | PASS |
| EFS | 挂载与三会话持久化 | PASS |

隔离项仅验证北京 EC2 客户端宿主标记和跨会话私有文件，不是 AWS 底层宿主逃逸审计。
stdout / stderr 验证到 Python SDK 工具入口，没有调用额外 LLM。

180 秒原生执行观察的耗时为 180.178 秒，
原生自动执行超时证据：`False`；
调用端期限 + stopTask 的终止证据：`True`。
单次执行没有 SDK timeout 字段；不能把主动取消或会话过期替代原生执行超时验收。

依赖安装保留默认沙箱与 PUBLIC 两种配置的结果。PUBLIC 测试固定
`pytimeparse==1.1.8`、禁用缓存、独立安装目录，并执行导入后的真实函数。

EFS 使用北京区独立文件系统和 access point，VPC 挂载 `/mnt/efs`。
从北京 EC2 调用三个独立会话：A 写入后停止，B 读取并追加后停止，C 验证追加内容及 SHA-256。
最终哈希 `6aa5e32285fd17408806844ffcd25c9d09249b091278130a6e36cf98f4cc55e1`。
用户原有 EFS / Runtime 不属于本次测试资源。

## CPU 与比较限制

| 阶段 | 样本 | busy 平均 | busy max | steal max |
| --- | --- | --- | --- | --- |
| code_interpreter_concurrency | 5 | 16.51% | 29.82% | 11.40% |
| code_interpreter_efs | 11 | 2.17% | 10.40% | 0.50% |
| code_interpreter_functional | 6 | 0.67% | 2.00% | 0.00% |
| code_interpreter_public_dependencies | 6 | 2.39% | 12.38% | 0.50% |
| code_interpreter_serial | 110 | 1.37% | 11.06% | 4.41% |
| code_interpreter_timeout | 199 | 0.37% | 10.95% | 8.10% |
| runtime | 268 | 1.12% | 46.38% | 3.85% |

宁夏客户端是 t3.small（x86_64），北京是 t4g.small（ARM64），两者都在被测区域内。
跨区域比较还包含客户端架构、服务状态和测试时间差异，不能把全部差异归因于地域。
所有异常值和失败都保留；没有为通过阈值而剔除样本。

北京 EC2 访问 Docker Hub 超时，使用经北京 S3 传输的固定基础镜像离线构建。
压缩包哈希、镜像 ID、RootFS、运行配置在 EC2 上验证，再原生运行容器检查并推送北京 ECR。
构建准备和服务 CREATING → READY 不计入冷启动延迟。
两次准备阶段失败（Docker Hub 超时、Docker inspect 可选字段差异）保留日志，
均发生在正式性能采样之前。

## 清理

Runtime 会话 151 个；Code Interpreter 会话：{"code_interpreter": 164, "code_interpreter_public": 1, "efs": 3}，
全部取得成功 Stop 响应。
EC2 按已确认偏好停止并保留，保留其 EBS、SSM 角色/profile 和安全组；
临时被测资源及传输 bucket 的独立复核如下：

```json
{
  "at": "2026-09-22T05:39:25.557868+00:00",
  "ec2": {
    "id": "i-0ab621bda76e7f0af",
    "state": "stopped",
    "az": "cn-north-1a",
    "type": "t4g.small"
  },
  "sessions": {
    "runtime_stop_acknowledged": 151,
    "default_ci_terminated": 164,
    "public_ci_stop_acknowledged": 1,
    "efs_ci_stop_acknowledged": 3
  },
  "deletion_checks": [
    {
      "resource": "cn_runtime24_ec55b7f117_baseline-zcut37GVyp",
      "deleted": true,
      "request_id": "691a754c-32e8-4389-af65-33cdae05a855"
    },
    {
      "resource": "cn_runtime24_ec55b7f117_scale-9gxMJk2Ys6",
      "deleted": true,
      "request_id": "9c523c5d-8916-421b-a39e-877c1b5872de"
    },
    {
      "resource": "cn_runtime24_ec55b7f117_public-X94BdJdLva",
      "deleted": true,
      "request_id": "154a2c11-e7fa-40c4-a352-2ea4c507d03a"
    },
    {
      "resource": "cn_ci_efs_53c6d40f78-eXhLPXpSHB",
      "deleted": true,
      "request_id": "25b16d66-6d10-4738-aab9-ba11edcab6ef"
    },
    {
      "resource": "fs-0fdffaca3f1fa9fa8",
      "deleted": true,
      "request_id": "dfed2313-8fcd-46c3-ad6f-df0fcd87fca3"
    },
    {
      "resource": "fsap-0746b08f9ed31a81c",
      "deleted": true,
      "request_id": "14bf07ed-5114-40d7-9653-9de30234fd6e"
    },
    {
      "resource": "fsmt-0593f099897908535",
      "deleted": true,
      "request_id": "9946fa88-8358-414f-88bb-13de945b5671"
    },
    {
      "resource": "cn-runtime24-ec55b7f117",
      "deleted": true,
      "request_id": "5db5142a-1de8-4190-ad8e-7cc8a0b3a8db"
    },
    {
      "resource": "cn-ec2-bench-0eec9b6240-447150580482",
      "deleted": true,
      "request_id": "3WH767EGJD7DP1XQ"
    },
    {
      "resource": "cn-runtime24-ec55b7f117-runtime",
      "deleted": true,
      "request_id": "e80e7261-c229-4908-9268-15357f3dc829"
    },
    {
      "resource": "cn_ci_efs_53c6d40f78",
      "deleted": false
    },
    {
      "resource": "sg-02b4c907a70fbe1be",
      "deleted": false
    },
    {
      "resource": "sg-040d03b6d253d1c49",
      "deleted": true,
      "request_id": "6f8e2675-45a0-4d70-861e-0c1bd2b792cd"
    }
  ],
  "retained": {
    "instance": "i-0ab621bda76e7f0af",
    "instance_state": "stopped",
    "role": "cn-ec2-bench-0eec9b6240",
    "profile": "cn-ec2-bench-0eec9b6240",
    "security_group": "sg-05e4383d86a02b2b2",
    "volumes": [
      "vol-0f849d955f70fa9f5"
    ]
  },
  "existing_runtimes": [
    {
      "id": "uat_smoke-7r6g859pPV",
      "status": "READY",
      "version": "1"
    },
    {
      "id": "uat_obs_runtime-7Jdad74K4S",
      "status": "READY",
      "version": "1"
    },
    {
      "id": "hosted_agent_wf7di-QuFH9IBBx8",
      "status": "READY",
      "version": "1"
    },
    {
      "id": "efs_write_test-NNI37O554n",
      "status": "READY",
      "version": "1"
    }
  ],
  "network_retry": "efs/cleanup-retry.json"
}
```

## 证据

- [执行计划](PLAN.md)与[运行方法](README.md)。
- [结构化最终结果](results/20260922/final_summary.json)。
- [实例身份和客户端环境](results/20260922/ec2/collected/results/ec2_environment.json)。
- [原始 Runtime 请求](results/20260922/runtime/benchmark_results.json)。
- [CI 并发结果](results/20260922/code_interpreter/concurrency.json)。
- [CI 功能结果](results/20260922/code_interpreter/summary.json)。
- [PUBLIC 包源结果](results/20260922/code_interpreter_public/summary.json)。
- [EFS 结果](results/20260922/efs/result.json)。
- [CPU 原始采样](results/20260922/ec2/collected/results/cpu_samples.jsonl)。
- [独立重算与完整性检查](results/20260922/evidence_audit.json)。
- [清理独立复核](results/20260922/cleanup_audit.json)。
- [EFS 网络清理重试状态](results/20260922/efs/cleanup-retry.json)。
