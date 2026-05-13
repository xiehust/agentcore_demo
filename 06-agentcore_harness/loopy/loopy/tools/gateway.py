"""AgentCore Gateway tool integration — resolves Gateway ARN into an authenticated MCP client.

Gateway supports three inbound auth types (configured per-Gateway by the customer):
  - AWS_IAM: We SigV4-sign requests using the execution role credentials (default)
  - CUSTOM_JWT: We obtain an OAuth token via AgentCore Identity and pass it as Bearer
  - NONE: No auth headers

The customer specifies which auth to use via the `outboundAuth` field on HarnessAgentCoreGatewayConfig.
If omitted, defaults to AWS_IAM (SigV4).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time

import botocore.auth
import botocore.session
import httpx
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

from bedrock_agentcore.services.identity import IdentityClient

from loopy.api_model.generated import HarnessAgentCoreGatewayConfig, OAuthCredentialProvider
from loopy.api_model.generated import HarnessGatewayOutboundAuth1 as AwsIamOutboundAuth
from loopy.api_model.generated import HarnessGatewayOutboundAuth2 as NoneOutboundAuth
from loopy.api_model.generated import HarnessGatewayOutboundAuth3 as OAuthOutboundAuth
from loopy.util.arn import region_from_arn, resource_id_from_arn
from loopy.util.identity import get_workload_access_token

logger = logging.getLogger(__name__)
_TOKEN_TTL_SECONDS = 120  # Conservative TTL — Identity SDK doesn't expose token expiry metadata
_GATEWAY_URL_TEMPLATE = "https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"


_default_workload_token_provider = get_workload_access_token


def _run_async(coro):
    """Run an async coroutine from sync code, handling both async and sync contexts.

    Needed because httpx.Auth.auth_flow() is a sync generator, but IdentityClient.get_token()
    is async.

    - If there's a running event loop (production/uvicorn): can't call asyncio.run() directly,
      so offload to a thread that gets its own loop.
    - If there's no running loop (tests, scripts): asyncio.run() works directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — safe to use asyncio.run directly
        return asyncio.run(coro)
    # Running inside an async context (e.g., uvicorn) — run in a separate thread
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result()


class SigV4Auth(httpx.Auth):
    """httpx Auth that SigV4-signs every request using the default credential chain.

    The execution role credentials are available in the container via env vars / instance profile.
    The role needs bedrock-agentcore:InvokeGateway permission on the Gateway resource.
    """

    def __init__(self, region: str, service: str = "bedrock-agentcore"):
        session = botocore.session.get_session()
        self._credentials = session.get_credentials()
        self._signer = botocore.auth.SigV4Auth(self._credentials, service, region)

    def auth_flow(self, request: httpx.Request):
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            data=request.content,
        )
        self._signer.add_auth(aws_request)
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        yield request


class OAuthBearerAuth(httpx.Auth):
    """httpx Auth that injects a Bearer token obtained from AgentCore Identity.

    Constructor accepts optional workload_token_provider for testing.
    """

    def __init__(self, region: str, oauth_config: OAuthCredentialProvider, identity_client: IdentityClient, workload_token_provider=None):
        self._client = identity_client
        self._get_workload_token = workload_token_provider or _default_workload_token_provider
        self._provider_id = resource_id_from_arn(oauth_config.providerArn)
        self._scopes = [s.root for s in oauth_config.scopes]
        # Map Smithy enum values to what IdentityClient.get_token() expects
        self._flow = "M2M" if oauth_config.grantType is None or oauth_config.grantType.value == "CLIENT_CREDENTIALS" else "USER_FEDERATION"
        self._custom_parameters = dict(oauth_config.customParameters.root) if oauth_config.customParameters and oauth_config.customParameters.root else None
        self._cached_token: str | None = None
        self._token_fetched_at: float = 0

    def _get_token(self) -> str:
        if self._cached_token and (time.monotonic() - self._token_fetched_at) < _TOKEN_TTL_SECONDS:
            return self._cached_token

        workload_token = self._get_workload_token()
        if workload_token is None:
            raise RuntimeError("Workload access token not available — is the agent running in AgentCore Runtime?")

        async def _fetch():
            return await self._client.get_token(
                provider_name=self._provider_id,
                agent_identity_token=workload_token,
                scopes=self._scopes,
                auth_flow=self._flow,
                custom_parameters=self._custom_parameters,
            )

        try:
            token = _run_async(_fetch())
        except Exception as e:
            raise RuntimeError(f"Failed to get OAuth token for Gateway (provider={self._provider_id}): {e}") from e

        self._cached_token = token
        self._token_fetched_at = time.monotonic()
        return token

    def auth_flow(self, request: httpx.Request):
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _gateway_url(gateway_arn: str) -> str:
    """Derive the Gateway MCP URL from the ARN."""
    region = region_from_arn(gateway_arn)
    gateway_id = resource_id_from_arn(gateway_arn)
    return _GATEWAY_URL_TEMPLATE.format(gateway_id=gateway_id, region=region)


def create_gateway_mcp_client(name: str, config: HarnessAgentCoreGatewayConfig, identity_client: IdentityClient) -> MCPClient:
    """Create a Strands MCPClient for an AgentCore Gateway."""
    url = _gateway_url(config.gatewayArn)
    region = region_from_arn(config.gatewayArn)
    outbound_auth = config.outboundAuth

    if outbound_auth is None or isinstance(outbound_auth.root, AwsIamOutboundAuth):
        # Default or explicit AWS_IAM — SigV4-sign with execution role
        auth = SigV4Auth(region)
    elif isinstance(outbound_auth.root, NoneOutboundAuth):
        # NONE — no auth headers
        auth = None
    elif isinstance(outbound_auth.root, OAuthOutboundAuth):
        # OAuth — get token via AgentCore Identity
        auth = OAuthBearerAuth(region, outbound_auth.root.oauth, identity_client=identity_client)
    else:
        raise ValueError(f"Unknown Gateway outboundAuth: {outbound_auth}")

    return MCPClient(
        lambda _url=url, _auth=auth: streamablehttp_client(url=_url, auth=_auth),
        prefix=name,
    )
