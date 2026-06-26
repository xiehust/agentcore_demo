"""A/B test scaffold for AgentCore Optimization (the production validation step).

A real A/B test splits LIVE production traffic between a control variant (baseline prompt)
and a treatment variant (optimized prompt) through an AgentCore Gateway, scores each session
with an online evaluator, and reports statistical significance. That requires standing
infrastructure — a Gateway, immutable configuration bundles per variant, an online evaluation
config, and a role — plus sustained live traffic, which is out of scope for this simplified
demo (the executed closed loop here is recommend -> apply -> batch re-evaluate -> compare).

This script prints the exact CreateABTest setup with this demo's two variants, checks which
prerequisites exist in the account, and exits 0 (documented mode). Flip ATTEMPT_REAL to True
only once a Gateway + online evaluation config exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import control_client, data_client, load_deployment  # noqa: E402

ATTEMPT_REAL = False  # set True once a Gateway + online eval config are provisioned


def check_prereqs() -> dict:
    ctrl = control_client()
    data = data_client()
    found = {}
    try:
        found["gateways"] = len(ctrl.list_gateways().get("items", ctrl.list_gateways().get("gateways", [])))
    except Exception as e:  # noqa: BLE001
        found["gateways"] = f"n/a ({type(e).__name__})"
    try:
        found["online_eval_configs"] = len(
            ctrl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
        )
    except Exception as e:  # noqa: BLE001
        found["online_eval_configs"] = f"n/a ({type(e).__name__})"
    try:
        found["config_bundles"] = len(ctrl.list_configuration_bundles().get("configurationBundles", []))
    except Exception as e:  # noqa: BLE001
        found["config_bundles"] = f"n/a ({type(e).__name__})"
    try:
        found["existing_ab_tests"] = len(data.list_ab_tests().get("abTests", []))
    except Exception as e:  # noqa: BLE001
        found["existing_ab_tests"] = f"n/a ({type(e).__name__})"
    return found


def main() -> int:
    dep = load_deployment()
    print("=" * 70)
    print("AgentCore A/B test — the production validation step of the optimize loop")
    print("=" * 70)
    print(
        "\nDesign:\n"
        "  control   (weight 50): baseline system prompt  (config bundle v1)\n"
        "  treatment (weight 50): optimized system prompt  (config bundle v2)\n"
        "  Gateway splits live traffic 50/50; an online evaluator scores every session;\n"
        "  results report per-variant scores with statistical significance. Promote the\n"
        "  winner to 100% and it becomes the new baseline for the next iteration.\n"
    )
    print("Exact call (bedrock-agentcore.create_ab_test):\n")
    print(
        "  data.create_ab_test(\n"
        "    name='acmesupport_prompt_ab',\n"
        "    gatewayArn=<your AgentCore Gateway ARN>,\n"
        "    roleArn=<role the A/B test assumes>,\n"
        "    variants=[\n"
        "      {'name':'control','weight':50,'variantConfiguration':{\n"
        "         'configurationBundle':{'bundleArn':<baseline-bundle>,'bundleVersion':'1'}}},\n"
        "      {'name':'treatment','weight':50,'variantConfiguration':{\n"
        "         'configurationBundle':{'bundleArn':<optimized-bundle>,'bundleVersion':'1'}}},\n"
        "    ],\n"
        "    evaluationConfig={'onlineEvaluationConfigArn':<online-eval-config-arn>},\n"
        "    enableOnCreate=True)\n"
    )
    print("Prerequisites to create first (control plane):")
    print("  1. create_configuration_bundle  x2  (baseline prompt, optimized prompt)")
    print("  2. create_gateway + a runtime target pointing at this agent:")
    print(f"       runtime: {dep.get('agent_runtime_arn')}")
    print("  3. create_online_evaluation_config  (e.g. Builtin.GoalSuccessRate, sampling rate)")
    print("  4. create_ab_test(...) with the variants above; then drive live traffic.\n")

    found = check_prereqs()
    print("Account prerequisite check:")
    for k, v in found.items():
        print(f"  {k}: {v}")

    if ATTEMPT_REAL:
        print("\nATTEMPT_REAL=True but real A/B creation is intentionally left to the operator")
        print("(needs a Gateway + online eval config + live traffic). See the runbook above.")
    else:
        print("\nMode: DOCUMENTED (ATTEMPT_REAL=False).")
        print("The executed closed loop in this demo is recommend -> apply -> batch re-evaluate")
        print("-> compare (see results/comparison.json). A/B is the live-traffic validation you")
        print("run in production once the prerequisites above are provisioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
