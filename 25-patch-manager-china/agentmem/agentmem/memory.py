"""Facade: one object the agent holds. Wires config + profile -> store, embedder, LTM, STM, strategies."""
from .config import MemoryConfig
from .embeddings import BedrockEmbedder
from .ltm import LongTermMemory
from .profile import Profile
from .store import OpenSearchStore
from .strategies import MemoryStrategies, StructuredLLM, dedupe_reflections, refilter_keywords


class Memory:
    def __init__(self, config: MemoryConfig, profile: Profile, *, store=None, embedder=None, stm_repo=None, api_key_fn=None):
        """api_key_fn: Bedrock API key for embeddings (cross-partition); None = SigV4 with the default credentials."""
        self.config, self.profile = config, profile
        self.embedder = embedder or BedrockEmbedder(config.embed_model_id, config.embed_dim, config.model_region, api_key_fn=api_key_fn)
        self.store = store or OpenSearchStore(config.os_endpoint, config.os_index, config.region, config.embed_dim)
        self.ltm = LongTermMemory(self.store, self.embedder, config, profile)
        self._stm_repo = stm_repo
        self._stm = None

    # ---- short-term (optional: needs strands-agents and a table)
    @property
    def stm(self):
        if self._stm is None:
            from .stm import DynamoDBSessionRepository, ShortTermMemory
            if self._stm_repo is None and not self.config.stm_table:
                raise ValueError("MemoryConfig.stm_table (env MEMORY_TABLE) is required for short-term memory")
            repo = self._stm_repo or DynamoDBSessionRepository(self.config.stm_table, self.config.region, self.config.stm_ttl_days)
            self._stm = ShortTermMemory(repo, self.profile.agent_id, self.profile.evidence_tools)
        return self._stm

    @property
    def session_repository(self):
        """Pass to strands RepositorySessionManager(session_repository=...)."""
        return self.stm.repo

    # ---- long-term
    def recall(self, subject: str, query: str, debug: bool = False) -> tuple[str, dict]:
        return self.ltm.recall(subject, query, debug)

    def search(self, query: str) -> str:
        return self.ltm.search(query)

    def strategies(self, llm: StructuredLLM) -> MemoryStrategies:
        return MemoryStrategies(self.ltm, llm)

    def process_session(self, llm: StructuredLLM, *, session_id: str, subject: str, batch_id: str, result: dict) -> dict:
        """Post-session pipeline (what AgentCore Memory strategies do asynchronously): STM -> episode, reflections, KB."""
        return self.strategies(llm).process_session(session_id=session_id, subject=subject, batch_id=batch_id, result=result,
                                                    transcript=self.stm.transcript(session_id), evidence=self.stm.evidence(session_id))

    # ---- maintenance
    def refilter_keywords(self) -> list[str]:
        return refilter_keywords(self.ltm)

    def dedupe_reflections(self, gate: float | None = None) -> list[str]:
        return dedupe_reflections(self.ltm, gate)

    def admin(self, payload: dict, allow_reset: bool = False) -> dict | None:
        """Generic maintenance ops; returns None for ops the caller must handle itself."""
        op, m = payload.get("op"), self.ltm
        if op == "reset":  # destructive: needs the caller's opt-in AND an explicit confirmation string
            if not allow_reset or payload.get("confirm") != f"reset-{self.profile.name}-ltm":
                return {"error": "reset disabled"}
            m.reset()
            return {"reset": True}
        if op == "seed":
            for e in payload["items"]:
                m.seed_knowledge(e)
            return {"seeded": [e["signature"] for e in payload["items"]], "stats": m.stats()}
        if op == "list":
            return {"stats": m.stats(), **{k.lower(): m.list_kind(k) for k in ("KB", "REFLECTION", "EPISODE")}}
        if op == "search":
            txt, meta = m.recall(payload.get("subject") or payload.get("instanceId", "-"), payload["query"], debug=True)
            return {"meta": meta, "block": txt}
        if op == "dedupe_reflections":
            return {"ops": self.dedupe_reflections(float(payload["gate"]) if payload.get("gate") is not None else None)}
        if op == "refilter_keywords":
            return {"ops": self.refilter_keywords()}
        if op == "verify":
            return {"verified": m.verify_knowledge(payload["signature"], payload.get("reviewer", "unknown"))}
        return None
