"""Store the Exa API key in AgentCore Identity (API key credential provider).

Reads the key from stdin so it never lands in a file or in argv:
    printf %s "$EXA_KEY" | .venv/bin/python store_exa_key.py
Writes only the provider ARN to state.json.
"""

import json
import sys
from pathlib import Path

import boto3

REGION = "us-east-1"
NAME = "ic-reroute-poc-exa"
STATE = Path(__file__).parent / "state.json"

key = sys.stdin.read().strip()
if not key:
    sys.exit("no key on stdin")

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
try:
    r = ctl.create_api_key_credential_provider(name=NAME, apiKey=key)
except ctl.exceptions.ConflictException:
    r = ctl.update_api_key_credential_provider(name=NAME, apiKey=key)

state = json.loads(STATE.read_text()) if STATE.exists() else {}
state["exa_provider_arn"] = r["credentialProviderArn"]
STATE.write_text(json.dumps(state, indent=2))
print("stored:", r["credentialProviderArn"])
