# 25 · Patch Manager × AgentCore 中国区实测

客户 IT Infra 目前用 Patch Manager、自动化脚本和 Kiro 诊断来跑补丁批次（每批 75~100 台），值守靠人。本目录用中国区（宁夏 `cn-northwest-1`）的两个真实账号，跑通"Patch Manager 负责批量、AgentCore 负责诊断异常实例"的完整链路，逐项回答客户的 7 个问题。

- 面向客户的方案说明见 [SOLUTION.zh.html](SOLUTION.zh.html)；架构设计文档见 [ARCHITECTURE.zh.html](ARCHITECTURE.zh.html)。
- 测试日期：2026-11-02（UTC）。账号：运维中心账号（同时作为业务账号 A）、业务账号 B，均在宁夏区；模型 API key 由一个全球区账号签发。账号 ID 与本地 AWS profile 写在 `common/accounts.json`（不入库，参考 `common/accounts.example.json`）。
- 目标机：5 台 t4g.micro Amazon Linux 2023（t4g.nano 的 512 MiB 内存下 dnf 可能 OOM，会引入计划外故障）。

## 1. 结论速览

| 客户问题 | 结论 | 实测证据 |
|---|---|---|
| Q1 同一个 Runtime 跨多账号/多 VPC 执行，权限和网络怎么配？Runtime endpoint 支持内网访问吗？ | Runtime 用 **VPC 模式 + IAM(SigV4) 入站**，资源策略拒绝所有不经运维 VPC `bedrock-agentcore` VPCE 的调用（`bedrock-agentcore:*`）。跨账号走 Hub-Spoke：经 STS VPCE AssumeRole 进入各账号 `PatchAgentSpokeRole`，**只调 SSM/EC2 API，不需要打通业务 VPC 网络**。人工经 IAM Identity Center permission set 角色带 `runtimeUserId` 调用。**支持内网访问。** | §3：公网 Invoke / StopRuntimeSession 均被拒，VPC 内成功；10 项 IAM 正反向用例全部符合预期 |
| Q2 microVM 最长 8h，75~100 台怎么分 Target 并行？单台出问题怎么不阻塞？单会话还是多会话？ | **批量交给 Patch Manager**（波次、≤50 台/命令分片、MaxConcurrency/MaxErrors），不受 8h 约束。**每台失败实例一个 session**，**按实例流水线**：每 30s 的 tick 发现失败就立即派发，`maxAgents` 限流。单台兜底：PingStatus + 根盘余量预检、单台运行截止（从 InProgress 起算）+ 排队硬上限、Agent 超时 StopRuntimeSession、派发未认领自动重排、单台 API 出错隔离、批次 Abort 兜底。 | §4：批次 4~6 中失败实例在失败后 ≤36s 开始诊断，挂起实例不影响其他实例；突发 100×50 new session 全部成功 |
| Q3 自建 memory 怎么配？ | **中国区没有 AgentCore Memory，也没有 OpenSearch Serverless**。STM = DynamoDB（Strands SessionRepository，可人工接管恢复）；LTM = **OpenSearch VPC 域 + 全球区 Titan Text Embeddings V2**，**按 AgentCore Memory 内置策略实现**：semantic（extraction → consolidation Add/Update/Skip）、episodic（episode → reflection add/update + confidence）。代码层硬规则：只从 FIXED 学、verified 不被覆盖、关键词必须出现在补丁输出且非通用、记忆读写都去注入。 | §5：两个 repo signature 余弦 0.80 被合并；10 条旧 lesson 合并为 7 条 reflection；批次 4 起 verified 条目只收到修改提案 |
| Q4 类似 Guardrails 的功能怎么做？ | **中国区没有 Bedrock Guardrails / AgentCore Policy**。6 层自建：输入净化、工具层（无 instance 参数、文档 allowlist、配额、无豁免的工具预算）、结果校验（FIXED 必须有真实重跑成功）、IAM（session tag 绑单实例、无 reboot/stop/CancelCommand、只能经 VPCE）、记忆隔离（召回内容围栏 + 去注入）、人工审批；全程审计。 | §6：注入文本每批剔除 3 行；跨实例、RunShellScript、reboot、stop 全部被 IAM 拒绝 |
| Q5 关键组件代码 | §10 目录说明；SOLUTION.zh.html 有精选片段。 | — |
| Q6 ECR 方式部署 / 运行方式 | arm64 容器 → 中国区 ECR（scanOnPush）→ `update_agent_runtime` 生成不可变版本，DEFAULT endpoint 跟随最新；运行在 VPC 私有子网，HTTP 协议 8080，lifecycle idle 600s / maxLifetime 7200s，密钥不进镜像/环境变量。 | §7：本次共 14 个版本全部 READY |
| Q7 会话隔离 | 每个 `runtimeSessionId` 一个独立 microVM；再叠加 session=批次+实例、入口校验实例归属、IAM 单实例绑定、STM 按 session 分区、按操作人签发 workload token。 | §8：A/B 两会话文件与内存互不可见；StopRuntimeSession 后 A 换新 microVM，对话从 DynamoDB 恢复 |

## 2. 关键设计要点（均经中国区实测）

