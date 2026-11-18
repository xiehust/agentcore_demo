#!/bin/bash
# Obtain only the NEW parent's restricted, temporary role credentials. Never print them.
set -euo pipefail
cd /opt/dsh-nitro-benchmark
CREDENTIALS_FILE=$(mktemp /run/dsh-nitro-creds.XXXXXX)
export CREDENTIALS_FILE
trap 'rm -f -- "$CREDENTIALS_FILE"' EXIT
python3 -c '
import json, os, urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
base="http://169.254.169.254/latest/"
req=urllib.request.Request(base+"api/token",method="PUT",headers={"X-aws-ec2-metadata-token-ttl-seconds":"300"})
with opener.open(req,timeout=5) as r: token=r.read().decode()
headers={"X-aws-ec2-metadata-token":token}
with opener.open(urllib.request.Request(base+"meta-data/iam/security-credentials/",headers=headers),timeout=5) as r: role=r.read().decode().strip()
assert role == "dsh-nitro-bench-20261117", "unexpected parent role"
with opener.open(urllib.request.Request(base+"meta-data/iam/security-credentials/"+role,headers=headers),timeout=5) as r: credentials=json.load(r)
assert credentials.get("Code") == "Success", "metadata credentials not ready"
# IMDS names this field Token; AWS CLI process JSON names it SessionToken.
credentials["SessionToken"] = credentials.pop("Token")
with open(os.environ["CREDENTIALS_FILE"],"w") as f: json.dump(credentials,f)
'
python3 benchmark.py --workload agent --direct-mode \
  --credentials-file "$CREDENTIALS_FILE" --region us-east-1 --model deepseek.v3.2 \
  --rounds 20 --continue-on-answer-mismatch --out results/agent-direct-batch2
