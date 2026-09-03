#!/usr/bin/env python3
"""Live verification of the gh shim on a deployed AgentCore Runtime.

Evidence is collected from two independent vantage points inside the SAME session:

  1. the agent process (InvokeAgentRuntime {"verify": true} -> agent.py verify());
  2. a shell opened by InvokeAgentRuntimeCommand next to the agent process
     (/proc/<agent pid>/environ, gh config dir, PATH resolution, a real `gh api` call).

Then a normal model-driven turn ({"prompt": ...}) shows the Strands agent using gh
through the shim without ever seeing a token.

Usage: python3 scripts/04-verify-gh-shim.py [--config runtime-gh-shim.json] [--keep-session]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_cmd import RuntimeSession  # noqa: E402

USER_ID = "gh-shim-verifier"  # any stable opaque id; required for WAT injection under SigV4 inbound auth

SHELL_EVIDENCE = r"""
set +e
export LC_ALL=C
AGENT_PID=""
for p in /proc/[0-9]*; do
  case "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" in *agent.py*) AGENT_PID="${p#/proc/}"; break;; esac
done
echo "agent_pid=$AGENT_PID"
echo "workload_token_file=$(stat -c '%a %U' /dev/shm/agentcore/workload_token 2>/dev/null || echo absent)"
echo "which_gh=$(command -v gh)"
echo "path=$PATH"
echo "agent_environ_GH_TOKEN_count=$(tr '\0' '\n' < /proc/$AGENT_PID/environ | grep -c '^GH_TOKEN=')"
echo "agent_environ_GITHUB_TOKEN_count=$(tr '\0' '\n' < /proc/$AGENT_PID/environ | grep -c '^GITHUB_TOKEN=')"
echo "shell_env_GH_TOKEN_count=$(env | grep -c '^GH_TOKEN=')"
echo "home_gh_hosts_yml=$( [ -e ~/.config/gh/hosts.yml ] && echo present || echo absent)"
echo "shm_gh_config=$(ls -A /dev/shm/gh-config 2>/dev/null | tr '\n' ',')"
echo "git_credentials_file=$( [ -e ~/.git-credentials ] && echo present || echo absent)"
echo "grep_token_on_disk=$(grep -rl 'gho_\|ghp_\|ghs_' /root /app /tmp /dev/shm 2>/dev/null | head -3 | tr '\n' ',')"
echo "--- gh api /user via shim"
gh api /user --jq '.login' ; echo "rc=$?"
echo "--- gh auth login via shim"
gh auth login --with-token </dev/null ; echo "rc=$?"
echo "--- child-env proof"
GH_REAL=/opt/shim/print_token_env.sh gh api /rate_limit ; echo "rc=$?"
echo "--- after: shell env still clean"
echo "shell_env_GH_TOKEN_count_after=$(env | grep -c '^GH_TOKEN=')"
exit 0
"""


def parse_kv(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        if "=" in line and not line.startswith("---") and not line.startswith("rc="):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "runtime-gh-shim.json"))
    p.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "results" / "gh_shim_verification.json"))
    p.add_argument("--keep-session", action="store_true")
    a = p.parse_args()
    cfg = json.loads(Path(a.config).read_text())

    record = {"verified_at": datetime.now(timezone.utc).isoformat(), "runtime_arn": cfg["runtimeArn"], "region": cfg["region"]}
    with RuntimeSession(cfg["runtimeArn"], cfg["region"], keep_session=a.keep_session, read_timeout=300) as s:
        record["session_id"] = s.session_id

        # SigV4 inbound auth: the workload access token is injected only when runtimeUserId is set.
        invoke_kwargs = dict(agentRuntimeArn=cfg["runtimeArn"], runtimeSessionId=s.session_id, qualifier="DEFAULT",
                             contentType="application/json", accept="application/json", runtimeUserId=USER_ID)
        # 1) agent-process evidence (warm_up activates the session; second call reads the full body)
        record["warm_up_status"] = s.warm_up({"verify": True}, user_id=USER_ID)["api_status"]
        resp = s.client.invoke_agent_runtime(payload=json.dumps({"verify": True}).encode(), **invoke_kwargs)
        record["agent_verify"] = json.loads(resp["response"].read().decode()).get("verify")

        # 2) shell evidence next to the agent process
        shell = s.run_script(SHELL_EVIDENCE, timeout=120, require_success=True)
        record["shell_stdout"] = shell["stdout"]
        record["shell"] = parse_kv(shell["stdout"])

        # 3) model-driven turn
        resp = s.client.invoke_agent_runtime(
            payload=json.dumps({"prompt": "Who am I on GitHub? Use gh and reply with just the login."}).encode(), **invoke_kwargs)
        record["agent_turn"] = json.loads(resp["response"].read().decode())
    record["session_cleanup"] = s.stop_result

    av, sh = record["agent_verify"], record["shell"]
    checks = {
        "gh resolves to shim (agent)": av["which_gh"] == "/opt/shim/gh",
        "gh resolves to shim (shell)": sh.get("which_gh") == "/opt/shim/gh",
        "agent process env has no GH_TOKEN": (av["agent_env_has_GH_TOKEN"] is False and bool(sh.get("agent_pid"))
                                              and sh.get("agent_environ_GH_TOKEN_count") == "0"),
        "workload token handed over as 0600 tmpfs file": av["workload_token_handed_over"] is True and sh.get("workload_token_file", "").startswith("600"),
        "no gh hosts.yml / .git-credentials / token on disk": (not av["gh_hosts_yml_exists"] and sh.get("home_gh_hosts_yml") == "absent"
                                                               and sh.get("git_credentials_file") == "absent" and not sh.get("grep_token_on_disk")),
        "shim injects GH_TOKEN into child only": "GH_TOKEN_PRESENT_IN_CHILD" in av["shim_injects_token_into_child"]["stdout"]
                                                 and "GH_TOKEN_PRESENT_IN_CHILD" in record["shell_stdout"],
        "real GitHub call succeeds through shim": av["gh_api_user"]["rc"] == 0 and bool(av["gh_api_user"]["stdout"].strip()),
        "gh auth login blocked": av["gh_auth_login_blocked"]["rc"] == 126,
        "disallowed subcommand blocked": av["gh_disallowed_subcommand_blocked"]["rc"] == 126,
        "model-driven turn used gh and env stayed clean": record["agent_turn"].get("tool_calls", 0) >= 1
                                                          and record["agent_turn"].get("agent_env_has_GH_TOKEN") is False,
        "session stopped": bool(record["session_cleanup"] and record["session_cleanup"].get("success")),
    }
    record["checks"] = checks
    record["passed"] = sum(checks.values())
    record["total"] = len(checks)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{record['passed']}/{record['total']} passed; github login = {av['gh_api_user']['stdout'].strip()!r}; saved {a.out}")
    return 0 if record["passed"] == record["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
