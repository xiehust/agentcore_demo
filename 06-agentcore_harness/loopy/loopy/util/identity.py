"""Shared workload access token retrieval and identity client factory."""

import os
from typing import Optional

import boto3

from bedrock_agentcore.runtime import BedrockAgentCoreContext
from bedrock_agentcore.services.identity import IdentityClient

_WORKLOAD_ACCESS_TOKEN_ENV_VAR = "AWS_WORKLOAD_ACCESS_TOKEN"

_PRE_PROD_STAGES = {"alpha", "beta", "gamma", "personal"}
_GAMMA_DP_TEMPLATE = "https://gamma.{region}.elcapdp.genesis-primitives.aws.dev"
_GAMMA_CP_TEMPLATE = "https://gamma.{region}.elcapcp.genesis-primitives.aws.dev"


def get_identity_endpoint_overrides(region: str, stage: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (dp_endpoint, cp_endpoint) for the given stage and region.

    Pre-prod runtimes (alpha, beta, gamma) are provisioned with workload
    identities from the gamma Identity service. API key retrieval and OAuth
    token calls must therefore target the gamma endpoints so the workload
    access token is recognized. The SDK's default endpoint resolution only
    handles prod, so we override explicitly for pre-prod stages.

    Returns (None, None) for prod — the SDK defaults are correct.
    """
    if stage in _PRE_PROD_STAGES:
        return (
            _GAMMA_DP_TEMPLATE.format(region=region),
            _GAMMA_CP_TEMPLATE.format(region=region),
        )
    return (None, None)


def create_identity_client(region: str, stage: Optional[str] = None) -> IdentityClient:
    """Create an IdentityClient with stage-aware endpoints."""
    dp_endpoint, cp_endpoint = get_identity_endpoint_overrides(region, stage)
    client = IdentityClient(region)
    if dp_endpoint:
        client.dp_client = boto3.client("bedrock-agentcore", region_name=region, endpoint_url=dp_endpoint)
    if cp_endpoint:
        client.cp_client = boto3.client("bedrock-agentcore-control", region_name=region, endpoint_url=cp_endpoint)
    return client


def get_workload_access_token() -> Optional[str]:
    """Get workload access token from env var (local dev) or Runtime context (production).

    In production, Runtime sets up the workload identity automatically and
    BedrockAgentCoreContext provides the token. For local testing, set
    AWS_WORKLOAD_ACCESS_TOKEN to a token obtained via the Identity SDK/CLI.
    """
    token = os.environ.get(_WORKLOAD_ACCESS_TOKEN_ENV_VAR)
    if token:
        return token
    return BedrockAgentCoreContext.get_workload_access_token()
