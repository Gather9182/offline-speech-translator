"""
Terminology Pair Import (Append / Upsert)

Purpose
- Add a single bilingual terminology pair to the existing PostgreSQL glossary_terms table.
- No deletion: existing rows remain. If an identical unique key already exists, it is updated (upsert).
- Compute embeddings batch-wise to keep resource usage predictable.

How pairing works
- You provide two Excel files that represent aligned rows (same number of filtered rows).
- The script writes two directions per row by default:
    src_lang -> tgt_lang
    tgt_lang -> src_lang

Uniqueness / overwrite semantics
- The table is expected to have a unique constraint on:
  (src_lang, tgt_lang, src_term, tgt_term, mode)
- On conflict, we update: priority, meta, embedding.

Input expectations
- Each XLSX contains columns for term, definition, language.
- Header names may vary; _find_col() accepts a small set of candidates.
- Rows are filtered by the language code prefix you enter (e.g., "de", "en", "it").

Operational notes
- DSN can be provided via --dsn or environment variable OEBB_PG_DSN.
- Embeddings use sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384 dims).
- Threading is limited via env vars to reduce CPU spikes on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, List, Tuple

import psycopg
from openpyxl import load_workbook
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer


# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims

DEFAULT_MODE = "force"
DEFAULT_PRIORITY = 100


DEFAULT_DSN_FALLBACK = "postgresql://postgres:oebb@127.0.0.1:5432/terminology"

# -----------------------------------------------------------------------------
# Config parser
# -----------------------------------------------------------------------------
def load_config() -> dict:
    """
    Load runtime configuration.

    Uses environment variable OEBB_CONFIG if set. Otherwise reads config.json
    located next to this script (same behavior as realtime_translate.py).
    """
    cfg_path = os.environ.get("OEBB_CONFIG")
    if cfg_path:
        path = Path(cfg_path)
    else:
        path = Path(__file__).with_name("config.json")

    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dsn_from_config() -> str:
    """
    Resolve Postgres DSN from config.json.

    Expected key:
      cfg["rag"]["pg_dsn"]

    Falls back to DEFAULT_DSN_FALLBACK if missing.
    """
    try:
        cfg = load_config()
    except Exception:
        return DEFAULT_DSN_FALLBACK

    rag_cfg = cfg.get("rag", {}) or {}
    dsn = rag_cfg.get("pg_dsn")
    return dsn or DEFAULT_DSN_FALLBACK



# -----------------------------------------------------------------------------
# Excel parsing helpers
# -----------------------------------------------------------------------------
def _s(x: Any) -> str:
    """Normalize Excel cell values into trimmed strings."""
    return "" if x is None else str(x).strip()


def _headers(ws) -> List[str]:
    """Read the first row (headers) and normalize."""
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return [_s(h) for h in header_row]


def _find_col(headers: List[str], candidates: List[str]) -> int:
    """
    Find the column index by trying multiple header names.
    This keeps the importer resilient to small format changes.
    """
    for c in candidates:
        if c in headers:
            return headers.index(c)
    raise SystemExit(f"Missing header. Need one of {candidates}. Found (first 40): {headers[:40]}")


def read_rows_lang(xlsx: Path, lang_prefix: str) -> List[Tuple[str, str]]:
    """
    Read rows from an XLSX and filter by language prefix.

    Returns a list of (term, definition), filtered where the language column starts with lang_prefix.
    """
    if not xlsx.exists():
        raise SystemExit(f"File not found: {xlsx}")

    wb = load_workbook(filename=xlsx, read_only=True, data_only=True)
    ws = wb.active
    headers = _headers(ws)

    # Candidate header names (support both DE/EN variants that we have seen).
    term_i = _find_col(headers, ["Begriff", "term", "Term"])
    def_i = _find_col(headers, ["Definition", "definition"])
    lang_i = _find_col(headers, ["Sprache", "language", "Lang"])

    out: List[Tuple[str, str]] = []
    lp = lang_prefix.lower().strip()

    for row in ws.iter_rows(min_row=2, values_only=True):
        term = _s(row[term_i])
        definition = _s(row[def_i])
        lang = _s(row[lang_i]).lower()

        if not term:
            continue
        if lp and not lang.startswith(lp):
            continue

        out.append((term, definition))

    wb.close()
    return out


# -----------------------------------------------------------------------------
# DB schema management (minimal)
# -----------------------------------------------------------------------------
def ensure_schema(conn: psycopg.Connection) -> None:
    """
    Ensure pgvector extension and the required table/constraints exist.

    This is safe to call repeatedly.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS glossary_terms_lang_idx ON public.glossary_terms (src_lang, tgt_lang);"
        )
    conn.commit()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def prompt_path(label: str) -> Path:
    """Prompt the user for a file path and return a resolved Path."""
    while True:
        p = input(f"{label}: ").strip().strip('"')
        if not p:
            print("Please enter a path.")
            continue
        path = Path(p).expanduser()
        if path.exists():
            return path.resolve()
        print(f"Not found: {path}")


