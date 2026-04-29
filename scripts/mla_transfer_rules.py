"""
Business rules for parsing agent notes into the MLA Transfer Form (Google Form).

- Device: exact Google Form choice labels (see DEVICE_* below).
- First name field: include middle initial with first name (e.g. \"Donn H\" not split).
- Center name: always HS.
- First time getting a device?: Yes if no prior-device cues; No if note suggests they had one before.
- Current company name: NA when not stated in the message.
- Bank vs card: U.S. routing numbers are 9 digits; account number is distinguished from routing when both appear.
"""

from __future__ import annotations

import re
from typing import Iterable

# Exact labels as on the Google Form radio choices (must match character-for-character).
DEVICE_SMARTWATCH = "Smartwatch $44.95$"
DEVICE_NECKLACE = "Necklace $39.95"

CENTER_NAME = "HS"

DEFAULT_CURRENT_COMPANY = "NA"

# If any of these match (case-insensitive), treat as "had a device before" → First time? No.
PRIOR_DEVICE_PATTERNS = (
    r"\bone\s+before\b",
    r"\bhas\s+one\b",
    r"\bgot\s+one\b",
    r"\bhad\s+(?:one|it|a\s+device|a\s+necklace|before)\b",
    r"\bold\s+one\b",
    r"\breceived\s+(?:a\s+)?(?:necklace|device|one|before)\b",
    r"\brecieved\b",  # common misspelling
    r"\bnecklace\s+before\b",
    r"\bprevious\b",
    r"\bprior\b",
    r"\breplacement\b",
    r"\bmedical\s+guardian\b",
    r"\bhas\s+an\s+old\s+device\b",
    r"\bold\s+device\b",
)


def split_combined_name(full: str) -> tuple[str, str, str]:
    """Split 'First [M] Last' into (first_name, middle_initial, last_name)."""
    s = (full or "").strip()
    if not s:
        return "", "", ""
    parts = s.split()
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    first, last = parts[0], parts[-1]
    middle = " ".join(parts[1:-1])
    if len(middle) == 1:
        mi = middle
    else:
        mi = middle.split()[0][:1] if middle.split() else ""
    return first, mi, last


def first_name_for_form(full_name: str) -> tuple[str, str]:
    """
    First name field on the form includes middle initial: \"Donn H\", last name separate.
    Returns (first_name_with_mi, last_name).
    """
    first, mi, last = split_combined_name(full_name)
    if mi:
        first_out = f"{first} {mi}"
    else:
        first_out = first
    return first_out.strip(), last


def normalize_device(note: str) -> str:
    """
    Map note text to DEVICE_SMARTWATCH or DEVICE_NECKLACE only.
    Defaults to necklace if unclear (adjust if you prefer smartwatch).
    """
    t = (note or "").lower().replace("\u00a0", " ")
    if re.search(r"smart[\s-]*watch\s*[:$]?", t) or "smartwatch" in t:
        return DEVICE_SMARTWATCH
    if "necklace" in t or "neclace" in t or "necklase" in t:
        return DEVICE_NECKLACE
    # price hints
    if "44.95" in t:
        return DEVICE_SMARTWATCH
    if "39.95" in t:
        return DEVICE_NECKLACE
    return DEVICE_NECKLACE


def had_prior_device(note: str) -> bool:
    """True if the message suggests the patient had a device before."""
    t = (note or "").lower()
    for pat in PRIOR_DEVICE_PATTERNS:
        if re.search(pat, t, re.I):
            return True
    return False


def first_time_getting_device_yes_no(note: str) -> str:
    """
    Form: First time getting a device? Yes / No
    No prior-device cues → Yes. Prior-device cues → No.
    """
    return "No" if had_prior_device(note) else "Yes"


def current_company_name(note: str) -> str:
    """Extract company if mentioned; otherwise NA."""
    # Optional: look for "company:", "with:", "from:", etc.
    t = note or ""
    m = re.search(
        r"(?:company|carrier|provider|with)\s*[:#]?\s*([A-Za-z][A-Za-z\s&'.-]{1,40})",
        t,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return DEFAULT_CURRENT_COMPANY


def extract_digit_sequences(text: str) -> list[str]:
    """Runs of digits (possible card, routing, account)."""
    return re.findall(r"\d{4,}", text or "")


def classify_routing_and_account(digit_strings: Iterable[str]) -> tuple[str, str]:
    """
    From candidate digit strings, pick U.S. routing (9 digits) vs account.
    Returns (routing, account) — empty string if unknown.
    Routing is exactly 9 digits; account is typically the other long bank-style number.
    Ignores 15–16 digit PAN-length groups when both bank numbers are present (heuristic).
    """
    seqs = list(digit_strings)
    routing = ""
    account = ""
    nine = [s for s in seqs if len(s) == 9]
    if len(nine) == 1:
        routing = nine[0]
    elif len(nine) > 1:
        routing = nine[0]  # ambiguous: take first

    non_pan = [s for s in seqs if not (15 <= len(s) <= 19)]
    for s in non_pan:
        if len(s) == 9 and s == routing:
            continue
        if len(s) >= 4 and (not account or len(s) >= len(account)):
            account = s
    # If we only have two long numbers and one is 9-digit, other is account
    if routing and not account:
        for s in seqs:
            if s != routing and len(s) >= 4 and len(s) != 9:
                account = s
                break
    return routing, account


__all__ = [
    "DEVICE_SMARTWATCH",
    "DEVICE_NECKLACE",
    "CENTER_NAME",
    "DEFAULT_CURRENT_COMPANY",
    "PRIOR_DEVICE_PATTERNS",
    "split_combined_name",
    "first_name_for_form",
    "normalize_device",
    "had_prior_device",
    "first_time_getting_device_yes_no",
    "current_company_name",
    "extract_digit_sequences",
    "classify_routing_and_account",
]
