#!/usr/bin/env bash
# `gh` shim for the AgentCore sandbox: every gh invocation gets a fresh short-lived token,
# injected ONLY into the gh child process environment. Nothing is written to
# ~/.config/gh/hosts.yml, the agent process environment stays token-free, and the
# token dies with the gh process.
#
# Install (in the container image or at session start):
#   mkdir -p /opt/shim && cp gh_wrapper.sh /opt/shim/gh && chmod 755 /opt/shim/gh
#   export PATH=/opt/shim:$PATH          # agent / shell tools now call the shim
#   export GH_REAL=/usr/bin/gh           # real binary (default)
#   export TOKEN_SOURCE=broker BROKER_FUNCTION_ARN=arn:aws:lambda:...   # or TOKEN_SOURCE=identity ...
#   (token sources are resolved by git_askpass.py; see docs/02 §3.1)
#
# Inside AgentCore Runtime with TOKEN_SOURCE=identity, the agent must hand over the per-request
# workload access token to /dev/shm/agentcore/workload_token (0600) — see deploy/gh_shim_agent/agent.py —
# because the Runtime's own workload identity cannot be self-served via GetWorkloadAccessToken.
# Verified live (11/11): results/gh_shim_verification.json.
#
# Optional allowlist of first-level subcommands, e.g. GH_ALLOWED_SUBCOMMANDS="pr issue api repo search":
# blocks `gh auth ...` (would try to persist credentials) and anything else not listed.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REAL_GH="${GH_REAL:-/usr/bin/gh}"
if [[ ! -x "$REAL_GH" || "$REAL_GH" == "$(readlink -f "$0")" ]]; then
  echo "gh shim: real gh binary not found at $REAL_GH (set GH_REAL)" >&2; exit 127
fi

if [[ -n "${GH_ALLOWED_SUBCOMMANDS:-}" && $# -gt 0 ]]; then
  case " ${GH_ALLOWED_SUBCOMMANDS} " in
    *" $1 "*) ;;
    *) echo "gh shim: subcommand '$1' is not allowed by policy" >&2; exit 126 ;;
  esac
fi
if [[ "${1:-}" == "auth" ]]; then
  echo "gh shim: 'gh auth' is disabled; tokens are injected per invocation" >&2; exit 126
fi

# Fresh token each call (Identity caches server-side; broker tokens are 1h). Never echoed.
TOKEN="$(python3 "$HERE/git_askpass.py" --token)"
[[ -n "$TOKEN" ]] || { echo "gh shim: empty token from token source" >&2; exit 1; }

# Keep gh's own config on tmpfs so nothing about this session survives the microVM.
export GH_CONFIG_DIR="${GH_CONFIG_DIR:-/dev/shm/gh-config}"
mkdir -p "$GH_CONFIG_DIR" && chmod 700 "$GH_CONFIG_DIR"

# exec replaces this shell: the token exists only in the gh process environment.
exec env GH_TOKEN="$TOKEN" GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 "$REAL_GH" "$@"
