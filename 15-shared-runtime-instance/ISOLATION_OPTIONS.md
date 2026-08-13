# 实例内容器隔离——方案与待验证事项

> 2026-08-12 讨论记录。目标：在**同一台 EC2**（EC2 Capacity Provider 模式、
> 共享 runtimeSessionId）内，为多用户提供比当前应用层守卫更强的隔离。
> 前提事实：AgentCore 为每个 `runtimeSessionId` 分配独立 microVM/EC2，
> **不做**同实例多 session 装箱，所以"每用户独立 session"= 每用户独占一台
> EC2，成本不可接受，不在本文讨论范围。

## 已完成的探测（实测数据）

探测对象：`i-093ad3cf11259bdb3` 上运行中的 agent 容器
（`ctr -n agentcore`，容器 id 前缀 `ac-dc7aea...`）。

宿主机（ASG `agentcore-managed-instances-capacity_provider_arm_kb-FQtDNVGq1t`，
5 台 c7g.large，全 on-demand，MaxInstanceLifetime 14 天）：

- 容器运行时：**containerd**（namespace `agentcore`）+ runc + **nerdctl**；无 docker
- SSM 可达，命令以 root 执行
- `/mnt/scratch` = 宿主机 `/var/lib/agentcore/volumes/scratch`，rbind rw 进容器
  ——宿主机与 agent 容器天然共享这块盘

Agent 容器 OCI spec / 运行时状态：

| 项 | 实测值 | 含义 |
|---|---|---|
| user | uid=0 gid=0 | 容器内是 root…… |
| CapEff / CapBnd | `0000000000000000` / `0000000000000000` | ……但**零 capabilities**，且 bounding set 为空，无法重新获得任何权限 |
| NoNewPrivs | 1 | setuid 二进制无法提权 |
| namespaces | pid, ipc, uts, mount, **user**, network | 容器本身已在 user namespace 里 |
| seccomp | defaultAction=ALLOW + **6 条规则**（内容未 dump） | 默认放行、少量黑名单 |
| `unshare -Ur true` | **EPERM 失败** | 嵌套 user namespace 被禁 |
| /dev/kvm、docker.sock | 均不存在 | 无嵌套虚拟化/容器逃逸口 |
| linux.resources | null | 容器无 cgroup 资源限制（整机 2 vCPU 全给它） |

## 各方案判定

### 路线 A：容器内 per-user UID 降权 —— 基本判死（差最后一锤实证）

思路：每用户一个 Linux UID，Claude 子进程 setuid 运行 + 工作区 chown 0700。

实测障碍：容器虽是 uid 0，但 CapEff=0（没有 CAP_SETUID/CAP_SETGID/CAP_CHOWN），
CapBnd=0 + NoNewPrivs=1 意味着没有任何取回权限的途径。理论上 `setuid()`、
`chown()` 都会 EPERM。

**待验证**（预期失败，做实证收尾）：

```bash
# 容器内执行（ctr -n agentcore tasks exec 或临时调试 endpoint）
python3 -c "import os; os.setuid(1000)"                 # 预期 PermissionError
python3 -c "import os,tempfile; f=tempfile.mktemp(); open(f,'w').close(); os.chown(f,1000,1000)"  # 预期 PermissionError
```

### 路线 B：容器内无特权沙箱（bubblewrap / nsjail）—— 判死（可选：查清死因）

思路：user namespace 里给每个 Claude 子进程独立 mount/pid 视图。

实测障碍：`unshare -Ur true` → EPERM，嵌套 userns 被禁。死因有两个候选，
结论不变，但查清后可知道有没有配置层面的翻案余地：

**待验证**：

```bash
# 1) dump 那 6 条 seccomp 规则，看是否黑名单了 unshare/clone(CLONE_NEWUSER)/mount/setns
#    宿主机执行：
ctr -n agentcore containers info <容器ID> \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['Spec']['linux']['seccomp']['syscalls'], indent=1))"

# 2) 容器内检查 userns 配额是否被清零
cat /proc/sys/user/max_user_namespaces
```

### 路线 C：宿主机侧 sibling container —— 唯一活路，主攻方向

思路：agent 容器只做编排；宿主机上的 runner 用 containerd/nerdctl 起
per-user 容器，任务与产物通过共享盘交换（宿主机
`/var/lib/agentcore/volumes/scratch` = 容器内 `/mnt/scratch`，已实测 rw 共享）。
每个用户容器 `--user <uid>` + 只 bind 自己的工作区，得到真容器隔离。

已确认的有利条件：

- 宿主机有 containerd + nerdctl + runc，SSM root 可达
- `/mnt/scratch` 双向读写共享（load test 已长期依赖此机制验文件）
- 实例全 on-demand，MaxInstanceLifetime 14 天（会被回收，runner 需自愈）

代价与定位：绕过 AgentCore 管理面，demo 可行、生产不推荐；宿主机被 ASG回收/替换时 runner 与本地镜像全部丢失，需要 SSM State Manager association
（按 ASG tag `bedrock-agentcore:capacity-provider-id` 定向）在新实例上自动
  重装 runner。