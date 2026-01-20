"""
Post-Editing Layer (Deterministic + optional LanguageTool)

Purpose
- Improve linguistic quality of MT output while enforcing strict terminology rules.
- Provide a low-latency deterministic mode ("fast") and an optional grammar mode ("full")
  using LanguageTool with a policy that protects glossary terms.

Core concepts
- protected_terms:
  Terms that must never be altered by post-editing. Any LanguageTool suggestion that
  overlaps these spans is ignored.
- inflectable_terms:
  Terms that may be altered only in tightly controlled ways, primarily safe German
  inflection endings (n, s, es). This allows grammatical integration of glossary
  terms without letting a grammar tool rewrite domain terminology.

Modes
- fast:
  Deterministic heuristics only (regex based), no LanguageTool instantiation.
  Lowest latency, stable behavior, reproducible results.
- full:
  fast layer + LanguageTool suggestions, restricted by the term policy.
  Heavier runtime footprint due to tool startup and analysis.

Implementation summary
- Span detection: locate occurrences of protected_terms and inflectable_terms.
- Policy application:
  - Protected spans: never edit.
  - Inflectable spans: allow only safe edits and only for German text.
- Deterministic German inflector:
  - Fixes common MT errors around prepositions and cases (genitive, dative plural).
"""

import re
from typing import Iterable, List, Optional, Tuple

import language_tool_python


# -----------------------------------------------------------------------------
# Span utilities
# -----------------------------------------------------------------------------
def _find_term_spans(text: str, terms: Iterable[str]) -> List[Tuple[int, int]]:
    """
    Find all occurrences of each term in the given text.

    Notes
    - Terms are deduplicated and searched longest-first to reduce partial overlap issues.
    - Word boundaries are enforced via negative lookbehind/lookahead to avoid matching
      inside other words.
    - The function returns raw spans and does not attempt to resolve overlaps. The
      caller must decide how to handle conflicts.
    """
    spans: List[Tuple[int, int]] = []
    for term in sorted({t for t in terms if t and t.strip()}, key=len, reverse=True):
        pat = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    """Return True if the ranges [a0, a1) and [b0, b1) overlap."""
    return not (a1 <= b0 or b1 <= a0)


# -----------------------------------------------------------------------------
# Safe-edit policy for German inflection
# -----------------------------------------------------------------------------
def _allow_term_edit_de(original: str, replacement: str) -> bool:
    """
    Allow only safe inflection-like edits for German glossary terms.

    Allowed patterns
    - Genitive: TERM -> TERM+s / TERM+es
      Example: Stellwerk -> Stellwerks, Stellwerkes
    - Dative plural: TERM -> TERM+n
      Example: Gleise -> Gleisen

    Blocked
    - Any stem-changing edit or non-suffix edit
    - Adding s/es to terms that end with 'en' (conservative anti-plural heuristic)

    Rationale
    - We only accept suffix-only transformations to avoid term corruption.
    - This is intentionally conservative and should be extended only with clear
      linguistic rules and unit tests.
    """
    o = original.strip()
    r = replacement.strip()
    if not o or not r:
        return False

    # Only accept edits that keep the original as a strict prefix and add suffix characters.
    if not r.startswith(o) or len(r) <= len(o):
        return False

    suffix = r[len(o):]
    if not re.fullmatch(r"[A-Za-zÄÖÜäöüß]{1,3}", suffix):
        return False

    o_l = o.lower()
    s_l = suffix.lower()

    # Dative plural: add -n, but do not add -n to words that already end in letters where it is usually invalid.
    if s_l == "n":
        if o_l.endswith(("n", "s", "ß", "x", "z")):
            return False
        return True

    # Genitive: add -s or -es, but avoid "TERM+ s/es" for terms ending in -en (often plural-like or derived forms).
    if s_l in {"s", "es"}:
        if o_l.endswith("en"):
            return False
        return True

    return False


def _term_pattern_with_suffix_de(term: str) -> str:
    """
    Build a regex pattern that matches a term plus an optional safe suffix on the last token.

    Allowed suffixes on the last token: n, s, es

    This is used for phrase-level replacements, where LanguageTool might propose
    a multi-token rewrite. We validate that the rewritten phrase still contains the
    term, possibly with an inflection suffix.
    """
    parts = term.split()
    if not parts:
        return re.escape(term)

    last = re.escape(parts[-1])
    prefix = r"\s+".join(re.escape(p) for p in parts[:-1])

    last_pat = rf"{last}(?:n|s|es)?"
    if prefix:
        return rf"{prefix}\s+{last_pat}"
    return last_pat


