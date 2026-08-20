# Shared Runtime microVM 用户映射池架构

## 1. 文档目标

本文为 `shared-runtime-microvm` 方案设计一套可落地的 `userId → runtimeSessionId` 映射池，用于数千注册用户规模的 Agent 应用。目标包括：

- 每个共享 Runtime Session 同时执行的用户请求不超过 10；
- 保持用户与 session 的短期亲和性；
- 防止并发超卖、重复执行和失效请求写回；
- 支持按实际活跃并发弹性扩缩；
- 在 microVM 换代、请求失败和状态漂移后自动恢复；
- 以成功请求成本为优化目标，而不是简单追求最少 session 数；
- 延续本项目已经验证的用户隔离、Claude SDK hook 和 Runtime 调用契约。

本文是生产架构建议，不代表已经创建对应 AWS 资源。截至本文编写时，项目测试 Runtime 均已删除，ECR 测试镜像仍保留。

## 2. 核心结论

不要按照以下方式估算 Runtime 数量：

```text
5000 个注册用户 / 每 session 10 个用户 = 500 个 Runtime
```

推荐方式是：

1. 每个 Region、应用版本和模型组合只维护少量 AgentCore Runtime ARN/Endpoint；
2. 在 Runtime ARN 下按需使用多个 `runtimeSessionId`；
3. 将 `runtimeSessionId` 作为共享执行和亲和调度单元；
4. 一个 session 可以亲和绑定多于 10 个低频用户；
5. 任意时刻最多允许 10 个请求在同一 session 内执行；
6. 正常调度目标保持在 6–7/10，10 仅作为硬上限；
7. 按峰值活跃并发而不是注册用户总数扩容。

典型的 5000 用户应用可能只需要约 8 个活跃 session 和 1–2 个 warm session，而不是数百个 Runtime。

## 3. 术语和资源边界

```text
AgentCore Runtime ARN / Endpoint        控制面部署资源
└── runtimeSessionId                    逻辑 session 和执行位置
    └── 当前 microVM execution env      可因 idle/maxLifetime 被替换
        ├── runtimeUserId A             应用用户
        ├── runtimeUserId B
        └── runtimeUserId C
```

### 3.1 AgentCore Runtime ARN

Runtime ARN 是应用部署和版本的控制面资源，不应为每个用户单独创建。通常按照以下维度拆分即可：

- Region；
- 应用版本；
- 模型或资源等级；
- 必须隔离的租户等级。

### 3.2 `runtimeSessionId`

`runtimeSessionId` 是映射池的调度单位。首次调用新 ID 时，AgentCore 会为它准备执行环境。同一个 ID 的当前执行环境可能因为 idle timeout、max lifetime 或健康检查失败而被终止和替换。

官方生命周期文档明确指出：达到 `maxLifetime` 后，当前 instance 会终止，但逻辑 session 可以由新 instance 继续承接。因此必须区分：

- 逻辑 `runtimeSessionId`；
- 当前 microVM generation；
- microVM 内的临时文件和进程状态。

### 3.3 `runtimeUserId`

`runtimeUserId` 是应用用户身份，也是本项目中用户 workspace、HOME、Claude metadata 和锁的隔离键。它不是 AgentCore 控制面资源。

`userId → runtimeSessionId` 应被视为有有效期的亲和路由关系，而不是永久资源所有权。

## 4. 已验证的容量和隔离边界

完整实测数据见 [`results/REPORT.md`](results/REPORT.md)。

本项目在同一个 `runtimeSessionId` 内完成了短程和长程并发测试：

- 短程并发阶梯：`2/4/8/12/16/24/32/40`，共 138/138 成功；
- 长程并发 1–32：共 99/99 端到端成功；
- 长程 32 并发：最低可用内存约 1144 MB，cgroup current 峰值约 6506 MB；
- 长程 40 并发：0/40 完成；foundation 请求均返回 HTTP 200，但缺少最终 complete SSE event；
- 没有直接 OOM 证据，但失败与资源压力高度一致。

因此：

```text
推荐调度目标：6–7
生产硬上限：10
容器内部 semaphore：10
```

