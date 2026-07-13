from __future__ import annotations

import logging
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("bezkolejki_bot")


def strip_diacritics(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_label(s: str) -> str:
    return strip_diacritics(s).lower().strip()


def fuzzy_match_field(field_label: str, form_data: dict) -> Optional[str]:
    """Match a form field's label against configured FORM_* keys.

    Exact (normalized) match wins. Otherwise fall back to substring matching in
    either direction, but pick the MOST SPECIFIC (longest normalized key) among
    all candidates — several keys share the token "numer " (numer sprawy /
    dokumentu / paszportu), and picking arbitrarily by dict order could submit
    the wrong ID number to a government form. Ambiguity is logged loudly."""
    norm_label = normalize_label(field_label)
    # exact match first
    for key, value in form_data.items():
        if normalize_label(key) == norm_label:
            return value
    # substring match either direction — collect all, prefer the longest key
    candidates = []
    for key, value in form_data.items():
        nk = normalize_label(key)
        if nk and (nk in norm_label or norm_label in nk):
            candidates.append((nk, key, value))
    if not candidates:
        return None
    candidates.sort(key=lambda c: len(c[0]), reverse=True)
    if len(candidates) > 1:
        logger.warning(
            "Field %r fuzzy-matched %d config keys %s — using most specific (%r). "
            "Verify this is correct before confirming!",
            field_label, len(candidates), [c[1] for c in candidates], candidates[0][1],
        )
    return candidates[0][2]


def build_filled_properties(field_defs: Any, form_data: dict) -> list:
    """
    field_defs: whatever GetPropertiesForSlot returned (list of field definition
    dicts, expected to have at least a "name"/"label" and a value slot). Since
    the exact shape is unverified, this is defensive: it looks for common key
    names and fills what it can, leaving the rest of the structure untouched
    so UpdateSlotProperties gets a shape as close as possible to what the site
    expects.
    """
    if not isinstance(field_defs, list):
        logger.warning("GetPropertiesForSlot did not return a list (got %s); cannot auto-fill.",
                        type(field_defs))
        return []

    filled = []
    for f in field_defs:
        if not isinstance(f, dict):
            filled.append(f)
            continue
        label = f.get("name") or f.get("label") or f.get("propertyName") or f.get("displayName") or ""
        match = fuzzy_match_field(str(label), form_data) if label else None
        new_field = dict(f)
        if match is not None:
            # Only set value-keys that ACTUALLY exist on the field; invent a
            # plain "value" only if none of the known value-keys were present.
            # (Previously this always injected a spurious "value" key, risking a
            # payload shape the API rejects.)
            present = [k for k in ("value", "propertyValue", "fieldValue") if k in new_field]
            if present:
                for k in present:
                    new_field[k] = match
            else:
                new_field["value"] = match
            logger.info("Matched form field %r -> %r", label, match)
        filled.append(new_field)
    return filled
