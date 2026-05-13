import logging

from strands.models.bedrock import BedrockModel
from strands.models.model import Model
from strands.models.openai import OpenAIModel

from bedrock_agentcore.services.identity import IdentityClient

from loopy.abstract import LoopyModelProvider
from loopy.model.gemini_legacy_schema import LegacySchemaGeminiModel
from loopy.api_model.generated import (
    HarnessModelConfiguration,
    HarnessModelConfiguration1 as BedrockModelConfig,
    HarnessModelConfiguration2 as OpenAiModelConfig,
    HarnessModelConfiguration3 as GeminiModelConfig,
)
from loopy.util.arn import resource_id_from_arn
from loopy.util.identity import get_workload_access_token

logger = logging.getLogger(__name__)


class ModelProvider(LoopyModelProvider):
    def __init__(self, identity_client: IdentityClient, region: str = "us-west-2") -> None:
        self._region = region
        self._identity_client = identity_client

    def fetch_api_key(self, api_key_arn: str) -> str:
        """Fetch an API key from AgentCore Identity using the workload access token."""
        provider_name = resource_id_from_arn(api_key_arn)
        token = get_workload_access_token()
        if token is None:
            raise ValueError("No workload access token was included in the request")
        return self._identity_client.dp_client.get_resource_api_key(
            resourceCredentialProviderName=provider_name,
            workloadIdentityToken=token,
        )["apiKey"]

    def resolve_model(self, model_config: HarnessModelConfiguration) -> Model:
        config = model_config.root
        match config:
            case BedrockModelConfig():
                cfg = config.bedrockModelConfig
                kwargs = {}
                if cfg.maxTokens is not None:
                    kwargs["max_tokens"] = int(cfg.maxTokens)
                if cfg.temperature is not None:
                    kwargs["temperature"] = float(cfg.temperature)
                if cfg.topP is not None:
                    kwargs["top_p"] = float(cfg.topP)
                return BedrockModel(model_id=cfg.modelId, **kwargs)
            case OpenAiModelConfig():
                cfg = config.openAiModelConfig
                api_key = self.fetch_api_key(cfg.apiKeyArn)
                params = {}
                if cfg.maxTokens is not None:
                    params["max_tokens"] = int(cfg.maxTokens)
                if cfg.temperature is not None:
                    params["temperature"] = float(cfg.temperature)
                if cfg.topP is not None:
                    params["top_p"] = float(cfg.topP)
                return OpenAIModel(client_args={"api_key": api_key}, model_id=cfg.modelId, params=params)
            case GeminiModelConfig():
                cfg = config.geminiModelConfig
                api_key = self.fetch_api_key(cfg.apiKeyArn)
                params = {}
                if cfg.maxTokens is not None:
                    params["maxOutputTokens"] = int(cfg.maxTokens)
                if cfg.temperature is not None:
                    params["temperature"] = float(cfg.temperature)
                if cfg.topP is not None:
                    params["topP"] = float(cfg.topP)
                if cfg.topK is not None:
                    params["topK"] = int(cfg.topK)
                return LegacySchemaGeminiModel(client_args={"api_key": api_key}, model_id=cfg.modelId, params=params)
        raise ValueError(f"Unknown model config type: {type(config)}")