10 明显低于已验证的长程安全建议上限，具有较好资源余量；但仍不应长期保持 10/10，以便为突发流量、任务内存波动、子进程启动和故障恢复保留空间。

同一 shared session 内的用户共享：

- FastAPI 进程；
- OS 用户；
- Runtime IAM 凭证；
- CPU 和内存；
- Claude SDK 子进程域。

因此该方案仅适合同一信任域内的用户。互不信任的 tenant 不应共享 session，涉及任意代码或 shell 执行时尤其如此。路径守卫和身份一致性检查是应用层保护，不是强租户隔离边界。

## 5. 推荐生产架构

```text
Client
  │
  ▼
ALB / API Gateway WebSocket
  │
  ▼
Stateless Session Router
  ├── DynamoDB
  │   ├── UserAffinity
  │   ├── SessionPool
  │   ├── RequestLease
  │   └── Idempotency
  ├── SQS FIFO（需要排队或异步执行时）
  ├── AgentCore Memory / DynamoDB / S3（持久状态）
  │
  ▼
AgentCore Runtime ARN / Endpoint
  ├── runtimeSessionId S1：inflight <= 10
  ├── runtimeSessionId S2：inflight <= 10
  └── runtimeSessionId SN：inflight <= 10

EventBridge + Reconciler
  ├── 扩缩容
  ├── lease 修复
  ├── drain
  └── idle stop

CloudWatch / OpenTelemetry
  └── 延迟、队列、资源、token 和成本指标
```

长时间 SSE 或流式请求建议使用 ALB + ECS/Fargate Router。只有请求时长和响应模式适合时才使用 Lambda，避免入口层超时成为实际瓶颈。

## 6. DynamoDB 数据模型

可以使用单表设计，也可以按职责拆表。以下按逻辑实体描述。

### 6.1 UserAffinity

```text
PK: USER#<tenantId>#<userId>
SK: AFFINITY

runtimeSessionId
runtimeArn
endpoint
sessionGeneration
affinityVersion
leaseUntil
lastActiveAt
conversationStateRef
status
region
tenantClass
modelId
appVersion
```

用途：

- 定位用户当前亲和 session；
- 通过 `leaseUntil` 避免永久绑定；
- 通过 `affinityVersion` 防止迁移前的旧请求覆盖新状态；
- 通过 `sessionGeneration` 识别 microVM 已经换代；
- 通过 `conversationStateRef` 从外部存储恢复上下文。

映射租约应根据用户回访间隔设置。可以从 30–60 分钟开始调优，但必须受 session drain 和应用版本生命周期约束。DynamoDB TTL 只用于异步清理，业务判断必须直接检查 `leaseUntil`。

### 6.2 SessionPool

```text
PK: SESSION#<runtimeSessionId>
SK: META

runtimeArn
endpoint
region
schedulerStatus       # COLD | WARMING | ACTIVE | DRAINING | QUARANTINED
inflight
maxInflight           # 10
assignedUsers
maxAssignedUsers
generation
bootId
createdAt
lastActiveAt
environmentStartedAt
drainAt
hardExpiresAt
tenantClass
modelId
appVersion
schedulerShard
```

必须区分两个限制：

```text
maxInflight = 10       # 同时执行请求数
maxAssignedUsers       # 具有亲和映射的用户数，可以大于 10
```

`maxAssignedUsers` 需要根据每用户 workspace、磁盘文件和 metadata 占用实测确定，不能简单等于 10。

调度状态是应用层状态：

- `COLD`：逻辑 session 记录存在，但没有需要保持的 warm microVM；
- `WARMING`：正在通过首次调用触发执行环境；
- `ACTIVE`：可以接收请求；
- `DRAINING`：不接收新请求，等待现有请求完成；
- `QUARANTINED`：发生连续缺失 complete event 或其他异常，等待诊断或重建。

### 6.3 RequestLease

```text
PK: SESSION#<runtimeSessionId>
SK: LEASE#<requestId>

tenantId
userId
leaseToken
leaseUntil
heartbeatAt
createdAt
```

