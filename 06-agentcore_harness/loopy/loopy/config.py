import os
from dataclasses import dataclass, field
from typing import Any, Optional

from loopy.util.constants import (
    CUSTOMER_CONTAINER_URI_ENV_VAR,
    FILESYSTEM_MOUNT_PATHS_ENV_VAR,
    MEMORY_ACTOR_ID_ENV_VAR,
    MEMORY_ARN_ENV_VAR,
    REGION_ENV_VAR,
    STAGE_ENV_VAR,
    TRUNCATION_MESSAGES_COUNT_ENV_VAR,
    TRUNCATION_PRESERVE_RECENT_MESSAGES_COUNT_ENV_VAR,
    TRUNCATION_STRATEGY_ENV_VAR,
    TRUNCATION_SUMMARY_RATIO_ENV_VAR,
    TRUNCATION_SUMMARIZATION_SYSTEM_PROMPT_ENV_VAR,
    TruncationStrategy,
)


@dataclass
class EnvConfig:
    region: str = "us-west-2"
    stage: Optional[str] = None
    container_uri: Optional[str] = None
    filesystem_mount_paths: list[str] = field(default_factory=list)
    memory_arn: Optional[str] = None
    memory_actor_id: Optional[str] = None
    truncation_strategy: Optional[TruncationStrategy] = None
    truncation_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "EnvConfig":
        strategy_str = os.environ.get(TRUNCATION_STRATEGY_ENV_VAR)
        strategy = TruncationStrategy(strategy_str) if strategy_str else None
        truncation_config: dict[str, Any] = {}
        if strategy == TruncationStrategy.SLIDING_WINDOW:
            messages_count = os.environ.get(TRUNCATION_MESSAGES_COUNT_ENV_VAR)
            if messages_count:
                truncation_config["window_size"] = int(messages_count)
        elif strategy == TruncationStrategy.SUMMARIZATION:
            summary_ratio = os.environ.get(TRUNCATION_SUMMARY_RATIO_ENV_VAR)
            if summary_ratio:
                truncation_config["summary_ratio"] = float(summary_ratio)
            preserve_recent = os.environ.get(TRUNCATION_PRESERVE_RECENT_MESSAGES_COUNT_ENV_VAR)
            if preserve_recent:
                truncation_config["preserve_recent_messages"] = int(preserve_recent)
            system_prompt = os.environ.get(TRUNCATION_SUMMARIZATION_SYSTEM_PROMPT_ENV_VAR)
            if system_prompt:
                truncation_config["summarization_system_prompt"] = system_prompt

        mount_paths_raw = os.environ.get(FILESYSTEM_MOUNT_PATHS_ENV_VAR, "")
        filesystem_mount_paths = [p.strip() for p in mount_paths_raw.split(",") if p.strip()]

        return cls(
            region=os.environ.get(REGION_ENV_VAR, "us-west-2"),
            stage=os.environ.get(STAGE_ENV_VAR),
            container_uri=os.environ.get(CUSTOMER_CONTAINER_URI_ENV_VAR),
            filesystem_mount_paths=filesystem_mount_paths,
            memory_arn=os.environ.get(MEMORY_ARN_ENV_VAR),
            memory_actor_id=os.environ.get(MEMORY_ACTOR_ID_ENV_VAR),
            truncation_strategy=strategy,
            truncation_config=truncation_config,
        )
