"""Deployment/verification CLI. AWS IDs are persisted after each creation in .state/."""
import argparse
import base64
import json
import hashlib
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
STATE.mkdir(mode=0o700, exist_ok=True)
STATE_FILE = STATE / "resources.json"
S = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
REGION = "us-west-2"
NAME = "mtls22-20260909"
VPC = "vpc-0edf3a4e323c23b22"
HOST_IP = "172.31.30.139"
PUBLIC_SUBNET = "subnet-092fdd48227f9da16"


def client(service):
    return boto3.client(service, region_name=REGION)


def save(key, value):
    S[key] = value
    STATE_FILE.write_text(json.dumps(S, indent=2, default=str) + "\n")
    STATE_FILE.chmod(0o600)
    print(f"Saved {key}", flush=True)
    return value


def ensure(key, factory):
    return S[key] if key in S else save(key, factory())


def tags(kind):
    return [{"ResourceType": kind, "Tags": [{"Key": "Name", "Value": NAME},
             {"Key": "Project", "Value": "agentcore-mtls-verify"}]}]


def statement(actions, resource="*", **kwargs):
    return {"Effect": "Allow", "Action": actions, "Resource": resource, **kwargs}


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def infra():
    ec2, iam, sm = client("ec2"), client("iam"), client("secretsmanager")
    account = ensure("account", lambda: client("sts").get_caller_identity()["Account"])
    save("region", REGION)
    save("vpc", VPC)
    subnet = ensure("subnet", lambda: ec2.create_subnet(VpcId=VPC, CidrBlock="172.31.240.0/24",
                    AvailabilityZone="us-west-2b", TagSpecifications=tags("subnet"))["Subnet"]["SubnetId"])
    rt = ensure("route_table", lambda: ec2.create_route_table(VpcId=VPC,
                TagSpecifications=tags("route-table"))["RouteTable"]["RouteTableId"])
    ensure("route_association", lambda: ec2.associate_route_table(SubnetId=subnet, RouteTableId=rt)["AssociationId"])
    for key in ["node_sg", "runtime_sg", "endpoint_sg", "platform_endpoint_sg"]:
        ensure(key, lambda key=key: ec2.create_security_group(GroupName=f"{NAME}-{key}",
               Description=f"Isolated mTLS lab {key}", VpcId=VPC,
               TagSpecifications=tags("security-group"))["GroupId"])
    if "security_rules" not in S:
        # Default egress is removed for Runtime; only K3s, endpoints and S3 are reachable.
        ec2.revoke_security_group_egress(GroupId=S["runtime_sg"], IpPermissions=[
            {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        ec2.authorize_security_group_ingress(GroupId=S["node_sg"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 6443, "ToPort": 6443,
            "UserIdGroupPairs": [{"GroupId": S["runtime_sg"]}],
            "IpRanges": [{"CidrIp": HOST_IP + "/32"}]}])
        ec2.authorize_security_group_ingress(GroupId=S["endpoint_sg"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "UserIdGroupPairs": [{"GroupId": S["runtime_sg"]}],
            "IpRanges": [{"CidrIp": HOST_IP + "/32"}]}])
        # ECR/Logs private DNS is VPC-wide; preserve access for existing VPC workloads.
        ec2.authorize_security_group_ingress(GroupId=S["platform_endpoint_sg"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "172.31.0.0/16"}]}])
        s3_prefix = ec2.describe_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": [
            f"com.amazonaws.{REGION}.s3"]}])["PrefixLists"][0]["PrefixListId"]
        ec2.authorize_security_group_egress(GroupId=S["runtime_sg"], IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 6443, "ToPort": 6443,
             "UserIdGroupPairs": [{"GroupId": S["node_sg"]}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
             "UserIdGroupPairs": [{"GroupId": S["endpoint_sg"]}, {"GroupId": S["platform_endpoint_sg"]}],
             "PrefixListIds": [{"PrefixListId": s3_prefix}]}])
        save("security_rules", True)
    for service in ["bedrock-agentcore", "bedrock-runtime", "secretsmanager", "ecr.api", "ecr.dkr", "logs"]:
        platform = service in {"ecr.api", "ecr.dkr", "logs"}
        ensure("vpce_" + service, lambda service=service, platform=platform: ec2.create_vpc_endpoint(
            VpcId=VPC, VpcEndpointType="Interface", ServiceName=f"com.amazonaws.{REGION}.{service}",
            SubnetIds=[subnet], SecurityGroupIds=[S["platform_endpoint_sg" if platform else "endpoint_sg"]],
            PrivateDnsEnabled=platform, TagSpecifications=tags("vpc-endpoint"))["VpcEndpoint"]["VpcEndpointId"])
    ensure("vpce_s3", lambda: ec2.create_vpc_endpoint(VpcId=VPC, VpcEndpointType="Gateway",
           ServiceName=f"com.amazonaws.{REGION}.s3", RouteTableIds=[rt], TagSpecifications=tags("vpc-endpoint"),
           PolicyDocument=policy([statement(["s3:GetObject"],
               f"arn:aws:s3:::prod-{REGION}-starport-layer-bucket/*", Principal="*")]))["VpcEndpoint"]["VpcEndpointId"])
    secret = ensure("secret_arn", lambda: sm.create_secret(Name=f"{NAME}/kubernetes-client",
                    Description="24-hour, read-only K3s mTLS client; no admin credential",
                    Tags=[{"Key": "Project", "Value": "agentcore-mtls-verify"}])["ARN"])
    node_role = ensure("node_role", lambda: iam.create_role(RoleName=f"{NAME}-node",
        AssumeRolePolicyDocument=policy([{ "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                           "Action": "sts:AssumeRole"}]))["Role"]["RoleName"])
    iam.attach_role_policy(RoleName=node_role, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    iam.put_role_policy(RoleName=node_role, PolicyName="WriteLabCertificate",
                        PolicyDocument=policy([statement(["secretsmanager:PutSecretValue"], secret)]))
    ensure("instance_profile", lambda: iam.create_instance_profile(
           InstanceProfileName=node_role)["InstanceProfile"]["InstanceProfileName"])
    if "profile_attached" not in S:
        iam.add_role_to_instance_profile(InstanceProfileName=node_role, RoleName=node_role)
        save("profile_attached", True)
        time.sleep(12)
    ami = ensure("ami", lambda: client("ssm").get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"])
    ensure("instance_id", lambda: ec2.run_instances(ImageId=ami, InstanceType="t4g.small", MinCount=1, MaxCount=1,
        ClientToken=NAME, IamInstanceProfile={"Name": node_role},
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": PUBLIC_SUBNET,
                            "Groups": [S["node_sg"]], "AssociatePublicIpAddress": True}],
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": 12, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        TagSpecifications=tags("instance") + tags("volume"))["Instances"][0]["InstanceId"])
    ec2.get_waiter("instance_running").wait(InstanceIds=[S["instance_id"]], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    instance = ec2.describe_instances(InstanceIds=[S["instance_id"]])["Reservations"][0]["Instances"][0]
    save("node_private_ip", instance["PrivateIpAddress"])
    save("node_public_ip", instance.get("PublicIpAddress"))
    print("Infrastructure ready; next: bootstrap", flush=True)

def bootstrap():
    ssm = client("ssm")
    for _ in range(60):
        nodes = ssm.describe_instance_information(Filters=[{
            "Key": "InstanceIds", "Values": [S["instance_id"]]}])["InstanceInformationList"]
        if nodes and nodes[0]["PingStatus"] == "Online":
            break
        time.sleep(5)
    else:
        raise RuntimeError("New node has not registered with SSM")
    # No secrets are sent through SSM: it uploads the read-only bundle directly to Secrets Manager.
    code = base64.b64encode((ROOT / "bootstrap_node.py").read_bytes()).decode()
    command = (f"python3 -c \"import base64,sys; sys.argv=['bootstrap', '{REGION}', "
               f"'{S['secret_arn']}', '{S['node_private_ip']}']; exec(base64.b64decode('{code}'))\"")
    command_id = ssm.send_command(InstanceIds=[S["instance_id"]], DocumentName="AWS-RunShellScript",
                    TimeoutSeconds=900, Parameters={"commands": [command], "executionTimeout": ["900"]})["Command"]["CommandId"]
    save("bootstrap_command", command_id)
    for _ in range(190):
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=S["instance_id"])
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(5)
            continue
        if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
            (STATE / "bootstrap-result.json").write_text(json.dumps(result, indent=2, default=str))
            print(result["StandardOutputContent"][-7000:])
            if result["Status"] != "Success":
                print(result["StandardErrorContent"][-7000:])
                raise RuntimeError(f"SSM bootstrap {result['Status']}")
            save("bootstrapped", True)
            return
        time.sleep(5)
    raise TimeoutError("SSM bootstrap exceeded 950 seconds")

def endpoint_url(service):
    ep = client("ec2").describe_vpc_endpoints(VpcEndpointIds=[S["vpce_" + service]])["VpcEndpoints"][0]
    if ep["State"] != "available":
        raise RuntimeError(f"{service} endpoint is {ep['State']}")
    # First entry is the regional endpoint name, not the zonal or private-DNS alias.
    return "https://" + ep["DnsEntries"][0]["DnsName"]


def runtime():
    iam, ecr, control = client("iam"), client("ecr"), client("bedrock-agentcore-control")
    account = S["account"]
    repo = ensure("repository", lambda: ecr.create_repository(repositoryName=NAME,
        imageTagMutability="IMMUTABLE", imageScanningConfiguration={"scanOnPush": True})["repository"])
    role = ensure("runtime_role", lambda: iam.create_role(RoleName=f"{NAME}-runtime",
        AssumeRolePolicyDocument=policy([{"Effect": "Allow", "Principal": {
            "Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account}, "ArnLike": {
                "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:*"}}}]))["Role"])
    iam.put_role_policy(RoleName=role["RoleName"], PolicyName="RuntimeLab",
        PolicyDocument=policy([
            statement(["ecr:GetAuthorizationToken"]),
            statement(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], repo["repositoryArn"]),
            statement(["logs:DescribeLogGroups"]),
            statement(["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents"],
                      f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"),
            statement(["secretsmanager:GetSecretValue"], S["secret_arn"]),
            statement(["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], [
                f"arn:aws:bedrock:{REGION}:{account}:inference-profile/us.amazon.nova-2-lite-v1:0",
                "arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0"]),
        ]))
    digest = hashlib.sha256(b"".join((ROOT / p).read_bytes() for p in ["agent.py", "requirements.txt", "Dockerfile"])).hexdigest()[:12]
    image = repo["repositoryUri"] + ":" + digest
    if S.get("image_uri") != image:
        subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", f"{NAME}:{digest}", str(ROOT)], check=True)
        auth = ecr.get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        # Isolate registry credentials from the developer's Docker config.
        docker_config = STATE / "docker"
        docker_config.mkdir(mode=0o700, exist_ok=True)
        subprocess.run(["docker", "--config", str(docker_config), "login", "--username", user,
                        "--password-stdin", auth["proxyEndpoint"]], input=password, text=True, check=True)
        subprocess.run(["docker", "tag", f"{NAME}:{digest}", image], check=True)
        subprocess.run(["docker", "--config", str(docker_config), "push", image], check=True)
        subprocess.run(["docker", "--config", str(docker_config), "logout", auth["proxyEndpoint"]], check=True)
        save("image_uri", image)
    if "runtime_id" in S:
        current = control.get_agent_runtime(agentRuntimeId=S["runtime_id"])
        if current["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] != image:
            control.update_agent_runtime(agentRuntimeId=S["runtime_id"],
                agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
                roleArn=role["Arn"], networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
                    "subnets": [S["subnet"]], "securityGroups": [S["runtime_sg"]]}},
                protocolConfiguration=current["protocolConfiguration"],
                lifecycleConfiguration=current["lifecycleConfiguration"],
                environmentVariables=current["environmentVariables"])
    if "runtime_id" not in S:
        time.sleep(10)
        response = control.create_agent_runtime(agentRuntimeName=NAME.replace("-", "_"),
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": S["image_uri"]}},
            roleArn=role["Arn"], networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
                "subnets": [S["subnet"]], "securityGroups": [S["runtime_sg"]]}},
            protocolConfiguration={"serverProtocol": "HTTP"},
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
            environmentVariables={"AWS_REGION": REGION, "MODEL_ID": "us.amazon.nova-2-lite-v1:0",
                "K8S_SECRET_ARN": S["secret_arn"],
                "SECRETS_ENDPOINT_URL": endpoint_url("secretsmanager"),
                "BEDROCK_ENDPOINT_URL": endpoint_url("bedrock-runtime")},
            tags={"Project": "agentcore-mtls-verify"})
        save("runtime_id", response["agentRuntimeId"])
        save("runtime_arn", response["agentRuntimeArn"])
    for _ in range(90):
        response = control.get_agent_runtime(agentRuntimeId=S["runtime_id"])
        print("Runtime", response["status"], flush=True)
        if response["status"] == "READY":
            save("runtime_config", response)
            ep = control.get_agent_runtime_endpoint(agentRuntimeId=S["runtime_id"], endpointName="DEFAULT")
            save("runtime_endpoint_arn", ep["agentRuntimeEndpointArn"])
            # Restrict OUR invocation endpoint without modifying the host's IAM role.
            client("ec2").modify_vpc_endpoint(VpcEndpointId=S["vpce_bedrock-agentcore"],
                PolicyDocument=policy([statement(["bedrock-agentcore:InvokeAgentRuntime"],
                    [S["runtime_arn"], S["runtime_endpoint_arn"]],
                    Principal={"AWS": f"arn:aws:iam::{account}:role/admin_role_for_workshop"})]))
            return
        if response["status"] not in {"CREATING", "UPDATING"}:
            raise RuntimeError(json.dumps(response, default=str))
        time.sleep(10)
    raise TimeoutError("Runtime not ready after 15 minutes")

def call_runtime(payload):
    endpoint = endpoint_url("bedrock-agentcore")
    session_id = str(uuid.uuid4())
    data = boto3.client("bedrock-agentcore", region_name=REGION, endpoint_url=endpoint,
                       config=Config(read_timeout=180, connect_timeout=15, retries={"max_attempts": 0}))
    started = time.monotonic()
    response = data.invoke_agent_runtime(agentRuntimeArn=S["runtime_arn"], qualifier="DEFAULT",
        runtimeSessionId=session_id, contentType="application/json", accept="application/json",
        payload=json.dumps(payload).encode())
    raw = response["response"].read().decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected Runtime response: {raw[:2000]}")
    return {"payload": payload, "session_id": session_id, "endpoint_url": endpoint,
            "http_status": response["ResponseMetadata"]["HTTPStatusCode"],
            "request_id": response["ResponseMetadata"]["RequestId"],
            "elapsed_seconds": round(time.monotonic() - started, 2), "body": body}


def invoke():
    result = call_runtime({"prompt": "请调用 kubectl_read 读取节点和 proof，报告真实结果。"})
    (STATE / "last-invoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def verify():
    import ipaddress
    from cryptography import x509
    ec2 = client("ec2")
    network = {}
    for service in ["bedrock-agentcore", "bedrock-runtime", "secretsmanager"]:
        url = endpoint_url(service)
        ips = sorted({item[4][0] for item in socket.getaddrinfo(url.split("//")[1], 443, type=socket.SOCK_STREAM)})
        assert ips and all(ipaddress.ip_address(ip) in ipaddress.ip_network("172.31.0.0/16") for ip in ips)
        network[service] = {"endpoint": url, "ips": ips}
    routes = ec2.describe_route_tables(RouteTableIds=[S["route_table"]])["RouteTables"][0]["Routes"]
    assert not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in routes)
    network["runtime_routes"] = routes
    runtime_config = client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=S["runtime_id"])
    assert runtime_config["networkConfiguration"]["networkMode"] == "VPC"
    assert runtime_config["networkConfiguration"]["networkModeConfig"]["subnets"] == [S["subnet"]]
    node = ec2.describe_instances(InstanceIds=[S["instance_id"]])["Reservations"][0]["Instances"][0]
    assert node["VpcId"] == VPC and node["PrivateIpAddress"] == S["node_private_ip"]
    subnets = ec2.describe_subnets(SubnetIds=[S["subnet"], node["SubnetId"]])["Subnets"]
    assert all(subnet["VpcId"] == VPC for subnet in subnets)
    associations = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [S["subnet"]]}])["RouteTables"]
    assert len(associations) == 1 and associations[0]["RouteTableId"] == S["route_table"]
    network["subnets"] = subnets
    network["runtime_network_configuration"] = runtime_config["networkConfiguration"]
    network["node_private_ip"] = S["node_private_ip"]
    network["security_groups"] = ec2.describe_security_groups(GroupIds=[
        S["runtime_sg"], S["node_sg"], S["endpoint_sg"], S["platform_endpoint_sg"]])["SecurityGroups"]
    secret = json.loads(client("secretsmanager").get_secret_value(SecretId=S["secret_arn"])["SecretString"])
    cert = x509.load_pem_x509_certificate(secret["client_cert"].encode())
    report = {"region": REGION, "runtime_arn": S["runtime_arn"], "network": network,
              "certificate": {"subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
                  "not_before": cert.not_valid_before_utc.isoformat(), "not_after": cert.not_valid_after_utc.isoformat()},
              "checks": []}
    cases = [("valid", "nodes"), ("valid", "proof"), ("no_client_cert", "nodes"),
             ("untrusted_client_cert", "nodes"), ("wrong_server_ca", "nodes"),
             ("wrong_server_name", "nodes"), ("valid", "forbidden_secrets"), ("valid", "forbidden_namespace"),
             *[("valid", "can_" + verb) for verb in ["create", "update", "patch", "delete"]]]
    for mode, operation in cases:
        result = call_runtime({"action": "verify", "mode": mode, "operation": operation})
        body = result["body"]
        rc = body.get("returncode")
        error = body.get("stderr", "").lower()
        if mode == "valid" and operation in {"nodes", "proof"}:
            passed = rc == 0
            if passed and operation == "nodes":
                nodes = json.loads(body["stdout"])["items"]
                passed = bool(nodes) and all(any(c["type"] == "Ready" and c["status"] == "True"
                    for c in n["status"]["conditions"]) for n in nodes)
            if passed and operation == "proof":
                passed = json.loads(body["stdout"])["data"]["expected_user"] == "agentcore-mtls-reader"
        elif operation.startswith("forbidden_"):
            passed = rc == 1 and "forbidden" in error and "agentcore-mtls-reader" in error
        elif operation.startswith("can_"):
            passed = rc == 1 and body.get("stdout", "").strip() == "no"
        elif mode == "no_client_cert":
            probe = body.get("no_certificate_https_probe", {})
            passed = probe.get("http_status") == 401 and "Unauthorized" in probe.get("body", "")
        elif mode == "wrong_server_ca":
            passed = rc == 1 and "certificate signed by unknown authority" in error
        elif mode == "wrong_server_name":
            passed = rc == 1 and "certificate is valid for" in error and "not-the-k3s-server.invalid" in error
        else:
            passed = (rc == 1 and any(x in error for x in ["credentials", "unauthorized", "remote error: tls: bad certificate"])
                      and "x509: certificate signed by unknown authority" not in error
                      and "timeout" not in error and "connection refused" not in error)
        result["passed"] = passed
        report["checks"].append(result)
        (STATE / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        print(f"{mode}/{operation}: {'PASS' if passed else 'FAIL'} rc={rc}", flush=True)
        if not passed:
            raise AssertionError(json.dumps(body, ensure_ascii=False))
    result = call_runtime({"prompt": "Use kubectl_read to inspect nodes and proof. Summarize the actual results."})
    calls = result["body"].get("tool_calls", [])
    result["passed"] = {c["operation"] for c in calls} >= {"nodes", "proof"} and all(c["returncode"] == 0 for c in calls)
    report["checks"].append(result)
    report["all_passed"] = all(c["passed"] for c in report["checks"])
    (STATE / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    assert report["all_passed"], result
    print("Strands model-driven tool calls: PASS", flush=True)
    print(result["body"]["answer"], flush=True)

def cleanup():
    if not args.confirm:
        raise SystemExit("Destructive: rerun cleanup --confirm to delete only resources recorded in .state/resources.json")
    if client("sts").get_caller_identity()["Account"] != S["account"]:
        raise RuntimeError("Wrong AWS account")
    ec2, iam, control = client("ec2"), client("iam"), client("bedrock-agentcore-control")
    done = set(S.get("cleanup_done", []))

    def step(key, fn):
        if key in S and key not in done:
            fn()
            done.add(key)
            save("cleanup_done", sorted(done))

    def delete_runtime():
        try:
            control.delete_agent_runtime(agentRuntimeId=S["runtime_id"])
        except control.exceptions.ResourceNotFoundException:
            return
        for _ in range(120):
            try:
                control.get_agent_runtime(agentRuntimeId=S["runtime_id"])
            except control.exceptions.ResourceNotFoundException:
                return
            time.sleep(10)
        raise TimeoutError("Runtime deletion not finished; rerun cleanup later")

    step("runtime_id", delete_runtime)

    def terminate():
        ec2.terminate_instances(InstanceIds=[S["instance_id"]])
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[S["instance_id"]])

    step("instance_id", terminate)
    for key in [k for k in S if k.startswith("vpce_")]:
        def delete_endpoint(key=key):
            response = ec2.delete_vpc_endpoints(VpcEndpointIds=[S[key]])
            if response.get("Unsuccessful"):
                raise RuntimeError(response["Unsuccessful"])
        step(key, delete_endpoint)
    step("secret_arn", lambda: client("secretsmanager").delete_secret(
        SecretId=S["secret_arn"], RecoveryWindowInDays=7))
    step("repository", lambda: client("ecr").delete_repository(repositoryName=S["repository"]["repositoryName"], force=True))
    step("profile_attached", lambda: iam.remove_role_from_instance_profile(
        InstanceProfileName=S["instance_profile"], RoleName=S["node_role"]))
    step("instance_profile", lambda: iam.delete_instance_profile(InstanceProfileName=S["instance_profile"]))

    def delete_role(name):
        for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=name)

    step("node_role", lambda: delete_role(S["node_role"]))
    step("runtime_role", lambda: delete_role(S["runtime_role"]["RoleName"]))
    # ENI deletion is asynchronous. Do not delete subnet/SGs until detached.
    if "subnet" in S and "subnet" not in done:
        for _ in range(120):
            enis = ec2.describe_network_interfaces(Filters=[{"Name": "subnet-id", "Values": [S["subnet"]]}])["NetworkInterfaces"]
            if not enis:
                break
            time.sleep(10)
        else:
            raise TimeoutError("Lab subnet still has ENIs; rerun cleanup later")
    step("route_association", lambda: ec2.disassociate_route_table(AssociationId=S["route_association"]))
    step("subnet", lambda: ec2.delete_subnet(SubnetId=S["subnet"]))
    step("route_table", lambda: ec2.delete_route_table(RouteTableId=S["route_table"]))
    # Remove only OUR SG references, then delete groups without circular dependencies.
    groups = [S[k] for k in ["node_sg", "runtime_sg", "endpoint_sg", "platform_endpoint_sg"] if k in S and k not in done]
    for group in groups:
        data = ec2.describe_security_groups(GroupIds=[group])["SecurityGroups"][0]
        for key, fn in [("IpPermissions", ec2.revoke_security_group_ingress),
                        ("IpPermissionsEgress", ec2.revoke_security_group_egress)]:
            if data[key]:
                fn(GroupId=group, IpPermissions=data[key])
    for key in ["node_sg", "runtime_sg", "endpoint_sg", "platform_endpoint_sg"]:
        step(key, lambda key=key: ec2.delete_security_group(GroupId=S[key]))
    logs = client("logs")
    if "runtime_id" in S:
        prefix = "/aws/bedrock-agentcore/runtimes/" + S["runtime_id"]
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for group in page["logGroups"]:
                logs.delete_log_group(logGroupName=group["logGroupName"])
    print("Lab resources removed; secret has a 7-day recovery window. Local evidence/images/venv retained.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["infra", "bootstrap", "runtime", "verify", "invoke", "cleanup"])
    parser.add_argument("--confirm", action="store_true", help="Confirm deletion of ONLY this lab's recorded resources")
    args = parser.parse_args()
    if args.action != "cleanup":
        account = client("sts").get_caller_identity()["Account"]
        if account != "434444145045" or (S.get("account") and S["account"] != account):
            raise SystemExit("Wrong AWS account: this lab is pinned to 434444145045")
        if S.get("cleanup_done"):
            raise SystemExit("Lab cleanup already started; do not reuse this state for deployment")
    globals()[args.action]()
