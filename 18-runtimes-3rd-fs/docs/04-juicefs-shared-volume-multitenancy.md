# AgentCore Runtime：共享 JuiceFS 卷与外置 S3 Gateway 多租户持久化方案

> 记录日期：2026-09-09。状态：方案设计与文档核对完成，尚未部署网关或完成端到端隔离、持久性和性能测试。

## 1. 方案结论与适用范围

采用 **一个共享 JuiceFS 卷 + 外置 JuiceFS S3 Gateway + 每租户逻辑 bucket + 网关服务端授权**。AgentCore Runtime 中的应用继续使用 S3 API 保存状态或同步工作区，不直接挂载 JuiceFS。

该方案可以作为租户间访问隔离的实现方式，但隔离依赖正确的网关授权及封闭的后端访问路径，不能仅靠目录命名或 Agent 代码自律。验收标准是：**租户 A 即使控制自己的 Agent 代码、获取该 session 的全部凭证并构造任意请求，也不能读写租户 B 的数据。**

适用范围：

- 应用自管的 session 文件、checkpoint 和工作区持久化，不是替换 AgentCore 官方 `sessionStorage` 的内部后端。
- 信任共享网关、元数据服务及其管理员；主要防范租户之间的 API 越权。
- 可以接受共享元数据、缓存和服务故障域，愿意自行管理网关授权、可用性与备份。

本方案不保证：抵御共享网关或宿主机失陷、管理员跨租户访问、严格的性能隔离，或自动满足金融合规要求。若这些是硬要求，应升级为独立卷并配套独立后端权限、服务身份和必要的独立部署。

**JuiceFS 不是任意原生 S3 bucket 的透明缓存。** 现有普通 S3 对象需要迁移或通过应用同步写入 JuiceFS；引入后必须同时保护元数据和对象存储数据块。是否比直接 S3 更快仍需实测。

## 2. 存储组织与租户生命周期

### 2.1 卷、逻辑 bucket 与底层对象存储不能混淆

以共享卷 `agent-data` 为例，卷内组织为：

```text
agent-data（JuiceFS 卷）
├── tenant-a/
│   └── sessions/session-001/state.json
└── tenant-b/
    └── sessions/session-002/state.json
```

网关启用 `--multi-buckets` 后，将卷的顶层目录呈现为逻辑 bucket：

| 层次 | 示例 | 含义 |
|---|---|---|
| Agent 请求中的 `Bucket` | `tenant-a` | 网关逻辑 bucket，对应卷内 `/tenant-a/` |
| Agent 请求中的 `Key` | `sessions/session-001/state.json` | 该逻辑 bucket 内的文件路径 |
| JuiceFS 文件系统 | `agent-data` | 所有租户共用的卷与元数据空间 |
| 底层 AWS S3 bucket | `agent-storage` | 网关使用的实际对象存储，示例名称 |
| 底层 S3 对象 | 卷范围内的 `chunks/...` | JuiceFS 数据块，不是上述逻辑文件名 |

`--multi-buckets` 只提供命名空间映射，**不会自动创建租户权限策略**。共享卷内的 `/tenant-a/` 也不天然对应底层 S3 的 `tenant-a/` prefix；不能照抄逻辑目录来配置 AWS IAM 租户隔离。

标准 `juicefs gateway` 连接一个卷；多网关副本可以连接同一个共享卷。本方案的多个逻辑 bucket 不是多个独立卷。

### 2.2 创建与使用时机

1. **平台初始化一次**：创建共享卷及元数据存储，配置底层 S3 权限，部署外置网关。
2. **租户开通**：可信管理服务分配稳定的逻辑 bucket 名，创建对应目录/bucket、租户网关用户和范围受限的策略，并设置配额及审计映射。不再次初始化卷。
3. **session 开始**：后端认证用户，校验租户归属和 session 所有权，为该 session 分配 key prefix 和受限凭证。
4. **运行与恢复**：应用读取状态或下载工作区，在确定性的 checkpoint/回合结束逻辑中保存变更；恢复时访问同一授权范围。
5. **租户注销**：先停用凭证和续签，再按保留政策清理数据、备份、缓存及审计记录。删除目录不等同于所有历史数据立即彻底清除。

默认租户凭证若可访问整个租户 bucket，则同租户的 session 之间可共享数据。如需用户或 session 级隔离，必须进一步限制 key prefix；仅创建不同 session 目录不够。

## 3. 隔离边界与授权要求

### 3.1 网关执行租户授权

JuiceFS 社区版 S3 Gateway 从 v1.2 起支持 IAM、多用户、服务账号和 STS 临时凭证。为每个租户配置独立网关用户及自定义策略，业务需要什么操作才授予什么操作。

