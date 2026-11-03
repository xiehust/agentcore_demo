"""Patch targets: 5 x t4g.micro Amazon Linux 2023 across the two accounts, each tagged with the fault to inject.

t4g.micro (1 GiB) instead of t4g.nano: dnf metadata processing on AL2023 can OOM at 512 MiB, which would add
an unplanned failure mode to the experiment.
Targets live in each account's default VPC and reach SSM over the internet -- the business VPCs are never
peered with the ops VPC; the agent only talks to SSM/EC2 APIs.
"""
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import PATCH_GROUP, PREFIX, SPOKE_PROFILES, TAGS, TARGET_ROLE, load_state, log, save_state, session  # noqa: E402

ACCTS = list(SPOKE_PROFILES)
TARGETS = [  # (account, name, fault, wave)
    (ACCTS[0], "pd-healthy", "none", "canary"),
    (ACCTS[0], "pd-disk-full", "disk_full", "main"),
    (ACCTS[1], "pd-repo-broken", "repo_broken", "main"),
    (ACCTS[1], "pd-rpm-lock", "rpm_lock", "main"),
    (ACCTS[1], "pd-ssm-offline", "ssm_offline", "main"),
]


def main():
    st = load_state()
    out = st.get("targets", {})
    for acct, profile in SPOKE_PROFILES.items():
        s = session(profile)
        ec2, ssm = s.client("ec2"), s.client("ssm")
        ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
        vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
        subnet = sorted(ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "default-for-az", "Values": ["true"]}])
                        ["Subnets"], key=lambda x: x["AvailabilityZone"])[0]["SubnetId"]
        sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-target-sg"]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
        sg = sgs[0]["GroupId"] if sgs else ec2.create_security_group(
            GroupName=f"{PREFIX}-target-sg", Description="patch targets: no ingress", VpcId=vpc)["GroupId"]
        for t_acct, name, fault, wave in TARGETS:
            if t_acct != acct or name in out:
                continue
            for attempt in range(10):
                try:
                    inst = ec2.run_instances(
                        ImageId=ami, InstanceType="t4g.micro", MinCount=1, MaxCount=1,
                        IamInstanceProfile={"Name": TARGET_ROLE},
                        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg], "AssociatePublicIpAddress": True}],
                        MetadataOptions={"HttpTokens": "required"},
                        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                        TagSpecifications=[{"ResourceType": "instance", "Tags": TAGS + [
                            {"Key": "Name", "Value": name}, {"Key": "PatchGroup", "Value": PATCH_GROUP},
                            {"Key": "PatchDemoFault", "Value": fault}, {"Key": "PatchWave", "Value": wave}]}])["Instances"][0]
                    break
                except ec2.exceptions.ClientError as e:
                    if "Invalid IAM Instance Profile" not in str(e) or attempt == 9:
                        raise
                    time.sleep(6)
            out[name] = {"account": acct, "instance_id": inst["InstanceId"], "fault": fault, "wave": wave}
            log(acct, name, inst["InstanceId"], fault)
            save_state(targets=out, **{f"target_sg_{acct}": sg})

    log("waiting for SSM registration...")
    for _ in range(60):
        online = 0
        for acct, profile in SPOKE_PROFILES.items():
            ids = [t["instance_id"] for t in out.values() if t["account"] == acct]
            info = session(profile).client("ssm").describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": ids}])["InstanceInformationList"]
            online += sum(1 for i in info if i["PingStatus"] == "Online")
        if online == len(out):
            break
        time.sleep(10)
    log(f"SSM online {online}/{len(out)}")


if __name__ == "__main__":
    main()