| # | 设计要点 | 依据 |
|---|---|---|
| 1 | 记忆与护栏自建 | 宁夏和北京调用 `ListMemories`、`ListPolicyEngines`、`ListEvaluators` 都返回 `Unable to determine service/operation name`，AgentCore Memory / Policy / Evaluations 未提供 |
| 2 | Runtime 子网配 NAT（或经 IDC 出口代理），只用于全球区模型 | 中国区没有 Bedrock endpoint；测试使用全球区 Bedrock 上的 Kimi K3（`global.moonshotai.kimi-k3`）和 Titan Text Embeddings V2，生产以实际选用的模型为准 |
| 3 | 编排层调用 Runtime 固定带 `runtimeUserId`（需 `InvokeAgentRuntimeForUser`） | Runtime 自动创建的 workload identity 由服务管理；SigV4 调用不带 `runtimeUserId` 时容器拿不到 workload token，也就读不到 token vault 里的 API key |
| 4 | Spoke 信任策略中 `sts:TagSession` 单独一条语句、不带 ExternalId 条件 | 鉴权 `sts:TagSession` 时请求上下文里没有 `sts:ExternalId` |
| 5 | 跨账号信任显式列出账号 ARN | 业务账号不一定在同一个 Organization，`aws:ResourceOrgID` 不适用 |
| 6 | 人工入口：IAM Identity Center → permission set 角色 → SigV4 + `runtimeUserId`；操作人放进 payload | cn-northwest-1 没有 Cognito；`runtimeUserId` 不会以 header 传进容器（容器只收到 `baggage` 和 `workloadaccesstoken`） |
| 7 | 补丁前做 PingStatus + 根盘余量预检 | 离线主机的 SendCommand 会一直 Pending；根盘 100% 时 SSM Agent 无法执行任何文档 |
| 8 | 故障复现需要真实的待装补丁 | 没有 Missing 包时 RunPatchBaseline 不发起 dnf 事务，rpm 锁 / 磁盘类故障不会触发（测试中通过降级 expat/less/libxml2 制造安全更新） |
| 9 | LTM 用 OpenSearch Service 托管域（VPC 访问） | cn-northwest-1 没有 OpenSearch Serverless（`aoss` endpoint 无法连接） |
| 10 | Agent 超时：先把实例写成终态，再 StopRuntimeSession | microVM 终止需要数秒，期间仍可能有写入；护栏的"实例已终态"检查阻止后续动作 |

## 3. 权限与网络（Q1）

### 3.1 拓扑

```
运维账号 (cn-northwest-1)
  patchdemo-ops-vpc 10.40.0.0/16
    私有子网 az1 / az2 ── Runtime ENI (VPC 模式) + Orchestrator Lambda ENI   SG: 只出 443，无入站
    VPCE(interface, private DNS): bedrock-agentcore, sts(cn.com.*), ssm, ec2(cn.com.*), logs
    VPCE(gateway): dynamodb, s3
    OpenSearch 域 patchdemo-ltm (VPC 访问, SG 只放行 agent SG 的 443, 资源策略只允许 Runtime 角色)
    NAT ── 只为调全球区 Bedrock（Kimi K3 + Titan Embeddings）
  Step Functions ─▶ Lambda(VPC) ─▶ bedrock-agentcore VPCE ─▶ Runtime (每台失败实例 1 个 session)
                                    └▶ sts VPCE ─▶ AssumeRole PatchAgentSpokeRole@各业务账号 ─▶ ssm/ec2 VPCE
业务账号
  PatchAgentSpokeRole   目标机在各自 VPC，SSM Agent 主动连回 SSM，不与运维 VPC 打通网络
```

### 3.2 关键策略（`infra/02_iam.py`、`infra/06_runtime.py`、`infra/08_opensearch.py`）

- **Runtime 资源策略**（Runtime 和 DEFAULT endpoint 两处）：Allow 编排角色和值班角色 `InvokeAgentRuntime(ForUser)`；Deny `Principal:*` 的 **`bedrock-agentcore:*`**，条件 `aws:SourceVpce != vpce-bedrock-agentcore`（覆盖调用、停止会话等全部数据面操作）。
- **Spoke 信任策略**：AssumeRole 必须带 ExternalId、经运维 STS VPCE、带 `TargetInstance=i-*` session tag；TagSession 单独一条（§2 第 4 条）。
- **Spoke 权限**：
  - `ssm:SendCommand` 只允许 `AWS-RunPatchBaseline`、`PatchDiagnose-Collect`、`PatchRemediate-*`（Sid `AllowedDocsOnly`）；
  - Agent 会话只能作用于 `instance/${aws:PrincipalTag/TargetInstance}`；
  - `ssm:CancelCommand` 只能 `*` 资源，**只给编排会话**（Null `aws:PrincipalTag/TargetInstance`），Agent 会话不能取消命令；
  - 兜底 Deny：不经运维 VPCE 的调用一律拒绝。
- **残余风险（写进代码注释）**：`ListCommandInvocations` / `GetCommandInvocation` 只支持 `*`，Agent 会话能读同账号其他实例的命令输出；IAM 无法约束文档参数，`RebootOption=NoReboot` 由代码写死、工具不暴露该参数 —— 生产建议用一个包装文档固定参数，并从 allowlist 中去掉直接调用 `AWS-RunPatchBaseline`。
- **OpenSearch**：无公网 endpoint；SG 只放行 agent SG 的 443；访问策略只允许 `PatchAgentRuntimeRole`；加密静态 + 节点间 TLS + HTTPS-only。未开 FGAC，同账号有 `es:*` 的管理员身份也能访问（生产建议开 FGAC 并只映射 Runtime 角色）。

### 3.3 实测结果（`scenarios/verify_access.py` + 独立验证复测）

