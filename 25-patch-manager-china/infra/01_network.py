"""Ops VPC in the hub account: 2 private subnets for Runtime/Lambda ENIs, NAT for model egress, VPC endpoints.

China-region facts that shape this layout (verified 2026-11-02):
  * Bedrock has no endpoint in cn-* -> the model is reached over NAT (global Bedrock us-west-2).
  * STS / EC2 endpoint service names use the "cn.com.amazonaws" prefix, others "com.amazonaws".
"""
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import PREFIX, TAGS, log, save_state, load_state, session  # noqa: E402

CIDR = "10.40.0.0/16"
AZ_IDS = ["cnnw1-az1", "cnnw1-az2"]
INTERFACE_SVCS = {
    "bedrock-agentcore": "com.amazonaws.cn-northwest-1.bedrock-agentcore",
    "sts": "cn.com.amazonaws.cn-northwest-1.sts",
    "ssm": "com.amazonaws.cn-northwest-1.ssm",
    "ec2": "cn.com.amazonaws.cn-northwest-1.ec2",
    "logs": "com.amazonaws.cn-northwest-1.logs",
}
GATEWAY_SVCS = {"dynamodb": "com.amazonaws.cn-northwest-1.dynamodb", "s3": "com.amazonaws.cn-northwest-1.s3"}


def tags(name, extra=()):
    return [{"ResourceType": extra, "Tags": TAGS + [{"Key": "Name", "Value": name}]}]


def main():
    ec2 = session().client("ec2")
    st = load_state()
    if st.get("vpc_id"):
        log("network already exists", st["vpc_id"])
        return
    az_by_id = {z["ZoneId"]: z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"]}

    vpc = ec2.create_vpc(CidrBlock=CIDR, TagSpecifications=tags(f"{PREFIX}-ops-vpc", "vpc"))["Vpc"]["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc])
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
    save_state(vpc_id=vpc)
    log("vpc", vpc)

    igw = ec2.create_internet_gateway(TagSpecifications=tags(f"{PREFIX}-igw", "internet-gateway"))["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)

    pub = ec2.create_subnet(VpcId=vpc, CidrBlock="10.40.0.0/24", AvailabilityZone=az_by_id[AZ_IDS[0]],
                            TagSpecifications=tags(f"{PREFIX}-public-a", "subnet"))["Subnet"]["SubnetId"]
    privs = []
    for i, azid in enumerate(AZ_IDS):
        privs.append(ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.40.{11 + i}.0/24", AvailabilityZone=az_by_id[azid],
                                       TagSpecifications=tags(f"{PREFIX}-private-{azid}", "subnet"))["Subnet"]["SubnetId"])

    pub_rt = ec2.create_route_table(VpcId=vpc, TagSpecifications=tags(f"{PREFIX}-public-rt", "route-table"))["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=pub_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    ec2.associate_route_table(RouteTableId=pub_rt, SubnetId=pub)

    eip = ec2.allocate_address(Domain="vpc", TagSpecifications=tags(f"{PREFIX}-nat-eip", "elastic-ip"))["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=pub, AllocationId=eip, TagSpecifications=tags(f"{PREFIX}-nat", "natgateway"))["NatGateway"]["NatGatewayId"]
    save_state(igw_id=igw, public_subnet=pub, private_subnets=privs, public_rt=pub_rt, eip_alloc=eip, nat_id=nat)
    log("waiting for NAT", nat)
    ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat])

    priv_rt = ec2.create_route_table(VpcId=vpc, TagSpecifications=tags(f"{PREFIX}-private-rt", "route-table"))["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=priv_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
    for s in privs:
        ec2.associate_route_table(RouteTableId=priv_rt, SubnetId=s)

    # Agent / Lambda SG: no ingress; egress 443 only (VPCEs inside the VPC + model over NAT).
    agent_sg = ec2.create_security_group(GroupName=f"{PREFIX}-agent-sg", Description="patch agent runtime and orchestrator ENIs",
                                         VpcId=vpc, TagSpecifications=tags(f"{PREFIX}-agent-sg", "security-group"))["GroupId"]
    ec2.revoke_security_group_egress(GroupId=agent_sg, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
    ec2.authorize_security_group_egress(GroupId=agent_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "VPCE + model over NAT"}]}])
    vpce_sg = ec2.create_security_group(GroupName=f"{PREFIX}-vpce-sg", Description="interface endpoints",
                                        VpcId=vpc, TagSpecifications=tags(f"{PREFIX}-vpce-sg", "security-group"))["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=vpce_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": CIDR}]}])
    save_state(private_rt=priv_rt, agent_sg=agent_sg, vpce_sg=vpce_sg)

    vpces = {}
    for key, svc in INTERFACE_SVCS.items():
        vpces[key] = ec2.create_vpc_endpoint(
            VpcEndpointType="Interface", VpcId=vpc, ServiceName=svc, SubnetIds=privs, SecurityGroupIds=[vpce_sg],
            PrivateDnsEnabled=True, TagSpecifications=tags(f"{PREFIX}-{key}", "vpc-endpoint"))["VpcEndpoint"]["VpcEndpointId"]
        log("vpce", key, vpces[key])
    for key, svc in GATEWAY_SVCS.items():
        vpces[key] = ec2.create_vpc_endpoint(
            VpcEndpointType="Gateway", VpcId=vpc, ServiceName=svc, RouteTableIds=[priv_rt],
            TagSpecifications=tags(f"{PREFIX}-{key}", "vpc-endpoint"))["VpcEndpoint"]["VpcEndpointId"]
        log("vpce", key, vpces[key])
    save_state(vpce=vpces)

    for _ in range(60):
        eps = ec2.describe_vpc_endpoints(VpcEndpointIds=list(vpces.values()))["VpcEndpoints"]
        pending = [e["ServiceName"] for e in eps if e["State"] != "available"]
        if not pending:
            break
        time.sleep(10)
    log("endpoints available; pending:", pending)


if __name__ == "__main__":
    main()
