"""Types for container request payload.

The invoke payload uses the generated InvokeHarnessRequestContent from LoopyDataPlaneServiceModel.
The command payload uses the generated InvokeAgentRuntimeCommandRequestBody.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, SecretStr, model_validator

from loopy.api_model.generated import InvokeAgentRuntimeCommandRequestBody, InvokeHarnessRequestContent


class Operation(str, Enum):
    INVOKE = "invoke"
    INVOKE_AGENT_RUNTIME_COMMAND = "invokeagentruntimecommand"


class Credentials(BaseModel):
    accessKeyId: str
    secretAccessKey: SecretStr
    sessionToken: Optional[str] = None


class MemoryRetrievalConfig(BaseModel):
    topK: Optional[int] = None
    relevanceScore: Optional[float] = None
    strategyId: Optional[str] = None


class HarnessAgentCoreMemoryConfiguration(BaseModel):
    arn: Optional[str] = None
    actorId: Optional[str] = None
    messagesCount: Optional[int] = None
    retrievalConfig: Optional[dict[str, MemoryRetrievalConfig]] = None


class HarnessMemoryConfiguration(BaseModel):
    agentCoreMemoryConfiguration: Optional[HarnessAgentCoreMemoryConfiguration] = None


class ContainerRequest(BaseModel):
    operation: Operation
    credentials: Optional[Credentials] = None
    memoryConfig: Optional[HarnessMemoryConfiguration] = None
    invokePayload: Optional[InvokeHarnessRequestContent] = None
    invokeAgentRuntimeCommandPayload: Optional[InvokeAgentRuntimeCommandRequestBody] = None
    # TODO: Remove once DP switches to invokeAgentRuntimeCommandPayload
    invokeAgentRuntimeCommandBody: Optional[InvokeAgentRuntimeCommandRequestBody] = None

    @model_validator(mode="after")
    def validate_payload_present(self):
        # Support both field names during migration — prefer Payload, fall back to Body
        if self.invokeAgentRuntimeCommandBody and not self.invokeAgentRuntimeCommandPayload:
            self.invokeAgentRuntimeCommandPayload = self.invokeAgentRuntimeCommandBody
        if self.operation == Operation.INVOKE_AGENT_RUNTIME_COMMAND and self.invokeAgentRuntimeCommandPayload is None:
            raise ValueError(
                "invokeAgentRuntimeCommandPayload is required when operation is 'invokeagentruntimecommand'"
            )
        if self.operation == Operation.INVOKE and self.invokePayload is None:
            raise ValueError("invokePayload is required when operation is 'invoke'")
        return self