| 用例 | 预期 | 实测 |
|---|---|---|
| 笔记本（公网，admin 角色）调用 InvokeAgentRuntime / StopRuntimeSession | 拒绝 | `AccessDeniedException`（explicit deny in resource-based policy） |
| VPC 内 Lambda 经 VPCE 调用 Runtime | 允许 | ✅ |
| 受信任的编排角色 + 正确 ExternalId，经 NAT 访问 cn-north-1 公网 STS 做 AssumeRole | 拒绝 | `AccessDenied` |
| 经 VPCE 合法拿到的 spoke 凭证，拿到 VPCE 之外（公网 endpoint）使用 | 拒绝 | `UnauthorizedOperation` |
| Runtime 角色 AssumeRole 时不带 TargetInstance tag | 拒绝 | `AccessDenied` |
| ExternalId 错误 | 拒绝 | `AccessDenied` |
| 绑定 A 机的凭证，对 B 机执行 allowlist 文档 | 拒绝 | `AccessDeniedException` |
| 绑定 A 机的凭证，对 A 机执行 `AWS-RunShellScript` | 拒绝 | `AccessDeniedException` |
| 绑定 A 机的凭证，对 A 机 `RebootInstances` / `StopInstances`（DryRun） | 拒绝 | `UnauthorizedOperation` |
| Agent 会话 `CancelCommand`（策略模拟） | 拒绝 | implicitDeny；编排会话 allowed |
| 绑定 A 机的凭证，对 A 机执行 allowlist 文档 | 允许 | ✅ |
| OpenSearch 域从 VPC 外访问 | 不可达 | DNS 解析为 10.40.x.x 私网地址，连接超时；无公网 endpoint |
| Runtime 经 NAT 调 Kimi K3 / Titan Embeddings（API key 来自 AgentCore Identity token vault） | 允许 | ✅ |
| 值班人员接管（PatchOnCallRole + `runtimeUserId`，经 VPCE） | 允许 | ✅ |

## 4. Session 设计与批次编排（Q2）

**推荐多会话，每台失败实例一个会话**：批量执行交给 Patch Manager（确定性高、无 8h 限制）；Agent 只处理失败的 5%~10%，每台上下文干净；microVM 级别隔离，任何一台崩溃、挂起或超时只影响它自己。只有值班人员接手同一台机器时复用同一个 `runtimeSessionId`。

### 4.1 编排流程（`orchestrator/statemachine.json` + `handler.py`）

```
Init  按 PatchGroup tag 跨账号分页发现实例 → DynamoDB PENDING；PatchWave 非 canary/main 的归入 main 并告警
 └─ Waves Map (MaxConcurrency=1): canary → main
      SendPatch: HALTED? → PingStatus 预检(分页) → 根盘余量预检(PatchDiagnose-Collect Check=disk)
                 → AWS-RunPatchBaseline Install/NoReboot（≤50 台/命令分片，MaxConcurrency/MaxErrors）
      [Wait 30s → Tick(wave)]* 直到本波无 PATCHING
         Tick = ① 轮询 PATCHING：首次 InProgress 记 inprogress_at；运行超 patchTimeout 或排队超 waveHardTimeout → 只取消这一台
                ② 新出现的 PATCH_FAILED / PATCH_TIMEOUT / UNDELIVERABLE / PRECHECK_FAILED 立即派发（DIAGNOSING ≤ maxAgents，按失败时间排队）
                ③ Agent 超 agentTimeout → 写 NEEDS_HUMAN + StopRuntimeSession；派发后 dispatchGrace 内未认领 → 重排（3 次后转人工）
                ④ 每台的处理独立 try/except，连续 5 次出错转人工，tick 本身不失败
      CanaryGate: canary 失败率 > 0 → Halt（批次置 HALTED，main 波次全部 SKIPPED_HALTED，canary 照常诊断）
 └─ [Wait 30s → Drain=Tick(all)]* 直到没有排队 / 诊断中的实例
 └─ Report → HALTED? Fail(CanaryCircuitBreaker) : Done
 任意一步出错或超过 batchTimeout → Abort（非终态实例转 NEEDS_HUMAN、停止 Agent、取消挂起命令）→ Report → Fail(BatchAborted)
```

输入参数（`scenarios/run_batch.py`）：`patchTimeout`(600) `waveHardTimeout`(3×patchTimeout) `agentTimeout`(2400) `maxAgents`(10) `minFreePct`(15) `minFreeMB`(500) `dispatchGrace`(180) `batchTimeout`(14400) `remediationLimit`。

Runtime 侧（`agent/app.py`）：入口立即返回 `accepted`，诊断用 `add_async_task` 放到后台线程（`/ping` 返回 HealthyBusy，session 不被 idle 回收），进度实时写 DynamoDB；`lifecycleConfiguration` idle 600s / maxLifetime 7200s。

### 4.2 实测批次

