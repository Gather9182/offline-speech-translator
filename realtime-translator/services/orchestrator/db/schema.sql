-- Create required extension (pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

-- Main terminology table
CREATE TABLE IF NOT EXISTS public.glossary_terms (
  id bigserial PRIMARY KEY,
  src_lang text NOT NULL,
  tgt_lang text NOT NULL,
  src_term text NOT NULL,
  tgt_term text NOT NULL,
  mode text NOT NULL,
  priority int NOT NULL DEFAULT 0,
  meta jsonb NULL,
  embedding vector(384) NULL
);

-- Uniqueness constraint for idempotent upserts
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

-- Helpful indexes
CREATE INDEX IF NOT EXISTS glossary_terms_lang_idx
  ON public.glossary_terms (src_lang, tgt_lang);

-- Optional vector index (enable when you use postgres_vector)
-- Note: HNSW needs a recent pgvector build. If it fails, use IVFFLAT instead.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'glossary_terms_embedding_idx'
      AND n.nspname = 'public'
  ) THEN
    BEGIN
      EXECUTE 'CREATE INDEX glossary_terms_embedding_idx
               ON public.glossary_terms
               USING hnsw (embedding vector_cosine_ops)';
    EXCEPTION WHEN OTHERS THEN
      EXECUTE 'CREATE INDEX glossary_terms_embedding_idx
               ON public.glossary_terms
               USING ivfflat (embedding vector_cosine_ops)
               WITH (lists = 50)';
    END;
  END IF;
END $$;

ANALYZE public.glossary_terms;
