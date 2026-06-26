# AgentCore Harness → 评估 → 优化:一个闭环演示

> 🌐 语言:[English](README.md) · **简体中文**

一个**简化的、可动手**的演示:把一个**客服 Agent 部署成托管的 Amazon Bedrock AgentCore
Harness**,然后用 **AgentCore Evaluations(评估)** 和 **AgentCore Optimization(优化)** 闭合
**观测 → 评估 → 改进** 这条质量回路——全程跑在真实 AWS 上。

![闭环架构](docs/agentcore-closed-loop.svg)

> 🇨🇳 **新手 / 想边学边做?** 看中文分步动手指南:[`docs/动手指南.md`](docs/动手指南.md) —— 每一步都讲清「为什么」「该看到什么」「会踩哪些坑」。

端到端演示了什么:

1. **创建 + 部署**:把 Agent 作为**托管 AgentCore Harness** 部署——模型 + 一份*故意写弱*的基线系统
   提示词 + **5 个内联函数工具**——通过 `CreateHarness`/`UpdateHarness`。**没有容器、没有编排代码**。
   通过 `InvokeHarness` 的「客户端工具循环」调用。
2. **评估**:用 **AgentCore Evaluations**(批量、LLM 当裁判)给会话打分 → 基线分数。
3. **优化**:用 **AgentCore Recommendations**(分析轨迹 → 改进提示词)产出新提示词,应用它、
   重新评估、**对比**——只有**不回退**才晋升。
4. **验证**:在生产用 **A/B 测试**验证(以运行手册形式给出)。

> ⚠️ **评估出于必要跑在 Runtime 镜像上。** 托管 Harness 是你创建、部署、调用的那个 Agent。但
> AgentCore Evaluations 目前**评不了**托管 Harness 的遥测(它的 Strands 内容事件是一种双层嵌套结构,
> 评估器解析不了——`AgentSpanMappingException`)。所以「评估→优化」回路跑在**同一个 Agent 的
> Strands-on-Runtime 镜像**上(相同工具/提示词,带 ADOT 埋点 → 可被评估映射),任何改进都会
> **同时应用到两边**。完整解释见 [`docs/CONCEPTS.md`](docs/CONCEPTS.md) §2。

---

## 前置条件

- 一个 AWS 账号,并在 `us-west-2` **开通了 Amazon Bedrock Claude 模型访问**
  (本演示会自动挑选最便宜的、已开通的 Claude 推理配置——Haiku 4.5)。