| 实例 | 故障 | 批次 1 | 批次 2 | 批次 3 | 批次 4（流水线 + 预检 + OpenSearch LTM） | 批次 5（独立验证修复后回归） | 批次 6（最终版本） |
|---|---|---|---|---|---|---|---|
| pd-healthy (canary) | 无 | PATCH_OK | PATCH_OK | PATCH_OK | PATCH_OK | PATCH_OK | PATCH_OK |
| pd-disk-full | 根盘满（批次 1~3 剩 30MB；4 起剩 300MB / 3%） | NEEDS_HUMAN | NEEDS_HUMAN | NEEDS_HUMAN | **FIXED**：预检拦截未发补丁 → FreeDisk → 重跑成功（6 次 / 114s） | **FIXED** 7 次 / 128s | **FIXED** 8 次 / 150s；另提审批单建议永久清理 |
| pd-repo-broken | 第三方 repo 不可达 + prompt 注入 | FIXED 6 次 / 115s | FIXED（截止取消后）7 次 / 410s | FIXED 6 次 | **FIXED** 6 次 / 429s（截止取消后立即诊断） | **FIXED** 6 次 / 469s（新截止逻辑：InProgress 后超时） | **FIXED** 7 次 / 462s；只召回 `repo_unreachable` |
| pd-rpm-lock | 非包管理进程持有 rpm 锁 | PATCH_OK（无待装补丁） | FIXED 7 次 / 129s | FIXED 6 次 / 125s | **FIXED** 6 次 / 133s（失败→派发 0s） | 锁已释放、重跑成功，但 Agent 因"第 4 次复发"报 NEEDS_HUMAN（系统提示随后明确：重跑成功即 FIXED，复发另提审批单） | **FIXED** 7 次 / 138s + 永久修复审批单 |
| pd-ssm-offline | SSM Agent 停止 | NEEDS_HUMAN 5 次 | NEEDS_HUMAN 5 次 | 预检 UNDELIVERABLE，6 次 | NEEDS_HUMAN 4 次 / 30s，审批单 + 识别"连续第 4 批" | NEEDS_HUMAN 4 次 / 29s | NEEDS_HUMAN 4 次 / 25s |

批次 4 时间线（秒）：21 canary 开始 → 102 预检完成（disk-full PRECHECK_FAILED、ssm-offline UNDELIVERABLE）→ 122 main 开始 → **143 disk-full 与 ssm-offline 已在诊断（main 仍在打补丁）** → 245 rpm-lock 失败即诊断 → 265 disk-full FIXED → 367 rpm-lock FIXED → 553 repo-broken 截止取消后诊断 → 964 全部完成。旧编排要等整个波次结束（≈553s）才开始任何诊断。

批次 5 时间线与批次 4 基本一致（143s 已有 2 台在诊断、main 仍在打补丁）；全批 1128s，执行 SUCCEEDED，无 warnings、无 abort。

批次 6：FIXED 3 / PATCH_OK 1 / NEEDS_HUMAN 1（ssm-offline，符合预期），全批 1129s，SUCCEEDED。REPORT 会等每个 Agent 写完指标和记忆提取结果再生成（最多 300s）。

- **挂起**：repo-broken 补丁一直 InProgress，只对这一台 CancelCommand，其他实例不受影响。
- **Agent 超时**：`agentTimeout=8` 的测试中实例被标 NEEDS_HUMAN 并 StopRuntimeSession（§2 第 10 条）。
- **new session 突发**（`scenarios/session_burst.py`，VPC 内 Lambda 发起）：

  | 新建 session × 并发 | 成功 | 重试 | P50 | 最大 |
  |---|---|---|---|---|
  | 20 × 10 | 20/20 | 0 | 2.06 秒 | 2.93 秒 |
  | 40 × 20 | 40/40 | 0 | 1.76 秒 | 2.18 秒 |
  | 100 × 50 | 100/100 | 0 | 1.68 秒 | 2.39 秒 |

  本账号未触发限流。`24-China-regions` 在另一个账号 100 并发时出现过 `New session creation rate exceeded`，退避重试要保留；`maxAgents` 建议 10~20。

## 5. 自建记忆（Q3）

STM 在 DynamoDB，LTM 在 OpenSearch。记忆实现为独立的 Python 包 [`agentmem/`](agentmem/README.md)（存储、召回、LLM 策略步骤，提示词改编自 AgentCore Memory 默认提示词）；补丁 Agent 一侧只有领域配置 `agent/memory_profile.py`（`PATCH_PROFILE`）。每条记录带 AgentCore 风格的 `namespace`，便于将来迁移到托管服务。

### 5.1 模块边界：agentmem 库

| 约定 | 做法 |
|---|---|
| 依赖单向 | `agentmem` 从不 import Agent 代码（app / tools / guardrails / state）；Agent 只经公开接口（`Memory`、`MemoryConfig`、`Profile`、`agentmem.llm.strands_llm`）使用它。只有 `stm.py`、`llm.py` 依赖 strands-agents（可选依赖 `agentmem[strands]`） |
| 领域差异只在 `Profile` | 提示词措辞（domain、success_criterion、keyword_guidance、recall_preamble…）、文本规则（signal_extra、generic_extra、injection——补丁 Agent 直接复用护栏的 `_INJECTION`）、学习策略（`learn_statuses=("FIXED",)`、`evidence_tools`）、namespace 根（`name="patch"` → `/patch/knowledge` 等） |
| 基础设施与阈值只在 `MemoryConfig` | OpenSearch 端点 / 索引、向量模型、STM 表与 TTL、全部召回与合并阈值；`MemoryConfig.from_env()` 读 `OS_ENDPOINT`、`OS_INDEX`、`MEMORY_TABLE`、`EMBED_MODEL_ID`、`KB_GATE` 等 |
| 依赖可注入 | `store`（`Store` 协议，生产为 `OpenSearchStore`）、`embedder`（任意 `text -> list[float]`，生产为 `BedrockEmbedder`，SigV4 或 API key bearer）、`llm`（`llm(system, prompt, schema) -> schema 实例`，生产为 `strands_llm`）；STM 的 `stm_repo` 也可替换 |
| 离线单元测试 | `agentmem.testing` 提供 `InMemoryStore`、`HashEmbedder`，测试用脚本化 LLM；17 个用例不需要 OpenSearch / Bedrock / 网络：`cd agentmem && python -m pytest -q tests` |

