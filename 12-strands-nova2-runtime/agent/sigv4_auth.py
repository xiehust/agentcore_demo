"""
httpx.Auth that SigV4-signs every request.

Needed because AgentCore Gateway can be created with `authorizerType=AWS_IAM`,
which expects SigV4 rather than a bearer token. A static `headers={...}` won't do:
the signature covers the request body and a timestamp, so it must be recomputed
per request. MCP's `streamablehttp_client` accepts `auth=`, which is exactly the
hook for that.
"""

import boto3
import httpx
from botocore.auth import SigV4Auth as _BotoSigV4
from botocore.awsrequest import AWSRequest


class SigV4HttpxAuth(httpx.Auth):
    # Tells httpx to materialise the body before calling auth_flow, so the
    # payload hash we sign matches what actually goes on the wire.
    requires_request_body = True

    def __init__(self, service: str, region: str, session: boto3.Session | None = None):
        self._service = service
        self._region = region
        self._session = session or boto3.Session()

    def auth_flow(self, request: httpx.Request):
        # Credentials are re-read per request so container role rotation works.
        creds = self._session.get_credentials().get_frozen_credentials()

        # Sign a minimal header set. SigV4 only requires that the headers it
        # signed are transmitted unchanged; extra unsigned headers that httpx
        # adds later (accept, mcp-session-id, content-length) are fine.
        signable = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"Content-Type": request.headers.get(
                "content-type", "application/json")},
        )
        _BotoSigV4(creds, self._service, self._region).add_auth(signable)

        for key, value in signable.headers.items():
            request.headers[key] = value
        yield request