- AWS 凭证可被默认凭证链获取(环境变量、`~/.aws`、SSO……)。
- [`uv`](https://docs.astral.sh/uv/) —— 管理 Python 环境(会自动拉取 Python 3.12)。
- `agentcore` CLI 随依赖 `bedrock-agentcore-starter-toolkit` 一起安装(不需要 npm)。
- CloudWatch **Transaction Search** 已开启(评估服务靠它读取轨迹);`preflight.py` 会检查/开启它。

---

## 复现步骤

```bash
# 0. 安装依赖(+ 拉取 Python 3.12)并核验前置条件 -> config.json
uv sync
uv run python preflight.py
uv run pytest -q                       # 给确定性工具跑单测

# 1. 把 Agent 部署成托管 Harness(模型 + 弱提示词 + 5 个内联工具),
#    并部署用于评估的 Runtime 镜像。
uv run python scripts/harness_create.py          # CreateHarness/UpdateHarness + 预热调用
uv run python scripts/invoke_deployed.py "ORD-1003 is really late, I want a discount."  # harness 工具循环

export AGENTCORE_SUPPRESS_RECOMMENDATION=1
MODEL_ID=$(uv run python -c "import json;print(json.load(open('config.json'))['agent_model_id'])")
printf '\n\n\n\n' | uv run agentcore configure -e agent/main.py -n acmesupport -rf requirements.txt --disable-memory
printf '\n\n\n\n' | uv run agentcore deploy --env AGENT_MODEL_ID="$MODEL_ID" --env AGENT_OBSERVABILITY_ENABLED=true --auto-update-on-conflict
uv run python scripts/capture_deployment.py

# 2. 基线评估(runtime 镜像):生成会话、等待轨迹被摄取、打分
uv run python scripts/generate_sessions.py --tag baseline --target runtime --wait 200
uv run python scripts/run_evaluation.py --tag baseline

# 3. 优化:推荐一版更好的提示词,应用到 harness + prompts.py,重新部署镜像,重评,对比
uv run python scripts/run_optimization.py        # StartRecommendation + UpdateHarness
printf '\n\n\n\n' | uv run agentcore deploy --env AGENT_MODEL_ID="$MODEL_ID" --env AGENT_OBSERVABILITY_ENABLED=true --auto-update-on-conflict
uv run python scripts/generate_sessions.py --tag improved --target runtime --wait 200
uv run python scripts/run_evaluation.py --tag improved
uv run python scripts/compare.py                 # -> results/comparison.json(晋升 / 不晋升)
uv run python scripts/ab_test.py                 # A/B 测试运行手册(生产验证步骤)

# 4. 全部拆除以停止持续计费
uv run python scripts/teardown.py                # dry run(只看不删)
uv run python scripts/teardown.py --yes          # 删除 harness + runtime + 评估/推荐记录
```

想让 Harness *本体*走一遍评估脚本(并亲眼看到它如文档所述地失败),给
`generate_sessions.py` 传 `--target harness`。

### 这一轮闭环跑出了什么(诚实的结果)

基线打出了 `GoalSuccessRate = 1.0`,于是推荐(没有失败可学)加了一条「行动前先等明确批准」的
安全约束——这让 Agent 变成*询问*而不是*完成*任务,把 `GoalSuccessRate` 从 **1.0 拉回到 0.6**。
评估**抓住了这次回退**,`compare.py` 返回 **`promote: false`**,改动被**回滚**到基线提示词。
这正是回路按设计在工作——它阻止了一次质量回退上线。详见 `results/comparison.json`。

---

## 成本

很小(个位数美元):Claude **Haiku 4.5**、回复短且有上限、每轮约 10 个会话。
AgentCore Runtime + Harness 是无服务器 / 按用量付费(空闲成本可忽略)。
**用完请跑 `scripts/teardown.py --yes`。** 想把账号清得彻底(IAM 角色、S3、ECR),再跑
`agentcore destroy`。

---

## 故障排查

- **`preflight.py` 提示 Bedrock 访问 DISABLED** → 去 Bedrock 控制台(us-west-2 → *Model access*)
  开通一个 Anthropic Claude 模型,等「Access granted」后重跑。
- **Harness 评估失败:`AgentSpanMappingException: Failed to parse user_query`** → 这是托管 Harness 的
  预期表现(它的内容事件结构目前还不能被 Evaluations 映射)。请评估 Runtime 镜像
  (`--target runtime`);见 CONCEPTS §2。
- **Runtime 批量评估全部会话失败** → 该 Runtime Agent 必须发出 GenAI span:确认
  `requirements.txt` 里有 `aws-opentelemetry-distro`,且 `agent/main.py` 里运行了
  `StrandsTelemetry().setup_otlp_exporter()`,然后重新部署。首次启动的 span 索引滞后约 5–10 分钟。
- **`StartBatchEvaluation` 报 `ValidationException`** → `serviceNames` 必须恰好一个条目
  (runtime 为 `<agent-name>.DEFAULT`,harness 为 `harness_<HarnessName>.DEFAULT`);
  `batchEvaluationName` 必须匹配 `[a-zA-Z][a-zA-Z0-9_]{0,47}`。
- **Harness 工具 `type` 被拒** → 用蛇形枚举 `inline_function`(配置键仍是驼峰 `inlineFunction`)。
- **`agentcore configure` 在 `/dev/null` 上崩溃** → 它是交互式的;用管道喂回车(`printf '\n\n\n\n' | …`)。

---

## 目录结构

```
agent/            orders.py(工具逻辑)· prompts.py(基线/优化提示词)· harness_tools.py(内联工具规格)
                  runtime_config.py + main.py(Runtime 镜像的 Strands Agent)
scripts/          preflight 辅助 + harness_agent(创建/更新 + 工具循环), harness_create, invoke_deployed,
                  capture_deployment, generate_sessions, run_evaluation, run_optimization, compare, ab_test, teardown
dataset/          eval_prompts.json(10 条客服问题)
results/          scores、recommendation、comparison(运行时生成)
docs/             CONCEPTS.md + 闭环架构图(svg + png)+ 动手指南.md(中文教程)
preflight.py      前置条件 + 能力检查 -> config.json
```

## 安全说明

- 不提交任何凭证或密钥;AWS 认证来自标准凭证链。
- `config.json` / `deployment.json` / `.bedrock_agentcore.yaml` 只含账号 id、区域、模型 id 和资源 ARN
  (无密钥)。`.gitignore` 排除了虚拟环境、缓存,以及工具链的构建缓存(`.bedrock_agentcore/`)。
- 工具链会自动创建一个最小权限的执行角色;生产环境请进一步收紧。