| 操作 | A 的身份 | B 的身份 |
|---|---|---|
| 列举、读取、写入、删除 `tenant-a` 授权范围 | 按业务授权 | 拒绝 |
| 列举、读取、写入、删除 `tenant-b` 授权范围 | 拒绝 | 按业务授权 |
| 修改用户、策略和网关配置 | 拒绝 | 拒绝 |
| 未授权 bucket 列举、匿名访问 | 拒绝或不暴露 | 拒绝或不暴露 |

落实策略时必须覆盖：bucket 级 LIST、对象级 GET/HEAD/PUT/DELETE、需要使用的 multipart 操作，以及 COPY 的源和目标授权。只限制写入路径，不能防止读取或列举泄露。

- 不使用预置的全局 `readwrite` 或 `consoleAdmin` 策略作为租户业务权限。
- 不向租户发放 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`。
- 服务账号继承父用户权限；从同一个高权限父用户生成多个 access key，并不等于租户隔离。
- STS 临时凭证的 session policy 可收窄权限；按官方说明，其权限不能超过原用户策略。实际策略语法和支持动作需针对选定网关版本验证。
- 拒绝访问必须由网关完成，不能依赖应用在发请求前检查 `tenant_id`、bucket 或路径。

### 3.2 两套 IAM 与旁路访问

**网关 IAM 和 AWS IAM 是两套授权系统。** 网关 IAM 控制租户对逻辑文件的访问；AWS IAM 控制网关对底层 S3 数据块的访问。网关 STS 也不是 AWS STS。

| 凭证/权限 | 放置位置 | 禁止事项 |
|---|---|---|
| 租户/session 范围的网关临时凭证 | 对应 session 的持久化模块 | 跨租户复用、放入 prompt 或日志 |
| 网关管理及签发所需凭证 | sandbox 外的可信管理服务 | 交给 Agent 或工具子进程 |
| JuiceFS 元数据库凭证 | 可信网关/运维侧 | Agent 直连共享元数据库 |
| 底层 S3 读写权限 | 网关服务身份 | Runtime 执行角色拥有全卷访问权限 |

不向 Agent 提供共享卷的直连 JuiceFS SDK/FUSE 配置；也不开放能绕过同等授权的 WebDAV、文件服务或管理端点。网络层限制 Agent 到元数据库和后端管理服务的访问，作为权限控制之外的额外防护。

AWS 明确说明 microVM 内代码可以取得 Runtime 执行角色凭证。因此必须检查该角色及其可进一步获取的权限，确保它不能绕过网关访问整个底层 bucket，也不能任意获取其他租户凭证。

### 3.3 信任假设

共享网关需要访问全卷；网关失陷或严重授权漏洞可能影响所有租户。POSIX ACL、目录权限和客户端 `--subdir` 可以辅助管理，但不能替代本方案的网关授权边界。

缓存文件、备份和日志同样属于可信服务侧数据，不直接提供给租户；缓存命中必须仍经过授权。共享基础设施的管理员与宿主机安全不属于本方案提供的租户隔离范围。

## 4. Agent 接入与凭证流程

### 4.1 接入约定

Agent 镜像预装 `s5cmd`，使用 S3 协议访问网关，凭证仅注入该 session 的子进程。无需在 Runtime 内安装或挂载 JuiceFS。当前可运行方案见 [s5cmd Demo](05-s5cmd-demo.md)，测试定义见 [s5cmd 测试说明](06-s5cmd-test-plan.md)。

示例配置：endpoint 为 `https://storage.example.com`（占位域名，不是已有服务），`Bucket` 为 `tenant-a`，`Key` 为 `sessions/session-001/state.json`。网关接收请求并授权，再由 JuiceFS 完成元数据和底层数据块操作；Agent 无需知道真实 AWS S3 bucket 名称。

凭证流程：

1. 可信后端认证用户，并绑定用户、租户和 session；不能直接相信用户提交的 `tenant_id` 或 session ID。
2. 后端使用租户受限的网关身份取得临时凭证，按需要进一步限定到 session prefix。
3. 应用通过经过认证的内部通道获取 endpoint、签名 region、bucket、prefix、临时凭证及到期时间。broker 接口尚未实现，本文不假定存在某个签发 API。
4. 凭证留在持久化模块，不放入 prompt、工具结果、命令行参数或日志。凭证进入 microVM 后仍应视为该 session 内代码可读取；保护目标是它只能访问授权范围。
5. 到期前重新获取凭证；每次启动 s5cmd 子进程显式设置当前凭证。续签必须重新检查授权，不假设 CLI 自动刷新外部注入的临时凭证。

不要通过修改父进程的全局 `AWS_ACCESS_KEY_ID` 等变量替换 Runtime 的默认 AWS 身份。只在启动 s5cmd 时构造独立子进程环境，禁止继承广泛权限或错误 endpoint/profile。

