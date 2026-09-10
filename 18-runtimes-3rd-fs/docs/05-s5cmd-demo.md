# 单 Runtime、多 session：s5cmd 与 JuiceFS 共享卷 Demo

> 2026-09-10：s5cmd 镜像已更新到现有单 Runtime（版本 3）。8/32/64/128 workers 各三轮云端测试完成，256 workers 未完成并保留失败记录。结果见 [s5cmd 云端实测](07-s5cmd-cloud-results.md)；不沿用旧 SDK 性能结论。

## 1. 架构和权限

**只创建一个 AgentCore Runtime。** 不同用户用不同 `runtimeSessionId` 调用同一个 ARN，各 session 对应独立 microVM。一个外置 JuiceFS S3 Gateway 连接一个共享卷，用 `tenant-a` / `tenant-b` 两个逻辑 bucket 分租户。

- 原生 S3：s5cmd 使用控制端签发的租户 STS 凭证，只访问 `direct/<tenant>/*`。
- JuiceFS：同一个 s5cmd 二进制，改用网关 endpoint 和租户网关凭证，只访问对应逻辑 bucket 的 `bench/*`。
- 共享 Runtime 执行角色仅有 ECR/日志权限，不读租户 Secret，不访问底层 JuiceFS S3 数据，也不 AssumeRole。
- 可信控制端读取 Secret、获取 STS、调用 Runtime。初始化绑定 tenant、真实 context session ID 和随机 session token；之后不能切换租户。
- s5cmd 凭证只注入子进程环境，不修改父进程全局 AWS 环境，也不出现在 argv、命令文件或结果里。`AWS_CA_BUNDLE` 信任网关证书，不使用 `--no-verify-ssl`。

boto3 仅留在部署、STS、Secrets Manager、Runtime 调用及网关初始化控制面；**本 demo 的工作区复制、manifest 复制及数据面隔离探测不使用 boto3**。原项目中无关的通用 S3 同步示例不属于此测试。

首次初始化只信任控制端的 SigV4 调用，不是面向任意终端用户的公开凭证 broker；上线前必须限制调用权限并维护真实用户/session 归属。静态网关凭证不会在 session 停止后自动撤销。同租户 session 级存储权限隔离、生产 broker 和网关 STS 不在此 demo 内。

## 2. 文件复制与一致性

镜像预装 **s5cmd v2.3.0 Linux ARM64**，安装时验证官方 archive SHA-256。每个文件树阶段只启动一个 `s5cmd run commands.txt` 进程，内部按 `--numworkers` 并发复制全部普通文件，不为每个文件 fork 一次。

命令文件从冻结的可信 manifest 生成，示意：

```text
cp --raw --concurrency 1 --no-follow-symlinks '/workspace/.git/HEAD' 's3://tenant-a/bench/<run>/s5cmd/git-clone/files/.git/HEAD'
cp --raw --concurrency 1 --no-follow-symlinks '/workspace/src/a b.py' 's3://tenant-a/bench/<run>/s5cmd/git-clone/files/src/a b.py'
```

这等效于复制整棵目录，但明确控制目标路径及文件集合；隐藏文件、`.git` 都包含。`--raw` 防止文件名中的 `*`、`[]` 被展开。命令参数经 shellquote 兼容编码，使用 argv 启动且不执行 shell；拒绝换行/NUL 等路径。

目录、符号链接和模式记入 manifest。所有普通文件成功后，另启动一次 s5cmd 复制 manifest，作为本次唯一 run prefix 的提交点；不能把 manifest 放在并行 batch 最后一行来假定它最后执行。

恢复先用 s5cmd 下载 manifest 并与 session 内可信 hash 对比，再批量下载普通文件至新建私有目录，恢复 0644/0755 模式和工作区内安全符号链接，校验完整 SHA-256、文件集合及 Git HEAD。s5cmd 成功 JSON 必须逐条匹配期望 source/destination/size；仅退出码 0 或文件数相同不够。

只支持同一存活 session 内保存/恢复原始冻结快照。跨 session 持久化可信 manifest、同 key 多写者冲突、fsync/断电恢复等不属于本实验。clone/unzip 在本地目录运行，不能称为直接在 JuiceFS POSIX 挂载点上运行。

