"""
Postgres Term Store (classic SQL-backed terminology loading)

Purpose
- Provide a simple PostgreSQL-backed terminology source without vector similarity search.
- Load all terms for a given language direction (src_lang -> tgt_lang) into memory so the
  glossary layer can apply them deterministically.

When to use
- rag.backend = "postgres" in the application config.
- Small to medium terminology sizes where "load all and apply" remains performant.
- Scenarios where deterministic ordering (priority, term length) is preferred over semantic retrieval.

Behavior
- Maintains a persistent database connection.
- Loads terms filtered by language direction.
- Orders results for stable application order:
  - priority DESC: higher priority terms are applied first
  - LENGTH(src_term) DESC: longer terms first to avoid partial matches overriding longer phrases

Safety / limits
- max_terms provides a hard cap to avoid accidentally loading an unbounded amount of data
  into memory (e.g., if a filter is misconfigured).

Database requirements
- Table: public.glossary_terms with columns:
  src_lang, tgt_lang, src_term, tgt_term, mode, priority
- A basic index on (src_lang, tgt_lang) is recommended for fast filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import psycopg

from app.glossary import Term  # canonical Term structure used by the glossary pipeline


# -----------------------------------------------------------------------------
# Configuration model
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class PgCfg:
    """
    Configuration for PostgresTermStore.

    Attributes
    - dsn:
      PostgreSQL DSN used to connect to the terminology database.
      Typically sourced from cfg["rag"]["pg_dsn"] in the main application config.
    - max_terms:
      Hard safety cap for loaded terms per language direction to keep memory usage bounded.
    """

    dsn: str
    max_terms: int = 20000  # cap safety


# -----------------------------------------------------------------------------
# Term store implementation
# -----------------------------------------------------------------------------
class PostgresTermStore:
    """
    Classic (non-vector) terminology store backed by PostgreSQL.

    Responsibilities
    - Maintain a DB connection for the lifetime of the process.
    - Load all terms for a language direction on demand (usually once per startup
      or per language switch).
    - Return a list of Term objects in a deterministic application order.

    Lifecycle
    - Instantiate once during app startup.
    - Call close() on shutdown.
    """

    def __init__(self, cfg: PgCfg) -> None:
        self.cfg = cfg
        self._conn = psycopg.connect(cfg.dsn)

    def close(self) -> None:
        """
        Close the database connection.

        Note
        - Unlike the vector store, this method does not swallow exceptions. If you want
          best-effort shutdown behavior, wrap this call in a try/except at the caller.
        """
        self._conn.close()

    def load_all(self, src_lang: str, tgt_lang: str) -> List[Term]:
        """
        Load all terminology entries for a given language direction.

        Args
        - src_lang / tgt_lang:
          Filter by language direction. Only rows matching this direction are returned.

        Ordering
        - priority DESC:
          Ensures that critical terms are applied before lower-priority ones.
        - LENGTH(src_term) DESC:
          Ensures longer phrases are applied before shorter substrings (reduces partial match issues).

        Returns
        - List[Term]:
          Terms ready to be consumed by apply_glossary().
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT src_lang, tgt_lang, src_term, tgt_term, mode, priority
                FROM public.glossary_terms
                WHERE src_lang = %s AND tgt_lang = %s
                ORDER BY priority DESC, LENGTH(src_term) DESC
                LIMIT %s
                """,
                (src_lang, tgt_lang, self.cfg.max_terms),
            )
            rows = cur.fetchall()

        terms: List[Term] = []
        for r in rows:
            terms.append(
                Term(
                    src_lang=r[0],
                    tgt_lang=r[1],
                    src_term=r[2],
                    tgt_term=r[3],
                    mode=(r[4] or "").strip().lower(),
                    priority=int(r[5] or 0),
                )
            )

        return terms
