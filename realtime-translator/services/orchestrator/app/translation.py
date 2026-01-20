"""
Translation Backends (Argos + optional ModernMT)

Purpose
- Provide a small, testable translation abstraction for the realtime pipeline.
- Support multiple MT backends behind a single `Translator.translate()` method:
  - Argos Translate (offline, local)
  - ModernMT (optional, HTTP endpoint)

Design notes
- This module intentionally does not load any configuration files directly.
  The caller (e.g., realtime_translate.py) is responsible for:
  - loading config.json
  - validating values
  - constructing TranslationConfig with the chosen parameters

Config mapping in the current app
- realtime_translate.py constructs TranslationConfig from cfg["mt"]:
  - backend
  - src_lang
  - tgt_lang
  - modernmt_url
  - Important: timeout_s is currently not passed by realtime_translate.py, so the default (10.0)
    is used unless the caller explicitly sets it. :contentReference[oaicite:0]{index=0}

Operational behavior
- Argos: direct local translate call, no networking.
- ModernMT: HTTP GET request with query parameters (q, source, target, optional context).
  The response is parsed in a tolerant way to support the common ModernMT JSON shapes.

Error handling
- Unknown backend raises ValueError.
- HTTP errors raise via requests.raise_for_status().
- Unexpected response shape raises RuntimeError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import argostranslate.translate
import requests


# -----------------------------------------------------------------------------
# Configuration model
# -----------------------------------------------------------------------------
@dataclass
class TranslationConfig:
    """
    Configuration for machine translation.

    Attributes
    - backend:
      Identifier of the translation backend.
      Supported values: "argos" or "modernmt".
    - src_lang / tgt_lang:
      ISO-like language codes as expected by the chosen backend.
      In the current app these values come from cfg["mt"]["src_lang"] / cfg["mt"]["tgt_lang"]. :contentReference[oaicite:1]{index=1}
    - modernmt_url:
      HTTP endpoint for ModernMT. Only used when backend == "modernmt".
    - timeout_s:
      HTTP timeout (seconds) for ModernMT requests.
      Note: If the caller never sets this, the default applies.
    """

    backend: str = "argos"  # "argos" oder "modernmt"
    src_lang: str = "en"
    tgt_lang: str = "de"
    modernmt_url: str = "http://127.0.0.1:8045/translate"
    timeout_s: float = 10.0

    @classmethod
    def from_dict(cls, mt_cfg: Dict[str, Any]) -> "TranslationConfig":
        """
        Convenience helper to build TranslationConfig from a config.json section.

        Expected keys (all optional except src_lang/tgt_lang depending on your app policy):
        - backend
        - src_lang
        - tgt_lang
        - modernmt_url
        - timeout_s

        This helper is optional. You can keep constructing TranslationConfig manually.
        """
        return cls(
            backend=str(mt_cfg.get("backend", "argos")).strip().lower(),
            src_lang=str(mt_cfg.get("src_lang", "en")).strip().lower(),
            tgt_lang=str(mt_cfg.get("tgt_lang", "de")).strip().lower(),
            modernmt_url=str(mt_cfg.get("modernmt_url", "http://127.0.0.1:8045/translate")).strip(),
            timeout_s=float(mt_cfg.get("timeout_s", 10.0)),
        )


# -----------------------------------------------------------------------------
# Translator facade
# -----------------------------------------------------------------------------
class Translator:
    """
    Translator facade over multiple MT backends.

    The realtime app uses this object as a stable interface:
    - create once during startup
    - reuse for all segments to avoid repeated initialization costs

    Threading note
    - This class is currently used in a single-threaded pipeline loop.
      If you later parallelize translation calls, ensure the chosen backend is
      thread-safe (HTTP usually is; Argos depends on its internal implementation).
    """

    def __init__(self, cfg: TranslationConfig):
        self.cfg = cfg

    def translate(
        self,
        text: str,
        src: Optional[str] = None,
        tgt: Optional[str] = None,
        context: str = "",
    ) -> str:
        """
        Translate a text from source language to target language.

        Args
        - text:
          Input text to translate.
        - src / tgt:
          Optional override for source/target language codes.
          If not provided, defaults from TranslationConfig are used.
        - context:
          Optional context string (ModernMT supports context, Argos ignores it).

        Returns
        - Translated text.

        Raises
        - ValueError: if an unknown backend is configured.
        - requests.HTTPError: for ModernMT HTTP failures.
        - RuntimeError: for unexpected ModernMT response shapes.
        """
        src = src or self.cfg.src_lang
        tgt = tgt or self.cfg.tgt_lang

        backend = (self.cfg.backend or "").strip().lower()

        if backend == "argos":
            return argostranslate.translate.translate(text, src, tgt)

        if backend == "modernmt":
            return self._translate_modernmt(text, src, tgt, context)

        raise ValueError(f"Unknown backend: {self.cfg.backend}")

    def _translate_modernmt(self, text: str, src: str, tgt: str, context: str) -> str:
        """
        Translate via ModernMT HTTP API.

        Request shape
        - GET {modernmt_url}?q=...&source=...&target=...&context=...

        Response parsing
        - Common ModernMT JSON variants are supported:
          - {"data": {"translation": "..."}}
          - {"translation": "..."}
        """
        params = {
            "q": text,
            "source": src,
            "target": tgt,
        }
        if context:
            params["context"] = context

        r = requests.get(self.cfg.modernmt_url, params=params, timeout=self.cfg.timeout_s)
        r.raise_for_status()
        data = r.json()

        if "data" in data and isinstance(data["data"], dict) and "translation" in data["data"]:
            return data["data"]["translation"]

        if "translation" in data:
            return data["translation"]

        raise RuntimeError(f"Unexpected ModernMT response: {data}")
