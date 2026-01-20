"""
Terminology Importer (XLSX -> PostgreSQL + pgvector)

Purpose
- Import curated railway terminology from two Excel files (German + English) into PostgreSQL.
- Compute multilingual semantic embeddings for each term pair and store them in a pgvector column.
- Optionally rebuild a vector index (HNSW preferred, IVFFLAT fallback) to enable fast similarity search.

Input files
- rag/terminology/railway_terminology.de.xlsx
- rag/terminology/railway_terminology.en.xlsx

Assumptions about input data
- Both XLSX files contain the same number of rows and represent aligned term pairs.
- Each sheet contains columns for term, definition, and language (header names can vary, see _find_col()).
- Rows are filtered by language prefix:
  - DE file: language starts with "de"
  - EN file: language starts with "en"

Database behavior
- Ensures the pgvector extension and the glossary_terms table exist.
- Truncates (clears) the glossary_terms table on each run to guarantee a clean, reproducible import.
- Inserts two directions per entry:
  - en -> de and de -> en
- Uses ON CONFLICT to make the operation idempotent if TRUNCATE is removed in the future.

Performance / resource footprint
- Embeddings are computed batch-wise (default batch size: 25) to keep RAM usage predictable.
- Tokenizer/BLAS thread counts are limited via environment variables to reduce CPU spikes on Windows.

Operational note
- DSN is loaded from config (cfg["rag"]["pg_dsn"]) to stay consistent with the runtime app.
  - Priority:
    1) OEBB_CONFIG (explicit config path)
    2) config.json in the orchestrator folder (parent of this scripts folder)
  - Fallback: DEFAULT_DSN_FALLBACK if config is missing or does not contain rag.pg_dsn
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import psycopg
from openpyxl import load_workbook
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer


# -----------------------------------------------------------------------------
# Configuration (development defaults)
# -----------------------------------------------------------------------------
DEFAULT_DSN_FALLBACK = "postgresql://postgres:oebb@127.0.0.1:5432/terminology"

# SentenceTransformers model used to compute embeddings.
# paraphrase-multilingual-MiniLM-L12-v2 produces 384-dimensional vectors.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims

# Terminology behavior flags stored in DB.
# DEFAULT_MODE typically controls how apply_glossary() treats the term (e.g., force vs protect).
DEFAULT_MODE = "force"
DEFAULT_PRIORITY = 100


# -----------------------------------------------------------------------------
# Project paths
# -----------------------------------------------------------------------------
# Script is expected to live under:
#   .../services/orchestrator/scripts
# Project root is derived by walking up:
#   scripts -> orchestrator -> services -> realtime-translator (parents[2])
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]  # ...\realtime-translator
TERMINOLOGY_DIR = PROJECT_ROOT / "rag" / "terminology"

# Canonical terminology source files (aligned term pairs).
XLSX_DE = TERMINOLOGY_DIR / "railway_terminology.de.xlsx"
XLSX_EN = TERMINOLOGY_DIR / "railway_terminology.en.xlsx"


# -----------------------------------------------------------------------------
# Config loading (DSN)
# -----------------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    """
    Load runtime configuration.

    Priority:
      1) Environment variable OEBB_CONFIG (absolute/relative path)
      2) config.json in the orchestrator folder (parent of this scripts folder)

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the config file is missing.
        json.JSONDecodeError: If config JSON is invalid.
        ValueError: If config root is not a JSON object.
    """
    cfg_path = os.environ.get("OEBB_CONFIG")
    if cfg_path:
        path = Path(cfg_path)
    else:
        # This script lives in: ...\services\orchestrator\scripts
        # Default config is:     ...\services\orchestrator\config.json
        path = Path(__file__).resolve().parents[1] / "config.json"

    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("Config root must be a JSON object.")

    return data


def dsn_from_config() -> str:
    """
    Resolve Postgres DSN from config.json.

    Expected location:
      cfg["rag"]["pg_dsn"]

    Falls back to DEFAULT_DSN_FALLBACK if missing.
    """
    try:
        cfg = load_config()
    except Exception:
        return DEFAULT_DSN_FALLBACK

    rag = cfg.get("rag", {}) or {}
    dsn = rag.get("pg_dsn")
    return str(dsn).strip() if dsn else DEFAULT_DSN_FALLBACK


# -----------------------------------------------------------------------------
# Small helpers for Excel parsing
# -----------------------------------------------------------------------------
def _s(x) -> str:
    """Normalize Excel cell values into trimmed strings."""
    return "" if x is None else str(x).strip()


def _headers(ws) -> List[str]:
    """Read the header row (row 1) and normalize header names."""
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return [_s(h) for h in header_row]


def _find_col(headers: List[str], candidates: List[str]) -> int:
    """
    Find a column index by trying multiple candidate header names.

    This allows the importer to survive small variations in the XLSX export format.
    """
    for c in candidates:
        if c in headers:
            return headers.index(c)
    raise SystemExit(f"Missing header. Need one of {candidates}. Found (first 40): {headers[:40]}")


def read_rows_de(xlsx: Path) -> List[Tuple[str, str]]:
    """
    Read German terms/definitions from the DE workbook.

    Filters rows to those whose language column starts with 'de' (case-insensitive).
    Returns list of (term, definition).
    """
    wb = load_workbook(filename=xlsx, read_only=True, data_only=True)
    ws = wb.active
    headers = _headers(ws)

    term_i = _find_col(headers, ["Begriff", "term", "Term"])
    def_i = _find_col(headers, ["Definition", "definition"])
    lang_i = _find_col(headers, ["Sprache", "language", "Lang"])

    out: List[Tuple[str, str]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        term = _s(row[term_i])
        definition = _s(row[def_i])
        lang = _s(row[lang_i]).lower()

        # Skip empty rows and non-DE rows.
        if not term:
            continue
        if not lang.startswith("de"):
            continue

        out.append((term, definition))

    wb.close()
    return out


def read_rows_en(xlsx: Path) -> List[Tuple[str, str]]:
    """
    Read English terms/definitions from the EN workbook.

    Filters rows to those whose language column starts with 'en' (case-insensitive).
    Returns list of (term, definition).
    """
    wb = load_workbook(filename=xlsx, read_only=True, data_only=True)
    ws = wb.active
    headers = _headers(ws)

    # Note: candidate header names differ slightly from DE file to handle format variations.
    term_i = _find_col(headers, ["term", "Term", "Begriff"])
    def_i = _find_col(headers, ["definition", "Definition"])
    lang_i = _find_col(headers, ["language", "Sprache", "Lang"])

    out: List[Tuple[str, str]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        term = _s(row[term_i])
        definition = _s(row[def_i])
        lang = _s(row[lang_i]).lower()

        if not term:
            continue
        if not lang.startswith("en"):
            continue

        out.append((term, definition))

    wb.close()
    return out


# -----------------------------------------------------------------------------
# Database schema management
# -----------------------------------------------------------------------------
def ensure_schema(conn: psycopg.Connection) -> None:
    """
    Ensure pgvector extension, glossary_terms table, constraint, and basic index exist.

    Notes
    - embedding vector(384) is NOT NULL to guarantee vector search can be performed for all rows.
    - A uniqueness constraint supports stable upserts and protects against accidental duplicates.
    """
    with conn.cursor() as cur:
        # Enable pgvector type and operators.
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # Create main terminology table if needed.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.glossary_terms (
              id bigserial PRIMARY KEY,
              src_lang text NOT NULL,
              tgt_lang text NOT NULL,
              src_term text NOT NULL,
              tgt_term text NOT NULL,
              mode text NOT NULL,
              priority int NOT NULL DEFAULT 0,
              meta jsonb NULL,
              embedding vector(384) NOT NULL
            );
            """
        )

        # Add uniqueness constraint if it does not exist.
        cur.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'glossary_terms_uniq'
                  AND conrelid = 'public.glossary_terms'::regclass
              ) THEN
                ALTER TABLE public.glossary_terms
                ADD CONSTRAINT glossary_terms_uniq
                UNIQUE (src_lang, tgt_lang, src_term, tgt_term, mode);
              END IF;
            END $$;
            """
        )

        # Non-vector index to speed up filtering by language direction.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS glossary_terms_lang_idx ON public.glossary_terms (src_lang, tgt_lang);"
        )

    conn.commit()


def recreate_vector_index(conn: psycopg.Connection) -> None:
    """
    Recreate the vector index on embedding.

    Strategy
    - Drop existing embedding index if present.
    - Try HNSW first (fast query, good recall).
    - If HNSW is not available in the installed pgvector version, fallback to IVFFLAT.
    """
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS public.glossary_terms_embedding_idx;")
    conn.commit()

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                CREATE INDEX glossary_terms_embedding_idx
                ON public.glossary_terms
                USING hnsw (embedding vector_cosine_ops);
                """
            )
            conn.commit()
            return
        except Exception:
            # If HNSW creation fails, rollback the failed statement and try IVFFLAT.
            conn.rollback()

        cur.execute(
            """
            CREATE INDEX glossary_terms_embedding_idx
            ON public.glossary_terms
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 50);
            """
        )

    conn.commit()


