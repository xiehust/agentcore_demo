"""Verified ECS Managed Instances/Nitro task and storage configuration generators."""
def capacity_provider(state):
    return {"name": state["capacity_provider"], "cluster": state["cluster"],
        "managedInstancesProvider": {
            "infrastructureRoleArn": state["infrastructure_role_arn"],
            "instanceLaunchTemplate": {
                "ec2InstanceProfileArn": state["instance_profile_arn"],
                "networkConfiguration": {"subnets": [state["subnet_id"]], "securityGroups": [state["host_sg"]]},
                "instanceRequirements": {"vCpuCount": {"min": 8, "max": 8},
                    "memoryMiB": {"min": 32768, "max": 32768}, "allowedInstanceTypes": ["m7g.2xlarge"]},
                "storageConfiguration": {"storageSizeGiB": 80},
                "capacityOptionType": "ON_DEMAND", "monitoring": "BASIC", "nitroEnclavesEnabled": True},
            "infrastructureOptimization": {"scaleInAfter": 300}, "propagateTags": "CAPACITY_PROVIDER"},
        "tags": [{"key": "Project", "value": "dsh-nitro-ecs"}]}


def task_definition(state, family=None, efs=None, task_role=None):
    volume = {"name": "workspace", "host": {}}
    if efs:
        volume = {"name": "workspace", "efsVolumeConfiguration": {"fileSystemId": efs["filesystem_id"],
            "rootDirectory": "/", "transitEncryption": "ENABLED",
            "authorizationConfig": {"accessPointId": efs["access_point_id"], "iam": "ENABLED"}}}
    return {"family": family or state["family"], "requiresCompatibilities": ["MANAGED_INSTANCES"],
        "networkMode": "awsvpc", "cpu": "2048", "memory": "4096",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "taskRoleArn": task_role or state["task_role_arn"], "executionRoleArn": state["execution_role_arn"],
        "trustedExecutionConfiguration": {"isolationMode": "ISOLATED_SANDBOX"},
        "volumes": [volume, {"name": "tmp", "host": {}}],
        "containerDefinitions": [{"name": "dsh", "image": state["image_uri"], "essential": True,
            "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
            "mountPoints": [{"sourceVolume": "workspace", "containerPath": "/workspace", "readOnly": False},
                            {"sourceVolume": "tmp", "containerPath": "/tmp", "readOnly": False}],
            "readonlyRootFilesystem": True,
            "environment": [{"name": "AWS_REGION", "value": state["region"]},
                            {"name": "DSH_MODEL_ID", "value": "deepseek.v3.2"},
                            {"name": "PRIVATE_BIND", "value": "true"}],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": state["log_group"], "awslogs-region": state["region"], "awslogs-stream-prefix": "task"}},
            "stopTimeout": 30}]}


def efs_client_policy(filesystem_arn, access_point_arn):
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Action": ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
        "Resource": filesystem_arn, "Condition": {
            "StringEquals": {"elasticfilesystem:AccessPointArn": access_point_arn},
            "Bool": {"aws:SecureTransport": "true", "elasticfilesystem:AccessedViaMountTarget": "true"}}}]}


def efs_filesystem_policy(filesystem_arn, access_point_arn, task_role_arn):
    actions = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "TenantAccess", "Effect": "Allow", "Principal": {"AWS": task_role_arn},
         "Action": actions, "Resource": filesystem_arn,
         "Condition": {"StringEquals": {"elasticfilesystem:AccessPointArn": access_point_arn},
             "Bool": {"aws:SecureTransport": "true"}}},
        {"Sid": "DenyOtherIdentities", "Effect": "Deny", "Principal": "*", "Action": actions,
         "Resource": filesystem_arn, "Condition": {"ArnNotEquals": {"aws:PrincipalArn": task_role_arn}}},
        {"Sid": "DenyOtherPaths", "Effect": "Deny", "Principal": "*", "Action": actions,
         "Resource": filesystem_arn, "Condition": {"StringNotEquals": {
             "elasticfilesystem:AccessPointArn": access_point_arn}}},
        {"Sid": "DenyPlaintext", "Effect": "Deny", "Principal": "*", "Action": actions,
         "Resource": filesystem_arn, "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyRoot", "Effect": "Deny", "Principal": "*",
         "Action": "elasticfilesystem:ClientRootAccess", "Resource": filesystem_arn}]}