def prompt_lang(label: str) -> str:
    """Prompt for a language code like de, en, it."""
    while True:
        v = input(f"{label} (e.g. de/en/it): ").strip().lower()
        if v:
            return v
        print("Please enter a language code.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides config rag.pg_dsn)")
    ap.add_argument("--mode", default=DEFAULT_MODE, help="Term mode (e.g. force/protect)")
    ap.add_argument("--priority", type=int, default=DEFAULT_PRIORITY, help="Default priority")
    ap.add_argument("--batch", type=int, default=25, help="Embedding batch size")
    ap.add_argument("--oneway", action="store_true", help="Only insert src->tgt (no reverse direction)")
    args = ap.parse_args()
    dsn = args.dsn or dsn_from_config()

    # Reduce CPU oversubscription on Windows during embedding computation.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    print("Terminology Pair Import (Append / Upsert)")
    print("========================================")
    print()

    src_lang = prompt_lang("Source language")
    tgt_lang = prompt_lang("Target language")

    print()
    print("Enter XLSX files for the aligned term lists.")
    print("They must have the same number of filtered rows.")
    print()

    src_xlsx = prompt_path(f"XLSX path for {src_lang}")
    tgt_xlsx = prompt_path(f"XLSX path for {tgt_lang}")

    print()
    print(f"Reading {src_lang} rows from: {src_xlsx}")
    src_rows = read_rows_lang(src_xlsx, src_lang)

    print(f"Reading {tgt_lang} rows from: {tgt_xlsx}")
    tgt_rows = read_rows_lang(tgt_xlsx, tgt_lang)

    print()
    print(f"{src_lang} rows: {len(src_rows)}")
    print(f"{tgt_lang} rows: {len(tgt_rows)}")

    if len(src_rows) != len(tgt_rows):
        raise SystemExit(f"Row mismatch after filtering: {src_lang}={len(src_rows)} {tgt_lang}={len(tgt_rows)}")

    if len(src_rows) == 0:
        raise SystemExit("No rows found after filtering. Check the language column and language codes.")

    model = SentenceTransformer(MODEL_NAME)
    batch = max(1, int(args.batch))

    inserted_total = 0

    with psycopg.connect(dsn) as conn:
        register_vector(conn)
        ensure_schema(conn)

        for i in range(0, len(src_rows), batch):
            chunk_src = src_rows[i : i + batch]
            chunk_tgt = tgt_rows[i : i + batch]

            # Build embedding texts and aligned pairs.
            texts: List[str] = []
            pairs: List[Tuple[str, str, str, str]] = []

            for (src_term, src_def), (tgt_term, tgt_def) in zip(chunk_src, chunk_tgt):
                # Include both languages and definitions to provide context for similarity search.
                texts.append("\n".join([src_term, src_def, tgt_term, tgt_def]).strip())
                pairs.append((src_term, src_def, tgt_term, tgt_def))

            embs = model.encode(texts, normalize_embeddings=True).tolist()

            rows = []
            for (src_term, src_def, tgt_term, tgt_def), emb in zip(pairs, embs):
                meta = json.dumps(
                    {
                        f"{src_lang}_def": src_def,
                        f"{tgt_lang}_def": tgt_def,
                    },
                    ensure_ascii=False,
                )

                # Forward direction.
                rows.append((src_lang, tgt_lang, src_term, tgt_term, args.mode, args.priority, meta, emb))

                # Reverse direction (default).
                if not args.oneway:
                    rows.append((tgt_lang, src_lang, tgt_term, src_term, args.mode, args.priority, meta, emb))

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

            inserted_total += len(rows)
            print(f"Upserted so far: {inserted_total}")

        # Helpful stats after import.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT src_lang, tgt_lang, COUNT(*) FROM public.glossary_terms GROUP BY 1,2 ORDER BY 1,2;"
            )
            by_pair = cur.fetchall()

        conn.commit()

    print()
    print("Done.")
    print("Counts by language direction:")
    for a, b, c in by_pair:
        print(f"  {a}->{b}: {c}")


if __name__ == "__main__":
    main()
