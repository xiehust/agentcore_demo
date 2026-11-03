"""Tear down everything the Patch Manager x AgentCore China demo created (ops account, spoke accounts, global account).

Default is a DRY RUN: every resource that would be deleted is printed after a read-only existence check.
Pass --yes to actually delete. Idempotent (NotFound == already deleted), continues on errors, prints a summary.

Scope is strictly: IDs from .state.json, exact names from common/config.py, and (for log groups) the two
documented prefixes. Nothing else is touched (launchpad_* runtimes, launchpad-global-bedrock, acdemo-noegress-vpc,
unrelated EC2 instances and the default VPC are never referenced). This script never writes .state.json.
"""
import argparse
import sys
import time

from botocore.exceptions import ClientError

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import (API_KEY_PROVIDER, BATCH_TABLE, DIAG_DOC, ECR_REPO, GLOBAL_PROFILE, LAMBDA_NAME,  # noqa: E402
                           MEMORY_TABLE, MODEL_REGION, ONCALL_ROLE, OS_DOMAIN, OPS_ACCOUNT, OPS_PROFILE, ORCH_ROLE, PREFIX,
                           REMEDIATE_DOCS, RUNTIME_ROLE, SFN_ROLE, SPOKE_PROFILES, SPOKE_ROLE, TARGET_ROLE,
                           load_state, session)

NOT_FOUND = ("NotFound", "NoSuchEntity", "ResourceNotFoundException", "ResourceNotFound", "RepositoryNotFoundException",
             "StateMachineDoesNotExist", "InvalidInstanceID.NotFound", "InvalidDocument", "DoesNotExist",
             "InvalidVpcID.NotFound", "InvalidSubnetID.NotFound", "InvalidGroup.NotFound", "InvalidRouteTableID.NotFound",
             "InvalidInternetGatewayID.NotFound", "InvalidAllocationID.NotFound", "InvalidVpcEndpointId.NotFound",
             "NatGatewayNotFound", "InvalidAssociationID.NotFound", "InvalidParameterValue.NotFound")
RETRY_CODES = ("DependencyViolation", "InvalidGroup.InUse", "ResourceInUse", "InvalidNetworkInterface.InUse",
               "IncorrectState", "InvalidParameterValue", "ConflictException", "ResourceInUseException",
               "InvalidIPAddress.InUse", "AuthFailure")
RETRY_SECONDS = 25 * 60

DRY = True
SUMMARY = []  # (status, what)


def code_of(e):
    return e.response.get("Error", {}).get("Code", "")


def is_nf(e):
    c = code_of(e)
    return c in NOT_FOUND or c.endswith("NotFound") or c.endswith("NotFoundException")


def say(msg):
    print(msg, flush=True)


def record(status, what, extra=""):
    SUMMARY.append((status, what, extra))
    say(f"  [{status}] {what} {extra}".rstrip())


