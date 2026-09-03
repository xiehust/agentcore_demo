# 关切点 2：第三方权限体系集成 —— sandbox 内"无秘钥"访问 GitHub / GitLab / Vault

> 结论先行：把"无秘钥"拆成三个可落地的层级。**长效凭证（client_secret、PAT、GitHub App 私钥、Vault
> root/approle secret）永远不进 sandbox**；sandbox 里最多只出现"分钟级—小时级的短期 token"，并且
> 优先让凭证根本不经过 sandbox（Gateway 出站鉴权）。AgentCore Runtime 自带 workload identity，
> 通过执行角色即可换取一切，所以"零静态秘钥"在 AgentCore 上是默认可达的。

---

## 1. 三个层级

```
 层级 A  凭证不进 sandbox            层级 B  只进短期 token                 层级 C  用 AWS 身份直接联邦
 ──────────────────────            ────────────────────────              ─────────────────────────
 Agent ──MCP(SigV4/JWT)──▶ Gateway   Agent ──exec role──▶ AgentCore Identity  Agent ──SigV4 签名──▶ Vault /auth/aws/login
            │ 出站注入 OAuth/API key            │ GetWorkloadAccessToken              │ Vault 调 sts:GetCallerIdentity 验签
            ▼                                   │ GetResourceOauth2Token / ApiKey    ▼
        GitHub / GitLab REST                    ▼                              Vault token (TTL 短) → 动态密钥
                                        短期 access token（内存态）
                                        → git / API 调用（GIT_ASKPASS）
 适用：REST API 型工具调用            适用：git clone/push、需要在 sandbox        适用：Vault、任何信任 AWS IAM 的系统
                                     内直接持 token 的 SDK                     （Vault 再作为 GitHub/DB 等的动态密钥源）
```

三层可叠加：例如 **Vault（层级 C）签发 GitHub App installation token（层级 B）**，REST 查询走 Gateway（层级 A）。
层级 A 里 Gateway 只覆盖 MCP 工具调用；git / gh 这类直连 HTTPS 的流量要达到同等效果，用自建的**凭证注入代理**（§3.1 Sidecar 模式：容器内 localhost 进程，或 VPC 内 sandbox 之外的代理）。

---

## 2. AgentCore Identity 的基础机制（层级 B 的地基）

来源：[Get workload access token](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html)、
[Obtain OAuth 2.0 access token](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html)。

1. **Workload identity**：每个 Runtime 自动拥有一个 workload identity。sandbox 内代码用**执行角色**（由平台注入的
   临时凭证，非静态 AK/SK）调用 `GetWorkloadAccessToken`（M2M）或 `GetWorkloadAccessTokenForJWT`（代表终端用户，
   传入 IdP 签发的用户 JWT）。这一步不需要任何秘钥。
2. **Token Vault**：`CreateOauth2CredentialProvider` / `CreateApiKeyCredentialProvider` 把 client_secret / API key 存进
   服务托管的 Secrets Manager（可选客户 KMS CMK），**创建后 sandbox 永远读不到原值**。
3. **换 token**：`GetResourceOauth2Token(workloadIdentityToken, provider, scopes, oauth2Flow=M2M|USER_FEDERATION)` 或
   `GetResourceApiKey(...)`。服务侧完成 OAuth 流程、刷新 token 的存储与续期；返回给 sandbox 的只是 access token。
4. **SDK 糖**：`from bedrock_agentcore.identity.auth import requires_access_token, requires_api_key`
   装饰器在 Runtime 内自动带上 workload token（见 [`demo/secretless_auth/agentcore_identity_tokens.py`](../demo/secretless_auth/agentcore_identity_tokens.py)）。
5. **审计**：`GetWorkloadAccessToken` / `GetResourceOauth2Token` 都记 CloudTrail，token 字段 `HIDDEN_DUE_TO_SECURITY_REASONS`，
   但 provider 名、scopes、flow 可见 —— 直接服务关切点 3。
6. **计费**：通过 Runtime / Gateway 使用 Identity **不额外收费**；独立调用 $0.010 / 1,000 次。
7. **私有 IdP**：`customOauth2ProviderConfig` 现已有 `privateEndpoint` / `privateEndpointOverrides` 字段
   （本机 botocore 1.42.97 可见），用于 VPC 内自建 GitLab / Keycloak 等；此前的 interceptor 绕行方案见
   [`13-private-idp-workaround`](../../13-private-idp-workaround)。