def _allow_phrase_edit_with_term_de(term_in_text: str, replacement: str) -> bool:
    """
    Allow LanguageTool phrase replacements if the replacement still contains the glossary term.

    Rules
    - The replacement must contain the term (case-insensitive), optionally with a safe suffix.
    - Blocks adding s/es to terms ending with 'en' (to avoid "Gleisarbeiten" -> "Gleisarbeitens").

    This is used for matches where the LT replacement spans a larger phrase than the term itself.
    """
    term_in_text = term_in_text.strip()
    replacement = replacement.strip()
    if not term_in_text or not replacement:
        return False

    pat = re.compile(rf"(?<!\w){_term_pattern_with_suffix_de(term_in_text)}(?!\w)", re.IGNORECASE)
    if not pat.search(replacement):
        return False

    if term_in_text.lower().endswith("en"):
        last = term_in_text.split()[-1]
        bad_pat = re.compile(rf"(?<!\w){re.escape(last)}(?:s|es)(?!\w)", re.IGNORECASE)
        if bad_pat.search(replacement):
            return False

    return True


# -----------------------------------------------------------------------------
# LanguageTool application with protected/inflectable policy
# -----------------------------------------------------------------------------
def _apply_matches_with_term_policy(
    text: str,
    matches,
    protected_spans: List[Tuple[int, int]],
    inflectable_spans: List[Tuple[int, int]],
    lang: str,
) -> str:
    """
    Apply LanguageTool matches while enforcing the glossary policy.

    Policy
    - Protected spans: never accept edits that overlap these spans.
    - Inflectable spans:
      - Only supported for German text.
      - If the match exactly covers the term span, allow only safe suffix edits.
      - If the match covers a broader phrase that overlaps the term, allow the edit
        only if the replacement still contains the term (optionally with safe suffix).

    Implementation details
    - Matches are applied from right to left (descending offset) so index positions
      remain valid after replacements.
    - For simplicity and determinism, only the first suggested replacement is used.
    """
    out = text
    matches_sorted = sorted(matches, key=lambda m: m.offset, reverse=True)

    for m in matches_sorted:
        replacements = getattr(m, "replacements", None)
        if not replacements:
            continue
        replacement = replacements[0]

        start = getattr(m, "offset")
        length = getattr(m, "error_length", None)
        if length is None:
            length = getattr(m, "errorLength")
        end = start + length

        # Protected spans: never edit.
        if any(_overlaps(start, end, s0, s1) for (s0, s1) in protected_spans):
            continue

        # Inflectable spans: edit only if it is a safe German rule.
        overlaps_inflectable = [(s0, s1) for (s0, s1) in inflectable_spans if _overlaps(start, end, s0, s1)]
        if overlaps_inflectable:
            if not lang.lower().startswith("de"):
                continue

            s0, s1 = overlaps_inflectable[0]
            term_text = text[s0:s1]

            # Exact term replacement: allow only suffix-only inflection edits.
            if start == s0 and end == s1:
                if not _allow_term_edit_de(term_text, replacement):
                    continue
            else:
                # Phrase replacement: allow only if the replacement still contains the term.
                if not _allow_phrase_edit_with_term_de(term_text, replacement):
                    continue

        out = out[:start] + replacement + out[end:]

    # Normalize whitespace after edits.
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


