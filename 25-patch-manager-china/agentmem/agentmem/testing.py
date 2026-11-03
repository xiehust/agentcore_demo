"""Test doubles: an in-memory Store and a deterministic bag-of-words embedder (no OpenSearch / Bedrock needed)."""
import hashlib
import math
import re


class HashEmbedder:
    """Deterministic, normalized bag-of-words vectors: similar wording -> high cosine."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def __call__(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9_]+", (text or "").lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class InMemoryStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def ensure_index(self, reset: bool = False):
        if reset:
            self.docs.clear()

    def put(self, doc_id, doc):
        self.docs[doc_id] = dict(doc)

    def get(self, doc_id):
        d = self.docs.get(doc_id)
        return {k: v for k, v in d.items() if k != "embedding"} if d else None

    def delete(self, doc_id):
        self.docs.pop(doc_id, None)

    def _match(self, d, kind, filters):
        return d.get("kind") == kind and all(d.get(k) == v for k, v in (filters or {}).items())

    @staticmethod
    def _src(d):
        return {k: v for k, v in d.items() if k != "embedding"}

    def knn(self, vec, kind, k=10, filters=None):
        hits = [(round(sum(a * b for a, b in zip(vec, d["embedding"])), 4), self._src(d))
                for d in self.docs.values() if self._match(d, kind, filters)]
        return sorted(hits, key=lambda x: -x[0])[:k]

    def bm25(self, query, kind, k=10):
        q = set(re.findall(r"[a-z0-9_]+", query.lower()))
        hits = []
        for d in self.docs.values():
            if not self._match(d, kind, None):
                continue
            text = " ".join([" ".join(d.get("keywords", []))] + [str(d.get(f, "")) for f in ("symptom", "root_cause", "lesson")]).lower()
            n = len(q & set(re.findall(r"[a-z0-9_]+", text)))
            if n:
                hits.append((n, self._src(d)))
        return [d for _, d in sorted(hits, key=lambda x: -x[0])[:k]]

    def find(self, kind, filters=None, size=500, newest_first=False, fields=None):
        out = sorted((self._src(d) for d in self.docs.values() if self._match(d, kind, filters)),
                     key=lambda d: d.get("ts", 0), reverse=newest_first)[:size]
        return [{f: d.get(f) for f in fields} for d in out] if fields else out

    def stats(self):
        out: dict = {}
        for d in self.docs.values():
            out[d["kind"]] = out.get(d["kind"], 0) + 1
        return out
