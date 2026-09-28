"""
Signed caller: forwards one JSON-RPC request to an AWS_IAM-inbound gateway.

Used by scripts/04-verify.py when the gateway's inbound auth is AWS_IAM (China
regions reject authorizerType=NONE). The request is SigV4-signed with THIS
function's execution role. The test machine uses AWS_PROFILE to invoke Lambda;
it mints the business JWTs itself and hands them over in the payload. This is a
test backend, not a public MCP proxy.

event = {"url": ..., "region": ..., "body": "<json-rpc string>",
         "headers": {...extra headers, e.g. the JWT header...},
         "signed": true}
returns {"status": int, "headers": {...}, "body": "<raw response text>"}
"""

import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

_session = boto3.Session()


def lambda_handler(event, _context):
    url, region, body = event["url"], event["region"], event["body"].encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               **(event.get("headers") or {})}
    if event.get("signed", True):
        req = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(_session.get_credentials().get_frozen_credentials(),
                  "bedrock-agentcore", region).add_auth(req)
        headers = dict(req.headers.items())

    http_req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(http_req, timeout=60) as resp:
            return {"status": resp.status, "headers": dict(resp.headers),
                    "body": resp.read().decode()}
    except urllib.error.HTTPError as err:
        return {"status": err.code, "headers": dict(err.headers),
                "body": err.read().decode()}