# -----------------------------------------------------------------------------
# Deterministic German grammar corrections (heuristics)
# -----------------------------------------------------------------------------
class _GermanInflector:
    """
    Deterministic post-edit for German around glossary terms.

    Scope
    - Applies generic grammar fixes only when the involved noun is in inflectable_terms.
    - Intended to correct common MT errors without introducing non-deterministic behavior.

    Fixes
    - Genitive with "des":
      - For non-plural-like terms: "des TERM" -> "des TERM+s/es"
      - For terms ending with "en" (conservative plural-like heuristic): "des TERM" -> "der TERM"
    - Dative plural after "an der/an dem":
      - "an der TERM" -> "an den TERM+n"
      - "an dem TERM" -> "an den TERM+n"

    Notes
    - The heuristics are intentionally conservative. If you broaden them, add tests.
    """

    @staticmethod
    def _fallback_genitive(head: str) -> str:
        """Form a conservative genitive suffix for German nouns."""
        low = head.lower()
        if low.endswith(("s", "ß", "x", "z")):
            return head + "es"
        return head + "s"

    @staticmethod
    def _is_plural_like_en(term: str) -> bool:
        """Conservative heuristic: treat terms ending with 'en' as plural-like."""
        return term.strip().lower().endswith("en")

    @staticmethod
    def _can_add_dative_n(term: str) -> bool:
        """Avoid adding -n when the term already ends with letters that commonly block it."""
        low = term.strip().lower()
        return not low.endswith(("n", "s", "ß", "x", "z"))

    def _genitive_term(self, term: str) -> str:
        """Apply genitive suffix to the last token of a multi-token term."""
        parts = term.split()
        if not parts:
            return term
        head = parts[-1]
        gen_head = self._fallback_genitive(head)
        return " ".join(parts[:-1] + [gen_head])

    def _dative_plural_term(self, term: str) -> str:
        """Apply a conservative dative plural -n to the last token if allowed."""
        parts = term.split()
        if not parts:
            return term
        head = parts[-1]
        if self._can_add_dative_n(head):
            head = head + "n"
        return " ".join(parts[:-1] + [head])

    def apply(self, text: str, inflectable_terms: Iterable[str]) -> str:
        """
        Apply deterministic corrections to the provided text.

        Terms are processed longest-first to avoid partial replacement side effects.
        """
        out = text
        terms = sorted({t for t in inflectable_terms if t and t.strip()}, key=len, reverse=True)

        for base in terms:
            # 1) Genitive with "des"
            if self._is_plural_like_en(base):
                out = re.sub(
                    rf"\bdes\s+{re.escape(base)}\b",
                    f"der {base}",
                    out,
                    flags=re.IGNORECASE,
                )
            else:
                gen = self._genitive_term(base)
                out = re.sub(
                    rf"\bdes\s+{re.escape(base)}\b",
                    f"des {gen}",
                    out,
                    flags=re.IGNORECASE,
                )

            # 2) Dative plural after "an der/an dem"
            dpl = self._dative_plural_term(base)

            out = re.sub(
                rf"\ban\s+der\s+{re.escape(base)}\b",
                f"an den {dpl}",
                out,
                flags=re.IGNORECASE,
            )

            out = re.sub(
                rf"\ban\s+dem\s+{re.escape(base)}\b",
                f"an den {dpl}",
                out,
                flags=re.IGNORECASE,
            )

        out = re.sub(r"\s{2,}", " ", out).strip()
        return out


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
class PostEditor:
    """
    Post-edit orchestrator.

    Responsibilities
    - Provide a single call interface correct(text, protected_terms, inflectable_terms).
    - Execute deterministic corrections always (fast layer).
    - Optionally apply LanguageTool suggestions (full layer) under strict term policy.

    Lifecycle
    - start(): instantiate LanguageTool only if mode is "full"
    - close(): release LanguageTool resources
    """

    def __init__(self, lang: str = "de-DE", mode: str = "fast") -> None:
        self.lang = lang
        self.mode = mode.lower()
        self._tool: Optional[language_tool_python.LanguageTool] = None
        self._de_inflector = _GermanInflector() if lang.lower().startswith("de") else None

    def start(self) -> None:
        """
        Lazily create the LanguageTool backend.

        This is intentionally deferred because LanguageTool startup can be expensive.
        """
        if self.mode == "full" and self._tool is None:
            self._tool = language_tool_python.LanguageTool(self.lang)

    def correct(
        self,
        text: str,
        protected_terms: Optional[Iterable[str]] = None,
        inflectable_terms: Optional[Iterable[str]] = None,
    ) -> str:
        """
        Apply post-editing to a text segment.

        Parameters
        - text:
          Input text to correct.
        - protected_terms:
          Glossary terms that must never be changed.
        - inflectable_terms:
          Glossary terms that may be changed only in safe, controlled ways.

        Returns
        - Corrected text, with whitespace normalized.
        """
        if not text:
            return text

        protected_terms = protected_terms or []
        inflectable_terms = inflectable_terms or []

        out = text

        # FAST deterministic layer (always enabled).
        if self._de_inflector is not None and inflectable_terms:
            out = self._de_inflector.apply(out, inflectable_terms)

        if self.mode != "full":
            return out

        # FULL: LanguageTool + term policy.
        self.start()
        assert self._tool is not None

        protected_spans = _find_term_spans(out, protected_terms)
        inflectable_spans = _find_term_spans(out, inflectable_terms)

        matches = self._tool.check(out)
        out = _apply_matches_with_term_policy(
            out,
            matches,
            protected_spans=protected_spans,
            inflectable_spans=inflectable_spans,
            lang=self.lang,
        )

        # Apply deterministic layer again in case LT changed surrounding tokens.
        if self._de_inflector is not None and inflectable_terms:
            out = self._de_inflector.apply(out, inflectable_terms)

        return out

    def close(self) -> None:
        """Release LanguageTool resources if initialized."""
        if self._tool is not None:
            self._tool.close()
            self._tool = None