Agent 侧接线（`agent/app.py`）：模块级 `MEM = Memory(MemoryConfig.from_env(), PATCH_PROFILE, api_key_fn=...)`（向量化用请求线程取到的全球区 API key）；诊断前 `MEM.recall`；`search_memory` 工具由 `build_tools(..., memory_search=MEM.search)` 注入，`agent/tools.py` 不 import 记忆库；STM 为 `MEM.session_repository`；会话结束 `MEM.process_session`；`mode=memory_admin` 的通用操作交给 `MEM.admin`（导入任务与任务状态由 Agent 自己处理）。

另一个 Agent 复用时只需写自己的 `Profile`，并给它独立的索引或 namespace 根：

```python
from agentmem import Memory, MemoryConfig, Profile
from agentmem.llm import strands_llm

PROFILE = Profile(name="dbops", agent_id="dbops-agent", subject_noun="database", domain="a database on-call system",
                  kb_topic="database troubleshooting", success_criterion="the health check passed after the fix",
                  learn_statuses=("RESOLVED",), evidence_tools=("get_error_log",),
                  signal_extra="deadlock|replication lag|too many connections")
MEM = Memory(MemoryConfig.from_env(os_index="dbops-ltm"), PROFILE)

recalled, meta = MEM.recall(subject=db_id, query=error_text)     # 放进 system prompt
agent = Agent(..., session_manager=RepositorySessionManager(session_id=sid, session_repository=MEM.session_repository))
MEM.process_session(strands_llm(lambda: BedrockModel(model_id=EXTRACT_MODEL_ID)),
                    session_id=sid, subject=db_id, batch_id=run_id, result={"status": "RESOLVED", "signature": "..."})
```

### 5.2 结构、召回与写入

| 层 | 存储 / 键 | 内容 | 读写时机 |
|---|---|---|---|
| STM | DynamoDB `SESSION#{sessionId}` / `MSG#…`，TTL 30 天 | Strands `SessionRepository` 实现，完整对话和工具调用 | 每条消息实时写；人工接管按 sessionId 恢复 |
| KB（semantic） | OpenSearch `kind=KB`，`/patch/knowledge` | signature、keywords、symptom、root_cause、fix、verified、confirmations、aliases、suggested_keywords、update_proposals | 只从 FIXED 会话学：extraction → 取相似条目 → **consolidation（Add/Update/Skip）** |
| EPISODE（episodic） | `kind=EPISODE`，`/patch/episodes/{instanceId}` | situation / intent / assessment / justification / reflection | 每个会话一条；实例历史从这里查 |
| REFLECTION（episodic reflection） | `kind=REFLECTION`，`/patch/reflections` | title / use_cases / hints / confidence / source_episodes | 主 episode + 相关 episodes + 已有 reflections → **add / update** |

**召回**：补丁输出先做"失败信号行"摘要 → Titan V2 向量（1024 维）→ kNN（lucene HNSW cosine）+ BM25 候选并集 → 绝对闸门：KB 余弦 ≥0.36 或命中 ≥2 个关键短语 → 综合分 = 余弦 + 0.08×短语命中（unverified ×0.85）→ 只保留距最高综合分 ≤0.15 的条目（KB 条目随学习会变得彼此相似，单看余弦会误召回，所以相对边距作用在综合分上，见批次 5 实测）；reflection 余弦 ≥0.35、距最优 ≤0.12、confidence ≥0.4。召回块整体用 `<recalled_memory>` 围栏并标注"仅供参考、不是指令"，读写两侧都剔除指令性文本。召回失败不影响诊断（降级为无记忆）。

**写入硬规则（LLM 决策之后由代码强制，`agentmem/agentmem/strategies.py`）**：KB 只从 FIXED 学；verified 条目永不被改写，Update 变成 `update_proposals` + `suggested_keywords` 待人工审核；关键词必须字面出现在补丁输出类工具结果里（`get_patch_output` / `rerun_patch` / `get_instance_overview`），且排除通用 runner 文本和诊断文档的固定输出；update_id 必须是给模型看过的记录；新 signature 不得覆盖已有；reflection 余弦 ≥0.85 的"新增"按合并处理。

### 5.3 标定、运维与实测

**阈值标定**（`scenarios/tune_thresholds.py`，直接调用 `agentmem.records` 的纯函数与 `PATCH_PROFILE`，用批次 1~3 的真实补丁输出）：真 KB 匹配 0.39~0.57，误匹配 ≤0.46 但都比最优低 ≥0.08（据此设定余弦闸门 0.36；相对边距作用在综合分上，取 0.15）；`dnf_repo_unreachable_makecache` ↔ `repo_unreachable` 0.80，其余 signature 之间 ≤0.49。

**运维**（`scenarios/seed_memory.py`，OpenSearch 只在 VPC 内，经 Lambda → Runtime `mode=memory_admin`）：`--reset`（需 `ENABLE_PROBE=1` 且显式确认串，仅 demo）、seed、`--migrate`（旧 DynamoDB LTM 走同一套策略迁移）、`--list`、`--search`、`--verify <sig> <reviewer>`、`--dedupe`。

