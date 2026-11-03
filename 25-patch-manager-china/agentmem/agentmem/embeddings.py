"""Embedder = any callable text -> list[float]. BedrockEmbedder covers Titan Text Embeddings V2 with either SigV4
(same partition) or a Bedrock API key (cross-partition, e.g. China-region runtime -> global Bedrock)."""
import json
from typing import Callable, Protocol

import boto3
from botocore import UNSIGNED
from botocore.config import Config


class Embedder(Protocol):
    def __call__(self, text: str) -> list[float]: ...


class BedrockEmbedder:
    def __init__(self, model_id: str, dim: int, region: str, api_key_fn: Callable[[], str] | None = None, client=None):
        self.model_id, self.dim, self.region = model_id, dim, region
        self.api_key_fn = api_key_fn
        self._client = client

    def set_api_key_fn(self, fn: Callable[[], str]):
        self.api_key_fn = fn

    @property
    def client(self):
        if self._client is None:
            if self.api_key_fn is None:  # SigV4 with the default credential chain
                self._client = boto3.client("bedrock-runtime", region_name=self.region,
                                            config=Config(read_timeout=30, retries={"max_attempts": 6, "mode": "adaptive"}))
            else:  # bearer token: unsigned request + Authorization header
                c = boto3.client("bedrock-runtime", region_name=self.region,
                                 config=Config(signature_version=UNSIGNED, read_timeout=30, retries={"max_attempts": 6, "mode": "adaptive"}))
                c.meta.events.register("before-send.bedrock-runtime.*",
                                       lambda request, **_: request.headers.__setitem__("Authorization", f"Bearer {self.api_key_fn()}"))
                self._client = c
        return self._client

    def __call__(self, text: str) -> list[float]:
        body = json.dumps({"inputText": (text or " ")[:20000], "dimensions": self.dim, "normalize": True})
        r = self.client.invoke_model(modelId=self.model_id, body=body, contentType="application/json", accept="application/json")
        return json.loads(r["body"].read())["embedding"]
