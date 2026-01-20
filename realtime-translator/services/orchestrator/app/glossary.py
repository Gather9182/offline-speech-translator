"""
Glossary Layer (Terminology Protection / Forcing)

Purpose
- Provide a terminology enforcement layer that runs before MT and (optionally) after MT.
- Ensure domain terms are translated consistently and are protected from unwanted changes
  in downstream steps (MT, post-edit, grammar correction).

Key concepts
- Term:
  A single terminology entry that maps a source term to a target term for a given language
  direction and policy mode.
- mode:
  Controls how the downstream pipeline should treat the injected target term.
  - "protect": term should not be altered by post-editing or grammar tools
  - "force": term may be inflected in a controlled manner (depending on post-edit policy)
- priority:
  Higher priority terms are applied first.

Current implementation note
- Although helper functions exist to create stable placeholders, the current implementation
  does not use placeholders during MT.
- Instead, it directly replaces matched source terms with the target terms in the source
  text before MT. This avoids issues where some MT systems:
  - drop placeholders
  - duplicate placeholders
  - change placeholder tokens in unexpected ways

Pipeline integration
- apply_glossary(text, terms, src_lang, tgt_lang):
  - filters term list for the requested language direction
  - applies replacements in a deterministic order (priority, then term length)
  - returns:
    - protected_text (currently: text with injected target terms)
    - protect_map: terms that must never be altered
    - force_map: terms that may be inflected under strict rules
- restore_glossary(text, protect_map, force_map):
  - currently a no-op in practice because we are not using placeholders
  - kept for API compatibility and future placeholder-based strategies

Operational considerations
- Matching is case-insensitive and avoids matching inside other words.
- Multi-word terms allow flexible whitespace between tokens.
- This layer assumes the term list is curated. Overlapping / conflicting terms should be
  resolved via priority and careful term design.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Term:
    """
    Single terminology entry.

    Attributes
    - src_lang / tgt_lang:
      Language direction in which this mapping applies.
    - src_term:
      Phrase expected in the source text.
    - tgt_term:
      Phrase that should appear in the MT output (injected pre-MT in this implementation).
    - mode:
      Policy flag for post-editing.
      - "protect": never alter tgt_term
      - "force": allow controlled inflection, depending on post-edit rules
    - priority:
      Higher values are applied first to ensure stable conflict resolution.
    """

    src_lang: str
    tgt_lang: str
    src_term: str
    tgt_term: str
    mode: str  # "protect" oder "force"
    priority: int


# -----------------------------------------------------------------------------
# CSV import
# -----------------------------------------------------------------------------
def load_terms(csv_path: Path) -> List[Term]:
    """
    Load terminology entries from a CSV file.

    Expected columns
    - src_lang
    - tgt_lang
    - src_term
    - tgt_term
    - mode
    - priority (optional, defaults to 0)

    Notes
    - This is the classic backend used when rag.backend == "glossary".
    - For postgres/postgres_vector, terms are loaded via the DB term stores instead.
    """
    terms: List[Term] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            terms.append(
                Term(
                    src_lang=row["src_lang"].strip(),
                    tgt_lang=row["tgt_lang"].strip(),
                    src_term=row["src_term"].strip(),
                    tgt_term=row["tgt_term"].strip(),
                    mode=row["mode"].strip().lower(),
                    priority=int(row.get("priority", "0")),
                )
            )
    return terms


# -----------------------------------------------------------------------------
# Placeholder strategy (currently unused)
# -----------------------------------------------------------------------------
def _make_placeholder(i: int) -> str:
    """
    Create a placeholder token that is relatively robust against MT tokenization changes.

    Notes
    - Uses only letters and digits to avoid punctuation and underscores, which some MT
      systems may split or normalize.
    - This is retained for potential future strategies where placeholders are used.
    """
    return f"ZXQTERM{i:04d}ZXQ"


# -----------------------------------------------------------------------------
# Glossary application
# -----------------------------------------------------------------------------
def apply_glossary(
    text: str, terms: List[Term], src_lang: str, tgt_lang: str
) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """
    Apply glossary terms to the given text.

    Behavior
    - Filters the term list by language direction.
    - Applies term replacements in deterministic order:
      1) higher priority first
      2) longer src_term first (reduces partial-match issues)
    - Performs case-insensitive matching and avoids matches inside other words.
    - Multi-word terms allow flexible whitespace between tokens.

    Current strategy
    - Directly injects the target term (tgt_term) into the source text prior to MT.
      This avoids placeholder instability in MT.

    Returns
    - protected_text:
      Text after terminology enforcement (currently: with injected target terms).
    - protect_map:
      Mapping used by post-edit to treat injected terms as immutable.
      Key and value are identical (tgt_term -> tgt_term).
    - force_map:
      Mapping used by post-edit to allow controlled inflection for injected terms.
      Key and value are identical (tgt_term -> tgt_term).

    Notes on naming
    - Historically these were "placeholder -> term" maps.
      Since we inject target terms directly, they act as "term -> term" sets represented
      as dicts for compatibility with existing pipeline code.
    """
    candidates = [
        t
        for t in terms
        if t.src_lang == src_lang and t.tgt_lang == tgt_lang and t.src_term and t.tgt_term
    ]

    # High priority first, then longer phrases first.
    candidates.sort(key=lambda t: (t.priority, len(t.src_term)), reverse=True)

    out = text
    protect_map: Dict[str, str] = {}
    force_map: Dict[str, str] = {}

    # Placeholder counter retained for potential future placeholder strategy.
    placeholder_idx = 1
    _ = placeholder_idx  # keep variable present without altering behavior

    for t in candidates:
        # Match whole phrase (not inside words). Allow flexible whitespace inside multiword terms.
        parts = [re.escape(p) for p in t.src_term.split()]
        pat = r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)"
        rx = re.compile(pat, flags=re.IGNORECASE)

        if not rx.search(out):
            continue

        # Direct injection strategy:
        # Instead of inserting placeholders (which MT can drop/duplicate), insert target term directly.
        out = rx.sub(t.tgt_term, out)

        # Track injected terms so post-edit can protect or safely inflect them.
        if t.mode == "protect":
            protect_map[t.tgt_term] = t.tgt_term
        else:
            force_map[t.tgt_term] = t.tgt_term

    return out, protect_map, force_map


def restore_glossary(text: str, protect_map: Dict[str, str], force_map: Dict[str, str]) -> str:
    """
    Restore glossary placeholders or injected terms after MT.

    Current behavior
    - With direct injection, this function is effectively a defensive cleanup step.
    - It replaces keys found in protect_map/force_map with their mapped values.

    Notes
    - If the placeholder strategy is re-enabled in the future, protect_map and force_map
      would likely contain placeholder keys again.
    """
    out = text

    # Replace longer keys first (defensive).
    for ph, val in sorted({**protect_map, **force_map}.items(), key=lambda x: len(x[0]), reverse=True):
        out = out.replace(ph, val)

    # Whitespace cleanup.
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out