---

## 3. 逐系统方案

### 3.1 GitHub

**官方参考实现**：[agentcore-samples › 02-outbound-auth › 03-outbound-auth-github](https://github.com/awslabs/agentcore-samples/tree/main/01-features/05-authenticate-and-authorize/02-outbound-auth/03-outbound-auth-github)
—— Strands agent 跑在 Runtime 上，用 `GithubOauth2` credential provider + `USER_FEDERATION`（3LO）代表登录用户读取私有仓库。
这是 GitHub 场景的**首选路径**，本目录 [`demo/secretless_auth/github_agent_3lo.py`](../demo/secretless_auth/github_agent_3lo.py) 按同一模式实现并补上了 `git clone`。

流程（sandbox 内始终没有静态凭证）：

```
运维一次性  create_oauth2_credential_provider(GithubOauth2, clientId, clientSecret) ──▶ 返回 callbackUrl
            把 callbackUrl 填进 GitHub OAuth App 的 "Authorization callback URL"     （client_secret 从此只在 Token Vault）

首次调用    tool 内 @requires_access_token(auth_flow="USER_FEDERATION") ──▶ 无 token
            ──▶ on_auth_url(url) 把 GitHub 授权链接流式返回给用户
用户同意    GitHub ──▶ AgentCore callback ──▶ 你的 callback server 调 CompleteResourceTokenAuth（把 token 绑到登录用户）
后续调用    装饰器直接注入缓存的 GitHub access token；刷新由 Identity 托管
```

核心代码（摘自官方 `github_agent.py`，要点已注释）：

```python
from bedrock_agentcore.identity.auth import requires_access_token

@tool
def inspect_github_repos() -> str:
    """List the user's private GitHub repositories."""

    # 装饰器嵌套在 tool 里：access_token 不会出现在给模型看的工具 schema 中
    @requires_access_token(
        provider_name="github-provider",          # create_oauth2_credential_provider 时的 name
        scopes=["repo", "read:user"],
        auth_flow="USER_FEDERATION",              # 3LO：代表用户
        on_auth_url=on_auth_url,                  # 首次：把授权 URL 推给用户（sample 里通过 streaming queue）
        force_authentication=False,               # 有缓存 token 就直接用
        callback_url=os.environ["CALLBACK_URL"],  # session binding：你的 callback server
    )
    def _tool(access_token: Optional[str] = None) -> str:
        if not access_token:
            return json.dumps({"auth_required": True, "message": "Please authorize via the link."})
        headers = {"Authorization": f"Bearer {access_token}"}
        return httpx.get("https://api.github.com/user/repos?type=private", headers=headers).text

    return _tool()
```

```python
# 运维侧（sample 的 outbound_auth_github.py）
resp = control.create_oauth2_credential_provider(
    name="github-provider", credentialProviderVendor="GithubOauth2",
    oauth2ProviderConfigInput={"githubOauth2ProviderConfig": {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}})
print(resp["callbackUrl"])   # → GitHub OAuth App → Authorization callback URL

# callback server（sample 的 oauth2_callback_server.py）收到 GitHub 重定向后：
identity_client.complete_resource_token_auth(session_uri=session_id, user_identifier=user_token_identifier)
```

sample 已知坑：`redirect_uri_mismatch` = GitHub OAuth App 的 callback URL 没有精确复制 `callbackUrl`；
`bad_verification_code` = 授权码过期/已用，重新触发一次拿新链接；只看到公共仓库 = 缺 `repo` scope。

**什么时候 3LO 不够用**——没有"登录用户"的服务型 agent（定时任务、CI 修复 bot），或需要 clone/push 且要按仓库/权限收窄：

| 场景 | 方案 | sandbox 内出现什么 | 长效凭证在哪 |
|---|---|---|---|
| 代表终端用户（读 PR、评论、clone 自己的仓库） | **官方路径**：`GithubOauth2` + `USER_FEDERATION`（上文） | 用户级 access token（GitHub App 用户 token 8h 过期） | Token Vault |
| 服务身份操作仓库（无人值守 clone / push / 开 PR） | **GitHub App installation token**：私钥放 Secrets Manager，由 **token broker Lambda** 签 JWT 换 1h 的 installation token，可按 `repositories` + `permissions` 收窄；sandbox 用执行角色 `lambda:InvokeFunction` 拉 token（[`demo/secretless_auth/github_app_broker/lambda_function.py`](../demo/secretless_auth/github_app_broker/lambda_function.py)） | 1h、仓库级、权限级 token | Secrets Manager（仅 Lambda 角色可读） |
| 只调 REST API（issues、code search） | **Gateway OpenAPI target + OAuth/API key credential provider**：sandbox 只拿到 MCP 工具，鉴权头由 Gateway 注入 | 无 | Token Vault |
| Vault 已是企业密钥中心 | Vault **GitHub secrets engine**（`vault-plugin-secrets-github`）签发 installation token，sandbox 用层级 C 登录 Vault 后读取 | 1h token | Vault |

无论 token 来自哪条路径，git 命令行都用 `GIT_ASKPASS` 传递（[`demo/secretless_auth/git_askpass.py`](../demo/secretless_auth/git_askpass.py)，
`github_agent_3lo.py` 的 `clone_repo` 工具内联了同一做法）：每次 git 需要凭证时才取 token，**不落盘、不写进 remote URL、不进 agent 进程环境变量**；
`GIT_TERMINAL_PROMPT=0`、`git -c credential.helper=` 防止 git 自己缓存。

#### gh CLI 模式（coding agent 常见：Claude Code / Codex / 自研 agent 直接调 `gh`）

`gh` 找 token 的顺序只有三种，逐条评估：

| gh 的凭证来源 | sandbox 内的问题 | 结论 |
|---|---|---|
| `gh auth login` / `gh auth login --with-token` | token 明文写入 `~/.config/gh/hosts.yml`（microVM 无系统 keyring）；若目录在 `sessionStorage` 上还会跨 stop/resume 持久化 | **禁用**（shim 直接拦掉 `gh auth`） |
| agent 进程全局 `export GH_TOKEN=...` | token 进入 agent 进程及**所有**子进程的环境，`env`、`/proc/*/environ`、崩溃日志、`shell` 工具输出都可能带出 | 不要 |
| **每次调用注入子进程环境**：`env GH_TOKEN=<短期 token> gh ...` | token 只存在于这一次 `gh` 进程的生命周期内；gh 有 `GH_TOKEN` 时**不会**持久化任何凭证 | **采用** |

两种实现，任选（都复用 §3.1 的 token 来源：3LO 用户 token、broker 的 installation token、或 Identity API key）：

**① `gh` shim 放在 PATH 最前面**（[`demo/secretless_auth/gh_wrapper.sh`](../demo/secretless_auth/gh_wrapper.sh)）——对 agent 透明，
任何工具（包括 `shell` 工具里的 `gh pr list`）都自动走它：

```bash
# /opt/shim/gh   （PATH=/opt/shim:$PATH；真 gh 在 $GH_REAL，默认 /usr/bin/gh）
[[ "$1" == "auth" ]] && { echo "gh auth is disabled" >&2; exit 126; }      # 禁止持久化登录
[[ -z "${GH_ALLOWED_SUBCOMMANDS:-}" || " $GH_ALLOWED_SUBCOMMANDS " == *" $1 "* ]] || exit 126   # 可选子命令白名单
TOKEN="$(python3 /opt/shim/git_askpass.py --token)"                         # 现取：3LO / broker / Identity API key
export GH_CONFIG_DIR=/dev/shm/gh-config                                      # gh 自己的配置放 tmpfs
exec env GH_TOKEN="$TOKEN" GH_PROMPT_DISABLED=1 "$GH_REAL" "$@"              # exec：token 只在 gh 进程环境里
```

**② Strands tool 内注入**（[`github_agent_3lo.py`](../demo/secretless_auth/github_agent_3lo.py) 的 `gh` 工具）——token 来自 `@requires_access_token`，
只传给 `subprocess.run` 的 `env`：

```python
@tool
def gh(args: str) -> str:
    argv = shlex.split(args)
    if argv[0] not in {"pr", "issue", "repo", "api", "search", "release", "run", "workflow"}:   # 没有 "auth"
        return "gh: subcommand not allowed"

    @_with_github_token                                   # 嵌套装饰器，同官方 sample
    def _run(access_token: Optional[str] = None) -> str:
        env = {**os.environ, "GH_TOKEN": access_token, "GH_PROMPT_DISABLED": "1", "GH_CONFIG_DIR": "/dev/shm/gh-config"}
        proc = subprocess.run(["gh", *argv], env=env, capture_output=True, text=True, timeout=120)
        return proc.stdout if proc.returncode == 0 else f"gh failed: {proc.stderr[-800:]}"
    return _run()
```

**真机验证（2026-09-03，us-east-2，容器部署 microVM，`scripts/03-deploy-gh-shim-agent.sh` + `04-verify-gh-shim.py`，
结果 [`results/gh_shim_verification.json`](../results/gh_shim_verification.json)）——11/11 通过：**

| 检查 | 取证方式 | 结果 |
|---|---|---|
| `gh` 解析到 shim | agent 进程 `shutil.which` + 同 session 内 `InvokeAgentRuntimeCommand` 的 `command -v gh` | `/opt/shim/gh` |
| agent 进程环境无 token | `/proc/<agent pid>/environ` 中 `GH_TOKEN`/`GITHUB_TOKEN` 计数 | 0 |
| 磁盘无凭证 | `~/.config/gh/hosts.yml`、`~/.git-credentials`、`grep -rl 'gho_\|ghp_\|ghs_' /root /app /tmp /dev/shm` | 均无 |
| token 只进 gh 子进程 | `GH_REAL` 指向只打印 `GH_TOKEN` 前缀/长度的桩 | `GH_TOKEN_PRESENT_IN_CHILD prefix=gho_ len=40`，随后 shell 环境仍为 0 |
| 真实 GitHub 调用 | `gh api /user --jq .login`、`gh repo list` | 返回真实登录名与仓库 |
| `gh auth login` 被拦 / 白名单外子命令被拦 | 退出码 | 126 / 126 |
| 模型驱动回合 | Strands agent 用 `run_gh` 工具回答 "Who am I on GitHub" | 正确登录名，`tool_calls=1`，agent 环境仍无 token |

部署链路：GitHub token 存入 **AgentCore Identity API key credential provider**（Token Vault）→ shim 调 `git_askpass.py --token`
→ `GetResourceApiKey(workloadIdentityToken, provider)` → 注入 gh 子进程。执行角色只有 `GetResourceApiKey`/`GetWorkloadAccessToken*`
（限定 provider ARN 与 workload identity directory）+ `secretsmanager:GetSecretValue` on `bedrock-agentcore-identity!*`。

验证过程中踩到的两个坑（已写进代码）：

1. **Runtime 自动创建的 workload identity 不能自取 token**：sandbox 内进程调 `GetWorkloadAccessToken(workloadName=<runtime 同名>)`
   报 `ValidationException: WorkloadIdentity is linked to a service and cannot retrieve an access token by the caller`。
   平台是按**请求**把 workload access token 以 header（`X-Amzn-Bedrock-AgentCore-Runtime-Workload-AccessToken`）注入给 agent 进程，
   SDK 存进 contextvar。shim 是独立进程，所以 agent 在每次 entrypoint 开头把 `BedrockAgentCoreContext.get_workload_access_token()`
   写到 `/dev/shm/agentcore/workload_token`（0600，tmpfs），`git_askpass.py` 优先读它，读不到再退回 `GetWorkloadAccessToken(WORKLOAD_NAME)`
   （ECS/EC2 等非 Runtime 托管时使用显式创建的 workload identity）。这个文件里是 agent 的**身份**令牌，不是 GitHub 凭证，
   且必须配合执行角色才能换 API key。
2. **SigV4 入站鉴权时 WAT 只在带 `runtimeUserId` 时注入**：不传 `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` 头，请求里根本没有 WAT header，
   `requires_access_token` / `requires_api_key` 会报 "Workload access token has not been set…please specify the
   X-Amzn-Bedrock-AgentCore-Runtime-User-Id header"。JWT 入站（官方 sample 用 Cognito）则总是注入。调用方要么用 JWT 入站，
   要么在 `InvokeAgentRuntime` 上传 `runtimeUserId`（任意稳定的不透明 id，勿含 PII）。

注意事项：

- **token 类型与 gh 命令的匹配**：3LO 拿到的是用户 OAuth token（`gho_…`），`gh` 全部命令可用；broker 给的是 GitHub App
  installation token（`ghs_…`），`gh pr/issue/api` 对仓库资源可用，但需要"用户身份"的命令（`gh api /user`、`gh auth status` 的身份显示）
  会 403 `Resource not accessible by integration`——服务型 agent 只用仓库级命令即可。
- `gh repo clone` / `gh pr checkout` 内部调 git，gh 会通过自己的 credential helper 把同一个 `GH_TOKEN` 给 git，无需再配 `GIT_ASKPASS`。
- GitHub Enterprise Server 用 `GH_ENTERPRISE_TOKEN` + `GH_HOST`，shim 里同样按调用注入。
- 每次 `gh` 调用都取一次 token：Identity 侧有缓存、broker token 1 小时有效，若调用非常频繁可在 agent 进程内存中缓存到过期前
  （不要缓存到磁盘）。
- 同一 microVM 内是 root，`/proc/<pid>/environ` 对同用户可读——这个模式**缩短的是 token 暴露面和寿命**，不是进程间的安全边界；
  sandbox 内恶意代码的防线仍是关切点 3 的 Gateway/Policy 与 VPC 出网管控。

#### Sidecar / 凭证注入代理模式（token 完全不经过 agent 代码）

思路：agent、git、gh 都把 GitHub 请求发给一个**代理**，代理在出口处补上真正的凭证。agent 进程、环境变量、git/gh 配置里
从头到尾没有 token——这比 shim 又前进一层。AgentCore **Gateway 就是这个模式的托管版**（MCP 工具 + 出站 credential provider），
但它只服务 MCP 工具调用；git smart-HTTP 和 gh 的 REST/GraphQL 需要自己搭一个 HTTP 层代理。

先澄清"sidecar"在 AgentCore 上的形态：microVM 只运行**一个**容器，没有 K8s 式 sidecar 容器。可行的两种落法：

| 形态 | 部署 | sandbox 内有什么 | 防的是什么 | 代价 |
|---|---|---|---|---|
| **A. 容器内 localhost 代理进程** | 容器 entrypoint 同时拉起 `credential_proxy.py --listen 127.0.0.1:8081` 与 agent（supervisor / `&`） | 代理进程内存中有 token；agent 进程没有 | 误泄露：日志、`env` 转储、prompt injection 读环境、gh/git 落盘 | 同一 root 用户，恶意代码仍可读代理进程内存或直接调它——**不是安全边界** |
| **B. VPC 内 sandbox 之外的代理**（EC2 / ECS / 内部 ALB） | Runtime 走 VPC 模式，SG 只放行 runtime → 代理；代理用自己的角色 / Secrets Manager 取 GitHub 凭证；`--listen 0.0.0.0:8081 --require-proxy-token` | **什么 GitHub 凭证都没有**，最多一个低价值的 `X-Proxy-Token` | 含恶意代码在内：sandbox 拿不到能带出去的 GitHub token | 多一个组件；代理成为出口审计点（顺带满足关切点 3）；需要 VPC 模式 |

demo：[`demo/secretless_auth/credential_proxy.py`](../demo/secretless_auth/credential_proxy.py)（仅标准库，同一份代码两种部署）。
路由按 GHES 风格设计，git 和 gh 都能指过来：

```
/api/graphql        -> https://api.github.com/graphql       Authorization: Bearer <token>
/api/v3/<rest>      -> https://api.github.com/<rest>        Authorization: Bearer <token>
/<owner>/<repo>...  -> https://github.com/<owner>/<repo>... Authorization: Basic x-access-token:<token>   （git smart HTTP）
```

核心代码：

```python
def route(path, api_upstream, git_upstream):                 # 见上表
    if path.startswith("/api/v3/"): return api_upstream, path[len("/api/v3"):], "bearer"
    if path.startswith("/api/graphql"): return api_upstream, path[len("/api"):], "bearer"
    return git_upstream, path, "basic"

class Handler(BaseHTTPRequestHandler):
    def _forward(self):
        if proxy_token and self.headers.get("X-Proxy-Token") != proxy_token:      # 形态 B：校验调用方
            return self._reply(401, b"missing or invalid X-Proxy-Token\n")
        base, upstream_path, style = route(self.path, api_upstream, git_upstream)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in STRIP_FROM_CLIENT}  # 丢掉客户端自带的 Authorization / X-Proxy-Token
        headers["Authorization"] = auth_header(style, tokens.get())               # tokens = TokenCache(resolve_token)，内存缓存到过期
        ...转发并回传响应；上游 401 时 tokens.invalidate() 让下一次重新取 token
```

形态 A 的容器 entrypoint 与客户端接法：

```bash
# entrypoint.sh：先起代理，再起 agent（同一容器、同一 microVM）
python3 /app/credential_proxy.py --listen 127.0.0.1:8081 &      # token 来源沿用 git_askpass.py：TOKEN_SOURCE=broker|identity
git config --global url."http://127.0.0.1:8081/".insteadOf "https://github.com/"   # 任何 git 命令自动走代理，不弹凭证提示
exec python3 /app/agent.py

# agent 内 / shell 工具内：直接打代理，不带任何 Authorization
git clone https://github.com/org/repo
curl http://127.0.0.1:8081/api/v3/user/repos?type=private
```

形态 B（VPC 内代理）的接法：

```bash
# 代理侧（EC2 / ECS，SG 只放行 runtime SG → 8081）
PROXY_TOKEN=<per-session-or-per-runtime secret> TOKEN_SOURCE=broker BROKER_FUNCTION_ARN=arn:... \
python3 credential_proxy.py --listen 0.0.0.0:8081 --require-proxy-token

# sandbox 侧（Runtime 为 VPC 模式，PROXY_TOKEN 通过执行角色从 Secrets Manager / broker 取得，本身不是 GitHub 凭证）
git config --global url."http://git-proxy.internal:8081/".insteadOf "https://github.com/"
git -c http.extraHeader="X-Proxy-Token: $PROXY_TOKEN" clone https://github.com/org/repo
curl -H "X-Proxy-Token: $PROXY_TOKEN" http://git-proxy.internal:8081/api/v3/user/repos
```

注意事项：

- **gh 接代理**：gh 只能通过 `GH_HOST=<proxy-host>` 把主机当 GHES，且默认要求 https（要给代理配证书并让容器信任），
  API 路径会变成 `/api/v3`、`/api/graphql`——demo 的路由正是为此设计，但**未做真机验证**；gh 场景更省事的仍是上文的 shim。
- 形态 A 与 shim 的取舍：shim 改动最小；代理的好处是 agent 代码"零凭证意识"，而且 git 与 REST 一套机制。二者防的都是误泄露，
  不是恶意代码；需要后者就上形态 B 或 Gateway。
- 形态 B 的调用方认证：git/gh 不能做 SigV4，所以用"网络路径（VPC + SG）+ 低价值 proxy token"；proxy token 可以由 broker Lambda
  按 session 签发、代理侧校验并映射到对应仓库权限，这样代理还能做**按 session 的仓库级授权与审计日志**。
  形态 B 的代理在 VPC 内以明文 HTTP 接收请求时，请配合 TLS（内部 ALB / 自签证书）或仅限同一 VPC 私网。
- 代理是出口单点：要把它的访问日志（method、path、session、状态码，**不记 Authorization**）接进关切点 3 的审计链。
- 对 GitLab 同理：把 `api_upstream` / `git_upstream` 指向 GitLab，`auth_header` 改成 `PRIVATE-TOKEN` 或 `Bearer`。

### 3.2 GitLab（SaaS 或自建）

- GitLab **不支持 `client_credentials`**（gitlab-org/gitlab#419240 仍开放），所以 M2M 没有标准 OAuth 路径。
- 代表用户：Identity `CustomOauth2` + discovery URL `https://gitlab.example.com/.well-known/openid-configuration`，
  `USER_FEDERATION`；access token 2 小时有效、refresh 由 Identity 托管。自建 GitLab 在 VPC 内 → `privateEndpoint`。
- 服务身份：**Project/Group Access Token**（可设过期、可 rotate API）存入 `CreateApiKeyCredentialProvider`，
  sandbox 通过 `GetResourceApiKey` 取；再叠加 Gateway（层级 A）让 REST 调用完全不接触 token。
  轮换：GitLab `POST /projects/:id/access_tokens/:token_id/rotate` → `UpdateApiKeyCredentialProvider`，用 EventBridge 定时。
- CI job token / `glab` device flow 不适合服务端 agent。

### 3.3 HashiCorp Vault（层级 C，真正的零秘钥）

Vault [AWS auth method（iam 类型）](https://developer.hashicorp.com/vault/docs/auth/aws)：客户端用当前 AWS 凭证对
`sts:GetCallerIdentity` 做 SigV4 签名，把 method/url/body/headers 四元组 POST 到 `/v1/auth/aws/login`，Vault 代为
调用 STS 验证签名，映射到 Vault role（`bound_iam_principal_arn` = Runtime 执行角色 ARN），返回短 TTL Vault token。

- sandbox 内**没有任何 Vault 秘钥**：签名用的是平台注入的执行角色临时凭证。
- Vault 侧 `iam_server_id_header_value` 要求客户端带 `X-Vault-AWS-IAM-Server-ID` 头并参与签名，防重放到别的 Vault。
- Vault 需要能访问 STS（VPC endpoint 或出网）；自建 Vault 在 VPC → Runtime 用 VPC 模式直连。
- 登录后用 Vault 的动态密钥：database / AWS / GitHub / SSH CA / PKI，全部短 TTL，agent 用完即弃。
- demo：[`demo/secretless_auth/vault_aws_iam_login.py`](../demo/secretless_auth/vault_aws_iam_login.py)，签名部分只依赖 botocore，
  单元测试验证签名请求结构（不需要真实 Vault）。

Vault 侧配置示例：

```bash
vault auth enable aws
vault write auth/aws/config/client iam_server_id_header_value=vault.internal.example.com
vault write auth/aws/role/agentcore-runtime \
  auth_type=iam \
  bound_iam_principal_arn=arn:aws:iam::<acct>:role/<runtime-execution-role> \
  policies=agent-readonly token_ttl=15m token_max_ttl=1h
```

---

## 4. 与"无秘钥"相配套的 sandbox 卫生要求

| 项 | 做法 |
|---|---|
| 执行角色最小化 | 只给 `bedrock-agentcore:GetWorkloadAccessToken*`、`GetResourceOauth2Token`、`GetResourceApiKey`（按 provider ARN 限定）、`lambda:InvokeFunction`（broker）、模型调用 |
| 每工具一凭证 | Strands 里不同 MCP client 可用不同 `AssumeRole` 会话（session policy 收窄），见 AWS Security Blog *Secure AI agent access patterns…* |
| token 不落盘 | `GIT_ASKPASS` / 内存变量；`sessionStorage` 里不要写 `.netrc`、`.git-credentials` |
| 日志脱敏 | 审计 hook（关切点 3）对 `Authorization`、`token`、`secret` 字段做 redaction |
| 用户会话绑定 | 多租户下 `runtimeSessionId` 与终端用户由后端绑定，用 `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` |
| 出网收口 | Gateway 之外的直连（git over https）在 VPC 模式下用 Network Firewall 域名白名单（关切点 3） |

---

## 5. 演示路径

1. **GitHub 3LO（官方 sample 原样跑通）**：`python outbound_auth_github.py` 建 Cognito + `GithubOauth2` provider，
   把 `callbackUrl` 填进 GitHub OAuth App；起 `oauth2_callback_server.py` 与 Streamlit，问 "What are my private repositories?"，
   点授权链接后再问一次即返回私有仓库。随后部署本目录 `github_agent_3lo.py`，追加演示 `clone_repo` 工具：
   仓库落到 `/mnt/workspace`，而 `git config -l`、`~/.git-credentials`、`env` 中均无 token。
2. **GitLab**：`agentcore_identity_tokens.py create-gitlab-provider --discovery-url ... --client-id ...`
   创建 CustomOauth2 provider；Runtime 内 `@requires_access_token(..., auth_flow="USER_FEDERATION")` 调 `/api/v4/user`，
   展示 CloudTrail 里 `GetResourceOauth2Token` 事件 token 被隐藏、provider/scopes 可见。
3. **GitHub 服务身份**：部署 `github_app_broker` Lambda，sandbox 内 `GIT_ASKPASS=git_askpass.py git clone ...` 成功，
   token 为 1 小时、仓库级；`ALLOWED_REPOS` 之外的请求被 broker 拒绝。
4. **Vault**：dev server（VPC 内 EC2）启用 aws auth；sandbox 内 `vault_aws_iam_login.py --vault-addr ... --role agentcore-runtime`
   返回 15 分钟 token 并读取 `secret/data/demo`。