每个执行中请求都必须持有租约。长任务应周期性 heartbeat 续租；进程崩溃后，由 reconciler 识别过期租约并释放槽位。

不能依赖 DynamoDB TTL 准时删除租约。槽位判断和回收必须直接使用 `leaseUntil < now`。

### 6.4 Idempotency

```text
PK: REQUEST#<tenantId>#<requestId>
SK: IDEMPOTENCY

userId
status                 # CLAIMED | RUNNING | COMPLETED | FAILED
resultRef
errorCode
createdAt
expiresAt
```

用于阻止客户端重试、Router 重启或网络重放造成重复 Agent 执行。

### 6.5 池索引和分片

```text
GSI PK:
POOL#<region>#<tenantClass>#<modelId>#<appVersion>#<shard>

GSI SK:
<status>#<loadBucket>#<lastActiveAt>
```

不要维护单个全局池键。建议使用多个 scheduler shard，从随机 2–3 个 shard 中各取少量候选，再选择负载更低的 session，即 power-of-two choices，避免池索引热键和全表扫描。

## 7. 原子请求分配流程

### 7.1 已有亲和映射

1. 以 `requestId` 抢占 idempotency 记录；
2. 获取用户级分布式 lease，保证同一用户请求串行；
3. 读取 `UserAffinity`；
4. 验证 mapping 未过期，应用版本、模型和 tenant 等级匹配；
5. 对目标 session 执行原子 acquire；
6. 调用相同 `runtimeSessionId`；
7. 要求完整响应中必须出现最终 complete SSE event；
8. 持久化摘要、记忆和 artifact reference；
9. 在 `finally` 中释放 session lease 和用户 lease。

伪代码：

```python
async def route(request):
    claim_idempotency_key(request.request_id)
    user_lease = acquire_user_lease(request.tenant_id, request.user_id)

    try:
        affinity = get_user_affinity(request.tenant_id, request.user_id)
        candidates = [affinity.runtime_session_id]
        candidates.extend(select_pool_candidates(request, exclude=candidates))

        for session in candidates:
            lease = try_acquire_session(session, request)
            if lease is None:
                continue

            try:
                if session.id != affinity.runtime_session_id:
                    cas_remap_user(
                        expected_affinity_version=affinity.version,
                        new_session_id=session.id,
                    )

                response = invoke_agent_runtime(
                    runtime_arn=session.runtime_arn,
                    runtime_session_id=session.id,
                    payload={
                        "runtimeUserId": request.user_id,
                        "user_id": request.user_id,
                        "requestId": request.request_id,
                        "conversationStateRef": affinity.state_ref,
                    },
                )

                if not response.has_complete_sse_event:
                    raise IncompleteInvocationError()

                persist_durable_state(response)
                mark_request_completed(request.request_id, response)
                return response
            finally:
                release_session_lease(lease)

        enqueue_or_return_backpressure(request)
    finally:
        release_user_lease(user_lease)
```

### 7.2 Acquire 事务

概念上的 DynamoDB transaction：

```text
1. Update SessionPool
   SET inflight = inflight + 1,
       lastActiveAt = now
   IF schedulerStatus = ACTIVE
      AND inflight < maxInflight
      AND drainAt > now

2. Put RequestLease
   IF attribute_not_exists(PK)
```

对于首次建立 affinity 的用户，可在同一个事务中：

```text
1. 条件增加 SessionPool.inflight 和 assignedUsers
2. 创建 RequestLease
3. Put UserAffinity IF 不存在或已经过期
```

如果事务竞争失败，选择下一个候选重试，不能先写 affinity 再非原子增加 inflight。

### 7.3 Release 事务

```text
1. Delete RequestLease
   IF leaseToken = 当前调用持有的 leaseToken

2. Update SessionPool
   SET inflight = inflight - 1,
       lastActiveAt = now
```

释放必须验证 `leaseToken`，避免过期请求在槽位已被重新分配后，错误释放其他请求的 lease。重复 release 应保持幂等。

### 7.4 没有可用 session

