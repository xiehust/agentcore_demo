"""Pydantic utilities for the Loopy runtime."""

from typing import Any
from pydantic import SecretStr

def reveal_secrets(obj: Any) -> Any:
    """Recursively convert SecretStr values to plain strings for SDK consumption."""
    if isinstance(obj, SecretStr):
        return obj.get_secret_value()
    if isinstance(obj, dict):
        return {k: reveal_secrets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [reveal_secrets(item) for item in obj]
    return obj