### 4.2 s5cmd 文件传输

当前 demo 使用一个 `s5cmd run` 进程批量复制整个工作区。文件清单来自可信 manifest，使用 `cp --raw --no-follow-symlinks --concurrency 1`，文件 worker 数由 `--numworkers` 控制。网关路径通过 `--endpoint-url` 指定，TLS CA 通过子进程 `AWS_CA_BUNDLE` 配置。

`bucket` 和 `prefix` 不是权限边界；租户即使修改参数，网关仍必须拒绝越权。s5cmd 不提供 POSIX 挂载，目录/链接/模式和恢复后哈希由应用 manifest 管理。当前基准只支持冻结快照、唯一 run prefix 和单写者，不是生产分布式同步器。

旧 SDK 文件传输性能测试和报告已删除；原项目的通用同步 demo 不参与新测试，也不沿用其性能结论。所有可执行命令、s5cmd 版本、worker 矩阵和验收规则集中在 [测试说明](06-s5cmd-test-plan.md)。

## 5. 部署、持久性与性能要求

### 5.1 部署与版本

- 私有网关部署在 VPC 中时，Runtime 使用 VPC 网络，配置 DNS 和到网关 HTTPS 端口的连通性，例如 443。网关再连接元数据服务和 AWS S3；S3 Gateway 访问不需要 NFS 的 2049 端口。
- 使用有效 TLS 证书；私有 CA 应部署正确的信任链，不能以关闭证书校验代替。
- 配置共享卷、多 bucket 映射、用户策略和必要的 metadata/ETag 能力，固定并记录 JuiceFS、s5cmd、管理客户端及控制面 SDK 版本。官方 Gateway 文档对 `mc` 管理客户端有版本要求，不能假定最新 MinIO 工具全部兼容。
- 多个网关副本可提升可用性，但共享元数据仍需高可用和备份。官方文档要求副本使用一致的初始化用户及 UID/GID。
- 网关 IAM 缓存默认刷新间隔为 **5 分钟**，可用 `--refresh-iam-interval` 调整。授权更新、撤权和禁用用户在多个副本上的生效时间必须实测，不能承诺立即生效。

### 5.2 持久性、恢复和删除

- 基线关闭客户端 `--writeback`。它会让尚未上传 S3 的数据仅存在本地缓存中；此时返回成功不等于已可靠保存到 S3，缓存丢失可能导致数据永久丢失。
- 区分单对象保存成功与整个工作区 checkpoint 完成。多文件一致性应由应用定义，例如单写者控制、版本化 manifest 和明确的提交点；不要假定目录同步是原子操作。
- 同时备份元数据和数据块，并验证可恢复的一致性。只有 S3 数据块、没有对应元数据，不等于可恢复完整文件系统。
- 底层 S3 生命周期规则不能在 JuiceFS 不知情的情况下删除仍被引用的数据块。
- 定义数据保留、租户注销及备份过期策略；回收站、缓存、备份和日志中的数据也应纳入删除与审计范围。

### 5.3 性能与资源隔离

外置常驻网关有机会复用缓存，但每次请求增加网关这一跳，冷缓存或小型 JSON 状态未必受益。网关缓存不等于跨节点分布式缓存；社区版共享网关模式也不等于商业版的分布式缓存能力。

设置租户目录容量/inode 配额，并在服务层设计请求速率、并发、带宽及资源保护。配额不等于 I/O 性能隔离，也不能防止单租户占满共享缓存。

性能对照至少包括：原生 S3 与网关两种路径的冷缓存恢复、热缓存读取、checkpoint 远端持久化时间、p95 延迟、S3 请求数以及网关/数据库/缓存成本。比较时必须使用相同的持久性要求，不能用异步写返回时间对比同步落盘时间。

## 6. 上线验收清单

以下项目全部为**待端到端验证**，不是已有通过记录。使用 A、B 两个租户的真实受限凭证测试，不用管理员身份代替。

