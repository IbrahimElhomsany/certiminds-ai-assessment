"""RAG pipeline (C1):

documents -> processing -> chunking -> embedding/index -> retrieval -> reranking
-> context construction -> (LLM, in backend/agent.py) -> grounded response with citations

Design choices (explained in reports/rag_design.md):
- Corpus is small (15 products, 10 rules, 2 guides), so each product card / rule / guide
  section is ONE chunk ("semantic chunking"). Splitting a product card would separate
  e.g. the price from the product name.
- Retrieval is HYBRID: BM25 keyword search (exact terms like "R12", "CN004") plus an optional
  dense embedding model (fastembed, BAAI/bge-small-en-v1.5) for meaning ("non-slippery" ~ "R12").
  Results are merged with Reciprocal Rank Fusion. If fastembed is not installed the
  pipeline still works with BM25 only.
- Vector store: an in-memory numpy matrix. Fine for ~40 chunks; production would use
  pgvector/Qdrant with a tenant_id filter on every query.
- Security: every chunk passes an injection scanner at ingestion. Suspicious chunks are
  QUARANTINED (not indexed) and logged. At query time, retrieved text is wrapped in
  <document> tags and the prompt declares it to be untrusted data.
"""
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior|above|your) (instructions|rules)",
    r"system (message|prompt)\s*:",
    r"reveal .*(prompt|credential|password)",
    r"administrator credentials",
    r"you are now",
    r"\bis (now )?free\b",
    r"disregard .*instructions",
]


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)


# -------------- 1-3: documents, processing, chunking

def scan_for_injection(text: str) -> list[str]:
    t = text.lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, t)]


def product_card(p: dict) -> str:
    slip = p["slip_rating"] or "not rated (wall product)"
    return (f"{p['name']} ({p['sku']}) is a {p['material']} tile, size {p['tile_width_cm']}x{p['tile_length_cm']} cm, "
            f"application: {p['application']} ({'/'.join(p['surfaces'])}, {'/'.join(p['locations'])}"
            f"{', commercial' if p['commercial'] else ''}). Finish: {p['finish']}. Slip rating: {slip}. "
            f"Style: {p['style']}. Price: QAR {p['price_per_m2']:.2f} per m2. Tiles per box: {p['tiles_per_box']}.")
    # Stock is deliberately NOT put in the text index: it changes often and must come from
    # check_inventory() at answer time (E.13).


def set_card(s: dict) -> str:
    """Bathroom set (sold per set, not per m2). Stock is left out for the same reason as tiles."""
    return (f"{s['name']} ({s['sku']}) is a bathroom {s['category']} set: {s['description']}. "
            f"Includes: {', '.join(s['includes'])}. Finish: {s['finish']}. Style: {s['style']}. "
            f"For: {', '.join(s['rooms'])}. Price: QAR {s['price']:.2f} per set.")


def split_markdown(path: Path) -> list[Chunk]:
    chunks, title, body = [], None, []
    doc_title = path.stem
    for line in path.read_text().splitlines():
        if line.startswith("# "):
            doc_title = line[2:].strip()
        elif line.startswith("## "):
            if title and body:
                chunks.append((title, " ".join(body).strip()))
            title, body = line[3:].strip(), []
        elif line.strip():
            body.append(line.strip())
    if title and body:
        chunks.append((title, " ".join(body).strip()))
    kind = "rule" if "rules" in path.stem else "policy" if "policy" in path.stem else "guide"
    return [Chunk(id=f"{path.stem}#{i+1}", text=f"{t}: {b}",
                  metadata={"type": kind, "source": f"data/knowledge/{path.name}", "section": t,
                            "document": doc_title, "trust": "internal"})
            for i, (t, b) in enumerate(chunks)]


def load_documents() -> tuple[list[Chunk], list[dict]]:
    """Returns (clean chunks, quarantined items)."""
    chunks: list[Chunk] = []
    from backend.catalog import CATALOG          # prices already converted to QAR
    for p in (prod.model_dump() for prod in CATALOG.values()):
        chunks.append(Chunk(id=p["sku"], text=product_card(p),
                            metadata={"type": "product", "sku": p["sku"], "source": "data/catalog.json",
                                      "trust": "catalog"}))
    from backend.catalog import SANITARY         # the 3 bathroom sets, so every product is searchable
    for s in (st.model_dump() for st in SANITARY.values()):
        chunks.append(Chunk(id=s["sku"], text=set_card(s),
                            metadata={"type": "product", "sku": s["sku"], "source": "data/sanitary.json",
                                      "trust": "catalog"}))
    for path in sorted((DATA / "knowledge").glob("*.md")):
        chunks.extend(split_markdown(path))
    for i, n in enumerate(json.loads((DATA / "product_notes.json").read_text())):
        chunks.append(Chunk(id=f"note-{n['sku']}-{i}", text=f"Note about {n['sku']}: {n['note']}",
                            metadata={"type": "note", "sku": n["sku"], "source": n["source"], "trust": "external"}))

    clean, quarantined = [], []
    for c in chunks:
        c.text = " ".join(c.text.split())                         # processing: normalise whitespace
        hits = scan_for_injection(c.text)
        if hits:
            quarantined.append({"id": c.id, "source": c.metadata.get("source"), "patterns": hits})
        else:
            clean.append(c)
    return clean, quarantined


