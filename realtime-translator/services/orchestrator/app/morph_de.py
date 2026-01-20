from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class DeTermMeta:
    gender: Optional[str] = None  # m/f/n
    number: Optional[str] = None  # sg/pl


def _genitive_article(meta: DeTermMeta) -> Optional[str]:
    if meta.number == "pl":
        return "der"
    if meta.gender == "f":
        return "der"
    if meta.gender in ("m", "n"):
        return "des"
    return None


def _genitive_form(term: str, meta: DeTermMeta) -> str:
    # Plural bleibt in der Regel unverändert: "der Gleisarbeiten"
    if meta.number == "pl":
        return term

    # Für m/n Singular oft -s oder -es
    if meta.gender in ("m", "n"):
        lower = term.lower()
        # grobe Heuristik: Wörter auf s/ß/x/z bekommen eher -es
        if lower.endswith(("s", "ß", "x", "z")):
            return term + "es"
        return term + "s"

    # feminin: i.d.R. unverändert im Genitiv
    return term


def apply_glossary_morphology_de(text: str, term_meta_by_base: Dict[str, DeTermMeta]) -> str:
    """
    Fix common genitive contexts around glossary terms:
    - "aufgrund des TERM" -> correct article and TERM genitive if needed
    - "in der Nähe des TERM" -> correct article and TERM genitive if needed
    Also fixes the frequent MT error "in der Nähe das TERM".
    """
    out = text

    # Process longer terms first (safer for multiword terms)
    for base in sorted(term_meta_by_base.keys(), key=len, reverse=True):
        meta = term_meta_by_base[base]
        art = _genitive_article(meta)
        if not art:
            continue

        gen_term = _genitive_form(base, meta)

        # 1) aufgrund de(s|r) TERM
        out = re.sub(
            rf"\baufgrund\s+de(s|r)\s+{re.escape(base)}\b",
            f"aufgrund {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

        # 2) in der Nähe de(s|r) TERM
        out = re.sub(
            rf"\bin\s+der\s+Nähe\s+de(s|r)\s+{re.escape(base)}\b",
            f"in der Nähe {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

        # 3) in der Nähe das TERM  (common MT mistake)
        out = re.sub(
            rf"\bin\s+der\s+Nähe\s+das\s+{re.escape(base)}\b",
            f"in der Nähe {art} {gen_term}",
            out,
            flags=re.IGNORECASE,
        )

    # whitespace cleanup
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out
