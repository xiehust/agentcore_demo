# AgentCore Runtime：三方文件系统 / 无秘钥三方权限 / 操作审计 —— 方案调研与 Demo

## [English](README.en.md)

对应一家金融行业（FSI）客户 Infra 负责人在 AgentCore 交流后提出的关切点：

| # | 客户关切 | 一句话结论 | 文档 | Demo |
|---|---|---|---|---|
| 1 | 三方文件系统（FUSE）支持与 workaround | **microVM guest 内核没有 FUSE 驱动**（真机实测 `ENODEV`），不是权限问题；用 Runtime 原生 `filesystemConfigurations`（sessionStorage / EFS / S3 Files）替代，非 AWS 存储走用户态同步；硬性 FUSE 需求走 Instances 计算类型 | [docs/01](docs/01-filesystem-fuse.md) | `scripts/01-fuse-probe.py`、`scripts/02-create-runtime-with-fs.sh`、`demo/fs_workaround/` |
| 2 | 第三方权限体系集成，sandbox 内无秘钥访问 GitHub / GitLab / Vault | 三层：**凭证不进 sandbox**（Gateway 出站鉴权）→ **只进短期 token**（AgentCore Identity Token Vault —— GitHub 按[官方 outbound-auth sample](https://github.com/awslabs/agentcore-samples/tree/main/01-features/05-authenticate-and-authorize/02-outbound-auth/03-outbound-auth-github) 的 3LO 模式；服务身份用 GitHub App broker + `GIT_ASKPASS`）→ **AWS 身份直接联邦**（Vault AWS IAM auth，零秘钥） | [docs/02](docs/02-secretless-third-party-auth.md) | `demo/secretless_auth/` |
| 3 | 监测模块：tool call / network access 审计 | 四层采集：CloudTrail（API）、AgentCore Observability OTEL span + 确定性审计 hook（tool call）、Gateway + Policy `LOG_ONLY`（授权即审计）、**VPC 模式**下 Flow Logs / DNS 日志 / Network Firewall（网络）；PUBLIC 模式没有出网明细 | [docs/03](docs/03-audit-observability.md) | `demo/audit/` |
| 4 | 评估模块：自建 LLM judge vs 托管 Evaluations | *本轮不做（按需求方要求移出范围）* | — | — |

---

## 关键实测证据（2026-09-03，us-east-2）

`scripts/01-fuse-probe.py` 通过 `InvokeAgentRuntimeCommand` 在真实 microVM session 内执行探测（完整数据 [`results/fuse_probe.json`](results/fuse_probe.json)）：

```
kernel                      Linux 6.1.161-18.298.amzn2023.aarch64 (Firecracker guest)
runs_as_root                true      CapEff = 全部 41 个 capability，Seccomp = 0
tmpfs_mount_allowed         true      -> 不是权限受限
dev_fuse_exists             false
kernel_fuse_registered      false     /proc/filesystems 无 fuse
mknod_fuse_open_error       OSError: [Errno 19] No such device   <- 驱动不存在
mount -t fuse               unknown filesystem type 'fuse'
loadable_modules            false     /proc/modules 不存在，无法补驱动
kernel_nfs_registered       true      mount.nfs4 存在（平台用它挂 EFS / S3 Files）
```

推论：**任何用户态 FUSE 客户端（s3fs / goofys / JuiceFS / sshfs / rclone mount）在 microVM 内都无法工作**；
NFS 客户端在，因此 VPC 模式下"第三方 FS → 桥接 EC2 → NFS 再导出"在技术上可行但非官方支持（详见 docs/01 §3.2）。

### gh CLI shim 真机验证（2026-09-03，us-east-2，容器部署）——11/11 通过

`scripts/03-deploy-gh-shim-agent.sh` 部署了一个带 `gh` + shim 的 Strands agent（`deploy/gh_shim_agent/`），GitHub token 存在
AgentCore Identity API key provider；`scripts/04-verify-gh-shim.py` 从 agent 进程和同 session 的 `InvokeAgentRuntimeCommand` 两个视角取证
（[`results/gh_shim_verification.json`](results/gh_shim_verification.json)）：

```
which gh                          /opt/shim/gh
/proc/<agent pid>/environ         GH_TOKEN=0  GITHUB_TOKEN=0        <- agent 进程环境无 token
~/.config/gh/hosts.yml            absent；磁盘 grep gho_/ghp_/ghs_ 无结果
GH_REAL=<打印桩> gh api ...        GH_TOKEN_PRESENT_IN_CHILD prefix=gho_ len=40   <- 只在 gh 子进程里
gh api /user --jq .login          真实登录名 rc=0
gh auth login                     rc=126（被 shim 拦截）
gh release list（白名单外）        rc=126
模型驱动回合                        agent 用 run_gh 工具答出登录名，tool_calls=1，环境仍无 token
```

两个已写进代码/文档的坑：Runtime 自带的 workload identity 不能自取 token（平台按请求注入，agent 需交接给 shim）；
SigV4 入站时只有带 `runtimeUserId` 才会注入 WAT。详见 docs/02 §3.1「gh CLI 模式」。

---

## 目录

```
18-runtimes-3rd-fs/
├── README.md / README.en.md
├── docs/
│   ├── 01-filesystem-fuse.md               FUSE 现状、原生挂载能力、workaround 矩阵、Instances
│   ├── 02-secretless-third-party-auth.md   GitHub / GitLab / Vault 三层无秘钥方案
│   └── 03-audit-observability.md           tool call / network 四层审计矩阵与落地清单
├── scripts/
│   ├── runtime_cmd.py                      自包含的 InvokeAgentRuntimeCommand 客户端（遵循 .trellis 合约）
│   ├── 01-fuse-probe.py                    真机 FUSE / mount / NFS 能力探测 → results/fuse_probe.json
│   ├── 02-create-runtime-with-fs.sh        给 Runtime 挂 sessionStorage + EFS + S3 Files
│   ├── 03-deploy-gh-shim-agent.sh          部署 gh shim 验证 agent（Identity API key provider + 角色 + ECR + runtime）
│   ├── 04-verify-gh-shim.py                两视角取证 → results/gh_shim_verification.json
│   └── cleanup-gh-shim.sh                  删除上述资源（含 Token Vault 里的 GitHub token）
├── deploy/gh_shim_agent/                   Dockerfile（python + gh + git + /opt/shim）、agent.py（Strands + run_gh 工具 + WAT 交接）
├── demo/
│   ├── fs_workaround/
│   │   ├── s3_workspace_sync.py            无 FUSE 的用户态工作区同步（哈希清单、增量、冲突策略、可换后端）
│   │   └── agent_with_mounts.py            Strands agent：原生挂载 + 生命周期同步
│   ├── secretless_auth/
│   │   ├── github_agent_3lo.py             GitHub 3LO（官方 sample 模式）：GithubOauth2 + requires_access_token + git clone
│   │   ├── vault_aws_iam_login.py          Vault AWS IAM auth：SigV4 GetCallerIdentity，零 Vault 秘钥
│   │   ├── git_askpass.py                  GIT_ASKPASS：每次 git 操作即时取短期 token，不落盘（--token 供 gh shim 用）
│   │   ├── gh_wrapper.sh                   gh CLI shim：每次调用把短期 token 只注入 gh 子进程，禁用 gh auth
│   │   ├── credential_proxy.py             Sidecar / 凭证注入代理：git & REST 走代理，token 不经过 agent 代码（容器内 localhost 或 VPC 内部署）
│   │   ├── agentcore_identity_tokens.py    创建 GitLab(CustomOauth2) / GitHub / API key provider；requires_access_token 示例
│   │   └── github_app_broker/              Lambda：签发 1h、仓库级 GitHub App installation token
│   └── audit/
│       ├── tool_audit_hooks.py             Strands HookProvider：tool call JSON Lines 审计 + 脱敏 + deny-list
│       └── network_audit.py                sandbox 内 socket 级出网审计 / 软 allowlist
├── tests/                                  41 个单测（纯逻辑，无需 AWS / strands）
└── results/                                fuse_probe.json、gh_shim_verification.json（真机原始数据）
```

## 运行

```bash
# 单测（本机 python3 + boto3 + pytest 即可）
python3 -m pytest tests -q

# 真机探测（任意 READY 的 HTTP 协议 runtime；需要 InvokeAgentRuntime/Command + StopRuntimeSession 权限）
python3 scripts/01-fuse-probe.py --runtime-arn arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id> --region <region>

# 给已有 runtime 挂原生文件系统
REGION=... RUNTIME_ID=... SUBNETS=... SG=... EFS_AP_ARN=... ./scripts/02-create-runtime-with-fs.sh

# 出网审计自演示
python3 demo/audit/network_audit.py https://example.com https://api.github.com

# gh shim 真机验证（需要 docker；token 从文件读，不经命令行）
gh auth token > /tmp/ghtoken && chmod 600 /tmp/ghtoken
GITHUB_TOKEN_FILE=/tmp/ghtoken REGION=us-east-2 ./scripts/03-deploy-gh-shim-agent.sh
python3 scripts/04-verify-gh-shim.py
./scripts/cleanup-gh-shim.sh        # 删 runtime / provider / 角色 / ECR
```

依赖：`boto3`（脚本/单测）；agent 与 Identity 示例运行时另需 `strands-agents`、`strands-agents-tools`、`bedrock-agentcore`；
broker Lambda 需 `PyJWT[crypto]`（见 `pyproject.toml`）。

## 备注

- 探测结论基于当前平台内核快照（2026-09-03），平台内核变化可能改变结论，复测脚本已就位。
- `runtime_cmd.py` 遵循仓库 `.trellis/spec/backend/agentcore-runtime-command.md`：解析全部事件流、校验 `COMPLETED`/`exitCode`、
  显式 `/bin/bash -c` 包装、`finally` 中 `StopRuntimeSession`。
