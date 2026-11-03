"""agentmem -- self-built agent memory for regions without AgentCore Memory (e.g. AWS China regions).

  STM  Strands SessionRepository on DynamoDB                          agentmem.stm
  LTM  OpenSearch kNN + BM25, Bedrock embeddings, AgentCore-style      agentmem.ltm / store / embeddings
       namespaces; semantic + episodic strategies with code-enforced
       hard rules                                                       agentmem.strategies
Everything domain-specific (prompt wording, keyword rules, learning policy) is a Profile supplied by the agent.
"""
from .config import MemoryConfig
from .embeddings import BedrockEmbedder
from .ltm import LongTermMemory
from .memory import Memory
from .profile import BASE_GENERIC, BASE_INJECTION, BASE_SIGNAL, Profile
from .records import filter_keywords, format_knowledge, format_reflections, query_digest
from .store import OpenSearchStore
from .strategies import KnowledgeCandidate, MemoryStrategies, StructuredLLM

__all__ = ["Memory", "MemoryConfig", "Profile", "LongTermMemory", "MemoryStrategies", "KnowledgeCandidate", "StructuredLLM",
           "OpenSearchStore", "BedrockEmbedder", "query_digest", "filter_keywords", "format_knowledge", "format_reflections",
           "BASE_SIGNAL", "BASE_GENERIC", "BASE_INJECTION"]
__version__ = "0.1.0"
