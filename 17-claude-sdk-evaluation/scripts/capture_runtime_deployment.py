"""Record the deployed runtime's identifiers so the other scripts don't need them typed in.

Reads `.bedrock_agentcore.yaml` (written by `agentcore configure` / `agentcore deploy`) and
writes `runtime_deployment.json` with the ARN, log group, and observability service name
that `generate_runtime_sessions.py` and `create_reward_batch_evaluation.py` consume.

The observability `service.name` for a Runtime agent is `<agent-name>.DEFAULT`, and its log
group is `/aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / ".bedrock_agentcore.yaml"
OUT_PATH = ROOT / "runtime_deployment.json"


def _scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1).strip("'\"") if match else None


def main() -> int:
    if not YAML_PATH.exists():
        print(f"ERROR: {YAML_PATH.name} not found — run agentcore configure, then deploy.")
        return 1
    text = YAML_PATH.read_text()

    agent_name = _scalar(text, "default_agent") or _scalar(text, "name")
    agent_id = _scalar(text, "agent_id")
    agent_arn = _scalar(text, "agent_arn")
    region = _scalar(text, "region")
    account = _scalar(text, "account")
    if not (agent_name and agent_id and agent_arn):
        print("ERROR: could not read agent name/id/arn — has `agentcore deploy` completed?")
        return 1

    payload = {
        "region": region,
        "account": account,
        "agent_name": agent_name,
        "agent_id": agent_id,
        "agent_runtime_arn": agent_arn,
        "log_group": f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT",
        "service_name": f"{agent_name}.DEFAULT",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    print(f"\nWrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
