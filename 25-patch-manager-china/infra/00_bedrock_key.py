"""Model access for a China-region Runtime: Bedrock API key from a global account, stored in AgentCore Identity.

Bedrock has no endpoint in cn-north-1 / cn-northwest-1, so the agent calls global Bedrock (us-west-2) over NAT.
  1. Global account: IAM user limited to the chat model + the Titan V2 embedding model, 30-day Bedrock API key.
  2. Ops account (China): AgentCore Identity API-key credential provider holds the key (token vault).
     The Runtime fetches it at request time with its workload identity -- no key in env vars or images.
The key is never written to disk.
"""
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import (API_KEY_PROVIDER, EMBED_MODEL_ID, EXTRACT_MODEL_ID, GLOBAL_PROFILE, MODEL_ID, MODEL_REGION, PREFIX,  # noqa: E402
                           log, save_state, session)

USER = f"{PREFIX}-cn-bedrock-invoker"
CANDIDATE_MODELS: list[str] = []  # extra models to allow for probing


def main():
    g = session(GLOBAL_PROFILE, MODEL_REGION)
    iam = g.client("iam")
    acct = g.client("sts").get_caller_identity()["Account"]
    profiles = list(dict.fromkeys([MODEL_ID, EXTRACT_MODEL_ID, EMBED_MODEL_ID] + CANDIDATE_MODELS))
    models = [m.split(".", 1)[1] if m.split(".")[0] in ("global", "us") else m for m in profiles]
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "bedrock:CallWithBearerToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:{MODEL_REGION}:{acct}:inference-profile/{m}" for m in profiles]
         + [f"arn:aws:bedrock:*::foundation-model/{m}*" for m in models]
         + [f"arn:aws:bedrock:::foundation-model/{m}*" for m in models]}]}
    try:
        iam.create_user(UserName=USER, Tags=[{"Key": "Project", "Value": PREFIX}])
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    iam.put_user_policy(UserName=USER, PolicyName="invoke-models", PolicyDocument=json.dumps(policy))
    if "--policy-only" in sys.argv:
        log("policy updated"); return
    for c in iam.list_service_specific_credentials(UserName=USER, ServiceName="bedrock.amazonaws.com")["ServiceSpecificCredentials"]:
        iam.delete_service_specific_credential(UserName=USER, ServiceSpecificCredentialId=c["ServiceSpecificCredentialId"])
    cred = iam.create_service_specific_credential(UserName=USER, ServiceName="bedrock.amazonaws.com", CredentialAgeDays=30)
    key = cred["ServiceSpecificCredential"]["ServiceCredentialSecret"]
    log("global bedrock key created for", USER, "expires in 30 days")

    ac = session().client("bedrock-agentcore-control")
    try:
        r = ac.create_api_key_credential_provider(name=API_KEY_PROVIDER, apiKey=key)
    except ac.exceptions.ValidationException as e:
        if "already exists" not in str(e):
            raise
        r = ac.update_api_key_credential_provider(name=API_KEY_PROVIDER, apiKey=key)
    except Exception as e:  # ConflictException on some SDK versions
        if "exist" not in str(e).lower():
            raise
        r = ac.update_api_key_credential_provider(name=API_KEY_PROVIDER, apiKey=key)
    log("AgentCore Identity provider", r["credentialProviderArn"])
    save_state(api_key_provider_arn=r["credentialProviderArn"], api_key_secret_arn=r["apiKeySecretArn"]["secretArn"],
               global_account=acct, global_iam_user=USER)

    # smoke test from here (global internet) -- the Runtime does the same call from China over NAT
    import time
    from strands.models.bedrock import BedrockModel
    from strands import Agent
    for i in range(6):
        try:
            out = Agent(model=BedrockModel(model_id=MODEL_ID, region_name=MODEL_REGION, api_key=key), callback_handler=None)("reply with OK")
            log("model smoke test:", str(out).strip()[:40])
            break
        except Exception as e:  # new IAM user / key propagation
            log("retry model call:", type(e).__name__, str(e)[:120])
            time.sleep(10)


if __name__ == "__main__":
    main()
