"""Infrastructure + retrieval tuning. Nothing here knows about the agent that uses the memory."""
import os
from dataclasses import dataclass, fields


@dataclass
class MemoryConfig:
    # long-term store: one OpenSearch index (VPC domain, SigV4 service "es")
    os_endpoint: str = ""
    os_index: str = "agent-ltm"
    region: str = "cn-northwest-1"            # region of the OpenSearch domain and the STM table
    # embeddings (Bedrock, may live in another partition -> API key instead of SigV4)
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dim: int = 1024
    model_region: str = "us-west-2"
    # short-term store: DynamoDB table (pk/sk/data/ttl)
    stm_table: str = ""
    stm_ttl_days: int = 30
    # retrieval gates (cosine, Titan V2 normalized). Calibrate per domain on real queries.
    kb_gate: float = 0.36             # recall a KB record when cos >= gate ...
    kb_phrase_hits: int = 2           # ... or when this many of its keywords occur literally in the query
    kb_phrase_bonus: float = 0.08     # combined score = cos + bonus * min(hits, 3)
    kb_margin: float = 0.15           # keep records whose combined score is within margin of the best
    unverified_weight: float = 0.85   # agent-learned (unverified) records are down-weighted
    refl_gate: float = 0.35
    refl_margin: float = 0.12
    refl_min_confidence: float = 0.4
    related_gate: float = 0.45        # existing records shown to the consolidation / reflection LLM
    refl_related_gate: float = 0.30
    refl_dup_gate: float = 0.85       # two reflections this close are the same pattern

    # env var -> field (only the ones a deployment typically sets)
    _ENV = {"OS_ENDPOINT": "os_endpoint", "OS_INDEX": "os_index", "AWS_REGION": "region", "EMBED_MODEL_ID": "embed_model_id",
            "EMBED_DIM": "embed_dim", "MODEL_REGION": "model_region", "MEMORY_TABLE": "stm_table", "KB_GATE": "kb_gate",
            "REFL_GATE": "refl_gate", "RELATED_GATE": "related_gate"}

    @classmethod
    def from_env(cls, env=None, **overrides) -> "MemoryConfig":
        env = os.environ if env is None else env
        types = {f.name: f.type for f in fields(cls)}
        kw = {}
        for var, name in cls._ENV.items():
            if env.get(var):
                t = types[name]
                kw[name] = (int if t in (int, "int") else float if t in (float, "float") else str)(env[var])
        return cls(**{**kw, **overrides})
