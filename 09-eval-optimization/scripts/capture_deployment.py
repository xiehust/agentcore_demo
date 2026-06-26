"""Write deployment.json from the agentcore toolkit config (.bedrock_agentcore.yaml).

`agentcore status` has no --json output, so we read the identifiers the deploy produced
(runtime ARN, agent id, execution role) from the toolkit's YAML and derive the log group +
observability service name the evaluation step needs. Run after `agentcore deploy`.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / ".bedrock_agentcore.yaml"
OUT = REPO_ROOT / "deployment.json"


def main() -> int:
    if not YAML_PATH.exists():
        print(f"ERROR: {YAML_PATH.name} not found — run `agentcore configure` + `agentcore deploy` first.")
        return 1
    cfg = yaml.safe_load(YAML_PATH.read_text())
    name = cfg.get("default_agent") or next(iter(cfg["agents"]))
    a = cfg["agents"][name]
    agent_id = a["bedrock_agentcore"]["agent_id"]
    account = a["bedrock_agentcore"]["agent_arn"].split(":")[4]

    # Preserve a previously-captured harness_arn if present.
    harness_arn = None
    if OUT.exists():
        harness_arn = json.loads(OUT.read_text()).get("harness_arn")

    dep = {
        "region": a["aws"]["region"],
        "account": account,
        "agent_name": a["name"],
        "agent_id": agent_id,
        "agent_runtime_arn": a["bedrock_agentcore"]["agent_arn"],
        "execution_role_arn": a["aws"]["execution_role"],
        "log_group": f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT",
        "service_name": f"{a['name']}.DEFAULT",
        "deploy_path": "agentcore-cli direct_code_deploy",
        "harness_arn": harness_arn,
    }
    OUT.write_text(json.dumps(dep, indent=2) + "\n")
    print(OUT.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
