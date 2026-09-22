# Code Interpreter EFS verification

Date: 2026-09-22. Profile: `agentcore_cn`. Region: `cn-northwest-1`.

The current AWS documentation and boto3 model expose `efsConfiguration` on both
`CreateCodeInterpreter` and `StartCodeInterpreterSession`.

Verify actual China-region behavior:

1. Create an isolated encrypted EFS file system, access point, and one mount target
   in the same Availability Zone as the test Code Interpreter subnet.
2. Create test security groups allowing NFS TCP 2049 only from the Code Interpreter
   group to the EFS mount target, and a temporary execution role with EFS and VPC permissions.
3. Create a VPC Code Interpreter with a mount at `/mnt/efs`.
4. Start session A, verify the mount and write a unique file; stop A.
5. Start session B, verify and append to A's file; stop B. Start C and verify the append.
6. Save API responses, request IDs, mount information and hash checks.
7. Stop all test sessions and delete only this experiment's Code Interpreter,
   EFS resources, security groups and IAM role.

The existing EFS file system and the retained, stopped EC2 are not test resources.
SDK schema support is separate from successful service-side mounting. Report any
regional rejection or incomplete verification explicitly.

Reference:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-filesystem-configurations.html

## Outcome

EFS mounting and persistence passed in three sessions. `/mnt/efs` reported NFSv4.1;
session C read the content appended by B after A and B had stopped.
The interpreter, EFS filesystem, access point, mount target, IAM role and EFS
security group were deleted. The client security group is waiting for a
service-attached ENI to be released; a bounded background retry is recorded in
`results/20260922/network_cleanup_retry.json`. No service ENI is force-detached.
