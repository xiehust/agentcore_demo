"""Q2: how fast can the orchestrator open new sessions (1 failed host = 1 session)? Runs inside the VPC Lambda.
usage: session_burst.py <count> <concurrency> [hold_seconds]"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common.config import LAMBDA_NAME, session  # noqa: E402

n, c = int(sys.argv[1]), int(sys.argv[2])
secs = int(sys.argv[3]) if len(sys.argv) > 3 else 60
r = session().client("lambda").invoke(FunctionName=LAMBDA_NAME,
                                      Payload=json.dumps({"action": "selftest", "count": n, "concurrency": c, "seconds": secs}).encode())
print(json.dumps(json.loads(r["Payload"].read()), ensure_ascii=False))
