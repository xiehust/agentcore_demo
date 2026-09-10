# 关切点 1：第三方文件系统（FUSE）支持与 Workaround

> 结论先行：**AgentCore Runtime microVM 的 guest 内核没有 FUSE 驱动，任何第三方 FUSE 客户端
> （s3fs / goofys / JuiceFS / sshfs / 自研 FUSE）都无法在 sandbox 内挂载。**
> 上述结论限于第 1 节的直接代码部署样本：该 session 的探测进程是 root、有 `CAP_SYS_ADMIN`、能挂载 tmpfs。
> **Instances 容器的实测结果相反：内核有 FUSE，但设备和权限受限，仍不能直接挂载（第 4 节）。**
> 官方给出的替代路径是 Runtime 原生的 `filesystemConfigurations`（Session Storage / EFS / S3 Files），
> 内核自带 NFS 客户端，也为"NFS 桥接"这一非官方 workaround 留了技术空间。

---

## 1. 真机探测结论（2026-09-03，us-east-2，microVM，直接代码部署 PYTHON_3_13 / arm64）

探测脚本：[`scripts/01-fuse-probe.py`](../scripts/01-fuse-probe.py)，原始数据：[`results/fuse_probe.json`](../results/fuse_probe.json)。
脚本通过 `InvokeAgentRuntimeCommand` 在**同一个 session 的 microVM 里**执行静态探测脚本，探测完成后 `StopRuntimeSession`。

| 探测项 | 实测值 | 含义 |
|---|---|---|
| 内核 | `Linux 6.1.161-18.298.amzn2023.aarch64` | Amazon Linux 2023 Firecracker guest kernel |
| 进程身份 | `uid=0(root)`，`CapEff=000001ffffffffff`（全部 41 个 capability） | **不是**权限受限容器 |
| Seccomp | `Seccomp: 0` | 无 syscall 过滤 |
| `mount -t tmpfs` | `rc=0` | mount 系统调用可用 |
| `unshare -Urm` + mount | `NS_TMPFS_OK` | 用户命名空间也可用 |
| `/dev/fuse` | **不存在** | — |
| `/proc/filesystems` 含 `fuse` | **否** | 内核未编译 FUSE |
| `mknod /dev/_probe_fuse c 10 229` 后 `open()` | `OSError: [Errno 19] No such device` | **ENODEV：驱动不存在，不是设备节点缺失** |
| `mount -t fuse none /tmp/x` | `unknown filesystem type 'fuse'` (rc=32) | 决定性证据 |
| `/proc/modules` | 不存在；`/lib/modules` 为空 | 单体内核，**无法 `insmod/modprobe` 补驱动** |
| `/proc/filesystems` 含 `nfs` / `nfs4` | **是** | 内核 NFS 客户端在 |
| `mount.nfs4` 二进制 | `/usr/sbin/mount.nfs4` | 平台自己用它挂 EFS / S3 Files |
| `mount -t nfs4 127.0.0.1:/` | `Connection refused` (rc=32) | NFS 挂载路径通到网络层（本次 runtime 是 PUBLIC 网络，无法测真实 NFS server） |
| 根文件系统 | overlayfs on `/dev/vdb` ext4，9.7G；`/tmp` tmpfs 4G | 临时盘随 session 销毁 |

> ⚠️ 这是一次 microVM 直接代码部署的探测快照，不能据此断言容器部署具有相同设备与权限。
> **Instances 的内核和容器权限需要分别检查**，见第 4 节的独立实测。平台实现未来可能变化，
> 交流时建议客户用同一脚本在自己账号复测。

---

## 2. 官方支持的文件系统能力（`filesystemConfigurations`）

来源：[File system configurations for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)。
平台在 microVM 启动时**由服务侧完成挂载**，agent 代码不需要任何 mount 权限或 mount helper。

| 类型 | 隔离粒度 | 持久性 | 计算类型 | 需 VPC | 适合 |
|---|---|---|---|---|---|
| `sessionStorage`（Preview） | 每 session | 跨 stop/resume；14 天不调用清空；runtime 版本更新时重置；上限 **1 GB**、约 10–20 万文件 | microVM | 否 | 工作区、pip/npm 缓存、git 仓库、agent 状态 |
| `efsAccessPoint` | 跨 session / 跨 agent 共享 | 客户自管，永久 | microVM | 是 | 共享工具库、模型权重、多 agent 读写协作、**完整 POSIX**（硬链接、advisory lock） |
| `s3FilesAccessPoint` | 跨 session / 跨 agent 共享 | 客户自管，双向同步到 S3 桶 | microVM | 是 | 数据集既要 POSIX 访问又要 S3 API 访问 |
| `capacityProviderVolume` | 每 session | EBS 卷，session 删除前保留 | Instances | 是（在 capacity provider 上配） | 长跑 session 的工作区/缓存/checkpoint |

