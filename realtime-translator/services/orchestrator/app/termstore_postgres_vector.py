"""
Postgres Vector Term Store (pgvector-based RAG for Terminology)

Purpose
- Provide a lightweight retrieval component that selects the most relevant glossary terms
  for a given text segment using semantic similarity search.
- Intended to reduce runtime cost versus applying an entire terminology list in Python by
  retrieving only Top-K candidates per segment.

High-level flow
1) Encode the incoming text into a dense embedding vector using SentenceTransformers.
2) Query PostgreSQL (pgvector) for rows matching the language direction (src_lang, tgt_lang).
3) Order results by vector distance (cosine distance via <=> with vector_cosine_ops index).
4) Return the Top-K rows as `Term` objects consumable by apply_glossary().

Database requirements
- Table: public.glossary_terms with columns:
  src_lang, tgt_lang, src_term, tgt_term, mode, priority, embedding
- pgvector extension must be installed.
- For performance, an embedding index is recommended:
  - HNSW or IVFFLAT on (embedding vector_cosine_ops)

Operational notes
- This store keeps a persistent DB connection open for the lifetime of the process.
- SentenceTransformer model is loaded once during initialization.
- Threading env vars are pinned to reduce CPU spikes on Windows during embedding inference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.glossary import Term


# -----------------------------------------------------------------------------
# Configuration model
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class PostgresVectorCfg:
    """
    Configuration for PostgresVectorTermStore.

    Attributes
    - dsn:
      PostgreSQL DSN for connecting to the terminology database.
      Typically loaded from cfg["rag"]["pg_dsn"] in the main application config.
    - model_name:
      SentenceTransformers model used to compute query embeddings.
      Must match the embedding dimension used for stored vectors (default: 384 dims).
    - top_k:
      Number of nearest neighbor candidates to retrieve per query.
      This should be chosen based on:
        - glossary size
        - performance constraints
        - how aggressive apply_glossary() should be
    """

    dsn: str
    model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims
    top_k: int = 50


# -----------------------------------------------------------------------------
# Term store implementation
# -----------------------------------------------------------------------------
class PostgresVectorTermStore:
    """
    Term retrieval backend using pgvector.

    Responsibilities
    - Maintain the embedding model used for query vector generation.
    - Maintain a DB connection and register pgvector adapters for psycopg.
    - Provide `search()` that returns Terms ranked by semantic similarity.

    Lifecycle
    - Instantiate once at application startup.
    - Call close() during shutdown to release the DB connection.
    """

    def __init__(self, cfg: PostgresVectorCfg) -> None:
        # Reduce CPU oversubscription / thread storms when embedding model runs on Windows.
        # These env vars affect tokenizers and common BLAS backends.
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"

        self.cfg = cfg

        # Load the embedding model once. This is the expensive step, so reuse it.
        self.model = SentenceTransformer(cfg.model_name)

        # Maintain a persistent DB connection to avoid reconnect overhead per segment.
        self.conn = psycopg.connect(cfg.dsn)

        # Register pgvector type adapters so Python lists can be bound to vector parameters.
        register_vector(self.conn)

    def close(self) -> None:
        """
        Close the underlying database connection.

        Notes
        - Shutdown should be best-effort; failures here should not crash the app.
        """
        try:
            self.conn.close()
        except Exception:
            pass

    def search(self, text: str, src_lang: str, tgt_lang: str) -> List[Term]:
        """
        Retrieve Top-K terminology candidates for a given text and language direction.

        Args
        - text:
          Input text segment (typically STT output or protected text) to search against.
        - src_lang / tgt_lang:
          Language direction filter. Only terms matching this direction are considered.

        Returns
        - List[Term]:
          Candidates ordered from most similar to least similar according to the vector distance.
          The resulting objects are compatible with the glossary layer (apply_glossary()).

        Query details
        - ORDER BY embedding <=> %s::vector
          This uses pgvector distance operator. With a cosine index (vector_cosine_ops),
          the ordering corresponds to cosine distance (smaller is more similar).

        Performance considerations
        - Ensure the DB has an index on embedding for fast ANN search.
        - top_k should remain bounded; very large values may degrade query latency.
        """
        # Compute query embedding. normalize_embeddings=True is important for cosine distance stability.
        q = self.model.encode(text, normalize_embeddings=True).tolist()

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT src_lang, tgt_lang, src_term, tgt_term, mode, priority
                FROM public.glossary_terms
                WHERE src_lang = %s AND tgt_lang = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (src_lang, tgt_lang, q, self.cfg.top_k),
            )
            rows = cur.fetchall()

        # Convert DB rows into the canonical Term object used by the glossary pipeline.
        return [
            Term(
                src_lang=r[0],
                tgt_lang=r[1],
                src_term=r[2],
                tgt_term=r[3],
                mode=(r[4] or "").strip().lower(),
                priority=int(r[5] or 0),
            )
            for r in rows
        ]
