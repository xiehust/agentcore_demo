"""Agent tools. None of them takes an instance id -- the target is bound to the session (guardrail by construction)."""
import os
import time
from dataclasses import dataclass
from typing import Literal

import boto3
from strands import tool

import state
from guardrails import sanitize, strip_injection

REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
PARTITION = "aws-cn" if REGION.startswith("cn-") else "aws"
SPOKE_ROLE = os.environ.get("SPOKE_ROLE", "PatchAgentSpokeRole")
EXTERNAL_ID = os.environ.get("EXTERNAL_ID", "patchdemo-agent-dev")
DIAG_DOC = "PatchDiagnose-Collect"
REMEDIATE_DOCS = ("PatchRemediate-FreeDisk", "PatchRemediate-DisableRepo", "PatchRemediate-ReleaseRpmLock")
RERUN_TIMEOUT = int(os.environ.get("RERUN_TIMEOUT", "600"))


@dataclass
class InstanceCtx:
    batch_id: str
    instance_id: str
    account: str
    session_id: str
    patch_command_id: str
    patch_status: str
    name: str = ""
    mode: str = "diagnose"
    _spoke: boto3.Session | None = None
    _spoke_exp: float = 0

    def spoke(self) -> boto3.Session:
        """AssumeRole into the business account through the STS VPCE, pinned to this instance by session tag."""
        if self._spoke is None or time.time() > self._spoke_exp:
            c = boto3.client("sts", region_name=REGION).assume_role(
                RoleArn=f"arn:{PARTITION}:iam::{self.account}:role/{SPOKE_ROLE}", ExternalId=EXTERNAL_ID,
                RoleSessionName=f"agent-{self.instance_id}"[:64], DurationSeconds=3600,
                Tags=[{"Key": "TargetInstance", "Value": self.instance_id}, {"Key": "BatchId", "Value": self.batch_id}])["Credentials"]
            self._spoke = boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                        aws_session_token=c["SessionToken"], region_name=REGION)
            self._spoke_exp = time.time() + 3000
        return self._spoke


def ping_status(ctx: InstanceCtx) -> dict:
    info = ctx.spoke().client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [ctx.instance_id]}])["InstanceInformationList"]
    return info[0] if info else {"PingStatus": "NotRegistered"}


def run_document(ctx: InstanceCtx, document: str, params: dict, timeout: int = 180, plugin: str | None = "run") -> dict:
    ssm = ctx.spoke().client("ssm")
    cmd = ssm.send_command(InstanceIds=[ctx.instance_id], DocumentName=document, Parameters=params, TimeoutSeconds=60,
                           Comment=f"patch-agent {ctx.batch_id}")["Command"]["CommandId"]
    deadline, status = time.time() + timeout, "Pending"
    while time.time() < deadline:
        time.sleep(4)
        inv = ssm.list_command_invocations(CommandId=cmd, InstanceId=ctx.instance_id, Details=True)["CommandInvocations"]
        if inv and inv[0]["Status"] not in ("Pending", "InProgress", "Delayed"):
            status = inv[0]["Status"]
            break
    else:
        # Agent sessions have no ssm:CancelCommand (it is account-wide, IAM cannot pin it to one instance); the
        # command keeps running under its own document timeout and the orchestrator / a human can cancel it.
        status = "TimedOutByAgent"
        inv = ssm.list_command_invocations(CommandId=cmd, InstanceId=ctx.instance_id, Details=True)["CommandInvocations"]
    plugins = inv[0].get("CommandPlugins", []) if inv else []
    out = "\n".join(f"--- {p['Name']} ({p.get('Status')}) ---\n{p.get('Output', '')}" for p in plugins
                    if plugin is None or p["Name"] == plugin or len(plugins) == 1)
    return {"command_id": cmd, "status": status, "output": out[-6000:]}


