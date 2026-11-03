"""Long-term memory store: Amazon OpenSearch Service domain, VPC-only, IAM (SigV4) access.

China regions have no OpenSearch Serverless (aoss endpoint does not resolve in cn-northwest-1), so this is a
managed domain in the ops VPC private subnets:
  - no public endpoint (VPC access), SG ingress 443 only from the agent/Lambda SG
  - resource policy: only PatchAgentRuntimeRole may call es:ESHttp* (the runtime owns all LTM reads/writes)
  - encryption at rest, node-to-node TLS, HTTPS-only TLS 1.2+
Vectors come from Titan Text Embeddings V2 in global Bedrock (same API key as the chat model, over NAT).
"""
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import PREFIX, RUNTIME_ROLE, TAGS, arn, load_state, log, role_arn, save_state, session  # noqa: E402

DOMAIN = f"{PREFIX}-ltm"


def main():
    st = load_state()
    ec2, aos = session().client("ec2"), session().client("opensearch")
    sg = st.get("os_sg")
    if not sg:
        sg = ec2.create_security_group(
            GroupName=f"{PREFIX}-opensearch-sg", Description="LTM OpenSearch domain: 443 from agent SG only", VpcId=st["vpc_id"],
            TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS + [{"Key": "Name", "Value": f"{PREFIX}-opensearch-sg"}]}])["GroupId"]
        ec2.revoke_security_group_egress(GroupId=sg, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "UserIdGroupPairs": [{"GroupId": st["agent_sg"], "Description": "agent runtime + orchestrator ENIs"}]}])
        save_state(os_sg=sg)
    domain_arn = arn("es", f"domain/{DOMAIN}")
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": role_arn(RUNTIME_ROLE)},
        "Action": ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete"],
        "Resource": f"{domain_arn}/*"}]}
    cfg = dict(
        EngineVersion="OpenSearch_2.19",
        ClusterConfig={"InstanceType": "m7g.medium.search", "InstanceCount": 1, "ZoneAwarenessEnabled": False},
        EBSOptions={"EBSEnabled": True, "VolumeType": "gp3", "VolumeSize": 10},
        VPCOptions={"SubnetIds": st["private_subnets"][:1], "SecurityGroupIds": [sg]},
        AccessPolicies=json.dumps(policy),
        EncryptionAtRestOptions={"Enabled": True},
        NodeToNodeEncryptionOptions={"Enabled": True},
        DomainEndpointOptions={"EnforceHTTPS": True, "TLSSecurityPolicy": "Policy-Min-TLS-1-2-2019-07"},
    )
    try:
        aos.create_domain(DomainName=DOMAIN, TagList=TAGS, **cfg)
        log("creating domain", DOMAIN, "(~15-20 min)")
    except aos.exceptions.ResourceAlreadyExistsException:
        aos.update_domain_config(DomainName=DOMAIN, AccessPolicies=cfg["AccessPolicies"])
        log("domain exists, access policy updated")
    while True:
        d = aos.describe_domain(DomainName=DOMAIN)["DomainStatus"]
        ep = d.get("Endpoints", {}).get("vpc")
        if ep and not d.get("Processing"):
            break
        time.sleep(30)
    save_state(os_domain=DOMAIN, os_endpoint=ep, os_domain_arn=domain_arn)
    log("domain ready:", ep)


if __name__ == "__main__":
    main()
