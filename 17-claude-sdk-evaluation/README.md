# Claude Agent SDK → Langfuse → AgentCore Evaluations

This example runs a tool-using agent with **Claude Agent SDK** and
`claude-sonnet-5`, exports its OpenTelemetry trace to **Langfuse**, reads the
session and full trace back from the Langfuse API, and submits the reconstructed
session spans directly to the Amazon Bedrock AgentCore `Evaluate` API. It can
also request an optimized system prompt from AgentCore Recommendations.

**The evaluation path uses no CloudWatch.** The on-demand `Evaluate` API accepts
caller-provided `sessionSpans`, so for evaluation Langfuse is the only trace store
this demo queries.

**The recommendation path cannot use the Langfuse spans, and does read CloudWatch.**
During the Optimization preview, `StartRecommendation` cannot identify Claude Agent SDK
spans as sessions — see
[Recommendation trace source: verified limitation](#recommendation-trace-source-verified-limitation).
The reward signal therefore comes from the *same shopping agent* deployed to AgentCore
Runtime (`runtime_agent/`, see
[Deploy the reward-source runtime](#deploy-the-reward-source-runtime)), scored by a batch
evaluation that resolves its sessions from CloudWatch Logs. Runs that request a
recommendation report `cloudwatch_used: true`.

## 架构图

![Claude Agent SDK、Langfuse 与 AgentCore Evaluator 架构图](assets/architecture.light.svg)

[查看 1920px PNG 原图](assets/architecture.light.png)

这张图从左到右分为三个区域，展示了 Agent 执行、可观测数据存储和按需评估之间的边界：

1. **本地执行区（Local Execution）**：CLI 启动 Claude Agent SDK，并固定使用 `claude-sonnet-5`。Agent 在推理过程中调用本地 MCP 工具 `lookup_product_price`，工具结果再返回 Agent，用于生成最终回答。
2. **Telemetry 采集与 Langfuse**：OpenInference 自动把 Agent 和工具调用转换为 OpenTelemetry spans，通过 OTLP 发送到 Langfuse。Langfuse 按 session、trace 和 observation 保存完整调用链，并通过 Sessions API 与 Trace API 对外提供查询。
3. **Langfuse Bridge**：本地桥接代码先从 Langfuse 读回指定 session 和完整 trace，再把 `AGENT`、`TOOL` observations 转换为 AgentCore 支持的 unified session spans。转换后的数据包含 `session.id`、`traceId`、`spanId`、`input.value` 和 `output.value` 等字段。
4. **AgentCore 评估区**：桥接代码把 `sessionSpans` 直接提交给 AgentCore `Evaluate` API。AgentCore 调用内置 Bedrock evaluator 完成评分，并返回分数、标签和解释；图中的 `0.83` 是本示例真实运行 `Builtin.Helpfulness` 得到的结果。

图中的蓝色箭头表示 Agent 请求，紫色箭头表示 telemetry 上报与 trace 回读，绿色箭头表示工具调用和评估数据流。右下角被划掉的 CloudWatch 表示**评估路径**不会查询 CloudWatch Logs，也不依赖 Transaction Search；对 `Evaluate` 而言 Langfuse 是唯一的 trace 数据来源。

> **两张图只覆盖评估路径。** Recommendation（系统提示词优化）路径不在图中：Optimization 预览版只从一个 instrumentation scope 白名单里识别 session，Claude Agent SDK 的 spans 不在白名单内，因此 recommendation 的 reward 只能来自一个已完成的 batch evaluation，而 batch evaluation 会从 CloudWatch Logs 读取 session。详见 [Recommendation trace source: verified limitation](#recommendation-trace-source-verified-limitation)。

## 流程图

![Claude Agent SDK 到 AgentCore Evaluator 的端到端流程图](assets/evaluation-flow.light.svg)

[查看 1920px PNG 原图](assets/evaluation-flow.light.png)

流程图把一次完整评估拆成三个阶段：

1. **Agent 执行（步骤 1–4）**：程序先检查 Claude、Langfuse 和 AWS 配置，然后初始化 Langfuse 与 OpenInference instrumentation，运行 Claude Agent，并执行需要的 MCP 工具调用。
2. **Trace 写入与回读（步骤 5–8）**：Agent 完成后调用 `langfuse.flush()`，确保 spans 被发送到 Langfuse。随后程序通过 Sessions API 和 Trace API 读取数据，并检查 trace 中是否已经出现 `AGENT` span。由于 Langfuse 写入存在短暂的最终一致性，如果数据尚未就绪，程序会等待 2 秒后继续轮询，而不是转而查询 CloudWatch。
3. **转换与评估（步骤 9–10）**：程序筛选 `AGENT` 和 `TOOL` observations，按 AgentCore Claude Agent SDK 的 unified telemetry 约定生成 `sessionSpans`，然后调用 `bedrock-agentcore.evaluate()`。评估结果最终写入 `results/evaluation.json`，转换后的原始 spans 写入 `results/session_spans.json`。

整个流程中，Claude 调用、工具调用和 trace 上报发生在前半段；AgentCore 只接收从 Langfuse 读回并转换后的 spans。这样可以保留 AgentCore evaluator 的能力，同时让 Langfuse 承担统一的可观测数据存储与查询职责。

可编辑的 SVG、PNG 和可重复执行的生成脚本均位于 `assets/`。重新生成图片：

```bash
python3 assets/generate_diagrams.py
```

## Data flow

```text
Claude Agent SDK
  └─ OpenInference instrumentation
       └─ Langfuse OpenTelemetry exporter
            └─ Langfuse Sessions API + Trace API
                 └─ unified OpenInference session spans
                      └─ bedrock-agentcore.evaluate(...)          # scores the run

claude_sdk_evaluation.shopping   (SYSTEM_PROMPT + lookup_product_price)
  ├─ Claude Agent SDK agent  → Langfuse            (evaluated, above)
  └─ Strands agent on AgentCore Runtime
       └─ CloudWatch Logs
            └─ bedrock-agentcore.start_batch_evaluation(...)
                 └─ completed batchEvaluationArn
                      └─ bedrock-agentcore.start_recommendation(...)
                           └─ get_recommendation(...) until terminal
```

Both agents run the same prompt and tool from one module, so the reward traces belong
to the prompt being optimized.

The conversion follows AgentCore's documented Claude Agent SDK contract:

- scope: `openinference.instrumentation.claude_agent_sdk`
- agent span: `openinference.span.kind=AGENT`
- tool span: `openinference.span.kind=TOOL`
- unified content: `input.value` and `output.value`
- correlation: `session.id`, `traceId`, and `spanId`

## Recommendation trace source: verified limitation

`StartRecommendation` accepts inline `sessionSpans`, but during the Optimization
preview it will not identify the Claude Agent SDK's spans as sessions. Every
attempt fails with:

```text
ValidationException: No sessions were identified from input agent traces.
```

This was isolated with a controlled A/B test against the live service. Both
requests carried an identical 169-record payload — 105 span documents plus 64
content log events, covering 10 real AgentCore Runtime sessions. The only
difference was `scope.name`:

| `scope.name` | Result |
| --- | --- |
| `strands.telemetry.tracer` | `COMPLETED`, prompt returned |
| `openinference.instrumentation.claude_agent_sdk` | `FAILED` — no sessions identified |

Session identification is gated on the **instrumentation scope**, not on span
shape, session count, or timing fields. The AgentCore CLI carries a matching scope
set (`@aws/agentcore` 0.26.0, `dist/cli/index.mjs`, `isRelevantForEval`) —
`strands.telemetry.tracer`, `opentelemetry.instrumentation.langchain`,
`openinference.instrumentation.langchain`. `claude_agent_sdk` is not on it.

### Relabelling the scope does not work

That CLI set is a **client-side pre-filter, not the server's gate**. The server
dispatches a span→session mapper chosen by the instrumentation scope *and* keyed to
that instrumentation's semantic conventions, so the scope string and the span
contents have to agree. Four further probes:

| Payload | `scope.name` | Result |
| --- | --- | --- |
| This demo's 6 Langfuse spans | `…instrumentation.langchain` | `FAILED` |
| The **same 169 records that COMPLETED above** | `…instrumentation.langchain` | `FAILED` |
| Langfuse spans hand-converted to Strands conventions, 1 session | `strands.telemetry.tracer` | `FAILED` |
| The same conversion across 10 synthetic sessions | `strands.telemetry.tracer` | `FAILED` |

Row 2 is the decisive one: relabelling **breaks a payload that succeeds under its
true scope**. Passing an allowlisted string is not sufficient, and mislabelling
Claude Agent SDK spans as LangChain does not rescue them either.

Rows 3–4 show why a hand-built Strands payload does not close the gap. A real
accepted session tree is much richer — per session, **two trace IDs** and ~10 span
documents spanning four different instrumentation scopes:

```text
AgentCore.Runtime.Invoke                 (no scope, aws.span.kind=LOCAL_ROOT)
└─ POST /invocations                     (opentelemetry.instrumentation.starlette)
   └─ invoke_agent …                      AGENT  (strands.telemetry.tracer)
      ├─ execute_event_loop_cycle                (strands.telemetry.tracer)
      │  ├─ chat                          LLM    (strands.telemetry.tracer)
      │  │  └─ chat <model>               LLM    (…botocore.bedrock-runtime)
      │  └─ execute_tool …                TOOL   (strands.telemetry.tracer)
      └─ execute_event_loop_cycle                (… repeated per turn)
```

…plus one `body.input` / `body.output` content log event paired to **every**
Strands-scope span. Reproducing that from Claude Agent SDK data means fabricating
Bedrock model-invocation spans, an ASGI server span, and an AgentCore Runtime
identity for calls that never happened. It is unverified, it breaks whenever the
mapper changes, and it makes the telemetry assert runs that did not occur — so this
demo does not do it.

A second, independent requirement surfaced along the way: inline `sessionSpans`
must carry **both** record types — span documents (`startTimeUnixNano` /
`endTimeUnixNano`) and content log events (`body.input` / `body.output`).
Submitting only log events fails with `Provided input contains N log event(s) but
no span documents`.

Evidence for all of this is written to
`results/finding_inline_scope_allowlist.json`.

## Deploy the reward-source runtime

Because the recommendation cannot read the Langfuse spans, the reward signal comes
from **the same shopping agent hosted on AgentCore Runtime**. `runtime_agent/` is
that agent: same `SYSTEM_PROMPT`, same `lookup_product_price` tool, same catalog —
all from `claude_sdk_evaluation.shopping`, so the prompt being optimized is exactly
the prompt that produced the reward traces.

It is a **Strands** agent rather than a Claude Agent SDK one, because
`strands.telemetry.tracer` is a scope the recommendation service understands. The
model differs as a result: the local agent runs `claude-sonnet-5` via your
Anthropic-compatible endpoint, the runtime agent runs a Bedrock model
(`AGENT_MODEL_ID`, default `global.anthropic.claude-haiku-4-5-20251001-v1:0`).
The prompt and tool surface — what the optimizer reasons about — are identical.

`agentcore deploy` packages only `runtime_agent/`, so the shared module is vendored
into it. `tests/test_runtime_agent.py` fails if that copy goes stale, which would
otherwise mean optimizing a prompt the reward sessions never ran.

```bash
# 1. Vendor the shared prompt/catalog and deploy (interactive; pipe newlines)
uv run python scripts/sync_runtime_shared.py
export AGENTCORE_SUPPRESS_RECOMMENDATION=1
printf '\n\n\n\n\n\n' | uv run --with bedrock-agentcore-starter-toolkit \
  agentcore configure -e runtime_agent/main.py -n shopagent \
  -rf runtime_agent/requirements.txt --disable-memory
printf '\n\n\n\n\n\n' | uv run --with bedrock-agentcore-starter-toolkit \
  agentcore deploy --env AGENT_OBSERVABILITY_ENABLED=true --auto-update-on-conflict
uv run python scripts/capture_runtime_deployment.py     # -> runtime_deployment.json

# 2. Run the reward sessions and wait for CloudWatch trace ingestion
uv run python scripts/generate_runtime_sessions.py --wait 240

# 3. Score them; this prints the ARN for the next step
uv run python scripts/create_reward_batch_evaluation.py \
  --log-group "$(jq -r .log_group runtime_deployment.json)" \
  --service-name "$(jq -r .service_name runtime_deployment.json)" \
  --session-ids-file results/runtime_sessions_shop.json

# 4. Recommend against the prompt those sessions ran
uv run claude-sdk-eval --skip-evaluation --recommend-system-prompt \
  --recommendation-evaluator Builtin.GoalSuccessRate \
  --recommendation-batch-evaluation-arn '<arn from step 3>'
```

Run the scripts through `uv run` so they use the project's pinned boto3 — an older
system boto3 has no `start_batch_evaluation`.

Requires `bedrock-agentcore:InvokeAgentRuntime`,
`bedrock-agentcore:StartBatchEvaluation` / `GetBatchEvaluation`, `logs:StartQuery`,
and Bedrock access to `AGENT_MODEL_ID`.

Batch evaluation reads the **runtime log group** you pass in `--log-group`, which is
where the ADOT collector writes the trajectory spans (`invoke_agent` AGENT, `chat`
LLM, `execute_tool` TOOL). Note that with `aws-opentelemetry-distro` 0.19.0-aws the
`aws/spans` log group receives only the `AgentCore.Runtime.Invoke` wrapper span, so
an empty-looking `aws/spans` is **not** a sign that telemetry is broken — query the
runtime log group instead. See the ADOT note in `runtime_agent/requirements.txt` and
`_setup_telemetry` in `runtime_agent/main.py`; Transaction Search only matters if you
also want the GenAI Observability dashboard to show the trajectory.

Batch evaluation reads sessions from CloudWatch Logs, so a reward source stops
working once those session logs age out of retention; the recommendation then fails
with `No reusable scores were found for batch eval ...`. Re-run steps 2–3 for a
fresh one. `trace_source` in the output records which traces were analyzed.

Tear the runtime down when finished — it bills while deployed:

```bash
uv run --with bedrock-agentcore-starter-toolkit agentcore destroy
```

## Prerequisites

- Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/)
- A Claude-compatible endpoint that exposes `claude-sonnet-5`
- A Langfuse project
- AWS credentials with `bedrock-agentcore:Evaluate` permission for evaluation
- For the opt-in recommendation path, `bedrock-agentcore:StartRecommendation`
  and `bedrock-agentcore:GetRecommendation` permissions
- Bedrock model access required by the selected built-in evaluator

The run invokes Claude and AgentCore evaluator/recommendation resources and can
incur charges. Recommendations are generated by LLMs; review and test a
recommended prompt before applying it.

## Environment

The program uses the existing process environment; it does not load or print
secret values.

```bash
export ANTHROPIC_BASE_URL='https://...'
export ANTHROPIC_API_KEY='...'

export LANGFUSE_PUBLIC_KEY='...'
export LANGFUSE_SECRET_KEY='...'
export LANGFUSE_BASE_URL='https://cloud.langfuse.com'
# LANGFUSE_HOST is also accepted for existing installations.

export AWS_REGION='us-west-2'
```

Standard AWS credential-chain configuration is supported (environment,
`~/.aws`, SSO, instance role, and so on). See `.env.example` for placeholders.

## Install and run

```bash
cd 17-claude-sdk-evaluation
uv sync
uv run claude-sdk-eval
```

The default prompt forces two deterministic MCP tool calls and the default
evaluator is `Builtin.Helpfulness`. Useful options:

```bash
# Run multiple evaluators (one Evaluate request per ID)
uv run claude-sdk-eval \
  --evaluator Builtin.Helpfulness \
  --evaluator Builtin.ToolSelectionAccuracy

# Override the prompt or region
uv run claude-sdk-eval \
  --prompt 'Use the tool to price one NOTEBOOK and two PEN items.' \
  --region us-west-2

# Validate Claude → Langfuse retrieval/conversion without calling AgentCore
uv run claude-sdk-eval --skip-evaluation

# Request a system prompt recommendation. A completed batch evaluation ARN is
# required — inline Langfuse spans are rejected during the Optimization preview.
# See "Recommendation trace source: verified limitation" above.
uv run claude-sdk-eval \
  --skip-evaluation \
  --recommend-system-prompt \
  --recommendation-evaluator Builtin.GoalSuccessRate \
  --recommendation-batch-evaluation-arn \
  'arn:aws:bedrock-agentcore:us-west-2:123456789012:batch-evaluate/eval-id'

# Without the ARN the inline path is attempted and fails with
# "No sessions were identified from input agent traces".
uv run claude-sdk-eval \
  --skip-evaluation \
  --recommend-system-prompt \
  --recommendation-evaluator Builtin.GoalSuccessRate
```

Recommendation is opt-in because it starts a separate asynchronous, billable
job. The CLI polls for up to 15 minutes by default; use
`--recommendation-timeout` and `--recommendation-poll-interval` to adjust that
behavior. `--recommendation-evaluator` accepts either a built-in evaluator ID or
a custom evaluator ARN and must identify a numerical evaluator.

The model is fixed to the exact identifier `claude-sonnet-5`; there is no model
override so runs cannot silently evaluate a different model.

## Outputs

Local outputs are written under the gitignored `results/` directory:

- `results/session_spans.json`: spans read from Langfuse and converted to the
  AgentCore unified telemetry schema
- `results/evaluation.json`: IDs, agent response, span summary, evaluator
  results, and the compact recommendation result when enabled
- `results/recommendation.json`: recommendation ID/status, `trace_source`
  (which traces were analyzed), the Langfuse session/trace IDs that produced the
  prompt under optimization, the recommended system prompt, and the explanation;
  failed or timed-out jobs retain their ID and diagnostic details
- `results/runtime_sessions_shop.json`: the AgentCore Runtime reward sessions from
  `scripts/generate_runtime_sessions.py`
- `results/reward_batch_evaluation.json`: batch evaluation ARN, status, and
  aggregate scores from `scripts/create_reward_batch_evaluation.py`
- `runtime_deployment.json`: deployed runtime ARN, log group, and service name from
  `scripts/capture_runtime_deployment.py` (not under `results/`)
- `results/finding_inline_scope_allowlist.json`: the A/B evidence for the inline
  scope limitation

The CLI also prints the final summary. `cloudwatch_used` reflects the path
actually taken: `false` for evaluation-only runs, `true` when a recommendation
resolves its reward sessions from a batch evaluation.

## Validation

```bash
uv run ruff check .
uv run pytest
uv run pyright
```

## Troubleshooting

- **No Langfuse observations:** verify the Langfuse URL and keys, then set
  `LANGFUSE_DEBUG=True`. The CLI calls `flush()` and polls for eventual
  consistency before reading the trace.
- **Trace never appears in the session:** ensure Langfuse supports session
  propagation and that both the session and trace APIs are reachable.
- **No AGENT span:** use
  `openinference-instrumentation-claude-agent-sdk>=0.1.3`; this project locks a
  tested newer version.
- **AgentCore AccessDenied:** grant `bedrock-agentcore:Evaluate` for evaluation
  and `bedrock-agentcore:StartRecommendation` / `GetRecommendation` for the
  recommendation path, plus model invocation permissions required by the
  evaluator.
- **Recommendation failed with "No sessions were identified from input agent
  traces":** the inline Claude Agent SDK spans are not an accepted trace source
  during the Optimization preview. Supply
  `--recommendation-batch-evaluation-arn` instead — see
  [Recommendation trace source: verified limitation](#recommendation-trace-source-verified-limitation).
- **Recommendation failed with "No reusable scores were found for batch eval":**
  the batch evaluation's session logs aged out of CloudWatch retention, or the
  batch used a different evaluator than `--recommendation-evaluator`. Re-run
  `scripts/create_reward_batch_evaluation.py` with the same evaluator.
- **Recommendation failed for another reason:** inspect
  `results/recommendation.json`; it preserves the recommendation ID plus service
  error code/message or the last status on timeout. Generated recommendations
  are not applied automatically.
- **Model not found:** confirm your `ANTHROPIC_BASE_URL` maps the exact model
  string `claude-sonnet-5`.

## References

- [Langfuse Claude Agent SDK integration](https://langfuse.com/integrations/frameworks/claude-agent-sdk)
- [AgentCore on-demand evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-on-demand.html)
- [AgentCore Claude Agent SDK span contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-claude-agent-sdk.html)
- [Start a system prompt recommendation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/recommendations-system-prompt.html)
- [Get recommendation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/recommendations-get.html)
