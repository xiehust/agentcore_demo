"""IAM: hub roles in the ops account, PatchAgentSpokeRole + target instance profile in every business account.

Hub (ops account)
  PatchAgentRuntimeRole       Runtime execution role -> AssumeRole into spokes, DDB state/memory, API-key vault
  PatchOrchestratorLambdaRole Step Functions task Lambda -> SendCommand via spokes, InvokeAgentRuntime
  PatchOnCallRole             stands in for an IAM Identity Center permission-set role (human take-over)
  PatchSfnRole                Step Functions execution role
Spoke (every business account, same name)
  PatchAgentSpokeRole         trusted only via ops STS VPCE + ExternalId; allowlisted SSM documents; no reboot/stop
  PatchDemoTargetRole         EC2 instance profile for SSM Agent
"""
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import (BATCH_TABLE, DIAG_DOC, EXTERNAL_ID, MEMORY_TABLE, ONCALL_ROLE, OPS_ACCOUNT, ORCH_ROLE,  # noqa: E402
                           OS_DOMAIN, PARTITION, PATCH_GROUP, REGION, RUNTIME_NAME, RUNTIME_ROLE, SFN_ROLE, SPOKE_PROFILES,
                           SPOKE_ROLE, TAGS, TARGET_ROLE, API_KEY_PROVIDER, LAMBDA_NAME, arn, load_state, log,
                           role_arn, save_state, session)

P = PARTITION


def doc(*stmts):
    return json.dumps({"Version": "2012-10-17", "Statement": list(stmts)})


def upsert_role(iam, name, trust, inline: dict, managed=(), desc=""):
    for attempt in range(12):  # a just-created role is not yet a valid principal (IAM eventual consistency)
        try:
            try:
                iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust, Description=desc, Tags=TAGS,
                                MaxSessionDuration=3600)
                log("created role", name)
            except iam.exceptions.EntityAlreadyExistsException:
                iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
                log("updated role", name)
            break
        except iam.exceptions.MalformedPolicyDocumentException as e:
            if "Invalid principal" not in str(e) or attempt == 11:
                raise
            time.sleep(5)
    for pname, pdoc in inline.items():
        iam.put_role_policy(RoleName=name, PolicyName=pname, PolicyDocument=pdoc)
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)


