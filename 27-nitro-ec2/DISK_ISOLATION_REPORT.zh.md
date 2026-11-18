# Nitro Sandbox 独立磁盘隔离与持久化实测

## 结论

**已在真实 Nitro EC2 上验证独立 raw 磁盘后端：direct 和 confidential 两种模式，各 32/32 项检查通过，总计 64/64。**

每种模式同时运行两个 Sandbox，分别把不同的 ext4 raw 文件映射为 `/dev/vda` 并挂载到 `/workspace`。结果确认：

- 相同路径 `/workspace/shared.txt` 可以保存 A、B 不同内容，互不覆盖；各自专有文件不会出现在另一方。
- 每种模式完成 10 轮双 VM 并行写入及读回核对，两 VM 在这些检查时始终占用 2 个硬件 Sandbox slot。
- 将 A、B 分别停止并从新 guest 重建：boot_id 改变、磁盘 UUID 不变，`/workspace` 和 `/workspace/.dsh` 的数据保留；`/tmp` 中标记消失。
- 重建 A 时 B 的 boot_id 和 RAM 标记保持不变，B 的文件读写不受影响。
- 所有 guest 正常卸载后，4 个 ext4 映像的只读离线 e2fsck 均通过。父实例用 debugfs 能读取两种模式的数据，说明**跨 Sandbox 隔离不等于对父实例保密**。

本次共启动 8 个不同 boot_id 的真实 Sandbox。实例已再次停止；磁盘文件和证据保留，原冷启动报告及归档未覆盖。

## 环境与方法