| 验收项 | 预期结果 |
|---|---|
| A 在自身授权范围 GET/HEAD/LIST/PUT/DELETE | 按业务权限成功，数据正确 |
| A 访问 B 的同类接口 | 拒绝，不泄露内容或对象列表 |
| 列举全部 bucket、未授权 prefix、匿名请求 | 拒绝或只返回授权范围 |
| COPY 的源/目标跨租户 | 任一端未授权都拒绝 |
| multipart 创建、上传、列举、完成及中止 | 不可跨越租户/session 授权范围 |
| A 修改 bucket、endpoint、session ID | 不能取得 B 的数据或凭证 |
| A 调用管理接口、修改用户/策略 | 拒绝 |
| A 直接访问元数据库或底层 S3 | 无可用凭证/权限或网络不可达 |
| 热缓存命中、同名 key 分别存在于 A/B | 授权不被跳过，数据不串租户 |
| 路径穿越、编码变体、符号链接及本地同步 | 不逃逸授权目录或本地工作区 |
| 凭证过期、禁用、策略收窄及多网关副本 | 在明确且可接受的时间内生效，记录最大延迟 |
| 网关/元数据库/S3 故障、重启、session 停止恢复 | 错误向应用传播；已确认 checkpoint 可恢复 |
| 大文件 multipart、metadata、ETag、SDK checksum | 与固定版本组合兼容，恢复后校验内容哈希 |
| 单租户超配额或大量请求 | 不破坏其他租户数据，有可观测的资源保护 |
| 租户删除及备份恢复 | 不误删其他租户，符合保留策略并可审计 |

记录测试使用的镜像/SDK 版本、策略版本、凭证范围、响应状态、请求 ID、内容校验结果和撤权延迟。权限策略变更、网关或 SDK 升级后重跑越权与兼容性用例。

## 7. 与独立卷方案的选择边界

| 维度 | 本方案：共享卷 + 网关授权 | 独立卷 + 独立后端授权 |
|---|---|---|
| 防租户 API 越权 | 可实现，依赖网关与无旁路权限 | 可实现，同样需要正确授权 |
| 新租户开通 | 创建逻辑 bucket、用户及策略 | 另需初始化卷、配置元数据和存储权限 |
| 元数据与对象存储 | 全卷共享 | 可分别限制每卷的元数据和 S3 范围 |
| 网关/服务身份失陷 | 可能影响整个共享卷 | 配合独立身份和部署可缩小影响范围 |
| 独立恢复、迁移、管理 | 更复杂 | 更容易按卷处理 |
| 缓存与带宽竞争 | 共享，需额外配额和限流 | 可进一步独立部署 |

普通 SaaS 若信任共享存储平台、主要防租户相互越权，可选择本方案。若要求独立后端授权、独立恢复或更小的失陷影响范围，应选择独立卷方案。独立卷仍可共用 S3 bucket，但必须按卷的实际物理 prefix 分别授权；仅拆卷或仅拆 bucket 都不自动构成完整隔离。

若没有文件系统语义或高缓存命中率需求，原生 S3 加租户受限授权通常更简单，应作为成本、安全和性能对照基线。

## 8. 证据、验证状态与参考资料

### 8.1 当前已知与未验证事项

- **已有真机证据**：[FUSE 探测结果](../results/fuse_probe.json) 记录了 2026-09-03、us-east-2 的 microVM 没有 FUSE 驱动，但存在 mount 权限。详见[文件系统调研](01-filesystem-fuse.md)。这只是该环境快照，不是平台永不支持 FUSE 的承诺。
- **官方文档已核对**：Gateway 的多 bucket 映射、IAM/STS、path-style、metadata/ETag 开关与权限刷新行为，以及 AWS 的 session 绑定和执行角色凭证暴露说明。
- **当前实现**：单 Runtime、多 session 的 s5cmd 方案及本地测试状态见 [Demo](05-s5cmd-demo.md) 与 [测试说明](06-s5cmd-test-plan.md)。s5cmd 镜像已上线，云端成功与失败的 worker 配置见 [实测报告](07-s5cmd-cloud-results.md)，不将旧结果转写为新结果。
- **尚未完成**：生产身份/凭证 broker、完整权限审计、灾备与高可用验收。本文不是已经通过安全验收的部署手册。

### 8.2 参考资料

1. [JuiceFS S3 Gateway](https://juicefs.com/docs/community/guide/gateway/)：多 bucket、用户/STS、客户端与网关功能配置。
2. [JuiceFS S3 Gateway: IAM and Bucket Event Notifications](https://juicefs.com/en/blog/usage-tips/s3-gateway)：用户策略、服务账号继承、临时凭证和多实例行为。
3. [JuiceFS Architecture](https://cf.juicefs.com/docs/community/architecture/)：元数据与对象数据块分离，不直接映射原生 S3 文件。
4. [JuiceFS Cache](https://juicefs.com/docs/community/guide/cache/)：缓存、writeback 的持久性和一致性边界。
5. [JuiceFS Quota](https://cf.juicefs.com/docs/community/guide/quota/)：卷和子目录容量/inode 配额。
6. [Security best practices for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)：业务后端负责 session 用户映射，microVM 内代码可访问执行角色凭证。
7. [File system configurations for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)：官方托管 session 存储与外部文件系统配置的区别。
8. [s5cmd v2.3.0](https://github.com/peak/s5cmd/tree/v2.3.0)：固定版本 CLI、并发配置、run 命令与 S3 兼容端点。