def hub_roles(st):
    iam = session().client("iam")
    runtime_arn_pat = arn("bedrock-agentcore", f"runtime/{RUNTIME_NAME}-*")
    tables = [arn("dynamodb", f"table/{t}") for t in (BATCH_TABLE, MEMORY_TABLE)]
    tables += [t + "/index/*" for t in tables]
    spoke_arns = [role_arn(SPOKE_ROLE, a) for a in SPOKE_PROFILES]

    upsert_role(iam, RUNTIME_ROLE, doc({
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": OPS_ACCOUNT},
                      "ArnLike": {"aws:SourceArn": arn("bedrock-agentcore", "*")}}}), {
        "runtime": doc(
            {"Sid": "Ecr", "Effect": "Allow", "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
                                                          "ecr:GetDownloadUrlForLayer"], "Resource": "*"},
            {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                                          "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
             "Resource": arn("logs", "log-group:/aws/bedrock-agentcore/*")},
            {"Sid": "Metrics", "Effect": "Allow", "Action": ["cloudwatch:PutMetricData", "xray:PutTraceSegments",
                                                             "xray:PutTelemetryRecords"], "Resource": "*"},
            # Only the dedicated Bedrock API key provider -- not every key in the vault.
            {"Sid": "ModelApiKey", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetResourceApiKey", "bedrock-agentcore:GetWorkloadAccessToken"],
             "Resource": [arn("bedrock-agentcore", "token-vault/default"),
                          arn("bedrock-agentcore", f"token-vault/default/apikeycredentialprovider/{API_KEY_PROVIDER}"),
                          arn("bedrock-agentcore", "workload-identity-directory/default"),
                          arn("bedrock-agentcore", f"workload-identity-directory/default/workload-identity/{RUNTIME_NAME}-*")]},
            {"Sid": "ModelApiKeySecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": arn("secretsmanager", f"secret:bedrock-agentcore-identity!default/apikey/{API_KEY_PROVIDER}*")},
            {"Sid": "StateAndMemory", "Effect": "Allow", "Resource": tables,
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                        "dynamodb:BatchWriteItem", "dynamodb:DeleteItem"]},
            {"Sid": "IntoSpokes", "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"], "Resource": spoke_arns},
            # Long-term memory (OpenSearch domain in the ops VPC); the domain's resource policy names this role too.
            {"Sid": "LtmOpenSearch", "Effect": "Allow", "Resource": arn("es", f"domain/{OS_DOMAIN}/*"),
             "Action": ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete"]},
        )}, desc="AgentCore Runtime execution role for the patch agent")

    upsert_role(iam, ORCH_ROLE, doc({
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}), {
        "orchestrator": doc(
            {"Sid": "State", "Effect": "Allow", "Resource": tables,
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]},
            {"Sid": "IntoSpokes", "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"],
             "Resource": spoke_arns + [role_arn(ONCALL_ROLE)]},
            {"Sid": "InvokeAgent", "Effect": "Allow", "Resource": [runtime_arn_pat, runtime_arn_pat + "/*"],
             "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser",
                        "bedrock-agentcore:StopRuntimeSession"]},
        )}, managed=[f"arn:{P}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"],
        desc="Step Functions task Lambda for patch batches")

    # In production this is AWSReservedSSO_PatchOnCall_xxxx created by IAM Identity Center; the portal backend
    # runs the human's call under it and passes runtimeUserId = the SSO user name.
    upsert_role(iam, ONCALL_ROLE, doc({
        "Effect": "Allow", "Principal": {"AWS": role_arn(ORCH_ROLE)}, "Action": ["sts:AssumeRole", "sts:TagSession"]}), {
        "oncall": doc({"Sid": "TakeOverSession", "Effect": "Allow", "Resource": [runtime_arn_pat, runtime_arn_pat + "/*"],
                       "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"]},
                      {"Sid": "ReadState", "Effect": "Allow", "Resource": tables, "Action": ["dynamodb:GetItem", "dynamodb:Query"]})},
        desc="Simulates the IAM Identity Center permission-set role used by on-call engineers")

    upsert_role(iam, SFN_ROLE, doc({
        "Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": OPS_ACCOUNT}}}), {
        "sfn": doc({"Effect": "Allow", "Action": "lambda:InvokeFunction",
                    "Resource": [arn("lambda", f"function:{LAMBDA_NAME}"), arn("lambda", f"function:{LAMBDA_NAME}:*")]})},
        desc="Step Functions role for patch batches")


