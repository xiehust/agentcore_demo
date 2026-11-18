# AgentCore Gateway 拦截器按语言路由搜索 PoC

[English](README.en.md)

本 PoC 验证一件事：能否用 AgentCore Gateway 的 interceptor（Lambda）按 query 语言把同一个搜索工具调用分流到不同的搜索服务。

- 英文 query 走 **AgentCore Web Search**
- 中文 query 走 **AnySearch**（默认）或 **Exa**，部署时用 `--cn-provider` 选择

Agent 只看到一个工具 `websearch___WebSearch`，看不到也调用不到第三方搜索工具，两边返回的结果格式完全一致。

## 结论

可行，2026-11-18 在 us-east-1 实测通过，AnySearch 和 Exa 两个版本都验证过。

- REQUEST interceptor 改写 `params.name` 后，Gateway **按改写后的工具名路由**，target 收到的是改写后的工具名和参数。这一点 AWS 文档没有明确写，是实测结果。
- RESPONSE interceptor 可以过滤 `tools/list`，也可以重写 `tools/call` 的返回结果。
- 生产使用前，建议向服务团队确认"按改写后的工具名路由"是受支持的行为。

## 架构

```
Agent ──SigV4──▶ AgentCore Gateway (入站认证 AWS_IAM)
                    │
                    ├─ REQUEST interceptor (Lambda)
                    │    ├─ tools/call 非 websearch___WebSearch → 直接返回 -32602 错误
                    │    └─ query 汉字占比 ≥ 30% → 改写为 DST_TOOL
                    │         (anysearch___search 或 exa___web_search_exa)
                    │
                    ├─▶ target: websearch   (AgentCore Web Search connector)
                    ├─▶ target: anysearch   (https://api.anysearch.com/mcp，API key 存在 AgentCore Identity)
                    ├─▶ target: exa         (https://mcp.exa.ai/mcp，API key 存在 AgentCore Identity)
                    ├─▶ target: mockexa     (早期测试用的 mock Lambda)
                    │
                    └─ RESPONSE interceptor (同一个 Lambda)
                         ├─ tools/list → 只保留 websearch___WebSearch
                         └─ tools/call → 各家结果统一成同一 JSON 格式
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `deploy.py` | 创建或更新全部资源，资源 ID 写入 `state.json`，可重复执行 |
| `store_anysearch_key.py` | 从 stdin 读取 AnySearch API key，存入 AgentCore Identity |
| `store_exa_key.py` | 从 stdin 读取 Exa API key，存入 AgentCore Identity |
| `probe_anysearch.py` | 不经过 Gateway 直接调用 AnySearch MCP，查看工具列表和原始返回 |
| `cleanup.py` | 删除本 PoC 创建的全部资源 |
| `lambdas/interceptor.py` | REQUEST 和 RESPONSE 两个阶段的逻辑 |
| `lambdas/normalize.py` | 把各家搜索结果转成统一格式 |
| `lambdas/mock_exa.py` | 模拟 Exa 的 Lambda，用于第一轮路由验证 |
| `test.py` | 第一轮：路由到 mock Lambda（4 个用例） |
| `test_exa.py` | 第二轮：路由到真实 Exa（3 个用例） |
| `test_exposure.py` | 只暴露一个工具，隐藏工具不能直接调用，中文路由到所选 provider（9 个用例） |
| `test_format.py` | 两条路径的返回格式一致（6 个用例） |

开启工具隐藏后，`test.py` 和 `test_exa.py` 里直接调用 mock 或 Exa 的对照用例会被拦截，按设计会失败。当前状态请以 `test_exposure.py` 和 `test_format.py` 为准，它们从 `state.json` 的 `cn_provider` 读取当前中文路由。

## 运行步骤

### 1. 准备环境

需要一个可创建 IAM 角色、Lambda 和 AgentCore 资源的 AWS 账号，区域为 us-east-1。

```bash
uv venv .venv && uv pip install --python .venv/bin/python boto3 requests
```

### 2. 保存第三方搜索的 API key

key 通过 stdin 传入，不会写进文件，也不会出现在命令行参数里。只需要保存你要用的那一家：

```bash
printf %s "$ANYSEARCH_KEY" | .venv/bin/python store_anysearch_key.py
printf %s "$EXA_KEY" | .venv/bin/python store_exa_key.py
```

### 3. 部署

```bash
.venv/bin/python deploy.py --cn-provider anysearch   # 或 exa / mock
```

`deploy.py` 只为已保存 key 的服务创建 target，然后把 interceptor 的 `DST_TOOL` 环境变量设成所选 provider 的工具名。切换 provider 时用新参数重新执行即可。

### 4. 测试

```bash
.venv/bin/python test_exposure.py
.venv/bin/python test_format.py
```

## 实现要点

### 语言判断

统计 query 里非空白、非数字字符中汉字（CJK 统一汉字区段）的占比，≥ 30% 判为中文。中英混合的 "AgentCore 网关拦截器怎么配置" 汉字占比 50%，走中文路由。

### 参数映射

| AgentCore Web Search | AnySearch `search` | Exa `web_search_exa` |
|---|---|---|
| `query` | `query` | `query` |
| `maxResults` | `max_results`（上限 10，超过时截到 10） | `numResults` |
| 无 | 无 | `objective`（必填，用 query 填充） |

AnySearch 的 `domain`、`sub_domain` 等垂直搜索参数没有用到，只做通用搜索。

### 凭证注入

| target | 请求头 | Gateway 配置 |
|---|---|---|
| anysearch | `Authorization: Bearer <key>` | `credentialParameterName=Authorization`，`credentialPrefix="Bearer "` |
| exa | `x-api-key: <key>` | `credentialParameterName=x-api-key` |

AnySearch 官方配置里的 `X-Anysearch-Client: mcp/1.0.0` 头实测不带也能正常搜索，而 Gateway 的 API key 凭证只能注入一个头，所以没有配置它。

### 统一返回格式

`content[0].text` 是如下 JSON，结果外层只保留 `content` 和 `isError`，第三方返回的 `_meta` 会被去掉：

```json
{
  "provider": "agentcore-web-search | anysearch | exa",
  "results": [
    {"title": "", "url": "", "publishedDate": "YYYY-MM-DD 或 null", "author": "字符串或 null", "text": ""}
  ]
}
```

各家原始格式的识别方式：

| provider | 原始格式 | 识别特征 |
|---|---|---|
| AgentCore Web Search | JSON | 能解析出带 `results` 的 JSON |
| AnySearch | Markdown：`### N. 标题`、`- **URL**: ...`、摘要行末尾 ` date: Aug 14, 2026`，最后是 `request_id` 的 yaml 块 | 以 `## Search Results` 开头 |
| Exa | 纯文本：`Title:/URL:/Published:/Author:/Highlights:`，`---` 分隔 | 以 `Title:` 开头 |

