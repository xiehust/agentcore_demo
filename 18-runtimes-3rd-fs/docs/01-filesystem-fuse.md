# 关切点 1：第三方文件系统（FUSE）支持与 Workaround

> 结论先行：**AgentCore Runtime microVM 的 guest 内核没有 FUSE 驱动，任何第三方 FUSE 客户端
> （s3fs / goofys / JuiceFS / sshfs / 自研 FUSE）都无法在 sandbox 内挂载。**
> 但这不是"权限"问题——session 里是 root、有 `CAP_SYS_ADMIN`、可以 `mount`。
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

> ⚠️ 这是一次 microVM 的探测快照，容器部署（`containerConfiguration`）共享同一 guest 内核，结论相同；
> 但 **Instances 计算类型是客户账号里的 EC2，内核由 AMI 决定**，见第 4 节。平台内核未来可能变化，
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
| 分布式文件系统 FUSE 客户端（JuiceFS、CephFS-FUSE、Lustre 客户端、GCS/Azure FUSE） | **桥接 EC2**：在 VPC 内一台 EC2 上用 FUSE 挂载，再 (a) 同步到 EFS/S3；或 (b) 通过 NFS 再导出（3.2）；或 (c) 改用 **Instances 计算类型**在自有 AMI 内核上直接 FUSE（第 4 节） | 客户内部安全评审通常更接受 (a) |
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

推荐给客户的排序：`filesystemConfigurations` 原生 > 用户态同步 > Instances 计算类型 > NFS 桥接。

---

## 4. Instances 计算类型：需要"真 FUSE"时的正路

[Runtime Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances.html) 把 agent 跑在**客户账号里的 EC2**
（AgentCore 负责供给/伸缩/回收，按 EC2 价 + 管理费计费），session 最长 14 天，支持 GPU：

- 内核由 capacity provider 指定的 OS/AMI 决定，是完整的 Amazon Linux 内核（含 `fuse` 模块），
  agent 以容器或直接进程运行；FUSE 客户端（JuiceFS、s3fs、goofys…）的可行性与普通 EC2 一致。
- 同一 instance 上的多个 agent **互不隔离**（共享文件系统），隔离单位是 session=instance。
- 存储用 `capacityProviderVolume`（EBS），不能用 sessionStorage/EFS/S3 Files 配置项——
  但 agent 自己在 instance 内 `mount -t nfs4` EFS 是普通 EC2 行为。

如果客户的 FUSE 需求是硬性的（例如金融数据平台只提供 JuiceFS/自研 FUSE 接入），
Instances 是唯一"在官方模型内"的答案；否则 microVM + 原生挂载性价比更高。

---

## 5. 交流时的问题清单

1. 第三方文件系统具体是什么？（对象存储 / NAS / 分布式 FS / 网盘）→ 决定走原生挂载还是桥接。
2. 访问模式：只读数据集、读写工作区、还是多 agent 并发写？→ S3 Files / sessionStorage / EFS。
3. 是否可接受 VPC 模式（EFS/S3 Files 都要求）？客户已有 VPC 出网管控（见关切点 3）。
4. 单 session 数据量是否 < 1 GB？超出则 EFS 或 Instances。
5. 是否需要跨 session 文件锁 / 硬链接 / xattr？→ 排除 sessionStorage。
6. 合规上能否接受"bridge EC2"承载第三方 FS 凭证？→ 决定是否考虑 NFS 桥接。
