"""Orchestrator: VPC Lambda (all AWS calls via the ops VPCEs) + Step Functions state machine."""
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import (BATCH_TABLE, EXTERNAL_ID, LAMBDA_NAME, ONCALL_ROLE, ORCH_ROLE, PATCH_GROUP, SFN_NAME,  # noqa: E402
                           SFN_ROLE, SPOKE_PROFILES, SPOKE_ROLE, TAGS, load_state, log, role_arn, save_state, session)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "orchestrator"


def package() -> bytes:
    with tempfile.TemporaryDirectory() as d:  # bundle a boto3 that knows bedrock-agentcore
        subprocess.run(["uv", "pip", "install", "-q", "--python", sys.executable, "--target", d, "boto3==1.43.123"], check=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in pathlib.Path(d).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, p.relative_to(d))
            z.write(ROOT / "handler.py", "handler.py")
        return buf.getvalue()


def main():
    st = load_state()
    lam = session().client("lambda")
    env = {"Variables": {"RUNTIME_ARN": st["runtime_arn"], "SPOKE_ACCOUNTS": ",".join(SPOKE_PROFILES), "SPOKE_ROLE": SPOKE_ROLE,
                         "EXTERNAL_ID": EXTERNAL_ID, "PATCH_GROUP": PATCH_GROUP, "BATCH_TABLE": BATCH_TABLE,
                         "ONCALL_ROLE_ARN": role_arn(ONCALL_ROLE)}}
    code = package()
    cfg = dict(Role=role_arn(ORCH_ROLE), Handler="handler.lambda_handler", Runtime="python3.12", Timeout=900, MemorySize=512,
               Environment=env,
               VpcConfig={"SubnetIds": st["private_subnets"], "SecurityGroupIds": [st["agent_sg"]]})
    try:
        lam.create_function(FunctionName=LAMBDA_NAME, Code={"ZipFile": code}, Architectures=["arm64"], Tags={t["Key"]: t["Value"] for t in TAGS}, **cfg)
        log("created lambda")
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
        lam.update_function_configuration(FunctionName=LAMBDA_NAME, **cfg)
        log("updated lambda")
    lam.get_waiter("function_active_v2").wait(FunctionName=LAMBDA_NAME)
    lam.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
    fn_arn = lam.get_function(FunctionName=LAMBDA_NAME)["Configuration"]["FunctionArn"]

    sfn = session().client("stepfunctions")
    definition = (ROOT / "statemachine.json").read_text().replace("${LAMBDA}", fn_arn)
    json.loads(definition)
    if st.get("sfn_arn"):
        sfn.update_state_machine(stateMachineArn=st["sfn_arn"], definition=definition, roleArn=role_arn(SFN_ROLE))
        log("updated state machine")
    else:
        for i in range(10):
            try:
                arn = sfn.create_state_machine(name=SFN_NAME, definition=definition, roleArn=role_arn(SFN_ROLE), type="STANDARD",
                                               tags=[{"key": t["Key"], "value": t["Value"]} for t in TAGS])["stateMachineArn"]
                break
            except sfn.exceptions.InvalidArn:
                raise
            except Exception as e:  # role propagation
                if "role" not in str(e).lower() or i == 9:
                    raise
                time.sleep(6)
        save_state(sfn_arn=arn)
        log("created state machine", arn)
    save_state(lambda_arn=fn_arn)


if __name__ == "__main__":
    main()