### 权限

- Gateway 服务角色只能调用两个 Lambda 和 `InvokeWebSearch`（`arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1`），每个第三方服务各有一条 inline policy，只能读取它自己的凭证提供方和对应的 secret。
- 信任策略带 `aws:SourceAccount` 和 `aws:SourceArn` 条件，防止 confused deputy。
- interceptor 配置为 `passRequestHeaders: false`，Lambda 拿不到调用方的 Authorization 头。

## 实测发现

1. Gateway 按 REQUEST interceptor 改写后的工具名路由。
2. RESPONSE 阶段看到的是**改写前**的请求：被转给第三方的请求，在 RESPONSE 阶段的工具名仍是 `websearch___WebSearch`。所以返回结果来自哪个 provider，只能根据返回内容判断。
3. REQUEST 阶段短路返回（拒绝调用）后，RESPONSE 阶段仍然会执行，与文档一致。
4. 第三方 MCP target 会带出额外工具：Exa 有 `agent_run`、`web_fetch_exa`，AnySearch 有 `batch_search`、`extract`、`get_sub_domains`、`score`。都在 RESPONSE 阶段过滤掉，REQUEST 阶段也拒绝直接调用。
5. AnySearch 用无效 key 调用会返回 `isError: true`，而经 Gateway 的中文搜索正常返回结果，说明 Gateway 确实注入了存在 Identity 里的 key。
6. Cedar Policy 评估的是改写后的请求（据文档，本 PoC 未挂 Policy Engine），所以策略要允许第三方 target。

## 局限

- AnySearch 和 Exa 的 MCP 返回都是文本，这里靠解析文本转换格式。对方改了格式时，转换代码会原样放行原文，不会报错。生产环境建议对"无法识别格式"加告警。
- AnySearch 的日期有时是相对时间（如 "2 days ago"），目前转成 `null`。
- AnySearch 的 MCP 说明要求 Agent 搜索后调用 `score` 工具反馈结果质量。本 PoC 隐藏了 `score`，没有提交反馈；如果 AnySearch 的计费或配额依赖这个反馈，需要另行处理。
- 暴露的工具名固定写在代码里（`EXPOSED_TOOL`）。Gateway 上如果还有其他业务工具，要改成白名单集合。
- 中文 query 会发给第三方（AnySearch 或 Exa），数据会离开 AWS，需要评估是否符合合规要求。
- interceptor 对 Gateway 上的每个请求都会执行，Lambda 冷启动会增加延迟，可以考虑 provisioned concurrency。

## 清理

```bash
.venv/bin/python cleanup.py
```

会删除 Gateway targets、Gateway、两个 Lambda、两个 IAM 角色和 AnySearch、Exa 的 API key 凭证提供方，最后删除 `state.json`。Lambda 的 CloudWatch 日志组不会被删除。
