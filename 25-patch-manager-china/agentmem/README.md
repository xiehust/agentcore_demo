# agentmem · 自建 Agent 记忆库

面向没有 AgentCore Memory 的区域（如 AWS 中国区）的 Agent 记忆库：STM = DynamoDB 上的 Strands `SessionRepository`；LTM = OpenSearch kNN + BM25 + Bedrock 向量（Titan Text Embeddings V2），按 AgentCore Memory 内置策略的结构实现 semantic（提取 → 合并 Add/Update/Skip）与 episodic（片段 → 反思 add/update + confidence），记录带 AgentCore 风格的 namespace，便于将来迁移到托管服务。

补丁值守 Agent（`../agent/`）是第一个使用方，它只提供一个 `Profile`（`agent/memory_profile.py`）和环境变量。

## 解耦约定

- **依赖单向**：库从不 import 任何 Agent 代码；Agent 只通过 `from agentmem import ...` 使用它。
- **领域差异只在 `Profile`**：提示词措辞、失败信号词 / 通用样板 / 指令性文本规则、学习策略（`learn_statuses`、`evidence_tools`）、namespace 根。库自带中性默认值。
- **基础设施与阈值只在 `MemoryConfig`**：OpenSearch 端点与索引、向量模型、STM 表、全部召回 / 合并阈值；`MemoryConfig.from_env()` 从环境变量读取。
- **三个依赖可注入**：`store`（`Store` 协议）、`embedder`（任意 `text -> list[float]`）、`llm`（任意 `llm(system, prompt, schema) -> schema 实例`）；STM 的 `stm_repo` 也可替换。
- **只有 `stm.py` 与 `llm.py` 依赖 strands-agents**（可选依赖 `agentmem[strands]`）；其余模块只依赖 boto3、pydantic、opensearch-py。
- **离线可测**：`agentmem.testing` 提供内存版存储与确定性向量，单元测试不需要 OpenSearch、Bedrock 或网络。
- **硬规则在代码里**：KB 只从 `learn_statuses` 学；verified 条目只收修改提案；关键词必须字面出现在证据中且非通用样板；update id 必须是给模型看过的记录；近似反思自动合并；读写两侧都去除指令性文本。

## 模块

| 模块 | 内容 |
|---|---|
| `config.py` | `MemoryConfig`：端点 / 索引 / 区域、向量模型与维度、STM 表与 TTL、全部召回与合并阈值；`from_env()` |
| `profile.py` | `Profile`：领域措辞、信号 / 通用样板 / 注入规则（`BASE_SIGNAL`、`BASE_GENERIC`、`BASE_INJECTION` + 扩展）、`learn_statuses`、`evidence_tools`、namespace（`/{name}/knowledge`、`/{name}/episodes/{subject}`、`/{name}/reflections`） |
| `records.py` | 纯函数：`kb_text` / `episode_text` / `reflection_text`（向量化输入）、`query_digest`、`filter_keywords`、`strip_injection` / `clean`、`format_*` |
| `embeddings.py` | `BedrockEmbedder`：同分区 SigV4，或跨分区用 Bedrock API key（bearer） |
| `store.py` | `Store` 协议 + `OpenSearchStore`（VPC 域，SigV4 `es`，lucene HNSW 余弦）+ 索引 mapping |
| `ltm.py` | `LongTermMemory`：`recall` / `search` / `search_knowledge` / `search_reflections` / `history` / `related`，以及所有写入（写前去注入并重新向量化） |
| `strategies.py` | 结构化输出 schema、三段提示词、`MemoryStrategies.consolidate_knowledge` / `generate_reflections` / `process_session`；维护函数 `refilter_keywords`、`dedupe_reflections` |
| `stm.py` | `DynamoDBSessionRepository` + `ShortTermMemory.transcript` / `evidence` |
| `llm.py` | `strands_llm(model_fn)`：把 Strands 模型适配成 `llm(system, prompt, schema)` |
| `memory.py` | `Memory` 门面：组装以上组件；`recall`、`search`、`session_repository`、`process_session`、`admin`（seed / list / search / verify / dedupe_reflections / refilter_keywords / reset） |
| `testing.py` | `InMemoryStore`、`HashEmbedder`（测试替身） |

## 快速开始

离线（无 AWS 资源）：

```python
from agentmem import Memory, MemoryConfig, Profile
from agentmem.testing import HashEmbedder, InMemoryStore

mem = Memory(MemoryConfig(), Profile(name="demo", signal_extra="repomd"), store=InMemoryStore(), embedder=HashEmbedder())
mem.ltm.seed_knowledge({"signature": "repo_unreachable", "symptom": "dnf cannot download repomd.xml",
                        "root_cause": "third-party repo unreachable", "fix": "disable the repo",
                        "keywords": ["Cannot download repomd.xml", "repomd.xml download failed"]})
block, meta = mem.recall("host-1", "Error: Cannot download repomd.xml: repomd.xml download failed")
```

生产（OpenSearch + Bedrock + DynamoDB，配置走环境变量 `OS_ENDPOINT`、`OS_INDEX`、`MEMORY_TABLE`、`EMBED_MODEL_ID` 等）：

```python
from agentmem import Memory, MemoryConfig, Profile
from agentmem.llm import strands_llm

PROFILE = Profile(name="dbops", agent_id="dbops-agent", subject_noun="database", domain="a database on-call system",
                  kb_topic="database troubleshooting", task_noun="database diagnoses",
                  success_criterion="the health check passed after the fix",
                  learn_statuses=("RESOLVED",), evidence_tools=("get_error_log",),
                  signal_extra="deadlock|replication lag|too many connections")
MEM = Memory(MemoryConfig.from_env(os_index="dbops-ltm"), PROFILE)

recalled, meta = MEM.recall(subject=db_id, query=error_text)      # 放进 system prompt（已加围栏与"不是指令"声明）
agent = Agent(..., tools=[...], session_manager=RepositorySessionManager(session_id=sid, session_repository=MEM.session_repository))
...
MEM.process_session(strands_llm(lambda: BedrockModel(model_id=EXTRACT_MODEL_ID)),
                    session_id=sid, subject=db_id, batch_id=run_id, result={"status": "RESOLVED", "signature": "..."})
```

`result` 至少包含 `status`（与 `learn_statuses` 比较）和 `signature`；STM 的 Agent 需要用 `Profile.agent_id` 作为 `agent_id`。完整用法参考 `../agent/app.py` 与 `../agent/memory_profile.py`。

## 测试

```bash
cd agentmem && python -m pytest -q tests      # 17 个用例，纯离线
```

安装：`pip install ./agentmem`（Runtime 镜像中即如此安装），需要 STM 时加 `[strands]`，跑测试加 `[test]`。