实现依据：[AWS Nitro Sandbox 数据盘配置](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nitro-sandboxes-run-firecracker.html#nitro-sandboxes-attach-storage)。本次采用其中 raw-file 后端方案，不共享可写 ext4，不在 guest 运行时从父实例挂载。

- 日期：2026-11-17 UTC。复用专用实例 `i-062d42b82e041af2d`，账号 `434444145045`，`us-east-1a`，机型 `m7g.xlarge`，4 vCPU / 16 GiB 标称内存；没有新建实例、EBS 或 IAM 资源。
- 父/guest kernel：`6.18.62-127.179.amzn2023.aarch64`；Firecracker `1.18.0`，Node `v24.20.0`，stock DSH `0.2.2-rc.1`；父启动时保留 8 GiB Nitro CMA pool。
- 每个 guest：2 vCPU、2048 MiB 内存、CPU overcommit=true、debug=false。两种模式顺序执行，各自在内部同时运行 A、B。
- 程序仍来自独立解包的 initramfs；每个 VM 配置 1 个 virtio 数据盘，`is_root_device=false`、`is_read_only=false`、`cache_type=Writeback`。只给 guest 自己的映像，不共享可写后端。
- 新 disk guest 镜像位于 `artifacts-disk-20261117/`，不覆盖原 `artifacts/`。initrd SHA256：`8b6a7467d55fcbc18b6910ddf4a20ad7fd92a838d5166db769cb532ac47e2f9d`。
- A/B 各使用独立 TAP、不同 `/30` 子网，拒绝 guest 转发和访问父实例服务；只允许父控制器发起探测并接收回复。原 demo 的固定 IP 顺序基准没有被误用为并发测试。
- 共创建 4 个新的 sparse raw ext4 文件，每个逻辑容量 2 GiB；合计逻辑容量 8,589,934,592 bytes（8 GiB），本次测试结束时实际分配 273,670,144 bytes（260.992 MiB）。它们存放在已有的加密 EBS 根卷 `vol-04b232b3446b422cc` 上，不是 4 块独立 EBS。
- 文件创建使用排他模式；仅对本次新建文件执行 mkfs，不格式化 `/dev/nvme*` 或旧文件。编排器拒绝重复后端 inode（包括硬链接别名），持有独占 lease；没有冒险把普通 ext4 同时交给多个 VM 写入。
- 数据探针在 guest 内使用实际文件打开/写入/read/fsync，文件和目录均同步；停止 VM 前执行 guest sync + umount，随后停止 VMM 并同步父后端文件，再离线检查。
- DSH_HOME 设置为 `/workspace/.dsh`，验证其中的独立标记及持久化；初始 A/B 中都实际执行了 stock `dsh headless --help`。本次没有调用模型，不是编码 Agent 回合或性能基准。

## 测试结果

| 检查 | Direct | Confidential |
|---|---|---|
| 两个硬件 Sandbox 同时运行，slot=2、boot_id 不同 | 通过 | 通过 |
| A/B 文件系统 UUID 不同且匹配父后端 | 通过 | 通过 |
| 初始 A/B 中的 stock DSH CLI help 正常退出 | 通过 | 通过 |
| A 创建文件，B 同路径不存在 | 通过 | 通过 |
| 同名文件分别写入 A/B 数据，互不覆盖 | 通过 | 通过 |
| A-only / B-only 文件互不可见 | 通过 | 通过 |
| `/workspace/.dsh/private.marker` 独立 | 通过 | 通过 |
| 10 轮并行写同名文件，逐轮读回各自数据 | 10/10 | 10/10 |
| A 停止及重建时，B 继续运行且数据不变 | 通过 | 通过 |
| 重建 A/B：新 boot_id、原磁盘 UUID、workspace 数据保留 | 通过 | 通过 |
| 重建 A/B：DSH_HOME 标记保留、`/tmp` 标记消失 | 通过 | 通过 |
| 两方重建后，A 修改文件不影响 B | 通过 | 通过 |
| 正常卸载、释放 slot，离线 e2fsck 返回 0 | 通过 | 通过 |
| 父实例管理员能读取明文块后端 | **能读取** | **能读取** |
| 同一可写后端重复分配被编排器拒绝 | 是 | 是 |
| 合计测试断言 | **32/32** | **32/32** |

磁盘 UUID（同一个磁盘在 Sandbox 重建前后不变）：

| 模式 | A | B |
|---|---|---|
| Direct | `948d6f0e-f45f-40b8-b921-286c51298276` | `508168c6-e929-4d2e-a927-f1de02b46313` |
| Confidential | `a76fabbb-6609-49c2-b527-90dccff987f4` | `da452225-0768-49d0-a49c-8ea4cda201bc` |

Direct 模式 A 的 boot_id 从 `92e79af8-e1d6-4b1d-94c0-972ac6a92346` 变为 `4a90cb46-2d04-42b2-9349-6353663ffb99`，而文件系统 UUID 保持不变。原始文件中同样保留 B 和 confidential 模式的重建记录，总计 8 个不重复 boot_id。

离线代码验证：34 项 Python unittest 和 2 项 Node 文件操作测试全部通过。包含旧文件不被格式化、重复后端/硬链接拒绝、POST 超时不重放、guest 身份匹配、路径穿越拒绝、fsync 后读回及内容长度限制。

## 原始证据

- 实际执行的 SSM 命令：`c226d351-eb4e-48e6-891d-38167d4a8475`，成功退出；未自动重试格式化或文件写入。
- 本机归档：`results/disk-evidence-20261117.tgz`，9957 bytes，SHA256 `cefcb4783d89094f3bba2531869c56078027f78a052ec6a0eed87b14fe3baea8`，与父实例端一致。
- 解包目录：`results/disk-evidence-20261117/`，包括 direct/confidential 的总 `results.json`、每个初始/重建 VM 的 `sandbox.json`、`health.json`、`shutdown.json`、Firecracker 日志和 A/B 的 e2fsck 输出。
- 两个 `results.json` SHA256：direct `d46f3c1bf71ce3c783f25ef4152598f33a1608a4db3f19e1bc72f5c5787f49cb`；confidential `a5527c13e62496ffc9c45d2d9fa8c4a37b96ce48d1ca1f07238e8e25063a581f`。
- `host/` 保存内核命令行、最后的 slot/nft/TAP 状态、disk guest manifest 和源文件 SHA256。独立本机审计确认 8 次 guest 正常卸载、所有 VMM 配置的后端路径符合 A/B 映射、所有 boot_id 不重复，部署源 hash 与本机一致。
- 父磁盘文件（保留在 EBS 中，未打入证据包）：direct 为 `/opt/dsh-nitro-benchmark/private-disks/f3ef17a022224d6f85d1d69aad4b946c/{A,B}.ext4`；confidential 为 `/opt/dsh-nitro-benchmark/private-disks/deec1938e7244f6f819390694c68dc6d/{A,B}.ext4`。
- 原冷启动归档的 SHA256 仍为 `cdcf9f69f19218a0c5ff74d4aa897c040667a39958ad9e6480b6fd84359640a2`，未覆盖旧报告或旧镜像。

实现代码：`disk_isolation.py`、`guest/disk-init`、`guest/disk-probe.mjs`、`scripts/build_disk_guest.sh`、`scripts/run_disk_isolation.sh`。它们是单独测试变体，没有替换原冷启动 guest/init。

本地测试：

```bash
python3 -m unittest discover -s 27-nitro-ec2/tests -v
node --test 27-nitro-ec2/tests/test_disk_probe.mjs
```

恢复父实例后若重新执行测试，需要使用新输出目录和新磁盘文件；不要再次格式化这 4 个已保存的后端。

## 边界与资源状态

1. 这是正常文件访问下的功能隔离和干净关闭后的持久化测试，不是 hypervisor escape、恶意 kernel、侧信道或断电/crash durability 安全评估。
2. 独立磁盘隔离依赖父控制器正确分配后端。duplicate-backend 检查是本 demo 的编排器保护，不意味着 Nitro 会自动阻止父实例故意共享同一存储。
3. 两模式都实测父实例可以读取块后端。若要求对父实例保密，必须在 guest 内加密并结合 attestation 释放密钥；EBS 加密本身不解决这个信任边界。本次没有实现 NSM/KMS 密钥释放。
4. 4 份映像仍共用父 EBS 容量和 I/O 性能，未验证性能配额隔离。未新增 EBS；现有 30 GiB 卷仍收费。Root 的 DeleteOnTermination=true，终止父实例会删除其中映像，因此本次仅停止实例、不终止或删除磁盘。
5. 结束前清理验证命令 `9e7c546d-9f05-4836-9b5b-cd8e4124f5f7` 成功：运行中 slot、测试 TAP、测试 nft table、临时凭证文件均为 0，IPv4 forwarding 为 0。
6. 父实例于 `2026-11-17 12:09:30 GMT` 请求停止，随后 waiter/DescribeInstances 确认 `stopped`；加密根卷和全部测试数据保留。没有改动其他既有实例、IAM 角色或网络资源。
7. 本实验不报告冷启动延迟，也不复用原 4 GiB、无数据盘 benchmark 的延迟结论。新增挂载和驱动的性能影响需另测。父实例停止/启动后的恢复和非正常关闭后的恢复未在本轮额外验证。
