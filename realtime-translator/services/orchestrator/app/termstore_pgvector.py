from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.glossary import Term

@dataclass(frozen=True)
class PgVectorCfg:
    dsn: str
    model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    top_k: int = 50

class PgVectorTermStore:
    def __init__(self, cfg: PgVectorCfg) -> None:
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"

        self.cfg = cfg
        self.model = SentenceTransformer(cfg.model_name)

        self.conn = psycopg.connect(cfg.dsn)
        register_vector(self.conn)

    def close(self) -> None:
        self.conn.close()

    def search(self, text: str, src_lang: str, tgt_lang: str) -> List[Term]:
        q = self.model.encode(text, normalize_embeddings=True).tolist()

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT src_lang, tgt_lang, src_term, tgt_term, mode, priority
                FROM public.glossary_terms
                WHERE src_lang = %s AND tgt_lang = %s
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (src_lang, tgt_lang, q, self.cfg.top_k),
            )
            rows = cur.fetchall()

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