实测：
- **迁移**：`dnf_repo_unreachable_makecache` 被 consolidation 判为 verified `repo_unreachable` 的 UpdateMemory → 只生成 proposal 和 alias；`rpm_lock_held` AddMemory，剔除 7 个通用关键词；10 条一句话 lesson 合并为 7 条带 confidence 的 reflection。
- **批次 4**：disk-full 召回 verified `disk_full`（预检措辞关键短语命中），学到新 reflection"预检拦截的低磁盘 → 一次诊断 → FreeDisk → 重跑"；`disk_full`、`repo_unreachable` 各收到 1 条 proposal，内容未变；`rpm_lock_held` 被更新、confirmations=1。
- **人工接管**：批次 1 中 pd-ssm-offline 的 microVM 已 idle 回收，值班角色用同一个 sessionId 接管，`restored_messages=10`。
- **清理**：3 条"命令输出不可信"的近似 reflection（两两余弦 0.75~0.77，不同主题之间最高 0.67）用 `--dedupe 0.74` 合并为 1 条（现共 7 条）；`rpm_lock_held` 按新规则剔除 2 个诊断文档固定输出关键词。
- **批次 5**：repo-broken 查询误召回 `disk_full`(0.505) 和 `rpm_lock_held`(0.48)（与 `repo_unreachable` 0.536 太接近）→ 使用综合分边距后，三类典型查询都只召回 1 条且正确。
- **批次 6**：关键词落地规则剔除了全部 13 个诊断文档 / 实例特有字符串；`rpm_lock_held` 走 SkipMemory 并 confirmations=2；verified 条目只收到 proposal；reflection 4 条 update、1 条新增（"同因复发的 disk_full：清理同时提永久修复"）。当前 LTM：KB 4、REFLECTION 8、EPISODE 23。
- **库的一致性**：`query_digest` / `filter_keywords` 与批次 1~6 运行时的实现逐条比对，0 处不一致；同一 OpenSearch 索引、同样的文档 ID（`KB#{signature}`、`EPISODE#{sessionId}`、`REFLECTION#{id}`），Runtime 版本 13 / 14 直接读取已积累的记忆。
- **批次 7（agentmem 独立模块上线后的端到端验证）**：Runtime 版本 13，执行 SUCCEEDED。disk-full 预检拦截 → FIXED，会后流水线完整走通：片段写入；KB 合并选择 UpdateMemory，因 `disk_full` 为 verified 条目只排入 proposal；关键词落地剔除 4 个不在补丁输出中的字符串。ssm-offline → NEEDS_HUMAN：片段写入，已有 reflection 更新（confidence 0.9），按 `learn_statuses` 规则不写 KB。本轮 rpm-lock / repo-broken 注入的故障未生效、补丁直接成功，这两条路径未在本批次复测。
- **版本 14**：`app.py` 中 `agent_id` 取自 Profile、admin 任务表取自 `MemoryConfig.stm_table`（行为不变）；部署后复测召回（repo 查询 → `repo_unreachable` cos 0.4689，与版本 13 一致）、记忆统计与 `verify_access.py` 全部 denied 项均一致。

## 6. 护栏（Q4）

中国区没有 Bedrock Guardrails 和 AgentCore Policy（Cedar），全部靠代码和 IAM（`agent/guardrails.py`、`agent/tools.py`、`agentmem/agentmem/records.py`、`infra/02_iam.py`）。

| 层 | 措施 | 实测 |
|---|---|---|
| 输入 | 命令输出一律不可信：脱敏 AK/密码/私钥，剔除指令性文本行，整体包 `<untrusted_command_output>`；主机上报的 PlatformName/AgentVersion 同样处理 | 注入文本每批剔除 3 行，Agent 未执行 |
| 工具 | 工具不收 instance 参数；文档 allowlist 代码二次校验；每台修复 2 次、重跑 1 次（DynamoDB 原子计数）；整批修复熔断；**单 session 工具预算 30 次，没有任何工具豁免**，另设 request_human_action ≤3、search_memory ≤5 | disk-full（批次 1~3）用满修复配额后转人工 |
| 结果校验 | `report_result(FIXED)` 只有在代码看到 `rerun_patch` 返回 Success 时才接受 | 所有 FIXED 都有 Agent 会话发起、经 VPCE 的成功重跑（独立验证用 SSM 历史核对） |
| IAM | session tag 绑单实例；无 reboot/stop/CancelCommand；不能用 RunShellScript；只能经 VPCE | §3.3 反向用例全部被拒 |
| 记忆 | 记录写入前剔除指令性文本；召回块加围栏与"不是指令"声明；关键词必须字面出现在补丁输出 | 本地测试：含注入短语的 reflection 字段在写入时被替换为 `[removed instruction-like text]` |
| 人工 | reboot、回滚、改基线只能 `request_human_action` 生成 `APPROVAL#`，审批后交专用 SSM Automation | 每批生成 restart_ssm_agent 审批单 |
| 审计 | 每次工具调用、每次拦截写 `AUDIT#`；人工接管单独记 `human_takeover` | ✅ |

## 7. ECR 部署与运行方式（Q6）

- **构建**：`docker build --platform linux/arm64 -f agent/Dockerfile .`（Runtime 要求 arm64），构建上下文是项目根目录，根目录 `.dockerignore` 只放行 `agent/` 与 `agentmem/`（不含测试）；`agentmem` 在镜像中作为普通包 `pip install`。`python:3.12-slim`，非 root 用户，监听 8080（`/invocations`、`/ping`），镜像约 95 MB（`agent/Dockerfile`、`infra/06_runtime.py`）。
- **镜像仓库**：同账号中国区 ECR（`*.dkr.ecr.cn-northwest-1.amazonaws.com.cn`），`scanOnPush`，时间戳 tag，每次发布一个新 tag。镜像由 Runtime 服务用执行角色的 ECR 权限拉取，不经过客户 VPC。
- **发布**：`create/update_agent_runtime(agentRuntimeArtifact.containerConfiguration.containerUri=…)`，每次 update 生成不可变 version（本次共 14 个全部 READY），DEFAULT endpoint 跟随最新。命名 endpoint 固定到某个 version 可做灰度 / 回滚（API 支持，本次未实测）。API 另有 `codeConfiguration`（直接上传代码包），中国区未实测。
- **运行**：VPC 模式（私有子网 + 只出 443 的 SG），`serverProtocol=HTTP`，IAM 入站 + 资源策略，idle 600s / maxLifetime 7200s；配置用环境变量，**密钥不进镜像和环境变量**（API key 运行时从 AgentCore Identity token vault 取，`env_has_aws_keys=false`）。
- **CI/CD 建议**：CodeBuild → ECR → `update_agent_runtime` → 冒烟（selftest / probe / isolation）→ 切命名 endpoint。

