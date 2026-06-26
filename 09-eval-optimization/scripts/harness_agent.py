"""The customer-support agent AS a managed AgentCore Harness (create/update + tool loop).

This is the primary "create and deploy the agent" path: the agent is declared as harness
configuration (model + system prompt + 5 inline-function tools) via CreateHarness/UpdateHarness
— no container, no orchestration code. InvokeHarness runs the model loop; when it emits a
`toolUse`, this client executes the tool (agent/harness_tools.dispatch) and returns a
`toolResult`, continuing until the model finishes (the client-side tool loop).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    REPO_ROOT,
    control_client,
    data_client,
    load_config,
    load_deployment,
)
from botocore.exceptions import ClientError  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from agent.harness_tools import TOOL_SPECS, dispatch  # noqa: E402

HARNESS_NAME = "AcmeSupportHarness"
MAX_TOKENS = 512
READY_TIMEOUT_S = 300
MAX_TOOL_TURNS = 6

# Capture GenAI message content in the harness telemetry. Without this the harness emits
# spans + token counts but NOT the user_query/response content, and AgentCore Evaluations
# fails every session with AgentSpanMappingException ("Failed to parse user_query"). This is
# the managed-harness equivalent of adding aws-opentelemetry-distro on AgentCore Runtime.
HARNESS_ENV = {
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
    "AGENT_OBSERVABILITY_ENABLED": "true",
}


def _find(control) -> tuple[str | None, str | None]:
    try:
        for h in control.list_harnesses().get("harnesses", []):
            if h.get("harnessName") == HARNESS_NAME and h.get("status") not in ("DELETING", "DELETE_FAILED"):
                return h.get("arn"), h.get("harnessId")
    except ClientError:
        pass
    return None, None


def _wait_ready(control, harness_id: str) -> str:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        st = control.get_harness(harnessId=harness_id)["harness"]["status"]
        if st != last:
            print(f"  harness status: {st}")
            last = st
        if st == "READY" or st.endswith("FAILED"):
            return st
        time.sleep(5)
    return last or "TIMEOUT"


def create_or_update_harness(system_prompt: str) -> str:
    """Create the tool-equipped support harness, or update its prompt/tools if it exists.

    Returns the harness ARN (and records it in deployment.json).
    """
    cfg = load_config()
    dep = load_deployment()
    control = control_client()
    model = {"bedrockModelConfig": {"modelId": cfg["agent_model_id"], "maxTokens": MAX_TOKENS}}
    arn, hid = _find(control)
    if arn:
        print(f"Updating existing harness {arn}")
        # Note: UpdateHarness uses a different `memory` shape than CreateHarness; omit it
        # to leave the existing (disabled) memory config untouched.
        control.update_harness(
            harnessId=hid, model=model, systemPrompt=[{"text": system_prompt}],
            tools=TOOL_SPECS, environmentVariables=HARNESS_ENV,
        )
    else:
        print(f"Creating harness '{HARNESS_NAME}' with {len(TOOL_SPECS)} inline tools")
        resp = control.create_harness(
            harnessName=HARNESS_NAME, executionRoleArn=dep["execution_role_arn"],
            model=model, systemPrompt=[{"text": system_prompt}],
            tools=TOOL_SPECS, memory={"disabled": {}}, environmentVariables=HARNESS_ENV,
        )
        arn, hid = resp["harness"]["arn"], resp["harness"]["harnessId"]
    status = _wait_ready(control, hid)
    if status != "READY":
        raise RuntimeError(f"Harness not READY (status={status})")
    dep["harness_arn"] = arn
    (REPO_ROOT / "deployment.json").write_text(json.dumps(dep, indent=2) + "\n")
    return arn


def _parse_stream(stream) -> tuple[list, str]:
    """Reconstruct assistant content blocks + stop reason from an InvokeHarness stream."""
    blocks: dict[int, dict] = {}
    stop_reason = "end_turn"
    for event in stream:
        if "contentBlockStart" in event:
            ev = event["contentBlockStart"]
            idx = ev["contentBlockIndex"]
            start = ev.get("start", {})
            if "toolUse" in start:
                tu = start["toolUse"]
                blocks[idx] = {"kind": "toolUse", "name": tu.get("name"), "toolUseId": tu.get("toolUseId"), "input": ""}
            else:
                blocks[idx] = {"kind": "text", "text": ""}
        elif "contentBlockDelta" in event:
            ev = event["contentBlockDelta"]
            idx = ev["contentBlockIndex"]
            delta = ev.get("delta", {})
            b = blocks.setdefault(idx, {"kind": "text", "text": ""})
            if "text" in delta:
                b.setdefault("text", "")
                b["kind"] = "text"
                b["text"] += delta["text"]
            elif "toolUse" in delta:
                b["kind"] = "toolUse"
                b["input"] = b.get("input", "") + (delta["toolUse"].get("input") or "")
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason", "end_turn")
        elif "runtimeClientError" in event:
            raise RuntimeError(f"runtimeClientError: {event['runtimeClientError'].get('message')}")
        elif "validationException" in event:
            raise RuntimeError(f"validationException: {event['validationException'].get('message')}")
    # build ordered content
    content = []
    for idx in sorted(blocks):
        b = blocks[idx]
        if b["kind"] == "text" and b.get("text"):
            content.append({"text": b["text"]})
        elif b["kind"] == "toolUse":
            try:
                parsed = json.loads(b["input"]) if b["input"] else {}
            except json.JSONDecodeError:
                parsed = {}
            content.append({"toolUse": {"toolUseId": b["toolUseId"], "name": b["name"], "input": parsed}})
    return content, stop_reason


def invoke_harness_loop(harness_arn: str, session_id: str, prompt: str) -> tuple[str, list[str]]:
    """Run the client-side tool loop over InvokeHarness. Returns (final_text, tools_used)."""
    client = data_client()
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    tools_used: list[str] = []
    final_text = ""
    for _turn in range(MAX_TOOL_TURNS):
        resp = client.invoke_harness(harnessArn=harness_arn, runtimeSessionId=session_id, messages=messages)
        content, stop = _parse_stream(resp["stream"])
        messages.append({"role": "assistant", "content": content})
        text_parts = [c["text"] for c in content if "text" in c]
        if text_parts:
            final_text = "\n".join(text_parts)
        tool_uses = [c["toolUse"] for c in content if "toolUse" in c]
        if stop != "tool_use" or not tool_uses:
            break
        results = []
        for tu in tool_uses:
            tools_used.append(tu["name"])
            out = dispatch(tu["name"], tu.get("input", {}))
            results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": out}], "status": "success"}})
        messages.append({"role": "user", "content": results})
    return final_text.strip(), tools_used
