"""Vector + lexical store. OpenSearchStore is the production backend (OpenSearch Service VPC domain, lucene HNSW
cosine). Any object with the same methods works (see agentmem.testing.InMemoryStore)."""
from typing import Protocol

import boto3


def mapping(dim: int) -> dict:
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {"properties": {
            "kind": {"type": "keyword"}, "signature": {"type": "keyword"}, "aliases": {"type": "keyword"},
            "keywords": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "symptom": {"type": "text"}, "root_cause": {"type": "text"}, "fix": {"type": "text"}, "lesson": {"type": "text"},
            "summary": {"type": "text"}, "status": {"type": "keyword"}, "instance_id": {"type": "keyword"},
            "batch_id": {"type": "keyword"}, "session_id": {"type": "keyword"}, "source": {"type": "keyword"},
            "verified": {"type": "boolean"}, "confirmations": {"type": "integer"}, "count": {"type": "integer"},
            "suggested_keywords": {"type": "keyword"}, "learned_from": {"type": "keyword"}, "ts": {"type": "long"},
            "namespace": {"type": "keyword"}, "title": {"type": "text"}, "use_cases": {"type": "text"}, "hints": {"type": "text"},
            "confidence": {"type": "float"}, "source_episodes": {"type": "keyword"}, "situation": {"type": "text"},
            "intent": {"type": "text"}, "assessment": {"type": "keyword"}, "justification": {"type": "text"},
            "reflection": {"type": "text"}, "update_proposals": {"type": "object", "enabled": False},
            "embedding": {"type": "knn_vector", "dimension": dim,
                          "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}},
        }},
    }


BM25_FIELDS = ["keywords^3", "symptom", "root_cause", "lesson"]


class Store(Protocol):
    def ensure_index(self, reset: bool = False) -> None: ...
    def put(self, doc_id: str, doc: dict) -> None: ...
    def get(self, doc_id: str) -> dict | None: ...
    def delete(self, doc_id: str) -> None: ...
    def knn(self, vec: list[float], kind: str, k: int = 10, filters: dict | None = None) -> list[tuple[float, dict]]: ...
    def bm25(self, query: str, kind: str, k: int = 10) -> list[dict]: ...
    def find(self, kind: str, filters: dict | None = None, size: int = 500, newest_first: bool = False, fields: list | None = None) -> list[dict]: ...
    def stats(self) -> dict: ...


class OpenSearchStore:
    def __init__(self, endpoint: str, index: str, region: str, dim: int, client=None):
        self.endpoint, self.index, self.region, self.dim = endpoint, index, region, dim
        self._client = client
        self._ready = False

    @property
    def client(self):
        if self._client is None:
            from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection
            creds = boto3.Session().get_credentials()
            self._client = OpenSearch(hosts=[{"host": self.endpoint, "port": 443}], http_auth=AWSV4SignerAuth(creds, self.region, "es"),
                                      use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=30)
        if not self._ready:
            self._ready = True
            self.ensure_index()
        return self._client

    def ensure_index(self, reset: bool = False):
        c = self.client
        if reset and c.indices.exists(index=self.index):
            c.indices.delete(index=self.index)
        if not c.indices.exists(index=self.index):
            c.indices.create(index=self.index, body=mapping(self.dim))

    def put(self, doc_id, doc):
        self.client.index(index=self.index, id=doc_id, body=doc, refresh=True)

    def get(self, doc_id):
        r = self.client.get(index=self.index, id=doc_id, ignore=[404])
        return r["_source"] if r.get("found") else None

    def delete(self, doc_id):
        self.client.delete(index=self.index, id=doc_id, refresh=True, ignore=[404])

    @staticmethod
    def _filters(kind: str, filters: dict | None) -> list:
        return [{"term": {"kind": kind}}] + [{"term": {k: v}} for k, v in (filters or {}).items()]

    def knn(self, vec, kind, k=10, filters=None):
        flt = {"bool": {"filter": self._filters(kind, filters)}}
        r = self.client.search(index=self.index, body={"size": k, "_source": {"excludes": ["embedding"]},
                                                       "query": {"knn": {"embedding": {"vector": vec, "k": k, "filter": flt}}}})
        # lucene cosinesimil score = (1 + cos) / 2
        return [(round(2 * h["_score"] - 1, 4), h["_source"]) for h in r["hits"]["hits"]]

    def bm25(self, query, kind, k=10):
        r = self.client.search(index=self.index, body={"size": k, "_source": {"excludes": ["embedding"]}, "query": {"bool": {
            "filter": [{"term": {"kind": kind}}], "must": {"multi_match": {"query": query[:5000], "fields": BM25_FIELDS}}}}})
        return [h["_source"] for h in r["hits"]["hits"]]

    def find(self, kind, filters=None, size=500, newest_first=False, fields=None):
        body = {"size": size, "_source": fields or {"excludes": ["embedding"]}, "sort": [{"ts": "desc" if newest_first else "asc"}],
                "query": {"bool": {"filter": self._filters(kind, filters)}}}
        return [h["_source"] for h in self.client.search(index=self.index, body=body)["hits"]["hits"]]

    def stats(self):
        r = self.client.search(index=self.index, body={"size": 0, "aggs": {"k": {"terms": {"field": "kind"}}}})
        return {b["key"]: b["doc_count"] for b in r["aggregations"]["k"]["buckets"]}