# -----------------------------------------------------------------------------
# Main import pipeline
# -----------------------------------------------------------------------------
def main() -> None:
    """
    Import runner.

    Steps
    1) Parse CLI arguments
    2) Reduce CPU oversubscription via environment settings (Windows-friendly)
    3) Read DE and EN XLSX files and validate row alignment
    4) Load embedding model
    5) Connect to Postgres, ensure schema, truncate table
    6) Batch encode aligned pairs and upsert both directions
    7) Optional vector index rebuild
    8) ANALYZE + final row count
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--reindex", action="store_true", help="Recreate vector index after import")
    ap.add_argument("--batch", type=int, default=25, help="Embedding batch size")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides config rag.pg_dsn)")
    args = ap.parse_args()

    # Reduce CPU spikes / thread storms on Windows when encoding embeddings.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    # Resolve DSN from CLI or config.
    dsn = args.dsn or dsn_from_config()

    # Validate expected input files.
    if not XLSX_DE.exists():
        raise SystemExit(f"DE file not found: {XLSX_DE}")
    if not XLSX_EN.exists():
        raise SystemExit(f"EN file not found: {XLSX_EN}")

    # Load and filter rows from both workbooks.
    de_rows = read_rows_de(XLSX_DE)
    en_rows = read_rows_en(XLSX_EN)

    print("DE rows:", len(de_rows))
    print("EN rows:", len(en_rows))

    # Safety check: rows must be aligned and equal count.
    # If this fails, embeddings and term pairing would be incorrect.
    if len(de_rows) != len(en_rows):
        raise SystemExit(f"Row mismatch: de={len(de_rows)} en={len(en_rows)}")

    # Load embedding model once and reuse.
    model = SentenceTransformer(MODEL_NAME)

    with psycopg.connect(dsn) as conn:
        # Register pgvector adapters so Python lists can be passed to vector columns.
        register_vector(conn)

        # Ensure schema exists before importing.
        ensure_schema(conn)

        # Clean import: always start from an empty table for reproducibility.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE public.glossary_terms;")
        conn.commit()

        total = 0
        batch = max(1, int(args.batch))

        # Process in batches to keep memory usage bounded.
        for i in range(0, len(de_rows), batch):
            chunk_de = de_rows[i : i + batch]
            chunk_en = en_rows[i : i + batch]

            # Build texts for embedding model and aligned term pairs for insertion.
            texts: List[str] = []
            pairs: List[Tuple[str, str, str, str]] = []

            for (de_term, de_def), (en_term, en_def) in zip(chunk_de, chunk_en):
                # Include both terms and both definitions to provide richer context for embeddings.
                texts.append("\n".join([en_term, en_def, de_term, de_def]).strip())
                pairs.append((en_term, en_def, de_term, de_def))

            # Encode and normalize embeddings to improve cosine distance behavior.
            embs = model.encode(texts, normalize_embeddings=True).tolist()

            # Build insert rows: write both directions (en->de and de->en).
            rows = []
            for (en_term, en_def, de_term, de_def), emb in zip(pairs, embs):
                meta = json.dumps({"de_def": de_def, "en_def": en_def}, ensure_ascii=False)

                rows.append(("en", "de", en_term, de_term, DEFAULT_MODE, DEFAULT_PRIORITY, meta, emb))
                rows.append(("de", "en", de_term, en_term, DEFAULT_MODE, DEFAULT_PRIORITY, meta, emb))

            # Upsert. With TRUNCATE this behaves like insert, but ON CONFLICT keeps it safe if TRUNCATE changes later.
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO public.glossary_terms
                      (src_lang, tgt_lang, src_term, tgt_term, mode, priority, meta, embedding)
                    VALUES
                      (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT (src_lang, tgt_lang, src_term, tgt_term, mode)
                    DO UPDATE SET
                      priority = EXCLUDED.priority,
                      meta = EXCLUDED.meta,
                      embedding = EXCLUDED.embedding
                    """,
                    rows,
                )
            conn.commit()

            total += len(rows)
            print("Inserted so far:", total)

        # Optional: rebuild vector index. Useful after major changes to terminology or embeddings.
        if args.reindex:
            recreate_vector_index(conn)

        # Refresh planner stats and print final row count.
        with conn.cursor() as cur:
            cur.execute("ANALYZE public.glossary_terms;")
            cur.execute("SELECT COUNT(*) FROM public.glossary_terms;")
            count = cur.fetchone()[0]
        conn.commit()

    print("Done. DB row count:", count)


if __name__ == "__main__":
    main()
