# AgentCore Runtime: third-party file systems, secret-less third-party auth, and operation auditing

## [中文](README.md)

Research and demo code for the concerns raised by the infrastructure lead of a financial-services (FSI)
customer after an AgentCore briefing:

| # | Concern | Verdict | Doc (zh) | Demo |
|---|---|---|---|---|
| 1 | Third-party file systems (FUSE) and workarounds | **The microVM guest kernel has no FUSE driver** (live probe: `ENODEV`); it is not a privilege problem. Use the native `filesystemConfigurations` (sessionStorage / EFS / S3 Files), userspace sync for non-AWS stores, and the Instances compute type when FUSE is a hard requirement | [docs/01](docs/01-filesystem-fuse.md) | `scripts/01-fuse-probe.py`, `scripts/02-create-runtime-with-fs.sh`, `demo/fs_workaround/` |
| 2 | Secret-less access to GitHub / GitLab / Vault from the sandbox | Three tiers: credentials never enter the sandbox (Gateway outbound auth) → only short-lived tokens enter (AgentCore Identity Token Vault — GitHub follows the [official outbound-auth sample](https://github.com/awslabs/agentcore-samples/tree/main/01-features/05-authenticate-and-authorize/02-outbound-auth/03-outbound-auth-github) 3LO pattern; service identity via GitHub App broker + `GIT_ASKPASS`) → federate the AWS identity directly (Vault AWS IAM auth, zero secrets) | [docs/02](docs/02-secretless-third-party-auth.md) | `demo/secretless_auth/` |
| 3 | Auditing tool calls and network access | Four layers: CloudTrail (APIs), AgentCore Observability OTEL spans + deterministic audit hook (tool calls), Gateway + Policy `LOG_ONLY` (authorisation as audit), and — **VPC mode only** — Flow Logs / DNS logs / Network Firewall (network). PUBLIC mode exposes no egress detail | [docs/03](docs/03-audit-observability.md) | `demo/audit/` |
| 4 | Evaluation: self-built LLM judge vs managed Evaluations | *Out of scope for this round (removed at the requester's request)* | — | — |

## Key live evidence (2026-09-03, us-east-2)

`scripts/01-fuse-probe.py` runs a static probe inside a real microVM session via `InvokeAgentRuntimeCommand`
(raw data: [`results/fuse_probe.json`](results/fuse_probe.json)):

```
kernel                      Linux 6.1.161-18.298.amzn2023.aarch64 (Firecracker guest)
runs_as_root                true      CapEff = all 41 capabilities, Seccomp = 0
tmpfs_mount_allowed         true      -> mounting itself is permitted
dev_fuse_exists             false
kernel_fuse_registered      false     no "fuse" in /proc/filesystems
mknod_fuse_open_error       OSError: [Errno 19] No such device   <- driver absent
mount -t fuse               unknown filesystem type 'fuse'
loadable_modules            false     no /proc/modules -> cannot add the driver
kernel_nfs_registered       true      mount.nfs4 present (the platform uses it for EFS / S3 Files)
```

Consequence: userspace FUSE clients (s3fs / goofys / JuiceFS / sshfs / rclone mount) cannot work inside the microVM.
The NFS client is present, so "third-party FS → bridge EC2 → NFS re-export" is technically possible in VPC mode but
unsupported (docs/01 §3.2).

## Layout

See the tree in [README.md](README.md#目录). Pure-logic unit tests: `python3 -m pytest tests -q` (41 tests, no AWS or
Strands required). `scripts/runtime_cmd.py` follows the repository contract in
`.trellis/spec/backend/agentcore-runtime-command.md` (consume every stream event, require `COMPLETED` + `exitCode 0`,
explicit `/bin/bash -c` wrapper, `StopRuntimeSession` in `finally`).