要点：

- microVM 上最多 5 个配置，可以 `sessionStorage` + EFS/S3 Files 组合；挂载点必须是 `/mnt/<name>`。
- EFS/S3 Files 走 **NFSv4.1/4.2 over TLS + IAM**，执行角色需要 `elasticfilesystem:ClientMount/ClientWrite`
  或 `s3files:ClientMount/ClientWrite/GetAccessPoint`，安全组放通 TCP 2049 到 mount target。
- Access point 上配置的 POSIX UID/GID 决定所有文件操作身份；microVM 里 agent 是 root（0:0）。
- `sessionStorage` 不支持：硬链接、设备文件/FIFO/socket、xattr、`fallocate`；权限位"存储但不校验"。
- Instances 上不能用 `sessionStorage` / EFS / S3 Files（会 `ValidationException`），只能用 `capacityProviderVolume`。

示例（脚本 [`scripts/02-create-runtime-with-fs.sh`](../scripts/02-create-runtime-with-fs.sh)）：

```bash
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"subnets":["subnet-a","subnet-b"],"securityGroups":["sg-runtime"]}}' \
  --filesystem-configurations '[
    {"sessionStorage":   {"mountPath":"/mnt/workspace"}},
    {"efsAccessPoint":   {"accessPointArn":"arn:aws:elasticfilesystem:...:access-point/fsap-...","mountPath":"/mnt/shared"}},
    {"s3FilesAccessPoint":{"accessPointArn":"arn:aws:s3files:...:file-system/.../access-point/...","mountPath":"/mnt/datasets"}}
  ]'
```

---

## 3. 第三方文件系统的 Workaround 矩阵

客户所说的"三方文件系统"通常是以下几类，逐一对应：

| 客户实际诉求 | 推荐方案 | 备注 |
|---|---|---|
| 对象存储当文件系统（S3 / MinIO / OSS 兼容） | **S3 Files access point**（原生）；非 AWS 对象存储则用 **用户态同步客户端**（本目录 [`demo/fs_workaround/s3_workspace_sync.py`](../demo/fs_workaround/s3_workspace_sync.py)） | 用户态方案零权限、任何 S3 兼容端点都行，代价是"显式 pull/push"而不是透明挂载 |
| 企业 NAS / NFS 共享 | **EFS access point**（原生）；数据在自建 NFS 上时，用 DataSync/rsync 单向或双向同步到 EFS | 见 3.2 NFS 桥接 |
| 分布式文件系统 FUSE 客户端（JuiceFS、CephFS-FUSE、Lustre 客户端、GCS/Azure FUSE） | **桥接 EC2**：在 VPC 内一台 EC2 上用 FUSE 挂载，再 (a) 同步到 EFS/S3；或 (b) 通过 NFS 再导出（3.2）；或 (c) 改用可控制设备与挂载权限的**自管 EC2/容器环境**；不能直接推荐 Instances（第 4 节实测不通过） | 客户内部安全评审通常更接受 (a) |
| SFTP / WebDAV / SMB 网盘 | 用户态协议客户端（paramiko / webdavclient / smbprotocol）做 pull/push | 与 S3 同步 demo 同一套 `SyncAdapter` 接口 |
| 只是想要"session 内持久工作区" | `sessionStorage` | 1 GB 内最简单 |

### 3.1 用户态同步（零依赖、官方支持路径内）

思路：把"挂载"换成"生命周期钩子"——session 首次调用时 `pull()`，每次工具调用/回合结束时 `push()` 增量，
配合 `sessionStorage` 做本地缓存。demo 支持：

- 内容哈希清单（`.sync-manifest.json`）→ 只上传变更文件；
- 排除规则（`.git/`, `node_modules/`, `__pycache__/`）；
- 冲突策略：`remote_wins` / `local_wins` / `fail`；
- boto3 client 可注入 → 单元测试用 stub，不需要真实 S3。

局限：不透明（应用要知道何时同步）；不适合随机写大文件；无跨 session 锁。

### 3.2 NFS 桥接（技术可行，非官方支持，需客户自担）

真机探测表明 guest 内核有 NFS 客户端且允许 `mount`。在 **VPC 模式**下，理论上 agent 可以自己执行
`mount -t nfs4 <bridge-ip>:/export /mnt/thirdparty`，其中 bridge 是 VPC 内一台 EC2：
用 FUSE 挂第三方 FS，再用 kernel nfsd 或 NFS-Ganesha 导出。

必须向客户明确：