1. 从多个随机 shard 查询候选；
2. 优先选择版本匹配、未 drain、目标负载以下且不接近换代时间的 session；
3. 如果所有候选都满载，先进入有界短队列；
4. queue wait 超过 SLA 后触发扩容；
5. 生成新的合法 `runtimeSessionId`；
6. 注册为 `WARMING`，通过首次健康调用触发 microVM 启动；
7. 健康检查成功后切换为 `ACTIVE`。

创建新 session 不等同于创建新的 Runtime ARN。绝大多数扩容只需要在现有 Runtime Endpoint 下使用新的 `runtimeSessionId`。

## 8. 同用户串行和超过 10 槽后的处理

本项目已有每用户 `asyncio.Lock`，但它只能保护单个 FastAPI 进程。生产路由层仍需提供用户级分布式串行语义。

推荐顺序：

1. 同一用户的请求首先串行化；
2. 原 affinity session 满载时短暂等待，例如 1–3 秒；
3. 超过等待 SLA 后：
   - 如果上下文已外部化，CAS 迁移到其他 session；
   - 如果仍依赖 microVM 本地 Claude transcript/resume，只能继续排队或返回背压；
4. 异步任务进入 SQS FIFO：

   ```text
   MessageGroupId = tenantId:userId
   ```

5. Router 通过 DynamoDB 控制 10 槽；
6. 容器内部设置 `MAX_PARALLEL_AGENTS=10`，作为最终硬保护。

不应在 session 内无限创建 Claude 子进程，也不应因为瞬时 10/10 就无条件新建大量 warm session。

## 9. 会话状态和迁移

当前实现将 Claude resume/session metadata 保存在 microVM 临时 workspace。microVM 终止或换代后，不能假定这些文件仍然存在，也不能假定 exact Claude SDK resume 可以迁移到其他 session。

生产状态应分层：

```text
短期执行状态
└── 当前 session 本地 workspace

长期用户记忆
├── AgentCore Memory
├── DynamoDB
└── S3

对话迁移载荷
├── 结构化摘要
├── 关键事实
├── 工具执行结果引用
└── artifact reference
```

App 启动时应生成新的 `bootId`。Router 或响应处理器发现同一 `runtimeSessionId` 返回了新 `bootId` 时，应：

1. 增加 `SessionPool.generation`；
2. 将旧本地状态视为失效；
3. 使用外部持久状态重建用户上下文；
4. 使用 `affinityVersion` 和 `sessionGeneration` 阻止旧请求写回。

## 10. 生命周期管理

截至 2026-08-20，官方 microVM 生命周期配置为：

- `idleRuntimeSessionTimeout` 默认 900 秒；
- microVM 可配置范围 60–28800 秒；
- `maxLifetime` 默认 28800 秒；
- microVM 的 `maxLifetime` 上限为 28800 秒，即 8 小时；
- 到达 max lifetime 后 instance 会初始化终止，逻辑 session 可由新 instance 承接；
- 终止过程可能持续最多约 15 秒。

推荐策略：

- 在当前 execution environment 运行约 7 小时 15–30 分后进入 `DRAINING`；
- DRAINING session 不接收新请求；
- `inflight=0` 后主动 stop 或切换为 COLD；
- 对低流量 session，在空闲 5–10 分钟后主动 stop；
- 保留 1–2 个 warm session 控制冷启动延迟；
- 不要通过高频无意义 ping 保持所有 session 存活。

对于映射到 COLD session 的用户，下一次请求可以触发新 microVM，但必须从外部状态恢复上下文。

## 11. 容量规划

设：

```text
C = 10        # 单 session 硬并发上限
ρ = 0.7       # 目标利用率
A             # 峰值同时执行请求数
```

则：

```text
每 session 目标槽位 = C × ρ = 7
需要的 active sessions = ceil(A / 7)
```

使用 Little’s Law 估算 `A`：

```text
A ≈ 请求到达率 λ × 平均执行时间 W
```

### 11.1 5000 用户、60 秒任务示例

假设：

- 5000 个注册用户；
- 峰值活跃比例 5%，即 250 人；
- 每个活跃用户平均每 5 分钟请求一次；
- 平均任务时长 60 秒。

