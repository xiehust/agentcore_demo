# Nitro EC2 / Nitro Sandboxes 中 DSH Agent 的冷启动验证

初学者可先看 [Nitro Sandbox 图文上手教程（中文 HTML）](../28-nitro-ecs/nitro-sandbox-guide.zh.html)，了解 EC2 / ECS 两条路线、磁盘隔离、冷启动与快照替代方案；可直接双击离线打开。

## 实测报告

已在真实 AWS `m7g.xlarge` 父实例上部署并测试，完整结果见 [TEST_REPORT.zh.md](TEST_REPORT.zh.md)。无模型 confidential/direct 各 20 轮完成；真实 Agent 实际完成 4 轮、8 个回合后因连续答案不匹配触发保护，未完成计划 20 轮。性能与答案符合率分开报告；父实例已停止，EBS 保留。

## 独立磁盘实测

另已完成 [双 Sandbox 独立磁盘隔离与持久化实测](DISK_ISOLATION_REPORT.zh.md)：direct / confidential 各 32/32 项检查通过。每种模式同时运行两个 Sandbox，各映射独立 raw ext4 文件到 `/workspace`；验证同名文件隔离、10 轮并发写入、DSH_HOME 标记隔离，以及重建后的磁盘数据保留和 `/tmp` 数据消失。父实例仍能读取块后端，不能将此结果解释为对父实例保密。

实现：`disk_isolation.py`、`guest/disk-init` / `disk-probe.mjs`、`scripts/build_disk_guest.sh` / `run_disk_isolation.sh`。使用单独 guest 镜像，不替换原冷启动 init，也没有继续调用此前暂停的模型测试。四份新磁盘文件保留在原加密 EBS 上；实例已再次停止。

## 范围与依据

基于用户提供的两篇官方材料（阅读日期：2026-11-17）：