# ------------- 4-5: embedding + index

TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class BM25:
    """Classic keyword ranking. k1/b are the standard defaults."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in docs]
        self.avgdl = sum(map(len, self.docs)) / max(1, len(self.docs))
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, d in zip(self.tf, self.docs):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            out.append(s)
        return out


class DenseEmbedder:
    """Optional. Loaded only if fastembed is installed (pip install fastembed)."""
    MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, ~130 MB, CPU-friendly, strong for its size

    def __init__(self):
        from fastembed import TextEmbedding
        import numpy as np
        self.np = np
        self.model = TextEmbedding(self.MODEL)

    def embed(self, texts: list[str]):
        vecs = self.np.array(list(self.model.embed(texts)))
        return vecs / self.np.linalg.norm(vecs, axis=1, keepdims=True)   # normalise -> dot = cosine


def try_dense() -> Optional[DenseEmbedder]:
    try:
        return DenseEmbedder()
    except Exception:
        return None


# ------------------ 6-8: retrieval, reranking, context

@dataclass
class Retrieved:
    chunk: Chunk
    score: float
    citation: str


class RAGIndex:
    def __init__(self, use_dense: bool = True, embedder=None):
        self.chunks, self.quarantined = load_documents()
        self.bm25 = BM25([c.text for c in self.chunks])
        self.embedder = embedder if embedder is not None else (try_dense() if use_dense else None)
        self.matrix = self.embedder.embed([c.text for c in self.chunks]) if self.embedder else None

    @property
    def mode(self) -> str:
        return "hybrid (BM25 + dense)" if self.embedder else "BM25 only"

    def retrieve(self, query: str, top_k: int = 5, types: Optional[list[str]] = None,
                 skus: Optional[list[str]] = None) -> list[Retrieved]:
        """Hybrid retrieval + metadata filtering + Reciprocal Rank Fusion."""
        idx = [i for i, c in enumerate(self.chunks)
               if (types is None or c.metadata["type"] in types)
               and (skus is None or c.metadata.get("sku") in skus)]
        if not idx:
            return []
        rankings = []
        bm = self.bm25.scores(query)
        rankings.append(sorted(idx, key=lambda i: -bm[i]))
        if self.embedder is not None:
            q = self.embedder.embed([query])[0]
            dense = self.matrix @ q
            rankings.append(sorted(idx, key=lambda i: -dense[i]))
        fused = Counter()
        for ranking in rankings:                      # RRF: 1/(60 + rank) summed over rankers
            for rank, i in enumerate(ranking):
                fused[i] += 1.0 / (60 + rank + 1)
        best = [i for i, _ in fused.most_common(top_k)]
        return [Retrieved(self.chunks[i], round(fused[i], 5), f"[{self.chunks[i].id}]") for i in best]


def build_context(results: list[Retrieved]) -> str:
    """Wrap each retrieved chunk in a tagged block with its citation id. The system prompt
    tells the model that text inside <document> is untrusted reference data."""
    blocks = []
    for r in results:
        safe = r.chunk.text.replace("<", "&lt;").replace(">", "&gt;")   # can't close our tags
        blocks.append(f'<document id="{r.chunk.id}" source="{r.chunk.metadata["source"]}" '
                      f'trust="{r.chunk.metadata["trust"]}">\n{safe}\n</document>')
    return "\n".join(blocks)


_INDEX: Optional[RAGIndex] = None


def get_index() -> RAGIndex:
    global _INDEX
    if _INDEX is None:
        import os
        _INDEX = RAGIndex(use_dense=os.getenv("RAG_DENSE", "1") == "1")
    return _INDEX


if __name__ == "__main__":
    import sys
    index = RAGIndex()
    print(f"Mode: {index.mode} | chunks indexed: {len(index.chunks)} | quarantined: {len(index.quarantined)}")
    for q in index.quarantined:
        print("  QUARANTINED:", q)
    query = " ".join(sys.argv[1:]) or "slip resistant outdoor balcony tiles"
    print(f"\nQuery: {query}")
    for r in index.retrieve(query, top_k=5):
        print(f"  {r.citation:28} score={r.score}  {r.chunk.text[:90]}...")
