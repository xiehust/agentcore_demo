"""CloudFormation template for an isolated, disposable JuiceFS benchmark lab."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ref(name):
    return {"Ref": name}


def attr(name, field="Arn"):
    return {"Fn::GetAtt": [name, field]}


def sub(value):
    return {"Fn::Sub": value}


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def build_template() -> dict:
    resources = {}
    resources["Vpc"] = {"Type": "AWS::EC2::VPC", "Properties": {
        "CidrBlock": "10.87.0.0/16", "EnableDnsSupport": True, "EnableDnsHostnames": True}}
    resources["Subnet"] = {"Type": "AWS::EC2::Subnet", "Properties": {
        "VpcId": ref("Vpc"), "CidrBlock": "10.87.1.0/24",
        "AvailabilityZone": {"Fn::Select": [0, {"Fn::GetAZs": ""}]}}}
    resources["InternetGateway"] = {"Type": "AWS::EC2::InternetGateway"}
    resources["AttachGateway"] = {"Type": "AWS::EC2::VPCGatewayAttachment", "Properties": {
        "VpcId": ref("Vpc"), "InternetGatewayId": ref("InternetGateway")}}
    resources["RouteTable"] = {"Type": "AWS::EC2::RouteTable", "Properties": {"VpcId": ref("Vpc")}}
    resources["SubnetRoute"] = {"Type": "AWS::EC2::SubnetRouteTableAssociation", "Properties": {
        "SubnetId": ref("Subnet"), "RouteTableId": ref("RouteTable")}}
    resources["InternetRoute"] = {"Type": "AWS::EC2::Route", "DependsOn": "AttachGateway", "Properties": {
        "RouteTableId": ref("RouteTable"), "DestinationCidrBlock": "0.0.0.0/0",
        "GatewayId": ref("InternetGateway")}}
    resources["RuntimeSG"] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {
        "GroupDescription": "Runtime clients, no inbound", "VpcId": ref("Vpc")}}
    resources["GatewaySG"] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {
        "GroupDescription": "JuiceFS TLS only from benchmark runtimes", "VpcId": ref("Vpc"),
        "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 9000, "ToPort": 9000,
                                  "SourceSecurityGroupId": ref("RuntimeSG")}]}}
    resources["EndpointSG"] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {
        "GroupDescription": "AWS endpoints from runtimes and gateway", "VpcId": ref("Vpc"),
        "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                                  "SourceSecurityGroupId": ref(group)}
                                 for group in ("RuntimeSG", "GatewaySG")]}}
    for logical, service in [("EcrApi", "ecr.api"), ("EcrDocker", "ecr.dkr"),
                             ("Logs", "logs"), ("Secrets", "secretsmanager")]:
        resources[logical + "Endpoint"] = {"Type": "AWS::EC2::VPCEndpoint", "Properties": {
            "VpcId": ref("Vpc"), "ServiceName": sub("com.amazonaws.${AWS::Region}." + service),
            "VpcEndpointType": "Interface", "SubnetIds": [ref("Subnet")],
            "SecurityGroupIds": [ref("EndpointSG")], "PrivateDnsEnabled": True}}
    resources["S3Endpoint"] = {"Type": "AWS::EC2::VPCEndpoint", "Properties": {
        "VpcId": ref("Vpc"), "ServiceName": sub("com.amazonaws.${AWS::Region}.s3"),
        "VpcEndpointType": "Gateway", "RouteTableIds": [ref("RouteTable")]}}
    resources["DataBucket"] = {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain", "Properties": {
            "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                                "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
            "BucketEncryption": {"ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}}
    resources["BucketPolicy"] = {"Type": "AWS::S3::BucketPolicy", "Properties": {
        "Bucket": ref("DataBucket"), "PolicyDocument": policy([{
            "Effect": "Deny", "Principal": "*", "Action": "s3:*",
            "Resource": [attr("DataBucket", "Arn"), sub("${DataBucket.Arn}/*")],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}}}])}}
    resources["Repository"] = {"Type": "AWS::ECR::Repository", "DeletionPolicy": "Retain",
                               "UpdateReplacePolicy": "Retain"}
    for name in ("AdminSecret", "TenantASecret", "TenantBSecret"):
        resources[name] = {"Type": "AWS::SecretsManager::Secret", "DeletionPolicy": "Retain",
                           "UpdateReplacePolicy": "Retain", "Properties": {
                               "Description": "Disposable JuiceFS benchmark credentials; populated by controller"}}
    gateway_statements = [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": sub("${DataBucket.Arn}/shared-demo/*")},
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": attr("DataBucket", "Arn"),
         "Condition": {"StringLike": {"s3:prefix": ["shared-demo/*"]}}},
        {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": attr("DataBucket", "Arn")},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
         "Resource": [ref(name) for name in ("AdminSecret", "TenantASecret", "TenantBSecret")]},
        {"Effect": "Allow", "Action": ["secretsmanager:PutSecretValue"],
         "Resource": [ref("TenantASecret"), ref("TenantBSecret")]},
    ]
    resources["GatewayRole"] = {"Type": "AWS::IAM::Role", "Properties": {
        "AssumeRolePolicyDocument": policy([{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                              "Action": "sts:AssumeRole"}]),
        "ManagedPolicyArns": [sub("arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore")],
        "Policies": [{"PolicyName": "gateway-data", "PolicyDocument": policy(gateway_statements)}]}}
    resources["InstanceProfile"] = {"Type": "AWS::IAM::InstanceProfile", "Properties": {
        "Roles": [ref("GatewayRole")]}}
    # One shared runtime execution role: deliberately NO tenant data, secret, STS or invocation access.
    resources["RuntimeRole"] = {"Type": "AWS::IAM::Role", "Properties": {
        "AssumeRolePolicyDocument": policy([{"Effect": "Allow", "Action": "sts:AssumeRole",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Condition": {"StringEquals": {"aws:SourceAccount": ref("AWS::AccountId")},
                "ArnLike": {"aws:SourceArn": sub("arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:*")}}}]),
        "Policies": [{"PolicyName": "runtime-infrastructure-only", "PolicyDocument": policy([
            {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": attr("Repository")},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                            "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
             "Resource": sub("arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:*")},
        ])}]}}
    for suffix, tenant in [("A", "tenant-a"), ("B", "tenant-b")]:
        # Data access roles are assumed ONLY by the trusted caller, not by the runtime.
        resources["TenantDataRole" + suffix] = {"Type": "AWS::IAM::Role", "Properties": {
            "AssumeRolePolicyDocument": policy([{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                 "Principal": {"AWS": ref("ControllerArn")}}]),
            "Policies": [{"PolicyName": "tenant-s3-data", "PolicyDocument": policy([
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                 "Resource": sub("${DataBucket.Arn}/direct/" + tenant + "/*")},
                {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": attr("DataBucket", "Arn"),
                 "Condition": {"StringLike": {"s3:prefix": ["direct/" + tenant + "/*"]}}},
            ])}]}}
    resources["GatewayInstance"] = {"Type": "AWS::EC2::Instance", "DependsOn": "InternetRoute", "Properties": {
        "ImageId": ref("AmiId"), "InstanceType": "m7g.large", "IamInstanceProfile": ref("InstanceProfile"),
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        "NetworkInterfaces": [{"DeviceIndex": "0", "AssociatePublicIpAddress": True,
                               "SubnetId": ref("Subnet"), "GroupSet": [ref("GatewaySG")]}],
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": 30, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        "Tags": [{"Key": "Name", "Value": sub("${AWS::StackName}-gateway")}],
    }}
    outputs = {name: {"Value": value} for name, value in {
        "VpcId": ref("Vpc"), "SubnetId": ref("Subnet"), "RuntimeSecurityGroup": ref("RuntimeSG"),
        "GatewayInstanceId": ref("GatewayInstance"), "GatewayPrivateIp": attr("GatewayInstance", "PrivateIp"),
        "DataBucket": ref("DataBucket"), "RepositoryUri": attr("Repository", "RepositoryUri"),
        "AdminSecretArn": ref("AdminSecret"), "TenantASecretArn": ref("TenantASecret"),
        "TenantBSecretArn": ref("TenantBSecret"), "RuntimeRole": attr("RuntimeRole"),
        "TenantDataRoleA": attr("TenantDataRoleA"), "TenantDataRoleB": attr("TenantDataRoleB"),
    }.items()}
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Disposable JuiceFS shared-volume vs native S3 benchmark; private TLS ingress, no NAT",
            "Parameters": {"AmiId": {"Type": "AWS::EC2::Image::Id"},
                           "ControllerArn": {"Type": "String", "Description": "Trusted caller IAM role/user ARN (not STS session ARN)"}},
            "Resources": resources, "Outputs": outputs}