def build_tools(ctx: InstanceCtx, guard, memory_search=None) -> list:
    """memory_search(query) -> text; the memory library is injected, tools do not import it."""
    def offline_guard():
        p = ping_status(ctx)
        if p.get("PingStatus") != "Online":
            return (f"SSM agent is {p.get('PingStatus')} (last ping {p.get('LastPingDateTime')}). Commands cannot be "
                    f"delivered; do not retry SSM commands. Consider request_human_action.")
        return None

    @tool
    def get_instance_overview() -> dict:
        """Instance facts: SSM ping status, OS, agent version, EC2 status checks, and the patch result recorded for this batch."""
        p = ping_status(ctx)
        st = ctx.spoke().client("ec2").describe_instance_status(InstanceIds=[ctx.instance_id], IncludeAllInstances=True)["InstanceStatuses"]
        item = state.get_instance(ctx.batch_id, ctx.instance_id) or {}
        return {"instance_id": ctx.instance_id, "name": ctx.name, "account": ctx.account,
                "ssm_ping_status": p.get("PingStatus"), "last_ping": str(p.get("LastPingDateTime")),
                # PlatformName/Version/AgentVersion are reported by the host -> untrusted, strip instruction-like text
                "platform": strip_injection(f"{p.get('PlatformName')} {p.get('PlatformVersion')}"[:120])[0],
                "ssm_agent_version": strip_injection(str(p.get("AgentVersion"))[:40])[0],
                "ec2_state": st[0]["InstanceState"]["Name"] if st else None,
                "ec2_status_checks": {"system": st[0]["SystemStatus"]["Status"], "instance": st[0]["InstanceStatus"]["Status"]} if st else None,
                "batch_patch_status": item.get("patch_status"), "remediations_used": int(item.get("remediations", 0)),
                "reruns_used": int(item.get("reruns", 0))}

    @tool
    def get_patch_output() -> str:
        """Output of the failed AWS-RunPatchBaseline run for this instance in this batch."""
        if not ctx.patch_command_id:
            return "no patch command recorded"
        inv = ctx.spoke().client("ssm").list_command_invocations(
            CommandId=ctx.patch_command_id, InstanceId=ctx.instance_id, Details=True)["CommandInvocations"]
        if not inv:
            return "invocation not found"
        parts = [f"status={inv[0]['Status']} detail={inv[0].get('StatusDetails')}"]
        for p in inv[0].get("CommandPlugins", []):
            if p.get("Output") or p.get("Status") not in ("Skipped",):
                parts.append(f"--- {p['Name']} ({p.get('Status')}, rc={p.get('ResponseCode')}) ---\n{p.get('Output', '')[-3500:]}")
        return sanitize("\n".join(parts), guard.flags)

    @tool
    def run_diagnostic(check: Literal["disk", "repos", "package_locks", "dnf_log", "ssm_agent", "all"]) -> str:
        """Run the read-only PatchDiagnose-Collect document. check selects the section."""
        if (msg := offline_guard()):
            return msg
        r = run_document(ctx, DIAG_DOC, {"Check": [check]})
        return sanitize(f"status={r['status']}\n{r['output']}", guard.flags)

    @tool
    def run_remediation(document: Literal["PatchRemediate-FreeDisk", "PatchRemediate-DisableRepo", "PatchRemediate-ReleaseRpmLock"],
                        repo_id: str = "") -> str:
        """Run ONE allowlisted remediation document. repo_id is required only for PatchRemediate-DisableRepo
        (the [section] id of the broken repo). Max 2 remediations per instance."""
        if (msg := offline_guard()):
            return msg
        params = {"RepoId": [repo_id]} if document == "PatchRemediate-DisableRepo" else {}
        r = run_document(ctx, document, params)
        state.audit(ctx.batch_id, ctx.instance_id, "remediation", document=document, repo_id=repo_id, status=r["status"])
        return sanitize(f"status={r['status']}\n{r['output']}", guard.flags)

    @tool
    def rerun_patch() -> str:
        """Re-run AWS-RunPatchBaseline (Install, NoReboot) on this instance only and wait for the result. Max 1 per instance."""
        if (msg := offline_guard()):
            return msg
        r = run_document(ctx, "AWS-RunPatchBaseline", {"Operation": ["Install"], "RebootOption": ["NoReboot"]},
                         timeout=RERUN_TIMEOUT, plugin="PatchLinux")
        guard.last_rerun_status = r["status"]
        state.update_instance(ctx.batch_id, ctx.instance_id, rerun_command_id=r["command_id"], rerun_status=r["status"])
        return sanitize(f"rerun status={r['status']}\n{r['output'][-3000:]}", guard.flags)

    @tool
    def search_memory(query: str) -> str:
        """Search the team knowledge base and lessons from earlier diagnoses (error text, symptoms, keywords)."""
        if memory_search is None:
            return "memory is not configured for this agent"
        return memory_search(query)

    @tool
    def request_human_action(action: Literal["reboot_instance", "restart_ssm_agent", "rollback_patch", "modify_patch_baseline", "other"],
                             reason: str, evidence: str) -> str:
        """Create an approval request for a high-risk action. The agent can NOT perform these itself;
        an engineer approves in the portal and a dedicated SSM Automation executes it."""
        aid = state.request_approval(ctx.batch_id, ctx.instance_id, action, reason, evidence, ctx.session_id)
        return f"approval request {aid} created (status PENDING_APPROVAL). Do not wait for it; report NEEDS_HUMAN."

    @tool
    def report_result(status: Literal["FIXED", "NEEDS_HUMAN"], signature: str, root_cause: str, summary: str, actions_taken: str) -> str:
        """Final report, call exactly once at the end.
        signature: short snake_case failure class, e.g. disk_full, repo_unreachable, rpm_lock_held, ssm_agent_offline.
        FIXED is only accepted after a successful rerun_patch."""
        if status == "FIXED" and guard.last_rerun_status != "Success":
            return (f"REJECTED: FIXED requires a successful rerun_patch in this session (observed: {guard.last_rerun_status}). "
                    f"Rerun the patch or report NEEDS_HUMAN.")
        guard.reported = True
        guard.result = {"status": status, "signature": signature, "root_cause": root_cause, "summary": summary,
                        "actions_taken": actions_taken}
        state.update_instance(ctx.batch_id, ctx.instance_id, status=status, signature=signature, root_cause=root_cause[:1000],
                              summary=summary[:2000], actions_taken=actions_taken[:2000], tool_calls=guard.tool_calls,
                              injection_lines=guard.flags.get("injection_lines", 0), guardrail_blocks=guard.blocked[:10],
                              diag_finished_at=state.now())
        return f"recorded {status}"

    return [get_instance_overview, get_patch_output, run_diagnostic, run_remediation, rerun_patch, search_memory,
            request_human_action, report_result]
