"""Shared names and settings for the Patch Manager x AgentCore China-region demo."""
import json
import os
import pathlib

import boto3

# Accounts / profiles are NOT in the repo: copy common/accounts.example.json to common/accounts.json (gitignored).
_ACCOUNTS_FILE = pathlib.Path(__file__).resolve().parent / "accounts.json"
if not _ACCOUNTS_FILE.exists():
    raise SystemExit(f"missing {_ACCOUNTS_FILE}: copy accounts.example.json and fill in your account ids / AWS profiles")
_ACC = json.loads(_ACCOUNTS_FILE.read_text())

REGION = _ACC.get("region", "cn-northwest-1")
PARTITION = "aws-cn" if REGION.startswith("cn-") else "aws"
PREFIX = "patchdemo"

OPS_PROFILE = _ACC["ops_profile"]
OPS_ACCOUNT = _ACC["ops_account"]
# account -> local profile. The ops account may also be a "business" (spoke) account.
SPOKE_PROFILES = _ACC["spoke_profiles"]

# Global-region account that issues the Bedrock API key (models live outside China).
GLOBAL_PROFILE = _ACC["global_profile"]
MODEL_REGION = _ACC.get("model_region", "us-west-2")
MODEL_ID = "global.moonshotai.kimi-k3"  # model used in the validation; production: the model you choose
EXTRACT_MODEL_ID = "global.moonshotai.kimi-k3"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"  # LTM vectors (1024-d, multilingual), same API key
EMBED_DIM = 1024
OS_DOMAIN = f"{PREFIX}-ltm"
OS_INDEX = "patch-ltm"
API_KEY_PROVIDER = f"{PREFIX}-global-bedrock"

EXTERNAL_ID = f"{PREFIX}-agent-dev"
SPOKE_ROLE = "PatchAgentSpokeRole"
RUNTIME_ROLE = "PatchAgentRuntimeRole"
ORCH_ROLE = "PatchOrchestratorLambdaRole"
ONCALL_ROLE = "PatchOnCallRole"  # stands in for an IAM Identity Center permission-set role
SFN_ROLE = "PatchSfnRole"
TARGET_ROLE = "PatchDemoTargetRole"

BATCH_TABLE = f"{PREFIX}-batch"
MEMORY_TABLE = f"{PREFIX}-memory"
ECR_REPO = f"{PREFIX}-agent"
RUNTIME_NAME = f"{PREFIX}_patch_agent"
LAMBDA_NAME = f"{PREFIX}-orchestrator"
SFN_NAME = f"{PREFIX}-batch"

PATCH_GROUP = PREFIX
DIAG_DOC = "PatchDiagnose-Collect"
REMEDIATE_DOCS = ["PatchRemediate-FreeDisk", "PatchRemediate-DisableRepo", "PatchRemediate-ReleaseRpmLock"]

TAGS = [{"Key": "Project", "Value": PREFIX}]

STATE_FILE = pathlib.Path(__file__).resolve().parent.parent / ".state.json"


def session(profile: str = OPS_PROFILE, region: str = REGION) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def arn(service: str, resource: str, account: str = OPS_ACCOUNT, region: str = REGION) -> str:
    return f"arn:{PARTITION}:{service}:{region}:{account}:{resource}"


def role_arn(name: str, account: str = OPS_ACCOUNT) -> str:
    return f"arn:{PARTITION}:iam::{account}:role/{name}"


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(**kv) -> dict:
    st = load_state()
    st.update(kv)
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False))
    return st


def log(*a):
    print("[%s]" % os.path.basename(os.sys.argv[0]), *a, flush=True)