计算：

```text
λ = 250 / 300 ≈ 0.833 request/s
A = 0.833 × 60 ≈ 50
active sessions = ceil(50 / 7) = 8
```

建议容量：

```text
8 个 active session
1–2 个 warm session
若干仅存在于 DynamoDB 的 COLD 逻辑 session
```

### 11.2 5000 用户、360 秒任务示例

相同请求到达率下：

```text
A = 0.833 × 360 ≈ 300
active sessions = ceil(300 / 7) = 43
```

可见实际容量主要取决于：

- 峰值请求到达率；
- 平均任务时长；
- 任务内存峰值；
- 模型 RPM/TPM；
- 冷启动和排队 SLA；
- warm/idle 策略。

注册用户总数只影响 affinity 数据量，不直接决定活跃 microVM 数量。

## 12. 弹性策略

### 12.1 扩容

满足任一条件并持续多个观测窗口时扩容：

- 平均有效占用超过 7/10；
- 可用目标槽位不足；
- queue wait p95 超过交互 SLA；
- warm session 数低于预测需求；
- 预测流量将在短时间内显著升高。

推荐公式：

```text
predictedConcurrent = 当前 inflight + 排队请求 + 短期预测增量
requiredSessions = ceil(predictedConcurrent / 7)
```

### 12.2 缩容

session 同时满足以下条件时可缩容：

- 没有有效 RequestLease；
- `inflight=0`；
- 超过 idle 阈值；
- 当前 warm 数高于最小 warm pool；
- 没有必须保留在本地的不可迁移状态。

长期状态外部化后，即使仍有 UserAffinity，也可以停止当前 microVM，并将逻辑 session 标记为 COLD。

### 12.3 Warm pool

初始建议：

```text
warmSessions = max(1–2, predictedActiveSessions × 5%–10%)
```

最终应根据实际冷启动 p95、idle GB-seconds 成本和交互 SLA 调整。

## 13. 成本模型和经济性

AgentCore microVM 按 active resource consumption 计费，而不是按固定实例规格计费。官方定价说明：

- CPU 按每秒实际消费计算；
- I/O wait 时无 CPU 消耗则没有对应 CPU 费用；
- 内存按照每秒峰值内存消费计算；
- 最低计费粒度为 1 秒；
- 计费包含系统开销；
- 内存最低计费量为 128 MB；
- ECR、网络和外部存储单独计费。

总成本应按以下模型观测：

```text
Runtime 成本
= CPU-seconds × CPU 单价
+ Σ(每秒峰值内存 GB) × Memory 单价
+ 模型 input/output/cache token
+ 网络
+ ECR/S3
+ DynamoDB/SQS
```

核心优化指标应为：

```text
cost per successful request
cost per tenant
cost per agent task minute
```

不能仅使用“活跃 session 数最少”作为经济性指标。更高并发可能减少重复启动和基础进程开销，也可能提高每秒峰值内存、排队和失败重试成本。

建议：

1. 分别测量并发 1、2、4、7、10 下的 CPU-seconds、GB-seconds 和成功请求成本；
2. 默认把调度目标设为 6–7，硬上限设为 10；
3. 突发请求先进入短队列，queue p95 超过 SLA 再扩容；
4. 简单请求路由到低成本模型，复杂任务再使用高能力模型；
5. 使用摘要、prompt caching 和 artifact reference，减少重复上下文 token；
6. 主动停止无负载 microVM，但只保留满足 SLA 所需的小 warm pool；
7. 按 tenant、model、session 和任务类型统计成本，而不是只看账户总账单。

## 14. 故障恢复

### 14.1 Lease 漂移

- acquire 后 Router 崩溃：lease 到期后由 reconciler 回收；
- 长任务：定期 heartbeat 延长 lease；
- release 重试：使用 `leaseToken` 保证幂等；
- `inflight` 与有效 lease 数不一致：reconciler 修正计数或隔离 session。

### 14.2 AgentCore session 错误

