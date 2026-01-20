from __future__ import annotations

import os
from typing import List, Tuple

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DSN = "postgresql://postgres:oebb@127.0.0.1:5432/terminology"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims
BATCH_SIZE = 25  # klein halten
MAX_BATCHES = 10_000  # safety

def main() -> None:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    model = SentenceTransformer(MODEL_NAME)

    with psycopg.connect(DSN) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            total = 0
            for _ in range(MAX_BATCHES):
                cur.execute(
                    """
                    SELECT id, src_term, tgt_term
                    FROM public.glossary_terms
                    WHERE embedding IS NULL
                    ORDER BY id
                    LIMIT %s
                    """,
                    (BATCH_SIZE,),
                )
                batch: List[Tuple[int, str, str]] = cur.fetchall()
                if not batch:
                    break

                ids = [r[0] for r in batch]
                texts = [f"{r[1]}\n{r[2]}".strip() for r in batch]

                embs = model.encode(texts, normalize_embeddings=True).tolist()

                for row_id, emb in zip(ids, embs):
                    cur.execute(
                        "UPDATE public.glossary_terms SET embedding = %s WHERE id = %s",
                        (emb, row_id),
                    )

                conn.commit()
                total += len(batch)
                print(f"Backfilled {total} rows (last id {ids[-1]})")

    print("Done.")

if __name__ == "__main__":
    main()