## 3. 安装、部署和执行

从 `18-runtimes-3rd-fs` 目录运行：

```bash
# 本地固定版本 s5cmd，安装到 build/s5cmd，不改系统 PATH。
python3 scripts/09-install-s5cmd.py
# 已有 fixture 时不用重复执行；脚本拒绝覆盖。
python3 scripts/07-prepare-workspace-fixture.py
python3 -m pytest tests -q
python3 scripts/06-juicefs-local-smoke.py --workspace --out results/s5cmd-local-check.json

# 镜像内独立安装同版本 s5cmd 并校验 SHA-256。
docker build --platform linux/arm64 -f deploy/juicefs/Dockerfile -t juicefs-s5cmd-demo:local .

# 查看资源计划，不创建资源。
python3 scripts/05-juicefs-demo.py plan
# 全新环境：会创建收费资源。
python3 scripts/05-juicefs-demo.py deploy --region us-west-2 --approve-costs
# 已有本 demo 环境：更新同一个 Runtime，不再创建第二个。
python3 scripts/05-juicefs-demo.py update --approve-costs
```

`deploy` 与 `update` 二选一。更新前确认没有其他控制机正在运行实验；本地文件锁只保护同控制机。旧云端镜像不会因为改了本地代码而自动升级；新控制器检查 `benchmark_engine=s5cmd` 和响应 schema，拒绝把旧镜像结果当作新结果。

```bash
python3 scripts/05-juicefs-demo.py run --workers 32 --repetitions 3 --out results/s5cmd-cloud-w32.json
python3 scripts/08-verify-benchmark-results.py results/s5cmd-cloud-w32.json --collect-cloud-evidence --out results/s5cmd-validation-w32.json
```

状态文件仍为 `build/juicefs-state.json`，结果使用 `s5cmd-*.json/.md` 新命名。新增 `--workers`，不保留旧 `--suite`、`--concurrency` 和合成 SDK 微基准入口。配置、命令与详细验收见 [测试说明](06-s5cmd-test-plan.md)。

## 4. 当前资源、限制和清理

现有实验栈 `jfsbench-d6935b4639`、Runtime `jfsbench_d6935b4639-5IfNz2BMEZ`、网关 `i-08523634aef9c3335` 保留；本轮更新同一个 Runtime 的 s5cmd 镜像并完成云端对照。完整资源证据见 [验收 JSON](../results/s5cmd-cloud-validation.json)，以 `build/juicefs-state.json` 和 AWS 实际状态为准。成功和失败结果均记录在 [实测报告](07-s5cmd-cloud-results.md)。

本地/网关初始化仍用 `mc` 创建用户；其管理命令在专用可信宿主机运行，部分密钥会短暂出现在该管理进程 argv，不应部署于不可信用户共享的主机。租户 s5cmd 传输进程只使用环境注入，不带这些管理权限。生产环境应改为安全管理 API 或受控凭证文件。

固定版本：[s5cmd v2.3.0](https://github.com/peak/s5cmd/releases/tag/v2.3.0)、[官方 checksums](https://github.com/peak/s5cmd/releases/download/v2.3.0/s5cmd_checksums.txt)、JuiceFS 1.4.1。网关为单 EC2 + SQLite/EBS + 1 GiB cache，禁用 writeback，并非生产 HA。

部署产生 EC2、EBS、IPv4、4 个 interface endpoint、S3、ECR、Secret、Runtime 和日志费用。既有基础费用估算约 $0.13/小时，另加用量；不是账单承诺。每次 run 结束会尝试停止测试 session，不会停止 EC2/endpoint 计费。

```bash
# 会销毁网关 EBS 和 SQLite 元数据，先备份再执行。
python3 scripts/05-juicefs-demo.py cleanup --destroy-demo
```

清理保留 S3 bucket、ECR、三个 Secret 和日志，需显式进一步处理。只保留 S3 数据块不能恢复完整 JuiceFS。用户要求删除的是旧本地性能测试及报告，本轮未删除云端数据或改写 git 历史。
