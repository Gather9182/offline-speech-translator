"""
DEPRECATED: German Glossary Morphology Helper (Experimental)

Status
- This module is currently **deprecated** and not part of the active runtime path.
- It was created as an experimental helper to fix a small set of German genitive
  constructions around glossary terms via deterministic regex rules.
- Keep it only if you want a reference for future morphology work or if you plan
  to re-introduce it explicitly in the post-edit pipeline.

Purpose (historical)
- Apply lightweight German morphology corrections for glossary terms in common
  genitive contexts, for example:
  - "aufgrund des TERM" → correct article + genitive term form
  - "in der Nähe des TERM" → correct article + genitive term form
  - Fix common MT mistake: "in der Nähe das TERM"

Approach
- Uses a minimal metadata model (gender/number) and conservative heuristics:
  - plural stays unchanged
  - masculine/neuter singular: add -s or -es (rough heuristic)
  - feminine: generally unchanged
- Applies replacements using regex, processing longer terms first to avoid
  partial matches in multi-word terms.

Notes / limitations
- This is not a full German morphology engine.
- It only covers a narrow set of patterns and relies on provided term metadata.
- The active project currently enforces terminology primarily through:
  - direct term injection in the glossary layer, and
  - post-edit policy (protected vs safely inflectable terms).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional


# -----------------------------------------------------------------------------
# Data model: minimal metadata for German nouns
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class DeTermMeta:
    """
    Minimal metadata used to decide the genitive article and term form.

    gender:
      - "m" = masculine
      - "f" = feminine
      - "n" = neuter

    number:
      - "sg" = singular
      - "pl" = plural
    """
    gender: Optional[str] = None  # m/f/n
    number: Optional[str] = None  # sg/pl


# -----------------------------------------------------------------------------
# Small deterministic helpers (genitive article + term form)
# -----------------------------------------------------------------------------
def _genitive_article(meta: DeTermMeta) -> Optional[str]:
    """
    Determine the German genitive article based on noun metadata.

    Rules (conservative):
    - plural: "der"
    - feminine: "der"
    - masculine/neuter: "des"
    - unknown: return None (skip correction)
    """
    if meta.number == "pl":
        return "der"
    if meta.gender == "f":
        return "der"
    if meta.gender in ("m", "n"):
        return "des"
    return None


def _genitive_form(term: str, meta: DeTermMeta) -> str:
    """
    Compute a simple genitive form for the noun term.

    Notes:
    - plural usually remains unchanged in genitive contexts
      (e.g., \"der Gleisarbeiten\").
    - masculine/neuter singular typically adds -s or -es.
    - feminine is usually unchanged in genitive.

    This is heuristic-based and intentionally conservative.
    """
    # Plural typically remains unchanged: "der Gleisarbeiten"
    if meta.number == "pl":
        return term

    # Masculine/neuter singular often uses -s or -es
    if meta.gender in ("m", "n"):
        lower = term.lower()

        # Rough heuristic: words ending in s/ß/x/z often prefer -es
        if lower.endswith(("s", "ß", "x", "z")):
            return term + "es"
        return term + "s"

    # Feminine: usually unchanged in genitive
    return term


# -----------------------------------------------------------------------------
# Public API (deprecated)
# -----------------------------------------------------------------------------
def apply_glossary_morphology_de(text: str, term_meta_by_base: Dict[str, DeTermMeta]) -> str:
    """
    DEPRECATED: Apply deterministic genitive fixes around glossary terms (German).

    Fixes common genitive contexts around glossary terms:
    - "aufgrund des TERM" -> correct article and TERM genitive if needed
    - "in der Nähe des TERM" -> correct article and TERM genitive if needed
    Also fixes the frequent MT error "in der Nähe das TERM".

    Parameters
    - text:
        Input text to post-process.
    - term_meta_by_base:
        Mapping from base term (as it appears in text) to minimal metadata
        (gender/number). If metadata is missing or insufficient, the term is
        skipped.

    Returns
    - Corrected text with normalized whitespace.
    """
    out = text

    # Process longer terms first (safer for multiword terms).
    # This reduces the chance of correcting a sub-phrase inside a longer term.
    for base in sorted(term_meta_by_base.keys(), key=len, reverse=True):
        meta = term_meta_by_base[base]
        art = _genitive_article(meta)
        if not art:
            continue

        gen_term = _genitive_form(base, meta)

        # 1) "aufgrund de(s|r) TERM" -> "aufgrund <art> <gen_term>"
        out = re.sub(
            rf"\baufgrund\s+de(s|r)\s+{re.escape(base)}\b",
            f"aufgrund {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

        # 2) "in der Nähe de(s|r) TERM" -> "in der Nähe <art> <gen_term>"
        out = re.sub(
            rf"\bin\s+der\s+Nähe\s+de(s|r)\s+{re.escape(base)}\b",
            f"in der Nähe {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

        # 3) "in der Nähe das TERM" (common MT mistake)
        out = re.sub(
            rf"\bin\s+der\s+Nähe\s+das\s+{re.escape(base)}\b",
            f"in der Nähe {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

    # Whitespace cleanup
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out
