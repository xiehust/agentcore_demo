# AgentCore Gateway 拦截器按语言路由搜索 PoC

[English](README.en.md)

本 PoC 验证一件事：能否用 AgentCore Gateway 的 interceptor（Lambda）按 query 语言把同一个搜索工具调用分流到不同的搜索服务。

- 英文 query 走 **AgentCore Web Search**
- 中文 query 走 **Exa**

Agent 只看到一个工具 `websearch___WebSearch`，看不到也调用不到 Exa，两边返回的结果格式完全一致。

## 结论

可行，2026-11-18 在 us-east-1 实测通过。

- REQUEST interceptor 改写 `params.name` 后，Gateway **按改写后的工具名路由**，target 收到的是改写后的工具名和参数。这一点 AWS 文档没有明确写，是实测结果。
- RESPONSE interceptor 可以过滤 `tools/list`，也可以重写 `tools/call` 的返回结果。
- 生产使用前，建议向服务团队确认"按改写后的工具名路由"是受支持的行为。

## 架构

```
Agent ──SigV4──▶ AgentCore Gateway (入站认证 AWS_IAM)
                    │
                    ├─ REQUEST interceptor (Lambda)
                    │    ├─ tools/call 非 websearch___WebSearch → 直接返回 -32602 错误
                    │    └─ query 汉字占比 ≥ 30% → 改写为 exa___web_search_exa
                    │
                    ├─▶ target: websearch   (AgentCore Web Search connector)
                    ├─▶ target: exa         (https://mcp.exa.ai/mcp，API key 存在 AgentCore Identity)
                    ├─▶ target: mockexa     (早期测试用的 mock Lambda)
                    │
                    └─ RESPONSE interceptor (同一个 Lambda)
                         ├─ tools/list → 只保留 websearch___WebSearch
                         └─ tools/call → 两种结果统一成同一 JSON 格式
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `deploy.py` | 创建或更新全部资源，资源 ID 写入 `state.json`，可重复执行 |
| `store_exa_key.py` | 从 stdin 读取 Exa API key，存入 AgentCore Identity |
| `cleanup.py` | 删除本 PoC 创建的全部资源 |
| `lambdas/interceptor.py` | REQUEST 和 RESPONSE 两个阶段的逻辑 |
| `lambdas/normalize.py` | 把两种搜索结果转成统一格式 |
| `lambdas/mock_exa.py` | 模拟 Exa 的 Lambda，用于第一轮路由验证 |
| `test.py` | 第一轮：路由到 mock Lambda（4 个用例） |
| `test_exa.py` | 第二轮：路由到真实 Exa（3 个用例） |
| `test_exposure.py` | 只暴露一个工具，隐藏工具不能直接调用（6 个用例） |
| `test_format.py` | 两条路径的返回格式一致（6 个用例） |

开启工具隐藏后，`test.py` 和 `test_exa.py` 里直接调用 mock 或 Exa 的对照用例会被拦截，按设计会失败。当前状态请以 `test_exposure.py` 和 `test_format.py` 为准。

## 运行步骤

### 1. 准备环境

需要一个可创建 IAM 角色、Lambda 和 AgentCore 资源的 AWS 账号，区域为 us-east-1。

```bash
uv venv .venv && uv pip install --python .venv/bin/python boto3 requests
```

### 2. 保存 Exa API key

key 通过 stdin 传入，不会写进文件，也不会出现在命令行参数里：

```bash
printf %s "$EXA_KEY" | .venv/bin/python store_exa_key.py
```

### 3. 部署

```bash
.venv/bin/python deploy.py
```

部署完成后，设置 interceptor 的目标工具（不设置时默认路由到 mockexa）：

```bash
aws lambda update-function-configuration \
  --function-name ic-reroute-poc-interceptor \
  --environment 'Variables={DST_TOOL=exa___web_search_exa}'
```

### 4. 测试

```bash
.venv/bin/python test_exposure.py
.venv/bin/python test_format.py
```

## 实现要点

### 语言判断

统计 query 里非空白、非数字字符中汉字（CJK 统一汉字区段）的占比，≥ 30% 判为中文。中英混合的 "AgentCore 网关拦截器怎么配置" 汉字占比 50%，走 Exa。

### 参数映射

| AgentCore Web Search | Exa `web_search_exa` |
|---|---|
| `query` | `query` |
| `maxResults` | `numResults` |
| 无 | `objective`（必填，用 query 填充） |

### 统一返回格式

`content[0].text` 是如下 JSON，结果外层只保留 `content` 和 `isError`，Exa 返回的 `_meta` 会被去掉：

```json
{
  "provider": "agentcore-web-search | exa",
  "results": [
    {"title": "", "url": "", "publishedDate": "YYYY-MM-DD 或 null", "author": "字符串或 null", "text": ""}
  ]
}
```

### 权限

- Gateway 服务角色只能调用两个 Lambda 和 `InvokeWebSearch`（`arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1`），并且只能读取 Exa 这一个凭证提供方和它对应的 secret。
- 信任策略带 `aws:SourceAccount` 和 `aws:SourceArn` 条件，防止 confused deputy。
- interceptor 配置为 `passRequestHeaders: false`，Lambda 拿不到调用方的 Authorization 头。

## 实测发现

1. Gateway 按 REQUEST interceptor 改写后的工具名路由。
2. RESPONSE 阶段看到的是**改写前**的请求：被转给 Exa 的请求，在 RESPONSE 阶段的工具名仍是 `websearch___WebSearch`。所以返回结果来自哪个 provider，只能根据返回内容判断。
3. REQUEST 阶段短路返回（拒绝调用）后，RESPONSE 阶段仍然会执行，与文档一致。
4. 直接接入 Exa 的 MCP target 会额外暴露 `agent_run` 和 `web_fetch_exa` 两个工具，需要在 RESPONSE 阶段过滤掉。
5. Cedar Policy 评估的是改写后的请求（据文档，本 PoC 未挂 Policy Engine），所以策略要允许 Exa target。

## 局限

- Exa MCP 返回的是纯文本，这里靠解析文本转换格式。Exa 改了格式时，转换代码会原样放行原文，不会报错。生产环境建议改用 Exa REST API（返回 JSON），或者对"无法识别格式"加告警。
- 暴露的工具名固定写在代码里（`EXPOSED_TOOL`）。Gateway 上如果还有其他业务工具，要改成白名单集合。
- 中文 query 会发给第三方（Exa），数据会离开 AWS，需要评估是否符合合规要求。
- interceptor 对 Gateway 上的每个请求都会执行，Lambda 冷启动会增加延迟，可以考虑 provisioned concurrency。

## 清理

```bash
.venv/bin/python cleanup.py
```

会删除 Gateway targets、Gateway、两个 Lambda、两个 IAM 角色和 Exa 的 API key 凭证提供方，最后删除 `state.json`。Lambda 的 CloudWatch 日志组不会被删除。