## 8. 会话隔离（Q7）

平台层：每个 `runtimeSessionId` 对应一个独立 microVM（独立 CPU、内存、文件系统），会话结束或 StopRuntimeSession 后销毁。方案层再叠 4 层：session = 批次+实例；入口 `load_ctx` 校验实例确实分配给该 session（不信 payload）；IAM session tag 把 spoke 凭证绑到单台实例；STM 按 sessionId 分区，人工接管用同一 sessionId + `runtimeUserId=操作人`，workload token 按用户签发。

实测（Lambda action `isolation_test`，VPC 内发起，Runtime `mode=isolation`）：

| 步骤 | 看到的文件标记 | 看到的内存数据 |
|---|---|---|
| A1 会话 A 写入 secret-A | 无（新 microVM） | secret-A |
| A2 会话 A 再次调用 | A 的标记 | secret-A（同一 microVM） |
| B1 会话 B | 无 | 只有 secret-B |
| A3 StopRuntimeSession(A) 后再调 A | 无 | 空（全新 microVM） |

补充观察：各 microVM 的 `boot_id`、进程启动时间完全一致——microVM 是从同一个已初始化快照启动的，不能用它们区分 microVM，隔离性由状态不串来证明。对话层面，microVM 回收后接管仍能从 DynamoDB 恢复（§5）。

## 9. 独立验证与修复

另起一个只拿到代码、没有看到结论的独立评审会话做验证（只读 + 允许的探测脚本），针对 6 条结论逐条找反例。发现并已修复：

| 发现 | 修复 |
|---|---|
| 派发占位后 Runtime 调用失败 / Lambda 崩溃，实例永远占名额，批次跑不完；PatchWave 非 canary/main 永远 PENDING | `dispatchGrace` 内未认领自动重排、3 次后转人工；未知波次归入 main；`batchTimeout` + 状态机 Catch → Abort → Report |
| `describe_instance_information` 未分页（第 11 台起被误判离线）；`send_command` 超 50 台报错 | 分页 + 按 50 台分片 |
| 截止时间从下发算，没算 Patch Manager 排队时间 | 从首次 InProgress 起算；另设排队硬上限 |
| 任何一台 API 报错让整批失败 | 每台独立 try/except、连续 5 次转人工 |
| Spoke 凭证可 CancelCommand 同账号任意命令 | CancelCommand 只给编排会话 |
| Runtime 资源策略只 Deny 了 2 个 action | Deny `bedrock-agentcore:*` |
| 记忆可被持久注入（召回内容原样进 system prompt）；PlatformName 未净化 | 写入去注入 + 召回围栏；主机字符串净化 |
| 记忆不可用时诊断直接失败 | 召回失败降级为无记忆诊断 |
| search_memory / request_human_action / report_result 不计工具预算 | 无豁免预算 + 单工具上限 |
| 关键词过滤放过 `Failed/Failed`、诊断文档固定输出；3 条近似 reflection 未合并 | 关键词必须出现在补丁输出 + 更多排除规则；reflection 0.85 合并 + `--dedupe` |
| `ENABLE_PROBE=1` 时能调用 Lambda 的人可清空记忆 | reset 额外要求确认串；生产关闭 `ENABLE_PROBE`，Lambda 调用权限只给 Step Functions 和管理员 |

未修（生产建议）：`portal_chat` 的操作人由调用方填写（demo 中 Lambda 代表门户；生产门户应从 Identity Center 身份取）；OpenSearch 单节点零副本（生产 3 AZ）；`ListCommandInvocations` 只能 `*`（§3.2 残余风险）。

## 10. 代码目录