def spoke_roles(st):
    sts_vpce, ssm_vpce, ec2_vpce = st["vpce"]["sts"], st["vpce"]["ssm"], st["vpce"]["ec2"]
    for acct, profile in SPOKE_PROFILES.items():
        iam = session(profile).client("iam")
        base = {"sts:ExternalId": EXTERNAL_ID, "aws:SourceVpce": sts_vpce}
        # Both statements require ExternalId AND that AssumeRole came through the ops VPC's STS endpoint.
        # Finding: sts:ExternalId is NOT in the request context when sts:TagSession is authorized, so a statement
        # that covers TagSession must not condition on ExternalId (it silently never matches).
        agent_tags = {"StringLike": {"aws:RequestTag/TargetInstance": "i-*"},
                      "ForAllValues:StringEquals": {"aws:TagKeys": ["TargetInstance", "BatchId"]}}
        trust = doc(
            {"Sid": "AgentAssume", "Effect": "Allow", "Principal": {"AWS": role_arn(RUNTIME_ROLE)}, "Action": "sts:AssumeRole",
             # the agent session must carry TargetInstance=<the one instance it diagnoses>
             "Condition": {"StringEquals": base, **agent_tags}},
            {"Sid": "AgentTagSession", "Effect": "Allow", "Principal": {"AWS": role_arn(RUNTIME_ROLE)}, "Action": "sts:TagSession",
             "Condition": {"StringEquals": {"aws:SourceVpce": sts_vpce}, **agent_tags}},
            {"Sid": "Orchestrator", "Effect": "Allow", "Principal": {"AWS": role_arn(ORCH_ROLE)},
             "Action": "sts:AssumeRole", "Condition": {"StringEquals": base}})
        perms = doc(
            # Residual risk (documented): ListCommandInvocations / GetCommandInvocation / ListCommands do not support
            # resource-level scoping, so an agent session can READ command output of other hosts in this account.
            # Writes are pinned (SendCommand) or orchestrator-only (CancelCommand) below.
            {"Sid": "Diagnose", "Effect": "Allow", "Resource": "*",
             "Action": ["ssm:DescribeInstanceInformation", "ssm:ListCommandInvocations", "ssm:ListCommands",
                        "ssm:GetCommandInvocation", "ssm:DescribeInstancePatchStates", "ssm:DescribeInstancePatches",
                        "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeTags"]},
            {"Sid": "AllowedDocsOnly", "Effect": "Allow", "Action": "ssm:SendCommand",
             "Resource": [f"arn:{P}:ssm:{REGION}::document/AWS-RunPatchBaseline",
                          f"arn:{P}:ssm:{REGION}:{acct}:document/{DIAG_DOC}",
                          f"arn:{P}:ssm:{REGION}:{acct}:document/PatchRemediate-*"]},
            # agent sessions: exactly the instance named in the session tag (IAM policy variable)
            {"Sid": "AgentPinnedInstance", "Effect": "Allow", "Action": "ssm:SendCommand",
             "Resource": f"arn:{P}:ec2:{REGION}:{acct}:instance/${{aws:PrincipalTag/TargetInstance}}",
             "Condition": {"StringEquals": {"ssm:resourceTag/PatchGroup": PATCH_GROUP}}},
            # orchestrator sessions (no TargetInstance tag): any instance of the patch group
            {"Sid": "OrchestratorTaggedInstances", "Effect": "Allow", "Action": "ssm:SendCommand",
             "Resource": f"arn:{P}:ec2:{REGION}:{acct}:instance/*",
             "Condition": {"StringEquals": {"ssm:resourceTag/PatchGroup": PATCH_GROUP},
                           "Null": {"aws:PrincipalTag/TargetInstance": "true"}}},
            # CancelCommand has no resource-level scoping: an agent credential could cancel any command in the account
            # (e.g. a whole patch wave). Only orchestrator sessions (no TargetInstance session tag) may cancel.
            {"Sid": "OrchestratorCancelOnly", "Effect": "Allow", "Action": "ssm:CancelCommand", "Resource": "*",
             "Condition": {"Null": {"aws:PrincipalTag/TargetInstance": "true"}}},
            # Belt and braces: even a leaked credential is useless outside the ops VPC.
            # ViaAWSService guard keeps service-to-service (FAS) calls working -- they carry no SourceVpce.
            {"Sid": "DenyIfNotViaOpsVpce", "Effect": "Deny", "Action": "*", "Resource": "*",
             "Condition": {"StringNotEquals": {"aws:SourceVpce": [ssm_vpce, ec2_vpce, sts_vpce]},
                           "BoolIfExists": {"aws:ViaAWSService": "false"}}},
        )
        upsert_role(iam, SPOKE_ROLE, trust, {"spoke": perms}, desc="Patch agent entry role (hub-spoke)")

        upsert_role(iam, TARGET_ROLE, doc({"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com.cn"},
                                           "Action": "sts:AssumeRole"}), {},
                    managed=[f"arn:{P}:iam::aws:policy/AmazonSSMManagedInstanceCore"], desc="patch demo target instances")
        try:
            iam.create_instance_profile(InstanceProfileName=TARGET_ROLE, Tags=TAGS)
            iam.add_role_to_instance_profile(InstanceProfileName=TARGET_ROLE, RoleName=TARGET_ROLE)
        except iam.exceptions.EntityAlreadyExistsException:
            pass
        log("spoke ready", acct)


if __name__ == "__main__":
    st = load_state()
    hub_roles(st)
    spoke_roles(st)
    save_state(iam_done=True)
    time.sleep(10)  # IAM propagation
