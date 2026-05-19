"""Local-only redaction pipeline for PR-8 jushi.

Two layers, both ``re.IGNORECASE``:

1. Keyword layer -- substring presence (case-insensitive). Triggers an
   *entire-row* drop. Use for words that should never co-occur with
   real prose in a dev log: ``password``, ``api_key``, etc.

2. Regex layer -- one row drops if any pattern matches. Use for
   structured secrets where shape is the giveaway: private IPs, long
   hex digests, AWS keys.

The "drop the whole row" choice is intentional. Inline replacement
(``****``) preserves the surrounding context and can leak secrets
through that context. We accept losing a row over half-leaking one.

Two output modes:

* ``placeholder`` (default) -- write a one-line ``[redacted: ...]``
  marker so the time series stays inspectable.
* ``drop`` -- write nothing.

Configuration extension lives in ``court.yaml``'s ``jushi:`` block:

    jushi:
      redact_extra_keywords: ["my-secret-keyword"]
      redact_extra_patterns: ['my-internal-id-[0-9]+']
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------

DEFAULT_KEYWORDS: tuple[str, ...] = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "secret_key",
    "secretkey",
    "private_key",
    "privatekey",
    "ssh-rsa",
    "-----begin",        # PEM blocks
    "bearer ",
    "authorization:",
    "mysql://",
    "mongodb://",
    "postgres://",
    "postgresql://",
    "redis://",
)


DEFAULT_PATTERNS: tuple[str, ...] = (
    # Private IPv4 ranges
    r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    # Long hex digest (sha-256 etc.) -- 32+ chars
    r"\b[a-f0-9]{32,}\b",
    # AWS access keys
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    # GitHub fine-grained / classic tokens
    r"\bghp_[A-Za-z0-9]{36}\b",
    r"\bgho_[A-Za-z0-9]{36}\b",
    r"\bghu_[A-Za-z0-9]{36}\b",
    r"\bghs_[A-Za-z0-9]{36}\b",
    # JWT-ish three-segment tokens (loose)
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b",
    # OpenAI / Anthropic style sk-... keys (>=20 chars after prefix)
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
)


# ---------------------------------------------------------------------------
# Rule set + result
# ---------------------------------------------------------------------------

@dataclass
class RedactionRules:
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    mode: str = "placeholder"   # placeholder | drop

    _compiled: list[re.Pattern] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def extend(self,
               *,
               extra_keywords: Optional[Iterable[str]] = None,
               extra_patterns: Optional[Iterable[str]] = None) -> "RedactionRules":
        kw = list(self.keywords)
        pat = list(self.patterns)
        if extra_keywords:
            kw.extend(extra_keywords)
        if extra_patterns:
            pat.extend(extra_patterns)
        return RedactionRules(keywords=kw, patterns=pat, mode=self.mode)


@dataclass
class RedactionResult:
    kept: bool                   # True -> use original text; False -> apply rule
    reason: Optional[str] = None # e.g. "keyword=password" or "pattern=..."
    placeholder: Optional[str] = None  # filled in placeholder mode


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_rules(text: str, rules: RedactionRules) -> RedactionResult:
    """Decide whether ``text`` should pass through, be replaced, or be dropped.

    Order: keyword layer first (cheaper), then regex layer. First hit wins;
    we do not enumerate every rule that matches because the row is dead
    after the first anyway.
    """
    lowered = text.lower()
    for kw in rules.keywords:
        if kw.lower() in lowered:
            return _make_failed_result(rules, f"keyword={kw}")

    for pat in rules._compiled:
        if pat.search(text):
            return _make_failed_result(rules, f"pattern={pat.pattern}")

    return RedactionResult(kept=True)


def _make_failed_result(rules: RedactionRules, reason: str) -> RedactionResult:
    if rules.mode == "drop":
        return RedactionResult(kept=False, reason=reason, placeholder=None)
    # placeholder mode (default)
    return RedactionResult(
        kept=False,
        reason=reason,
        placeholder=f"[redacted: {reason}]",
    )