```
common/config.py            账号、名称、模型、索引名等统一配置
infra/00_bedrock_key.py     全球区 IAM user(只允许 Kimi K3 + Titan Embed V2) + 30 天 Bedrock API key → AgentCore Identity token vault
infra/01_network.py         运维 VPC / 私有子网 / NAT / 5 个 interface VPCE + 2 个 gateway VPCE
infra/02_iam.py             Hub 角色 + 各账号 Spoke 角色(session tag 绑单实例) + 目标机 instance profile
infra/03_ssm_docs.py        allowlist SSM 文档：PatchDiagnose-Collect、PatchRemediate-{FreeDisk,DisableRepo,ReleaseRpmLock}
infra/04_targets.py         5 台 t4g.micro AL2023 目标机(两个账号)
infra/05_dynamodb.py        patchdemo-batch(状态/审计/审批)、patchdemo-memory(STM)
infra/06_runtime.py         arm64 镜像(构建上下文 = 项目根目录) → ECR → Runtime(VPC, IAM) → 资源策略(SourceVpce, bedrock-agentcore:*)
infra/07_orchestrator.py    VPC Lambda + Step Functions
infra/08_opensearch.py      LTM：OpenSearch 域(VPC-only, IAM 访问策略, 加密)
infra/99_cleanup.py         清理（默认 dry-run，--yes 才真正删除）
agent/app.py                Runtime 入口：diagnose(异步) / chat(人工接管) / memory_admin / selftest / probe / isolation
agent/tools.py              8 个工具，Spoke AssumeRole 时带 TargetInstance tag
agent/guardrails.py         输入净化 + BeforeToolCall/AfterToolCall 钩子 + 去注入
agent/memory_profile.py     PATCH_PROFILE：记忆库的补丁领域配置(措辞、关键词/信号/注入规则、只从 FIXED 学、证据工具)
agent/state.py              批次状态、原子配额、审计、审批
agent/Dockerfile            构建上下文为项目根目录；pip install agentmem
agentmem/                   独立记忆库(pyproject.toml；不依赖 agent/ 代码)，见 agentmem/README.md
  agentmem/config.py        MemoryConfig：端点/索引/向量模型/STM 表 + 全部召回阈值，from_env()
  agentmem/profile.py       Profile：领域措辞、关键词/信号/注入规则、learn_statuses、evidence_tools、namespace
  agentmem/records.py       纯函数：kb/episode/reflection_text、query_digest、filter_keywords、strip_injection/clean、format_*
  agentmem/embeddings.py    BedrockEmbedder(SigV4 或 API key bearer)
  agentmem/store.py         Store 协议 + OpenSearchStore + 索引 mapping
  agentmem/ltm.py           LongTermMemory：recall / search_knowledge / search_reflections / history / related + 写入
  agentmem/strategies.py    schema、提示词、MemoryStrategies(consolidate_knowledge / generate_reflections / process_session)、refilter / dedupe
  agentmem/stm.py           DynamoDBSessionRepository + ShortTermMemory(transcript / evidence)
  agentmem/llm.py           strands_llm：LLM 以 llm(system, prompt, schema) 注入
  agentmem/memory.py        Memory 门面(recall / search / session_repository / process_session / admin)
  agentmem/testing.py       InMemoryStore、HashEmbedder(离线测试替身)
  tests/                    17 个 pytest 用例，纯离线
orchestrator/handler.py     init / send_patch(预检) / tick(轮询+派发+超时) / halt / abort / report / portal_chat / memory_admin / selftest / probe / isolation_test
scenarios/                  inject_faults · seed_memory · run_batch · verify_access · takeover · session_burst · tune_thresholds
.dockerignore               镜像构建只放行 agent/ 与 agentmem/
```

## 11. 运行步骤

```bash
cp common/accounts.example.json common/accounts.json   # 填入运维账号 / 业务账号 ID 与本地 AWS profile（不入库）
uv venv -p 3.12 .venv && uv pip install -p .venv boto3 strands-agents bedrock-agentcore opensearch-py pydantic pytest
(cd agentmem && ../.venv/bin/python -m pytest -q tests)   # 记忆库离线单元测试（17 个）
for s in 01_network 02_iam 03_ssm_docs 04_targets 05_dynamodb 08_opensearch 00_bedrock_key 06_runtime 07_orchestrator; do
  env -u AWS_REGION .venv/bin/python infra/$s.py; done   # 06_runtime 以项目根目录为构建上下文：docker build -f agent/Dockerfile .
.venv/bin/python scenarios/verify_access.py global.moonshotai.kimi-k3   # 先确认公网调用被拒、VPC 内调用成功
.venv/bin/python scenarios/seed_memory.py --reset                         # 建索引 + 导入 seed/knowledge.json
.venv/bin/python scenarios/inject_faults.py
.venv/bin/python scenarios/run_batch.py b1 --patch-timeout 420
.venv/bin/python scenarios/seed_memory.py --list
BATCH=<batchId> .venv/bin/python scenarios/takeover.py pd-ssm-offline "说明排查过程和恢复步骤"
.venv/bin/python scenarios/session_burst.py 40 20
.venv/bin/python infra/99_cleanup.py          # dry-run；确认无误后加 --yes
```

## 12. 生产化建议

1. **模型**：以实际选用的模型为准（测试使用全球区 Bedrock 的 Kimi K3 + Titan Text Embeddings V2）。国内 / 自托管模型放在 VPC 内可去掉 NAT；使用全球区模型时经出口代理 + 域名允许列表收口。Agent 代码与模型解耦，替换只涉及配置。
2. **预检**：PingStatus + 根盘余量已实现；再加补丁源连通性检查。
3. **记忆**：OpenSearch 3 AZ + 副本 + FGAC；KB 条目人工确认后才置 `verified=true`（`--verify`）；定期审核 `update_proposals` / `suggested_keywords`；条目上千后按 namespace 分索引。中国区上线 AgentCore Memory 后按 namespace 迁移。其他 Agent 复用 `agentmem` 时各写一个 `Profile`，用独立索引或 namespace 根隔离记录。
4. **编排**：`maxAgents` 10~20；按账号 / 区域分 PatchGroup；`waveHardTimeout` 按 MaxConcurrency 估算。
5. **门户**：IAM Identity Center permission set 角色调用 `InvokeAgentRuntimeForUser`，操作人从 SSO 身份取；审批通过后由独立角色的 SSM Automation 执行高危动作。
6. **发布**：命名 endpoint 灰度；生产关闭 `ENABLE_PROBE`；Lambda 调用权限只给 Step Functions。
7. **双区**：北京区按同样结构再部署一套；VPCE 只服务本区域，Spoke 的 `aws:SourceVpce` 列表要包含两个区的 VPCE ID。
