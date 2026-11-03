"""Long-term memory: record writes and recall. LLM decisions (what to write) live in strategies.py.

kinds (one index, AgentCore-style namespaces):
  KB          semantic strategy   symptom -> root cause -> fix; human-seeded (verified) + agent-learned (unverified)
  EPISODE     episodic strategy   one per session: situation / intent / assessment / justification / reflection
  REFLECTION  episodic reflection cross-episode insight: title / use_cases / hints / confidence
Recall = kNN cosine (absolute gate) + BM25 lexical candidates, combined score (cos + literal keyword-phrase bonus,
unverified down-weighted), relative margin to the best hit.
"""
import time
from typing import Callable

from . import records as R
from .config import MemoryConfig
from .profile import Profile
from .store import Store


class LongTermMemory:
    def __init__(self, store: Store, embed: Callable[[str], list[float]], config: MemoryConfig, profile: Profile):
        self.store, self.embed, self.cfg, self.profile = store, embed, config, profile

    # ------------------------------------------------------------ helpers
    def digest(self, text: str) -> str:
        return R.query_digest(text, self.profile)

    def filter_keywords(self, keywords: list[str], evidence: str | None = None) -> tuple[list[str], list[str]]:
        return R.filter_keywords(keywords, self.profile, evidence)

    def strip(self, text: str) -> str:
        return R.strip_injection(text, self.profile)[0]

    # ------------------------------------------------------------ recall
    def search_knowledge(self, query: str, top_k: int = 3, vec: list[float] | None = None) -> list[dict]:
        c = self.cfg
        q = self.digest(query)
        vec = vec or self.embed(q)
        ql = (query or "").lower()
        cands = {e["signature"]: (cos, e) for cos, e in self.store.knn(vec, "KB")}
        for e in self.store.bm25(q, "KB"):  # lexical candidates the vector search missed
            cands.setdefault(e["signature"], (None, e))
        scored = []
        for _sig, (cos, e) in cands.items():
            phrases = [k for k in e.get("keywords", []) if k.lower() in ql]
            if not ((cos is not None and cos >= c.kb_gate) or len(phrases) >= c.kb_phrase_hits):  # absolute relevance gate
                continue
            score = (cos or 0) + c.kb_phrase_bonus * min(len(phrases), 3)
            if not e.get("verified"):
                score *= c.unverified_weight
            scored.append((score, {**e, "cos": cos, "phrase_hits": phrases, "score": round(score, 3)}))
        # relative margin on the combined score (cosine alone cannot separate records that grew similar over time)
        top = max([sc for sc, _ in scored] or [0])
        return [e for sc, e in sorted(scored, key=lambda x: -x[0]) if sc >= top - c.kb_margin][:top_k]

    def search_reflections(self, query: str, top_k: int = 3, vec: list[float] | None = None) -> list[dict]:
        c = self.cfg
        vec = vec or self.embed(self.digest(query))
        hits = [(s, r) for s, r in self.store.knn(vec, "REFLECTION", k=top_k * 3) if float(r.get("confidence", 0)) >= c.refl_min_confidence]
        top = hits[0][0] if hits else 0
        return [{**r, "cos": s} for s, r in hits if s >= c.refl_gate and s >= top - c.refl_margin][:top_k]

    def history(self, subject: str, n: int = 3) -> list[dict]:
        return self.store.find("EPISODE", {"instance_id": subject}, size=n, newest_first=True)

    def search(self, query: str) -> str:
        """Text for an on-demand memory search tool."""
        vec = self.embed(self.digest(query))
        return (R.format_knowledge(self.search_knowledge(query, vec=vec)) + "\nreflections:\n"
                + R.format_reflections(self.search_reflections(query, vec=vec)))

    def recall(self, subject: str, query: str, debug: bool = False) -> tuple[str, dict]:
        """Fenced block for the system prompt + metadata for audit / calibration."""
        vec = self.embed(self.digest(query))
        kb, refl, hist = self.search_knowledge(query, vec=vec), self.search_reflections(query, vec=vec), self.history(subject)
        body = ("## Recalled knowledge base entries\n" + R.format_knowledge(kb)
                + "\n## Reflections from earlier episodes\n" + R.format_reflections(refl)
                + f"\n## This {self.profile.subject_noun}'s earlier episodes\n" + R.format_history(hist))
        txt = "<recalled_memory>\n" + self.profile.recall_preamble + "\n" + self.strip(body) + "\n</recalled_memory>"
        meta = {"kb": [{"sig": k["signature"], "cos": k["cos"], "score": k["score"], "verified": bool(k.get("verified"))} for k in kb],
                "reflections": [{"title": r["title"], "cos": r["cos"]} for r in refl], "history": len(hist)}
        if debug:  # un-gated neighbours, for calibration
            meta["kb_all"] = [(e["signature"], s) for s, e in self.store.knn(vec, "KB")]
            meta["refl_all"] = [(r["title"][:60], s, r.get("confidence")) for s, r in self.store.knn(vec, "REFLECTION")]
        return txt, meta

    def related(self, kind: str, text: str, k: int = 3, gate: float | None = None) -> list[tuple[float, dict]]:
        gate = self.cfg.related_gate if gate is None else gate
        return [(s, e) for s, e in self.store.knn(self.embed(text), kind, k=k) if s >= gate]

    # ------------------------------------------------------------ reads
    def kb_catalog(self) -> list[dict]:
        return self.store.find("KB", fields=["signature", "symptom", "verified"])

    def get_kb(self, signature: str) -> dict | None:
        return self.store.get(f"KB#{signature}")

    def get_reflection(self, rid: str) -> dict | None:
        return self.store.get(f"REFLECTION#{rid}")

    def list_kind(self, kind: str) -> list[dict]:
        return self.store.find(kind)

    def stats(self) -> dict:
        return self.store.stats()

    # ------------------------------------------------------------ writes (every write is sanitized + re-embedded)
    def put_kb(self, doc: dict):
        doc = R.clean({**doc, "kind": "KB", "namespace": self.profile.ns_kb, "ts": int(time.time())}, self.profile)
        doc.pop("embedding", None)
        doc["embedding"] = self.embed(R.kb_text(doc))
        self.store.put(f"KB#{doc['signature']}", doc)

    def seed_knowledge(self, e: dict):
        """Human-curated record (runbook / wiki import): stored as-is, verified."""
        self.put_kb({**e, "verified": bool(e.get("verified", True)), "confirmations": 0, "aliases": []})

    def put_episode(self, session_id: str, doc: dict):
        doc = R.clean({**doc, "kind": "EPISODE", "namespace": self.profile.ns_episode(doc["instance_id"]), "session_id": session_id,
                       "ts": int(doc.get("ts") or time.time())}, self.profile)
        doc["embedding"] = self.embed(R.episode_text(doc))
        self.store.put(f"EPISODE#{session_id}", doc)

    def put_reflection(self, rid: str, doc: dict):
        doc = R.clean({**doc, "kind": "REFLECTION", "namespace": self.profile.ns_reflection, "id": rid, "ts": int(time.time())}, self.profile)
        doc.pop("embedding", None)
        doc["embedding"] = self.embed(R.reflection_text(doc))
        self.store.put(f"REFLECTION#{rid}", doc)

    def delete(self, doc_id: str):
        self.store.delete(doc_id)

    def verify_knowledge(self, signature: str, reviewer: str) -> bool:
        e = self.get_kb(signature)
        if not e:
            return False
        e.update(verified=True, verified_by=reviewer)
        self.put_kb(e)
        return True

    def reset(self):
        self.store.ensure_index(reset=True)