- 409 provisioning/teardown conflict：有限指数退避并加入 jitter；
- session/environment 终止：切换到 WARMING/COLD 并重新准备；
- Runtime 或 endpoint 不存在：标记 session 无效，清除旧 affinity 并重新分配；
- 连续健康检查失败：转入 `QUARANTINED`。

### 14.3 HTTP 200 但没有 complete event

本项目长程 40 并发已经验证：foundation HTTP 200 不等于 Agent 请求成功。

必须：

- 将缺少最终 complete SSE event 视为失败；
- 不写入成功状态；
- 根据 idempotency 规则决定是否重试；
- 对连续出现该错误的 session 增加 strike；
- 达到阈值后 drain 或 quarantine；
- 避免立即在同一高压 session 无限重试。

### 14.4 Generation 和旧请求写回

所有请求携带：

- `requestId`；
- `affinityVersion`；
- `sessionGeneration`；
- 外部状态版本。

完成写回时执行条件更新。若用户已迁移或 generation 已变化，旧请求只能写入自己的审计结果，不能覆盖当前对话状态。

## 15. 安全分池策略

建议 pool key 至少包含：

```text
region + tenantClass + modelId + appVersion
```

根据安全要求进一步选择：

- 同一组织内、相同信任级别用户：可共享 session；
- 不同外部 tenant：分别建池；
- 运行任意用户代码或 shell：优先每 tenant 或每用户独立 session；
- 高价值或高敏感任务：使用专属池，不与普通任务竞争资源。

`runtimeUserId` 与 payload `user_id` 的一致性检查、每用户 HOME/workspace、路径守卫和每用户锁必须继续保留。

## 16. 观测指标

至少记录：

- active/warm/cold/draining/quarantined session 数；
- 每 session inflight 和有效 lease 数；
- assigned users per session；
- queue wait p50/p95/p99；
- request duration p50/p95/p99；
- 冷启动和 warm 命中率；
- affinity remap 次数；
- bootId/generation 变化次数；
- stop 成功率；
- 409 和 throttling 次数；
- missing complete SSE 次数；
- 每用户、tenant、模型的 token 消耗；
- CPU-seconds、GB-seconds 和成功请求成本；
- session occupancy 和 idle 比例。

扩缩容应主要使用 queue wait、目标槽位和预测并发，不能只依赖 CPU 百分比。

## 17. 当前 Demo 的生产化改造清单

1. 增加 Stateless Session Router；
2. 增加 DynamoDB UserAffinity、SessionPool、RequestLease 和 Idempotency 数据；
3. 将容器 `MAX_PARALLEL_AGENTS` 设置为 10；
4. 增加用户级分布式 lease 或 SQS FIFO；
5. 将长期记忆、摘要和 artifact 外部化；
6. 增加 `bootId`、generation 和 affinity version；
7. 增加 scaler、drainer 和 lease reconciler；
8. 增加完整 SSE complete 校验和异常 session 隔离；
9. 增加 tenant/model/session 级成本指标；
10. 保持当前双向 `ClaudeSDKClient` 实现，不能退回一次性 `query()`，否则 Python function hooks 不执行；
11. 保持 `InvokeAgentRuntimeCommand` shell 操作显式使用 `/bin/bash -c` 的已验证契约。

## 18. 官方配额和文档基线

截至 2026-08-20，官方资料显示：

- active session workloads 默认配额：
  - `us-east-1` 和 `us-west-2`：每账户 5000；
  - 其他支持 Region：每账户 2500；
  - 可通过 Service Quotas 申请提升；
- 单 Runtime session 最大硬件分配：2 vCPU / 8 GB；
- microVM 最大 lifetime：8 小时；
- API 调用速率、新 session 创建速率和模型 RPM/TPM 仍是独立约束，投产前应以目标账户和 Region 的 Service Quotas 为准。

对本文示例中的 8–43 个 active session，active session 配额通常不是第一瓶颈。模型 RPM/TPM、Runtime 请求速率、session 创建速率和入口层连接能力需要一起容量规划。

## 19. 参考资料

- [Configure Amazon Bedrock AgentCore lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)
- [Quotas for Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
- [Amazon Bedrock AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [本项目 microVM 并发实测报告](results/REPORT.md)
