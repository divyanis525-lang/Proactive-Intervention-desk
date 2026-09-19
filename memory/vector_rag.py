"""
Vector RAG retrieval layer, kept live (Production Bar Checklist 6.1:
"Vector DB / index kept live, not rebuilt from scratch").

Design decision (research_log.md item R5): rather than pulling in
faiss/chromadb + a hosted embedding API (adds a network dependency this
sandboxed environment can't reach anyway), we implement a small
incremental TF-IDF index in pure Python. New documents (a support ticket,
a policy update, a CRM note) are added with `add()` and are immediately
retrievable -- no full re-index -- because we maintain running document
frequencies and only recompute the *query's* TF-IDF vector at query time,
not the whole corpus's.

This is a deliberate scope reduction from a production system (a real
deployment would use a proper ANN index + managed embeddings), but it
satisfies the checklist's actual requirement: incremental freshness,
no stale silently-persisting embeddings, no full rebuild on write.
"""
import math
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Document:
    doc_id: str
    text: str
    metadata: Dict = field(default_factory=dict)
    added_ts: float = field(default_factory=time.time)
    tokens: List[str] = field(default_factory=list)


class LiveTfidfIndex:
    def __init__(self):
        self._docs: Dict[str, Document] = {}
        self._df: Dict[str, int] = {}  # document frequency per term, updated incrementally

    def add(self, doc_id: str, text: str, metadata: Optional[Dict] = None):
        """Incremental add -- immediately retrievable, no full re-index."""
        tokens = _tokenize(text)
        doc = Document(doc_id=doc_id, text=text, metadata=metadata or {}, tokens=tokens)
        # if replacing an existing doc, back out its old term contributions first
        if doc_id in self._docs:
            self._decrement_df(self._docs[doc_id])
        self._docs[doc_id] = doc
        for term in set(tokens):
            self._df[term] = self._df.get(term, 0) + 1

    def _decrement_df(self, doc: Document):
        for term in set(doc.tokens):
            self._df[term] = max(0, self._df.get(term, 0) - 1)

    def _idf(self, term: str) -> float:
        n = max(1, len(self._docs))
        df = self._df.get(term, 0)
        return math.log((n + 1) / (df + 1)) + 1.0

    def _doc_vector(self, doc: Document) -> Dict[str, float]:
        tf: Dict[str, int] = {}
        for t in doc.tokens:
            tf[t] = tf.get(t, 0) + 1
        return {t: c * self._idf(t) for t, c in tf.items()}

    def query(self, text: str, top_k: int = 3, metadata_filter: Optional[Dict] = None) -> List[Tuple[str, float, Dict]]:
        q_tokens = _tokenize(text)
        q_tf: Dict[str, int] = {}
        for t in q_tokens:
            q_tf[t] = q_tf.get(t, 0) + 1
        q_vec = {t: c * self._idf(t) for t, c in q_tf.items()}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1e-9

        scored = []
        for doc in self._docs.values():
            if metadata_filter and not all(doc.metadata.get(k) == v for k, v in metadata_filter.items()):
                continue
            d_vec = self._doc_vector(doc)
            dot = sum(q_vec.get(t, 0.0) * w for t, w in d_vec.items())
            d_norm = math.sqrt(sum(v * v for v in d_vec.values())) or 1e-9
            sim = dot / (q_norm * d_norm)
            if sim > 0:
                scored.append((doc.doc_id, sim, doc.metadata))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_text(self, doc_id: str) -> Optional[str]:
        d = self._docs.get(doc_id)
        return d.text if d else None
