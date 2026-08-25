"""Deduplication and normalization key construction.

The generator and the ingester must agree on these functions exactly. If they
diverge, ground-truth links stop resolving and every reported metric becomes
meaningless, so both sides import from here rather than reimplementing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from reconproof.domain.entities import SourceKind

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")


def dedupe_key(source_kind: SourceKind, external_id: str | None, fallback: Any = None) -> str:
    """Natural key for one source row.

    When the source provides a stable identifier, the key is derived from it, so
    re-delivering the same webhook or re-uploading an overlapping settlement
    export collapses onto the existing row instead of double-counting.

    A row with *no* identifier falls back to a content hash. That is weaker: two
    genuinely distinct rows with identical content will collide. Bank statements
    are the real case here, and a same-day same-amount duplicate credit is
    exactly what the duplicate detector is meant to surface for review rather
    than silently merge.
    """
    if external_id:
        return f"{source_kind.value}:{external_id.strip()}"
    payload = repr(fallback)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{source_kind.value}:sha:{digest}"


def normalize_reference(value: str | None) -> str | None:
    """Collapse a reference to comparable form.

    Bank statements reformat provider references: ``RZPY-STLMNT/884193`` and
    ``rzpy stlmnt 884193`` are the same reference wearing different punctuation.
    Case folding and stripping non-alphanumerics makes them joinable.
    """
    if value is None:
        return None
    folded = _NON_ALNUM.sub("", value.strip().lower())
    return folded or None


def normalize_description(value: str | None) -> str | None:
    """Normalize free-text narration for similarity scoring."""
    if value is None:
        return None
    lowered = value.strip().lower()
    lowered = _NON_ALNUM.sub(" ", lowered)
    collapsed = _WHITESPACE.sub(" ", lowered).strip()
    return collapsed or None


def reference_tail(value: str | None, length: int = 6) -> str | None:
    """Trailing digits of a reference.

    Truncation in bank narration usually preserves the tail, so the last few
    digits are often the only surviving join key.
    """
    normalized = normalize_reference(value)
    if not normalized:
        return None
    digits = "".join(ch for ch in normalized if ch.isdigit())
    return digits[-length:] if len(digits) >= length else (digits or None)