def retrying(fn, what, seconds=RETRY_SECONDS):
    """Run fn() with backoff while the failure looks like 'something still depends on it' (ENIs release slowly)."""
    deadline, delay = time.time() + seconds, 15
    while True:
        try:
            return fn()
        except ClientError as e:
            if is_nf(e):
                raise
            if code_of(e) in RETRY_CODES and time.time() < deadline:
                say(f"    ... {what}: {code_of(e)}; retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay + 15, 60)
                continue
            raise


def step(what, exists, delete, wait=None):
    """exists() -> bool|None (read-only). delete() performs the deletion. wait() optional."""
    try:
        ex = exists()
    except ClientError as e:
        if is_nf(e):
            ex = False
        else:
            record("ERROR", what, f"(existence check: {code_of(e)} {e.response['Error'].get('Message', '')[:120]})")
            return False
    except Exception as e:  # noqa: BLE001
        record("ERROR", what, f"(existence check: {type(e).__name__} {str(e)[:120]})")
        return False
    if not ex:
        record("ABSENT", what, "(already deleted / never created)")
        return True
    if DRY:
        record("WOULD DELETE", what)
        return True
    try:
        delete()
        if wait:
            wait()
        record("DELETED", what)
        return True
    except ClientError as e:
        if is_nf(e):
            record("ABSENT", what, "(gone during delete)")
            return True
        record("ERROR", what, f"({code_of(e)}: {e.response['Error'].get('Message', '')[:160]})")
    except Exception as e:  # noqa: BLE001
        record("ERROR", what, f"({type(e).__name__}: {str(e)[:160]})")
    return False


def poll_gone(check, what, seconds=RETRY_SECONDS, every=15):
    """check() -> True while resource still exists."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if not check():
                return
        except ClientError as e:
            if is_nf(e):
                return
            raise
        say(f"    ... waiting for {what} to disappear")
        time.sleep(every)
    raise TimeoutError(f"{what} still present after {seconds}s")


# ---------------------------------------------------------------- IAM helpers
def iam_role_exists(iam, name):
    try:
        iam.get_role(RoleName=name)
        return True
    except ClientError as e:
        if is_nf(e):
            return False
        raise


def delete_role(iam, name):
    for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=p)
    for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    for ip in iam.list_instance_profiles_for_role(RoleName=name)["InstanceProfiles"]:
        iam.remove_role_from_instance_profile(InstanceProfileName=ip["InstanceProfileName"], RoleName=name)
    iam.delete_role(RoleName=name)


def role_step(iam, name, tag):
    def desc():
        try:
            inl = iam.list_role_policies(RoleName=name)["PolicyNames"]
            att = [p["PolicyName"] for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]]
            return f"inline={inl} managed={att}"
        except ClientError:
            return ""
    ex = None
    try:
        ex = iam_role_exists(iam, name)
    except ClientError:
        ex = None
    step(f"{tag} IAM role {name} {desc() if ex else ''}".strip(), lambda: iam_role_exists(iam, name),
         lambda: delete_role(iam, name))


# ---------------------------------------------------------------- ops account pieces
def sfn_cleanup(st, sfn):
    arn = st.get("sfn_arn")
    if not arn:
        return

    def exists():
        sfn.describe_state_machine(stateMachineArn=arn)
        return True

    def running():
        return [e["executionArn"] for p in sfn.get_paginator("list_executions").paginate(stateMachineArn=arn, statusFilter="RUNNING")
                for e in p["executions"]]

    try:
        if exists() and running():
            for x in running():
                if DRY:
                    record("WOULD STOP", f"[ops] SFN execution {x.split(':')[-1]}")
                else:
                    try:
                        sfn.stop_execution(executionArn=x, cause="demo cleanup")
                        record("STOPPED", f"[ops] SFN execution {x.split(':')[-1]}")
                    except ClientError as e:
                        record("ERROR", f"[ops] stop execution {x}", code_of(e))
    except ClientError as e:
        if not is_nf(e):
            record("ERROR", "[ops] list SFN executions", code_of(e))

    def wait_stopped():
        poll_gone(lambda: bool(running()), "running executions", 120, 5)

    step(f"[ops] Step Functions state machine {arn.split(':')[-1]}", exists,
         lambda: (wait_stopped(), sfn.delete_state_machine(stateMachineArn=arn)))


def lambda_cleanup(lam):
    def exists():
        lam.get_function(FunctionName=LAMBDA_NAME)
        return True
    step(f"[ops] Lambda {LAMBDA_NAME}", exists, lambda: lam.delete_function(FunctionName=LAMBDA_NAME))


def runtime_cleanup(st, ac, wait):
    rid = st.get("runtime_id")
    if not rid:
        return

    def exists():
        ac.get_agent_runtime(agentRuntimeId=rid)
        return True

    def endpoints():
        return [e["name"] for p in ac.get_paginator("list_agent_runtime_endpoints").paginate(agentRuntimeId=rid)
                for e in p.get("runtimeEndpoints", []) if e["name"] != "DEFAULT"]

    def delete():
        for en in endpoints():  # custom endpoints must go first (DEFAULT goes with the runtime)
            try:
                ac.delete_agent_runtime_endpoint(agentRuntimeId=rid, endpointName=en)
            except ClientError as e:
                if not is_nf(e):
                    raise
        retrying(lambda: ac.delete_agent_runtime(agentRuntimeId=rid), "delete_agent_runtime", 300)

    def gone():
        poll_gone(lambda: bool(exists()), f"runtime {rid}", RETRY_SECONDS, 20)

    step(f"[ops] AgentCore runtime {rid}", exists, delete, gone)

    wi = st.get("workload_identity", "").rsplit("/", 1)[-1]
    if wi:  # normally removed with the runtime; clean up if it lingers
        def wi_exists():
            ac.get_workload_identity(name=wi)
            return True
        step(f"[ops] AgentCore workload identity {wi} (runtime-owned)", wi_exists, lambda: ac.delete_workload_identity(name=wi))


def apikey_cleanup(ac):
    def exists():
        ac.get_api_key_credential_provider(name=API_KEY_PROVIDER)
        return True
    step(f"[ops] AgentCore Identity API key provider {API_KEY_PROVIDER}", exists,
         lambda: ac.delete_api_key_credential_provider(name=API_KEY_PROVIDER))


def ecr_cleanup(ecr):
    def exists():
        ecr.describe_repositories(repositoryNames=[ECR_REPO])
        return True
    step(f"[ops] ECR repo {ECR_REPO} (force)", exists, lambda: ecr.delete_repository(repositoryName=ECR_REPO, force=True))


def ddb_cleanup(ddb):
    for t in (BATCH_TABLE, MEMORY_TABLE):
        def exists(t=t):
            ddb.describe_table(TableName=t)
            return True
        step(f"[ops] DynamoDB table {t}", exists, lambda t=t: ddb.delete_table(TableName=t),
             lambda t=t: ddb.get_waiter("table_not_exists").wait(TableName=t))


def opensearch_cleanup(st, aos):
    """LTM domain (VPC ENIs must be gone before the subnets / SGs can be deleted)."""
    name = st.get("os_domain") or OS_DOMAIN

    def exists():
        d = aos.describe_domain(DomainName=name)["DomainStatus"]
        if d.get("Deleted"):
            return False
        if d.get("VPCOptions", {}).get("VPCId") not in (None, st.get("vpc_id")):
            raise RuntimeError(f"{name} is not in the demo VPC")
        return True

    def gone():
        poll_gone(lambda: _domain_present(aos, name), f"OpenSearch domain {name}", seconds=40 * 60, every=30)
    step(f"[ops] OpenSearch domain {name}", exists, lambda: aos.delete_domain(DomainName=name), gone)


def _domain_present(aos, name):
    try:
        aos.describe_domain(DomainName=name)
        return True
    except ClientError as e:
        if is_nf(e):
            return False
        raise


def logs_cleanup(st, logs):
    prefixes = [f"/aws/bedrock-agentcore/runtimes/{st['runtime_id']}"] if st.get("runtime_id") else []
    names = []
    for pfx in prefixes:
        try:
            for p in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=pfx):
                names += [g["logGroupName"] for g in p["logGroups"] if g["logGroupName"].startswith(pfx)]
        except ClientError as e:
            record("ERROR", f"[ops] list log groups {pfx}*", code_of(e))
    names.append(f"/aws/lambda/{LAMBDA_NAME}")
    for n in names:
        def exists(n=n):
            return any(g["logGroupName"] == n for g in logs.describe_log_groups(logGroupNamePrefix=n)["logGroups"])
        step(f"[ops] CloudWatch log group {n}", exists, lambda n=n: logs.delete_log_group(logGroupName=n))


def network_cleanup(st, ec2):
    vpc = st.get("vpc_id")
    if not vpc:
        return

    # Safety: the VPC must carry our Project tag (default VPC / acdemo-noegress-vpc never match).
    def vpc_ok():
        v = ec2.describe_vpcs(VpcIds=[vpc])["Vpcs"][0]
        tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
        if v.get("IsDefault") or tags.get("Project") != PREFIX:
            raise RuntimeError(f"{vpc} is not a {PREFIX}-tagged non-default VPC (tags={tags}); refusing")
        return True

    try:
        vpc_present = vpc_ok()
    except ClientError as e:
        if not is_nf(e):
            record("ERROR", f"[ops] describe VPC {vpc}", code_of(e))
            return
        vpc_present = False
    except RuntimeError as e:
        record("ERROR", f"[ops] VPC {vpc}", str(e))
        return

    def enis():
        return ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["NetworkInterfaces"]
    if vpc_present:
        try:
            for n in enis():
                say(f"  (info) ENI {n['NetworkInterfaceId']} in VPC: {n.get('Description', '')[:70]} status={n['Status']}")
        except ClientError:
            pass

    # VPC endpoints
    for key, vid in st.get("vpce", {}).items():
        def exists(vid=vid):
            vs = ec2.describe_vpc_endpoints(VpcEndpointIds=[vid])["VpcEndpoints"]
            return bool(vs) and vs[0]["State"].lower() not in ("deleted",)
        step(f"[ops] VPC endpoint {key} {vid}", exists, lambda vid=vid: ec2.delete_vpc_endpoints(VpcEndpointIds=[vid]))
    if not DRY:
        for key, vid in st.get("vpce", {}).items():
            def still(vid=vid):
                vs = ec2.describe_vpc_endpoints(VpcEndpointIds=[vid])["VpcEndpoints"]
                return bool(vs) and vs[0]["State"].lower() != "deleted"
            try:
                poll_gone(still, f"vpce {vid}", 900)
            except Exception as e:  # noqa: BLE001
                record("ERROR", f"wait vpce {vid}", str(e)[:100])

    # NAT gateway then EIP
    nat, eip = st.get("nat_id"), st.get("eip_alloc")
    if nat:
        def nat_exists():
            g = ec2.describe_nat_gateways(NatGatewayIds=[nat])["NatGateways"]
            return bool(g) and g[0]["State"] not in ("deleted",)
        step(f"[ops] NAT gateway {nat}", nat_exists, lambda: ec2.delete_nat_gateway(NatGatewayId=nat),
             lambda: poll_gone(nat_exists, f"NAT {nat}", 900))
    if eip:
        def eip_exists():
            return bool(ec2.describe_addresses(AllocationIds=[eip])["Addresses"])
        step(f"[ops] Elastic IP {eip}", eip_exists,
             lambda: retrying(lambda: ec2.release_address(AllocationId=eip), "release EIP", 300))

    # IGW
    igw = st.get("igw_id")
    if igw:
        def igw_exists():
            return bool(ec2.describe_internet_gateways(InternetGatewayIds=[igw])["InternetGateways"])

        def igw_del():
            g = ec2.describe_internet_gateways(InternetGatewayIds=[igw])["InternetGateways"][0]
            if any(a.get("VpcId") == vpc for a in g.get("Attachments", [])):
                retrying(lambda: ec2.detach_internet_gateway(InternetGatewayId=igw, VpcId=vpc), "detach IGW", 600)
            ec2.delete_internet_gateway(InternetGatewayId=igw)
        step(f"[ops] Internet gateway {igw} (detach + delete)", igw_exists, igw_del)

    # Subnets (ENIs from runtime/Lambda/endpoints release slowly -> backoff)
    subs = [st["public_subnet"]] + list(st.get("private_subnets", [])) if st.get("public_subnet") else list(st.get("private_subnets", []))
    for sn in subs:
        def sn_exists(sn=sn):
            s = ec2.describe_subnets(SubnetIds=[sn])["Subnets"]
            if s and s[0]["VpcId"] != vpc:
                raise RuntimeError(f"{sn} not in {vpc}")
            return bool(s)
        step(f"[ops] Subnet {sn}", sn_exists, lambda sn=sn: retrying(lambda: ec2.delete_subnet(SubnetId=sn), f"delete {sn}"))

    # Route tables (disassociate first)
    for key in ("public_rt", "private_rt"):
        rt = st.get(key)
        if not rt:
            continue

        def rt_exists(rt=rt):
            r = ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"]
            if r and r[0]["VpcId"] != vpc:
                raise RuntimeError(f"{rt} not in {vpc}")
            return bool(r)

        def rt_del(rt=rt):
            r = ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"][0]
            for a in r.get("Associations", []):
                if not a.get("Main"):
                    try:
                        ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
                    except ClientError as e:
                        if not is_nf(e):
                            raise
            retrying(lambda: ec2.delete_route_table(RouteTableId=rt), f"delete {rt}", 300)
        step(f"[ops] Route table {key} {rt}", rt_exists, rt_del)

    # Security groups (revoke rules first: the two demo SGs may reference each other)
    sgs = [st.get("os_sg"), st.get("agent_sg"), st.get("vpce_sg")]
    for sg in [s for s in sgs if s]:
        def sg_exists(sg=sg):
            r = ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"]
            if r and r[0]["VpcId"] != vpc:
                raise RuntimeError(f"{sg} not in {vpc}")
            return bool(r)

        def revoke(sg=sg):
            r = ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]
            if r["IpPermissions"]:
                ec2.revoke_security_group_ingress(GroupId=sg, IpPermissions=r["IpPermissions"])
            if r["IpPermissionsEgress"]:
                ec2.revoke_security_group_egress(GroupId=sg, IpPermissions=r["IpPermissionsEgress"])
        step(f"[ops] SG rules of {sg} (revoke before delete)", sg_exists, lambda sg=sg: revoke(sg))
    for sg in [s for s in sgs if s]:
        def sg_exists(sg=sg):
            return bool(ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"])
        step(f"[ops] Security group {sg}", sg_exists, lambda sg=sg: retrying(lambda: ec2.delete_security_group(GroupId=sg), f"delete {sg}"))

    # VPC
    def v_exists():
        return bool(ec2.describe_vpcs(VpcIds=[vpc])["Vpcs"])
    if vpc_present:
        step(f"[ops] VPC {vpc}", v_exists, lambda: retrying(lambda: ec2.delete_vpc(VpcId=vpc), f"delete {vpc}"))
    else:
        record("ABSENT", f"[ops] VPC {vpc}", "(already deleted)")


# ---------------------------------------------------------------- spokes
def spoke_cleanup(acct, profile, st):
    tag = f"[{profile}]"
    s = session(profile)
    ec2, ssm, iam = s.client("ec2"), s.client("ssm"), s.client("iam")

    ids = {n: t["instance_id"] for n, t in st.get("targets", {}).items() if t["account"] == acct}
    for name, iid in ids.items():
        def exists(iid=iid, name=name):
            r = ec2.describe_instances(InstanceIds=[iid])["Reservations"]
            if not r:
                return False
            inst = r[0]["Instances"][0]
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            if tags.get("Project") != PREFIX:
                raise RuntimeError(f"{iid} lacks Project={PREFIX} tag; refusing")
            return inst["State"]["Name"] != "terminated"
        step(f"{tag} EC2 instance {name} {iid}", exists, lambda iid=iid: ec2.terminate_instances(InstanceIds=[iid]))
    if ids and not DRY:
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=list(ids.values()), WaiterConfig={"Delay": 15, "MaxAttempts": 60})
        except Exception as e:  # noqa: BLE001
            record("ERROR", f"{tag} wait instances terminated", str(e)[:120])

    sg = st.get(f"target_sg_{acct}")
    if sg:
        def sg_exists():
            r = ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"]
            if r and r[0]["GroupName"] != f"{PREFIX}-target-sg":
                raise RuntimeError(f"{sg} is named {r[0]['GroupName']}; refusing")
            return bool(r)
        step(f"{tag} SG {PREFIX}-target-sg {sg}", sg_exists, lambda: retrying(lambda: ec2.delete_security_group(GroupId=sg), f"delete {sg}", 600))

    for d in [DIAG_DOC] + REMEDIATE_DOCS:
        def d_exists(d=d):
            ssm.describe_document(Name=d)
            return True
        step(f"{tag} SSM document {d}", d_exists, lambda d=d: ssm.delete_document(Name=d, Force=True))

    # instance profile (remove role first), then roles
    def ip_exists():
        iam.get_instance_profile(InstanceProfileName=TARGET_ROLE)
        return True

    def ip_del():
        for r in iam.get_instance_profile(InstanceProfileName=TARGET_ROLE)["InstanceProfile"]["Roles"]:
            iam.remove_role_from_instance_profile(InstanceProfileName=TARGET_ROLE, RoleName=r["RoleName"])
        iam.delete_instance_profile(InstanceProfileName=TARGET_ROLE)
    step(f"{tag} instance profile {TARGET_ROLE}", ip_exists, ip_del)
    role_step(iam, TARGET_ROLE, tag)
    role_step(iam, SPOKE_ROLE, tag)


# ---------------------------------------------------------------- global account
def global_cleanup(st):
    user = st.get("global_iam_user", "patchdemo-cn-bedrock-invoker")
    if user != "patchdemo-cn-bedrock-invoker":
        record("ERROR", "[global] IAM user", f"unexpected name {user}; refusing")
        return
    iam = session(GLOBAL_PROFILE, MODEL_REGION).client("iam")
    tag = "[global]"

    def exists():
        iam.get_user(UserName=user)
        return True

    def delete():
        for c in iam.list_service_specific_credentials(UserName=user, ServiceName="bedrock.amazonaws.com")["ServiceSpecificCredentials"]:
            iam.delete_service_specific_credential(UserName=user, ServiceSpecificCredentialId=c["ServiceSpecificCredentialId"])
        for p in iam.list_user_policies(UserName=user)["PolicyNames"]:
            iam.delete_user_policy(UserName=user, PolicyName=p)
        for p in iam.list_attached_user_policies(UserName=user)["AttachedPolicies"]:
            iam.detach_user_policy(UserName=user, PolicyArn=p["PolicyArn"])
        for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])
        iam.delete_user(UserName=user)

    info = ""
    try:
        if exists():
            cr = iam.list_service_specific_credentials(UserName=user, ServiceName="bedrock.amazonaws.com")["ServiceSpecificCredentials"]
            info = f"(bedrock creds={len(cr)}, inline={iam.list_user_policies(UserName=user)['PolicyNames']})"
    except ClientError:
        pass
    step(f"{tag} IAM user {user} {info}".strip(), exists, delete)


# ---------------------------------------------------------------- main
def main():
    global DRY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()
    DRY = not args.yes
    st = load_state()
    if not st:
        sys.exit(".state.json missing/empty; nothing to clean")
    say(("DRY RUN (no changes). Use --yes to delete." if DRY else "DELETING for real (--yes).") + "\n")

    ops = session(OPS_PROFILE)
    say("== ops orchestration / agent layer")
    sfn_cleanup(st, ops.client("stepfunctions"))
    lambda_cleanup(ops.client("lambda"))
    ac = ops.client("bedrock-agentcore-control")
    runtime_cleanup(st, ac, True)
    apikey_cleanup(ac)
    ecr_cleanup(ops.client("ecr"))
    ddb_cleanup(ops.client("dynamodb"))
    opensearch_cleanup(st, ops.client("opensearch"))
    logs_cleanup(st, ops.client("logs"))

    say("\n== ops network")
    network_cleanup(st, ops.client("ec2"))

    say("\n== ops hub IAM roles")
    iam = ops.client("iam")
    for r in (RUNTIME_ROLE, ORCH_ROLE, ONCALL_ROLE, SFN_ROLE):
        role_step(iam, r, "[ops]")

    for acct, profile in SPOKE_PROFILES.items():
        say(f"\n== spoke {profile} ({acct})")
        spoke_cleanup(acct, profile, st)

    say("\n== global (global)")
    global_cleanup(st)

    say("\n==== SUMMARY ====")
    counts = {}
    for status, what, extra in SUMMARY:
        counts[status] = counts.get(status, 0) + 1
    say(", ".join(f"{k}={v}" for k, v in counts.items()))
    errs = [(w, x) for s, w, x in SUMMARY if s == "ERROR"]
    for w, x in errs:
        say(f"  ERROR: {w} {x}")
    if not DRY and errs:
        say("Some resources remain; re-run with --yes (idempotent).")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
