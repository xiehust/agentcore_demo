"""Guardrails without Bedrock Guardrails / AgentCore Policy (neither is available in China regions).

Layer 1  input   : every command output is untrusted -> secrets redacted, prompt-injection lines removed,
                   wrapped in <untrusted_command_output>.
Layer 2  tools   : fixed tool set, no instance parameter (bound to the session), Document allowlist,
                   per-instance and per-batch quotas (atomic in DynamoDB), tool-call budget.
Layer 3  verify  : FIXED is accepted only if the code observed a successful rerun_patch (not the LLM's claim).
Layer 4  IAM     : spoke session pinned to one instance by session tag; no reboot/stop; only via ops VPCE.
Layer 5  human   : reboot / rollback / baseline changes become approval records, never executed by the agent.
"""
import re

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

import state

MAX_TOOL_CALLS = 30
MAX_REMEDIATIONS_PER_INSTANCE = 2
MAX_RERUNS_PER_INSTANCE = 1
PER_TOOL_LIMITS = {"request_human_action": 3, "search_memory": 5, "report_result": 3}  # no tool is exempt from limits

_SECRETS = [
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY_ID]"),
    (re.compile(r"(?i)(aws_secret_access_key|secret|password|passwd|token)(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
]
_INJECTION = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the |your )?(instructions|rules|system)"
    r"|you are now|new instructions|system prompt|as an ai|assistant:|AWS-RunShellScript|reboot (the |this )?instance"
    r"|rm -rf /|curl [^|]*\|\s*(ba)?sh)")


def strip_injection(text: str) -> tuple[str, int]:
    """Remove instruction-like lines (used for memory records, which are re-injected into future prompts)."""
    out, hits = [], 0
    for line in (text or "").splitlines() or [""]:
        if _INJECTION.search(line):
            hits += 1
            out.append("[removed instruction-like text]")
        else:
            out.append(line)
    return "\n".join(out), hits


def sanitize(text: str, flags: dict | None = None) -> str:
    text = text or ""
    for rx, rep in _SECRETS:
        text = rx.sub(rep, text)
    lines, hits = [], 0
    for line in text.splitlines():
        if _INJECTION.search(line):
            hits += 1
            lines.append("[guardrail: removed a line containing instruction-like text from command output]")
        else:
            lines.append(line)
    if flags is not None and hits:
        flags["injection_lines"] = flags.get("injection_lines", 0) + hits
    return "<untrusted_command_output>\n" + "\n".join(lines) + "\n</untrusted_command_output>"


class PatchGuardrails(HookProvider):
    def __init__(self, ctx):
        self.ctx = ctx
        self.tool_calls = 0
        self.per_tool: dict = {}
        self.flags: dict = {}
        self.reported = False
        self.last_rerun_status: str | None = None
        self.blocked: list[str] = []
        self.result: dict | None = None

    def register_hooks(self, registry: HookRegistry, **kw) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def _deny(self, event: BeforeToolCallEvent, why: str):
        self.blocked.append(f"{event.tool_use['name']}: {why}")
        state.audit(self.ctx.batch_id, self.ctx.instance_id, "guardrail_block", tool=event.tool_use["name"],
                    input=event.tool_use.get("input"), reason=why)
        event.cancel_tool = f"GUARDRAIL BLOCKED: {why}"

    def before_tool(self, event: BeforeToolCallEvent):
        name, args = event.tool_use["name"], event.tool_use.get("input") or {}
        self.tool_calls += 1
        self.per_tool[name] = self.per_tool.get(name, 0) + 1
        if self.reported and name != "report_result":
            return self._deny(event, "result already reported; session is closed for automation")
        if name in PER_TOOL_LIMITS and self.per_tool[name] > PER_TOOL_LIMITS[name]:
            return self._deny(event, f"{name} limit ({PER_TOOL_LIMITS[name]}) reached")
        if self.tool_calls > MAX_TOOL_CALLS and name != "report_result":
            return self._deny(event, f"tool budget {MAX_TOOL_CALLS} exhausted -> call report_result with NEEDS_HUMAN")
        item = state.get_instance(self.ctx.batch_id, self.ctx.instance_id) or {}
        if item.get("session_id") != self.ctx.session_id:
            return self._deny(event, "instance is not assigned to this session")
        if item.get("status") in state.TERMINAL and self.ctx.mode == "diagnose":
            return self._deny(event, f"instance already {item.get('status')}")
        if name == "run_remediation":
            from tools import REMEDIATE_DOCS  # code-level allowlist, independent of the tool schema
            if args.get("document") not in REMEDIATE_DOCS:
                return self._deny(event, f"document {args.get('document')!r} is not allowlisted")
            if args.get("document") == "PatchRemediate-DisableRepo" and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.get("repo_id", "")):
                return self._deny(event, "repo_id must match ^[A-Za-z0-9._-]{1,64}$")
            if not state.bump_counter(self.ctx.batch_id, self.ctx.instance_id, "remediations", MAX_REMEDIATIONS_PER_INSTANCE):
                return self._deny(event, f"per-instance remediation quota ({MAX_REMEDIATIONS_PER_INSTANCE}) used up -> escalate")
            if not state.bump_batch_remediations(self.ctx.batch_id):
                return self._deny(event, "batch-wide remediation limit reached (circuit breaker) -> escalate")
        if name == "rerun_patch" and not state.bump_counter(self.ctx.batch_id, self.ctx.instance_id, "reruns", MAX_RERUNS_PER_INSTANCE):
            return self._deny(event, f"rerun quota ({MAX_RERUNS_PER_INSTANCE}) used up -> escalate")

    def after_tool(self, event: AfterToolCallEvent):
        res = event.result or {}
        txt = " ".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        state.audit(self.ctx.batch_id, self.ctx.instance_id, "tool_call", tool=event.tool_use["name"],
                    input=event.tool_use.get("input"), status=res.get("status"), result=txt[:1500])
