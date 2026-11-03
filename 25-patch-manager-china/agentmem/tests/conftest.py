import pytest

from agentmem import Memory, MemoryConfig, Profile
from agentmem.testing import HashEmbedder, InMemoryStore

PROFILE = Profile(name="t", agent_id="a", signal_extra="repomd|lock", generic_extra=(r"\bpatchlinux\b",))


class ScriptedLLM:
    """Returns queued responses per schema name; records prompts."""

    def __init__(self, **by_schema):
        self.by_schema = {k: list(v) for k, v in by_schema.items()}
        self.calls = []

    def __call__(self, system, prompt, schema):
        self.calls.append((schema.__name__, system, prompt))
        return self.by_schema[schema.__name__].pop(0)


@pytest.fixture
def mem():
    cfg = MemoryConfig(kb_gate=0.5, refl_gate=0.3, related_gate=0.3, refl_related_gate=0.2, refl_dup_gate=0.9)
    return Memory(cfg, PROFILE, store=InMemoryStore(), embedder=HashEmbedder())


REPO = {"signature": "repo_unreachable", "symptom": "dnf cannot download repomd.xml from a mirror",
        "root_cause": "third-party repo unreachable", "fix": "PatchRemediate-DisableRepo",
        "keywords": ["repomd.xml download failed", "Cannot download repomd.xml"]}
LOCK = {"signature": "rpm_lock_held", "symptom": "rpm database lock held by another process",
        "root_cause": "stale lock", "fix": "PatchRemediate-ReleaseRpmLock", "keywords": ["waiting for transaction lock on /var/lib/rpm/.rpm.lock"]}
