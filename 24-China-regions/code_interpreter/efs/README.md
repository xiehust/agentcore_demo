# Code Interpreter supports Amazon EFS

**Verified with real file operations in `cn-northwest-1` on 2026-09-22**, using
profile `agentcore_cn`, account `447150580482`.

A custom VPC Code Interpreter successfully mounted an EFS access point at
`/mnt/efs`. Files remained available across three separate sessions, including
after stopping the session that wrote them.

## What passed

| Check | Result |
| --- | --- |
| Create Code Interpreter with `filesystemConfigurations[].efsConfiguration` | Accepted; resource became READY |
| Mount visible inside Python execution | `/mnt/efs`, filesystem `nfs4`, protocol version 4.1 |
| Write a file in session A | Successful; SHA-256 matched the local expected content |
| Stop A, start B, read A's file | Exact content matched |
| Append in B, stop B, start C, read the updated file | Exact content and SHA-256 matched |
| Access-point POSIX ownership | UID 1000, GID 1000 |

Final SHA-256 after the append:

```text
95deb8fe6349330ee127fcf5234fa3f48590bf8dfc6aead162a477d5d245ca9e
```

This verifies the actual mount and cross-session persistence, beyond SDK schema
support or acceptance of the create request. The test configured the filesystem
at interpreter creation; sessions inherited it. Session-level filesystem
configuration is also documented by AWS, but was not separately exercised here.

## Required configuration

- A **custom Code Interpreter with VPC networking**.
- An EFS filesystem and **access point**.
- An EFS mount target reachable in the VPC and in the same Availability Zone as
  at least one configured Code Interpreter subnet.
- NFS TCP **2049** allowed from the interpreter security group to the mount-target
  security group.
- An execution role with `elasticfilesystem:ClientMount` and
  `elasticfilesystem:ClientWrite`, scoped to the filesystem and access point.
- Writable access-point POSIX configuration and a mount path such as `/mnt/efs`.

The relevant boto3 configuration is:

```python
filesystemConfigurations=[
    {
        "efsConfiguration": {
            "accessPointArn": EFS_ACCESS_POINT_ARN,
            "fileSystemArn": EFS_FILE_SYSTEM_ARN,
            "mountPath": "/mnt/efs",
        }
    }
]
```

Pass this to `create_code_interpreter` together with `executionRoleArn` and
`networkConfiguration={"networkMode": "VPC", "vpcConfig": ...}`.
See the executable script for the complete IAM, network, filesystem and
interpreter setup.

## Evidence and reproduction

- [Plan](PLAN.md)
- [Verification script](verify_efs.py)
- [Result and mount details](results/20260922/result.json)
- [Create request and response](results/20260922/api/create-interpreter.json)
- [Final READY configuration](results/20260922/interpreter-ready.json)
- [Session A write](results/20260922/api/session-a-write.json)
- [Session B read and append](results/20260922/api/session-b-read-append.json)
- [Session C read](results/20260922/api/session-c-read.json)
- [Resource inventory](results/20260922/resources.json)
- [Cleanup operations](results/20260922/cleanup.json)
- [Independent verification and cleanup audit](results/20260922/audit.json)

From this directory, with Python 3.12 and the parent directory's boto3 requirements:

```bash
python3 verify_efs.py all --output results/new-run
```

The script is scoped to the test China account and its existing private subnet.
Use a new output directory for another run. It creates temporary resources and
cleans them in `finally`. An interrupted run can be cleaned with:

```bash
python3 verify_efs.py cleanup --output results/new-run
```

The pre-existing EFS filesystem was not modified. The retained EC2 instance was
not started or used for this verification.

The test interpreter, EFS filesystem, access point, mount target, role and EFS
security group have been deleted. The client security group
`sg-049391a8216deb6b8` remains dependent on service-attached ENI
`eni-0951f5e08b0da0589`. A bounded retry job in [cleanup_network.py](cleanup_network.py)
waits for AWS to detach that interface before deleting it and the group; it never
force-detaches a service interface. Current retry status is recorded in
`results/20260922/network_cleanup_retry.json`.
The verification result is PASS; the original command exited nonzero because
this final network cleanup exceeded its initial five-minute wait.

AWS reference:
[File system configurations for AgentCore Code Interpreter](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-filesystem-configurations.html).
