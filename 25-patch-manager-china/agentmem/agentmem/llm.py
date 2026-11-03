"""StructuredLLM adapters: llm(system_prompt, user_prompt, schema) -> schema instance."""
from typing import Callable


def strands_llm(model_fn: Callable):
    """model_fn() -> a Strands model (e.g. BedrockModel). A fresh Agent per call: no conversation state leaks
    between extraction steps."""
    def call(system: str, prompt: str, schema):
        from strands import Agent
        return Agent(model=model_fn(), system_prompt=system, callback_handler=None)(prompt, structured_output_model=schema).structured_output
    return call