- [2026-11-09 发布公告](https://aws.amazon.com/cn/about-aws/whats-new/2026/11/nitro-sandboxes-eks-ecs-ec2/)：EC2、EKS、ECS 可以共享父实例资源，同时让各工作负载拥有 Nitro 硬件隔离边界。
- [Nitro Sandboxes 概览](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nitro-sandboxes.html)：支持 Intel/AMD/Graviton；不支持 snapshot/restore；direct 模式父实例可以读写 guest 内存，confidential 模式不能。
- [EC2 + Firecracker 操作指南](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nitro-sandboxes-run-firecracker.html)：AL2023 `kernel6.18 >= 6.18.51-120.162.amzn2023`、Firecracker `>= 1.18.0`、启用 Nitro Enclaves、启动时保留内存池。

这里选择 **EC2 上直接管理 Firecracker Nitro Sandbox**，不是 Docker 基线，也不是 `/dev/kvm` 嵌套虚拟机。配置必须包含 `enclave` 和 `drives: []`。不创建 AgentCore Runtime、EKS 或 ECS 资源。

DSH 版本与 [26-runtime-dsh](../26-runtime-dsh/README.md) 一致，固定 `@deepseek-ai/dsh@0.2.2-rc.1`，Node 24。只复用其 stock `dsh headless` 调用和 Bedrock profile 配置方式，不改动旧 demo。这里从内核和 initramfs **完整启动**；不能把 AgentCore V2 的快照恢复结果当成同口径对照。

文件：

| 文件 | 作用 |
|---|---|
| `scripts/build_guest.sh` | 在父实例构建 EFI-stub 内核 + 含 Node/DSH 的 initramfs；不把凭证打入镜像 |
| `guest/sandbox-init` | guest PID 1：挂载、加载 virtio 驱动、配置 TAP 网络、启动 HTTP 包装器 |
| `guest/server.mjs` | 每次请求启动一个 DSH 进程，单调时钟测量、输出限制、超时终止 |
| `benchmark.py` | 父实例检查、临时网络、逐轮启动/停止 Sandbox、冷/热对照、统计与原始证据 |
| `tests/` | 无 AWS 调用的失败序列、时序和真实 HTTP/子进程离线测试 |

## 测量口径

每轮启动一个新的 Sandbox，HTTP 健康检查后跑一次 **cold** 请求，再在同一个 guest 内跑一次 **warm** 请求，最后停止 Firecracker 并等待 Nitro slot 释放。`run_id` + guest `boot_id` 防止旧进程冒充新 guest，`pcie_slots_in_use == 1` 提供硬件 Sandbox 的旁证。

两个 workload 必须分别运行（agent 前不先跑 `--help`，避免把 DSH 预热）：

- `startup`（默认）：`dsh headless --help`，无模型、无凭证、无出网。它验证 DSH CLI 可启动；**不是**一次真实 Agent 回合，也不保证覆盖完整 agent 初始化路径。
- `agent`：`dsh headless --json --approval-policy never -`，任务通过 stdin 传入，真实调用指定 Bedrock 模型。只有退出码为 0、收到 `final.reason.kind=completed` 且有非空 `text` 事件才算成功。不记录模型答案或 provider 原始错误。

| 指标（均为毫秒） | 定义 |
|---|---|
| `sandbox_ready_ms` | 父实例：创建 Firecracker 进程前 → 首次收到身份匹配的健康响应；含 VMM、内存/镜像准备、UEFI/kernel/initramfs、Node 包装器及健康探测开销 |
| `dsh_total_ms` | guest：spawn DSH → 子进程 stdout/stderr 关闭；startup 为 help 完整耗时，agent 含模型与进程退出 |
| `dsh_first_event_ms` | guest：spawn → 首个合法 NDJSON 事件；**只是启动代理指标，不保证不含模型时间** |
| `dsh_first_text_ms` | guest：spawn → 首个非空 text 事件；含 DSH 初始化、请求网络、模型 TTFT，不叫“纯启动” |
| `request_ms` | 父实例：POST 前 → 收到完整 JSON；不是流式客户端 TTFT |
| `launch_to_response_ms` | 父实例：创建 Firecracker 进程前 → cold 请求完整响应；第一条消息端到端总耗时 |
| `ready_to_invoke_ms` | 健康响应 → POST 前的检查/编排间隔，单列而不冒充 VM 时间 |
| `request_non_dsh_ms` | `request_ms - dsh_total_ms`，网络/HTTP/配置及测量残差；不解释为某个具体内部阶段 |

可核对：`launch_to_response_ms = sandbox_ready_ms + ready_to_invoke_ms + request_ms`。不要把各阶段的 p50 相加当成总耗时 p50。主机和 guest 各自用单调时钟，只比较本机时间差，不跨 VM 减 epoch timestamp。

guest 每轮从新的 RAM-backed initramfs 启动，没有 DSH 预热，也不恢复快照。**父实例的镜像页缓存没有清除**，且计算镜像 SHA256 会读取全部镜像；因此是“父实例已准备、镜像在本地”的新 VM 冷启动，不是 EBS 冷读、镜像下载、EC2 开机或发布全链路冷启动。构建/下载/网络配置/凭证准备都在计时外。initramfs 解压与把镜像装入 Sandbox 在计时内。

按 cold/warm 分组输出 nearest-rank p50/p90/p99、均值、最小/最大值；失败单独计数，不混入成功分位数。记录 requested/executed rounds，失败后未执行的样本不算失败。10 轮只适合冒烟；高分位至少增加样本并重复批次，保留 `round-0001` 而不是静默丢弃第一轮。另输出 `first_round_records` 与 `subsequent_summary`，便于区分第一轮和后续轮的父实例缓存/负载差异；都仍是新 guest，不宣称镜像读取同样冷。

## 准备专用 EC2 父实例

以下是手动部署步骤，执行前核对账号/区域。已完成的真实部署信息在 `deployment.json` 和实测报告中；不要再次运行这些创建命令来覆盖既有测试资源。使用独占、无其他 Sandbox 的测试实例，内存池与重启只作用于新父实例，不修改开发机内核。

1. 选择支持的机型。例如官方示例 `m7i.4xlarge`（x86_64），也可按支持列表选 Graviton；不要用 flex、metal 或开启 nested virtualization 的实例。
2. 使用对应架构的 AL2023 kernel 6.18 AMI。已有子网、安全组、实例 profile 由你提供。建议用 SSM，不开放公网 guest 8080；若使用 SSH，只允许管理来源。

```bash
# 先在你的 shell 设置 REGION、SUBNET_ID、SG_ID、INSTANCE_PROFILE。
# 下方是 x86_64 示例；Graviton 需改 AMI 参数末尾为 arm64 并选对应机型。
aws ec2 run-instances --region "$REGION" \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.18-x86_64 \
  --instance-type m7i.4xlarge \
  --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --enclave-options Enabled=true \
  --metadata-options HttpTokens=required \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=dsh-nitro-benchmark}]'
```

在父实例安装构建与网络工具，检查版本：

```bash
sudo dnf install -y dracut nftables iproute kmod
uname -r  # 至少 6.18.51-120.162.amzn2023
sudo grubby --update-kernel=ALL --args='nitro_enclaves.mempool=8G@0'
sudo reboot
# 重新连接后：
sudo dmesg | grep 'nitro_enclaves: CMA pool'
ls -l /dev/nitro_enclaves
cat /sys/class/misc/nitro_enclaves/pcie_slots{,_total,_in_use}
```

8 GiB 池是本 demo 顺序启动单个 4 GiB guest 的示例，必须在实例总内存内，并给父实例和设备缓冲区留余量。不要在运行其他服务的主机上随意修改。默认 CPU overcommit 开启，所有测试都需记录父实例竞争；这是共享 CPU，不是独占核心。

按官方安装方式装 Firecracker（在父实例）：

```bash
VERSION=1.18.0
ARCH=$(uname -m)
curl -sSfL -o firecracker.tgz \
  "https://github.com/firecracker-microvm/firecracker/releases/download/v${VERSION}/firecracker-v${VERSION}-${ARCH}.tgz"
tar -xzf firecracker.tgz
sudo install -m 755 "release-v${VERSION}-${ARCH}/firecracker-v${VERSION}-${ARCH}" /usr/local/bin/firecracker
firecracker --version
```

下载前按你的软件来源策略核验 release；benchmark 不替你下载或升级 Firecracker。

## 构建 DSH guest

在父实例上准备原生架构 **Node 24 + npm**（来自你信任的包源），检查 `node --version`，不要把本机 arm64 Debian Docker 镜像直接当作 x86_64 AL2023 guest。镜像需在目标 AL2023 上构建，特别是 DSH `.node` 原生插件及其动态库。`DSH_DIR` 使用绝对路径；guest 不复制宿主机的 passwd、证书私钥或其他用户配置。

```bash
cd 27-nitro-ec2
mkdir -p artifacts-src/dsh
cd artifacts-src/dsh
npm init -y
npm install --omit=dev --no-audit --no-fund '@deepseek-ai/dsh@0.2.2-rc.1'
cd ../..
sudo env DSH_DIR="$PWD/artifacts-src/dsh" NODE_BIN="$(command -v node)" \
  bash scripts/build_guest.sh
```

产物：`artifacts/vmlinuz`、`artifacts/initrd.img`、`artifacts/SHA256SUMS` 和 `artifacts/manifest.json`（kernel/DSH/Node 版本与 hash）。脚本拒绝覆盖已有 output；重新构建时指定新的 `OUTPUT_DIR`，保留旧镜像和 hash 做对照。自定义产物目录时同时传 `--kernel`、`--initrd`、`--manifest`；benchmark 核对 guest kernel 最低版本、架构和镜像 SHA256，避免错用旧产物。

`dracut` 只使用 `bash` module，**不能加 `base`**，否则它自己的 `/init` 会覆盖测试 init。包含 Node 动态库、DSH native addon 动态库和同一套 DSH node_modules；构建期只在隔离的临时 DSH_HOME 中生成 stock headless profile，不继承 AWS 环境变量。

这个 initramfs 是最小基准环境，DSH 以 root 运行，只测短文本任务；不是功能完整的编码 agent OS，没有完整工具链、持久卷或权限隔离配置。不应在其中运行未知用户的代码或使用高权限模型凭证。

## 运行基准

在父实例的 `27-nitro-ec2` 目录内运行，Python 3.9+、标准库，无需 `uv sync` 或 pip 安装。

### 1. 先验证无模型启动

```bash
sudo python3 benchmark.py --rounds 10 --out results/startup-confidential
# 对照 direct 模式，必须单独保存一批：
sudo python3 benchmark.py --rounds 10 --direct-mode --out results/startup-direct
```

benchmark 自动创建唯一 TAP 和 nft tables，只作用于测试 TAP；固定地址 `192.0.2.1/30`（父）和 `192.0.2.2/30`（guest），已有冲突路由则拒绝运行。默认拒绝 guest 转发和访问父实例服务，不改全局出网设置。无需手工创建 TAP。

### 2. 真实 DSH Agent 第一条消息

需要明确选择你的区域与已获访问权限的 Bedrock 模型，以及**短期、最小权限凭证**。与旧 demo 对齐时可选 `us-east-1`、`deepseek.v3.2`；先确认你账号在该区域可调用该模型。准备只含目标模型调用权限的角色；DSH 负责 provider 签名，本 demo 不创建 IAM 角色。

```bash
# 在你自己的 shell 中执行；不把凭证粘贴到对话或提交到 git。
# 文件需位于父实例本地；权限 600，测试结束后由你删除。
umask 077
aws configure export-credentials --profile YOUR_TEMPORARY_PROFILE --format process > credentials.json
sudo python3 benchmark.py --workload agent --direct-mode \
  --credentials-file "$PWD/credentials.json" \
  --region us-east-1 --model deepseek.v3.2 \
  --rounds 10 --out results/agent-direct
```

[凭证导出格式](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html)是 JSON `AccessKeyId` / `SecretAccessKey` / `SessionToken` / `Expiration`。脚本校验权限和有效期；凭证在计时前读取，通过父/guest 测试链路传给子进程环境，不写镜像、日志或结果。为避免暗示已经具备 confidential secret release，本 demo 的 agent 测试**强制 direct 模式**；它不实现 NSM attestation / KMS 验证，可信父实例可看到内存中的凭证。不要据此宣称父实例不可获取密钥。

agent 测试临时开启 IPv4 forwarding 与出网 NAT，测试 TAP 只允许 Amazon DNS 和公网 HTTPS，拒绝 IMDS `169.254.169.254`、IPv6 和私网目的地址。现有主机防火墙可能进一步阻止访问；不会冲掉现有规则。父实例需要公网出口或 NAT 出口，本版不支持私有 Bedrock endpoint。

默认 prompt 是短文本 `pong`，只测响应，不执行编码任务；`--approval-policy never` 不是禁用工具的安全策略。model profile 明确设置 `maxTokens=512`；`--expected-text` 默认 `pong`，同时记录答案是否精确匹配。默认一条失败就停；显式启用 `--continue-on-answer-mismatch` 时只允许已正常完成、但答案不符的回合继续统计，连续 3 轮不符则停止，执行/HTTP/超时失败仍立即停。`summary` 是严格答案通过的统计，`completion_summary` 包含所有已完成文本回合，不能混称成功率。模型调用会产生费用，stock DSH 的内部重试行为不算“纯 VM 冷启动”。单回合默认上限 120 秒，可用 `--agent-timeout` 调整（最大 300 秒）。外层不重试调用。

### 3. 查看原始证据

每个输出目录包含：

- `results.json`：配置、镜像 bytes/SHA256、主机负载、版本、逐轮冷/热记录、成功样本分位数、失败/提前停止状态。
- `round-0001/sandbox.json` 等：实际 Firecracker 配置（不含密钥）。
- `round-0001/firecracker.log` 等：VMM 日志；正常 confidential 模式不显示 guest console。
- `round-0001/records.json` 等：停止 VM 前保存的单轮样本，清理失败或 Ctrl+C 不会抹掉已完成请求；总结果会重新汇总这些文件。

```bash
sudo python3 -c 'import json; d=json.load(open("results/agent-direct/results.json")); print(json.dumps(d["summary"], indent=2))'
```

同一镜像、机型、vCPU/内存、mode、CPU overcommit、区域、模型、prompt 和负载下比较；startup 与 agent workload 不混合统计。CLI 默认顺序测试，不支持并发，不把 host 排队时间混入隔离的 boot 样本。

## 安全、失败与清理

- 仅用于专用测试父实例，benchmark 要求 root，拒绝已有运行中的 Sandbox，用 `/run/dsh-nitro-benchmark.lock` 防止本 demo 重复启动；它不能锁住其他编排器。
- 镜像只含代码与生成的 headless profile。DSH 包和 parent 必须可信，不能把 npm 未审核代码当生产可信镜像。
- Nitro guest 没有自己的 IMDS；父实例上的 IMDSv2 或 hop limit 不是隔离边界。本版使用 nft 显式阻止 TAP 访问父实例和 IMDS。不开放公网端口，不使用宿主角色的隐式 metadata 凭证。
- 建立网络、镜像 hash、credentials/export 都不算启动耗时；`--debug` 仅供定位 boot，记录在结果中，不用于正式结果或 attestation。
- 健康检查 GET 有整体 deadline；**POST 只执行一次**。返回失败、响应无法验证、超时、DSH 无完成事件都会停止批次。不会把超时后的第二次调用伪装为冷启动，也不会无人值守循环重试 AccessDenied/限流。
- 每轮 `finally` 停止自己的 Firecracker；未释放 slot 则不再启动下一轮。批次 `finally` 先删除自己的 TAP（切断流量），再恢复原 forwarding 设置、删除自己创建的 nft tables，保留结果/日志。不会终止其他实例或 Sandbox，不会清空系统 nft ruleset。
- Ctrl+C 会正常清理。SIGKILL/父实例宕机无法执行 finally；按结果配置中 TAP 名称和本 demo 的 `dshb_...` table 名手工核查，不要使用 `pkill firecracker` 或 `nft flush ruleset`。父实例停止/重启也会终止其 Sandbox。
- EC2、EBS 与模型费用不会因 benchmark 退出而自动停止。完成后确认实例 ID，再终止你为测试创建的实例，按需清理卷与本地凭证文件。脚本不自动删除云资源。

## 本地验证与限制

```bash
python3 -m unittest discover -s tests -v
node --check guest/server.mjs
bash -n scripts/build_guest.sh guest/sandbox-init
```

测试覆盖 Firecracker `enclave` 配置、cold/warm 统计、失败剔除、单调时间合法性、身份匹配、凭证权限/过期、IMDS 规则、POST 超时不重试、进程停止，以及 Node HTTP 包装器的 NDJSON 分片/Unicode/无结尾换行、无 text/失败、超时、输出上限和后续调用。

`tests/fake_dsh.mjs` 是测试协议用的模拟进程，**不是 DSH 冷启动实测**。实际 stock DSH CLI 可以用仓库现有本地镜像额外验证 help，但普通 Docker 结果也不是 Nitro 结果。

2026-11-17 初版（部署前）本地验证：19 项 unittest 全部通过，Python 编译、Node/Bash 语法与 whitespace 检查通过；使用已缓存的 `dsh-coldstart:dsh-0.2.2-rc.1` 镜像，在 `--network none` 下运行本 demo 包装器，stock DSH 的 startup 和第二次调用均成功，未调用模型。该初版验证阶段没有安装新依赖、创建云资源或运行 Nitro 硬件测试。

开发机本身仍没有 Nitro Sandbox 设备；后续硬件验证在专用 EC2 父实例 `i-062d42b82e041af2d` 上完成，见 [实测报告](TEST_REPORT.zh.md) 和 `deployment.json`。最终 28 项离线测试通过。真实部署修复了 npm 包内多平台/musl 插件依赖筛选，以及 IMDS `Token` → CLI `SessionToken` 的格式转换。

部署辅助代码：`parent_user_data.sh` 保留内存池并重启新实例；`prepare_parent.sh` 安装和构建；`ssm_command.py` 单次提交、有界等待、分片下载；`run_startup_benchmark.sh` / `run_agent_benchmark.sh` 是实际使用的批次；`collect_evidence.sh` / `unpack_evidence.py` / `analyze_results.py` 归档与审计。实例已停止；重新运行应使用新的输出目录，不覆盖原始证据，且 Agent 扩样需先确认任务和答案评判方式。