1. 这是利用了当前 microVM 的实现细节，AWS 没有承诺；内核收紧 capability 后会失效。
2. 需要 agent 进程自己 mount（在 `@app.entrypoint` 前或首次调用时），错误处理、重挂、超时自理。
3. 安全上把第三方 FS 的信任边界搬进了 VPC；bridge EC2 是新的单点。
4. 我们在 PUBLIC 网络的 runtime 里只验证到 "mount.nfs4 → Connection refused"（协议栈可达），
   **真实 NFS 挂载尚未验证**——AC 专项交流时可用客户 VPC 复测（探测脚本已内置该检查）。

优先推荐 `filesystemConfigurations` 原生挂载或用户态同步。FUSE 硬需求可考虑自管 EC2；
NFS 桥接仍需验证，Instances 不能视为已验证的 FUSE 替代方案。

---

## 4. Instances 实测：内核支持 FUSE，不代表 Runtime 容器能挂载

**2026-09-10 的实测推翻了本节原先“Instances 的 FUSE 可行性与普通 EC2 一致”的推断。**
[Runtime Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html)
通过 capacity provider 提供 EC2 计算资源，但 agent 容器的权限和设备暴露仍由平台控制。

本次使用现成资源，没有修改 provider、Runtime、镜像或 IAM 配置：

- 区域：`us-west-2`；provider：`capacity_provider_arm_m7g_large-1HB6aXJTVr`（`LINUX_ARM64`、`m7g.large`）。
- Runtime：`shared_runtime_multiuser_m7g-EZpQed4lPW`，version 1，容器部署。
- 在全新 session 中通过 `InvokeAgentRuntimeCommand` 探测；脚本：
  [`scripts/10-instances-fuse-probe.py`](../scripts/10-instances-fuse-probe.py)，
  原始结果：[`results/instances_fuse_probe.json`](../results/instances_fuse_probe.json)。

| 检查项 | 实测结果 | 含义 |
|---|---|---|
| 内核 | `6.18.44-99.149.amzn2023.aarch64` | 与第 1 节 microVM 样本不同 |
| FUSE 内核支持 | `/proc/filesystems` 有 `fuse`、`fuseblk`、`fusectl`；`/sys/module/fuse` 存在 | **内核有 FUSE** |
| 进程身份 | `uid=0(root)`；`uid_map` 为 `0 100000 65536` | 容器内 UID 0 映射到外层 UID 100000，不是宿主机 root |
| 权限 | 探测进程和 PID 1 均 `CapEff=0`、`CapBnd=0`；`Seccomp: 2` | 没有 `CAP_SYS_ADMIN` / `CAP_MKNOD`，并启用了 seccomp |
| `/dev/fuse` | 不存在，`open()` 返回 `ENOENT` | 设备未暴露给容器 |
| `mknod` FUSE 设备节点 | `Operation not permitted` | 不能自行补设备节点 |
| tmpfs / FUSE 挂载请求 | `permission denied`，`rc=32` | 挂载受到限制 |
| `unshare -Urm` | `Operation not permitted` | 本次不能通过新用户命名空间挂载 |

因此，**这个现成 Instances Runtime 不能在容器内直接挂载 FUSE**。带有效 FUSE fd 的完整挂载测试
在打开 `/dev/fuse` 时就被阻断，未进行用户态文件系统的读写验证，也未安装客户端或尝试修改宿主机。
安装 `fuse3`、JuiceFS 或 Mountpoint for Amazon S3 并不能单独解决设备和权限缺失。

结论限定于本次 provider 和容器部署，不推广到未测的直接代码部署或其他平台配置。
不能再把 Instances 当作已验证的 FUSE 解决方案，也不能假定 agent 可自行挂载 NFS。
若必须使用 FUSE，应选择能够明确配置设备与挂载权限的自管 EC2/容器环境，
或先向 AWS 确认 Instances 是否提供受支持的设备透传及权限配置方式。

测试 session 已通过 `StopRuntimeSession` 成功停止；随后调用 `DeleteCapacityProviderSession`
清理该测试 session，并复查得到 `ResourceNotFoundException`，确认 session 已删除。
现有 provider 的计算/权限配置保持不变，Runtime 仍为 READY、version 1。

---

## 5. 交流时的问题清单

1. 第三方文件系统具体是什么？（对象存储 / NAS / 分布式 FS / 网盘）→ 决定走原生挂载还是桥接。
2. 访问模式：只读数据集、读写工作区、还是多 agent 并发写？→ S3 Files / sessionStorage / EFS。
3. 是否可接受 VPC 模式（EFS/S3 Files 都要求）？客户已有 VPC 出网管控（见关切点 3）。
4. 单 session 数据量是否 < 1 GB？超出则 EFS 或 Instances。
5. 是否需要跨 session 文件锁 / 硬链接 / xattr？→ 排除 sessionStorage。
6. 合规上能否接受"bridge EC2"承载第三方 FS 凭证？→ 决定是否考虑 NFS 桥接。
