import os

from aws_bedrock_token_generator import provide_token
from strands.models.openai_responses import OpenAIResponsesModel

MODEL_ID = "xai.grok-4.3"


def load_model() -> OpenAIResponsesModel:
    """Get a Bedrock Mantle model client for xai.grok-4.3 via the OpenAI Responses API.

    Grok is served through the Bedrock Mantle endpoint (NOT the Converse API), so it is invoked
    through an OpenAI-style client authenticated with a short-lived Bedrock bearer token. Pinned to
    the /openai/v1 Mantle path. Region is read from AWS_REGION (set by the AgentCore runtime).
    """
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    token = provide_token(region=region)
    base_url = f"https://bedrock-mantle.{region}.api.aws/openai/v1"
    client_args = {"api_key": token, "base_url": base_url}

    # Responses API: Mantle does not persist responses, so disable server-side storage.
    params = {"store": False}
    return OpenAIResponsesModel(client_args=client_args, model_id=MODEL_ID, params=params)
