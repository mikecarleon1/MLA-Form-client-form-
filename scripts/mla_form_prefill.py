"""
Build a Google Forms *prefilled* URL from one agent note, then open it in the browser.

The form owner can change field IDs if the form is edited; re-run scripts/_dump_form_entries.py
and update ENTRY_IDS below if prefills stop matching.

Security: prefilled URLs contain whatever you put in the query string (including card numbers).
They may be logged (history, proxies). Use only on trusted machines; consider omitting payment
fields from the URL and typing those in the form (see --no-payment).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

from mla_transfer_rules import (
    CENTER_NAME,
    DEFAULT_CURRENT_COMPANY,
    DEVICE_NECKLACE,
    DEVICE_SMARTWATCH,
    classify_routing_and_account,
    current_company_name,
    extract_digit_sequences,
    first_name_for_form,
    first_time_getting_device_yes_no,
    normalize_device,
)

FORM_BASE = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSePxpEhm3MDlMydeTCYN-sYCSVy6ukey508rxFIsy70EybAzA/viewform"
)

# From FB_PUBLIC_LOAD_DATA (see scripts/_dump_form_entries.py)
ENTRY_IDS: dict[str, int] = {
    "phone_number": 935477316,
    "device": 1384845588,
    "first_name": 498512694,
    "last_name": 275495688,
    "date_of_birth": 1461002841,
    "address": 1842946664,
    "city": 5982582,
    "state": 450869036,
    "zip_code": 1614604753,
    "emergency_first": 382157456,
    "emergency_last": 1122804287,
    "emergency_phone": 1302115296,
    "emergency_relation": 538297232,
    "payment_method": 806946184,
    "card_type": 572824001,
    "exp_date": 2125077144,
    "cvv": 1448083221,
    "card_number": 1262838055,
    "bank_account_type": 1923918739,
    "bank_name": 192443813,
    "routing_number": 2077900982,
    "account_number": 1352212579,
    "billing_date": 425911382,
    "first_time_device": 50511349,
    "service_active": 1358154759,
    "current_company": 1677007562,
    "center_name": 636820014,
    "comments": 2142178029,
}

# Exact option labels as on the form (multiple-choice).
PAYMENT_BANK_CARD = "Bank card"
PAYMENT_BANK_ACCOUNT = "Bank account"
CARD_TYPE_VISA = "Visa"
CARD_TYPE_MASTER = "Master"
CARD_TYPE_AMEX = "American express"
CARD_TYPE_DISCOVER = "discover"
CARD_TYPE_BANK_ACCOUNT_LABEL = "Bank Account"  # form typo/label
BANK_TYPE_CHECKING = "Checking account"
BANK_TYPE_SAVING = "Saving account"
BANK_TYPE_OTHER = "Other"
FIRST_TIME_YES = "Yes"
FIRST_TIME_NO = "No"
SERVICE_ACTIVE_YES = "Yes"
SERVICE_ACTIVE_NO = "No"
SERVICE_NO_ACTIVE = "No active service"


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _find_phones(text: str) -> list[str]:
    """10-digit phones; skips digit runs that belong to payment card numbers (15–19 digits)."""
    raw = text or ""
    scrubbed = raw
    for long_d in re.findall(r"\d{15,22}", raw):
        scrubbed = scrubbed.replace(long_d, " " * len(long_d), 1)
    out: list[str] = []
    for m in re.finditer(
        r"(?:\+1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b",
        scrubbed,
    ):
        d = _digits_only(m.group(0))
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) == 10:
            out.append(d)
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _preferred_phone(text: str, phones: list[str]) -> str:
    """Prefer a number on a line labeled cell / mobile / phone / landline / pn."""
    if not phones:
        return ""
    m = re.search(
        r"\b(\d{10})\s*(?://|/)\s*(?:cell|mobile|phone|pn|cellphone|landline)\b",
        text or "",
        re.I,
    )
    if m:
        d = _digits_only(m.group(1))
        if d in phones:
            return d
    m = re.search(
        r"(?:cell|mobile|phone|pn|cellphone|landline)\s*[:#]?\s*(\d{10})\b",
        text or "",
        re.I,
    )
    if m:
        d = _digits_only(m.group(1))
        if d in phones:
            return d
    return phones[0]


def _strip_agent_attribution_lines(text: str) -> str:
    """Remove lines like 'for scarlet' (agent name), not patient data."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if re.match(r"(?i)^for\s+[a-z]", s):
            continue
        lines.append(line)
    return "\n".join(lines)


def _truncate_concatenated_leads(text: str) -> str:
    """If several leads were pasted, keep the first block (starts with phone // cell or landline)."""
    parts = re.split(
        r"(?m)\n(?=\d{10}\s*(?://|/)\s*(?:cell|landline|CELL|LANDLINE|cellphone))",
        (text or "").strip(),
    )
    if len(parts) >= 2 and parts[0].strip():
        return parts[0].strip()
    return (text or "").strip()


def _find_dates(text: str) -> list[str]:
    """Return dates as MM/DD/YYYY strings (multiple formats)."""
    out: list[str] = []
    t = text or ""

    def _push(mm: str, dd: str, yy: str) -> None:
        try:
            mi, di = int(mm), int(dd)
        except ValueError:
            return
        if not (1 <= mi <= 12 and 1 <= di <= 31):
            return
        yys = yy.strip()
        if len(yys) == 2:
            y = int(yys)
            yys = str(2000 + y if y < 50 else 1900 + y)
        elif len(yys) != 4:
            return
        try:
            yfull = int(yys)
        except ValueError:
            return
        if not (1900 <= yfull <= 2035):
            return
        s = f"{mi:02d}/{di:02d}/{yys}"
        if s not in out:
            out.append(s)

    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", t):
        _push(m.group(1), m.group(2), m.group(3))
    for m in re.finditer(r"\b(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})\b", t):
        _push(m.group(1), m.group(2), m.group(3))
    for m in re.finditer(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", t):
        _push(m.group(1), m.group(2), m.group(3))
    for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", t):
        _push(m.group(1), m.group(2), m.group(3))
    return out


def _parse_card_exp(text: str) -> str:
    t = text or ""
    m = re.search(
        r"(?:EXP|EXP:|EXP\s*DATE|exp|expires?|expiration\s*date)\s*[/#:]*\s*"
        r"(\d{1,2})\s*[/\s-]\s*(\d{1,2})\s*[/\s-]\s*(\d{2,4})\b",
        t,
        re.I,
    )
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if len(c) == 4:
            return f"{int(a):02d}/{c[2:]}"
        if len(c) == 2:
            return f"{int(a):02d}/{c}"
    m = re.search(
        r"(?:EXP|EXP:|EXP\s*DATE|exp|expires?|expiration\s*date)\s*[/#:]*\s*(\d{1,2})[/-](\d{2,4})",
        t,
        re.I,
    )
    if m:
        a, b = m.group(1), m.group(2)
        if len(b) == 2:
            return f"{int(a):02d}/{b}"
        if len(b) == 4:
            return f"{int(a):02d}/{b[2:]}"
    m0 = re.search(
        r"(?:EXP|exp)\s*[/#:]*\s*(\d{1,2})\s*[-/](\d{1,2})\b",
        t,
        re.I,
    )
    if m0:
        return f"{int(m0.group(1)):02d}/{m0.group(2)}"
    # Card tail on one line: "debit visa- 05/28-375-4016…" (avoid grabbing DOB MM/YY earlier in note).
    m_cardline = re.search(
        r"(?is)(?:visa|master(?:card)?|discover|\bdebit\b|\bcredit\b)[^\n]{0,120}?"
        r"\b(\d{1,2})\s*/\s*(\d{2})\s*[-–]\s*\d{3,4}\s*[-–]\s*\d{15,19}",
        t,
    )
    if m_cardline:
        return f"{int(m_cardline.group(1)):02d}/{m_cardline.group(2)}"
    m2 = re.search(r"\b(\d{1,2})[/-](\d{2})\b(?!\s*\d{4})", t)
    if m2:
        return f"{int(m2.group(1)):02d}/{m2.group(2)}"
    return ""


def _parse_cvv(text: str) -> str:
    m = re.search(r"(?:CVV|CVC)\s*[/#:]*\s*(\d{3,4})\b", text, re.I)
    if m:
        return m.group(1)
    # One line: "... 05/28-375-4016707005542315" (exp-CVV-PAN with hyphens).
    m_hyp = re.search(
        r"\b(\d{1,2})\s*/\s*(\d{2,4})\s*[-–]\s*(\d{3,4})\s*[-–]\s*(\d{15,19})\b",
        text or "",
    )
    if m_hyp:
        return m_hyp.group(3)
    # Common layout: MM/YY (no "EXP" label) on one line, 3-digit CVV next, spaced PAN after.
    m2 = re.search(
        r"(?im)^\s*\d{1,2}\s*/\s*\d{2,4}\s*\n\s*(\d{3})\s*\n\s*(?:\d{4}\s+){3}\d{4}",
        text or "",
    )
    if m2:
        return m2.group(1)
    # "Master" / Visa line, then MM/YY, then CVV (no PAN on next line).
    m2b = re.search(
        r"(?im)(?:master|visa|discover|\bamex\b|american\s+express)\b[^\n]*\n\s*"
        r"(\d{1,2}\s*/\s*\d{2,4})\s*\n\s*(\d{3,4})\b",
        text or "",
    )
    if m2b:
        return m2b.group(2)
    # MM/YY line then CVV-only line; next line is not a spaced PAN (e.g. cardholder name).
    m3 = re.search(
        r"(?im)^\s*\d{1,2}\s*/\s*\d{2,4}\s*\n\s*(\d{3,4})\s*\n(?!\s*(?:\d{4}\s+){2,}\d{4})",
        text or "",
    )
    if m3:
        return m3.group(1)
    return ""


def _parse_billing_date(text: str) -> str:
    """
    Day of month for billing only when explicitly indicated.
    Never guess from prices (e.g. 44.95) or random small numbers.
    """
    t = text or ""
    # Remove price-like spans so later patterns cannot match the dollars/cents part
    scrub = re.sub(r"\$?\d+\.\d{1,2}(?:\s*/\s*(?:month|mo\.?))?", " ", t, flags=re.I)
    scrub = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", scrub)

    def _day_ok(s: str) -> bool:
        if not s:
            return False
        try:
            d = int(s)
        except ValueError:
            return False
        return 1 <= d <= 31

    m = re.search(
        r"(?:BILLING\s*DATE|billing\s*date|bill\s*date)\s*[/#:]*\s*(.+?)(?:\n|$)",
        t,
        re.I,
    )
    if m:
        chunk = _norm_ws(m.group(1))
        chunk = re.sub(r"\$?\d+\.\d{2}.*$", "", chunk).strip()
        mday = re.search(
            r"\b(?:the\s+)?(\d{1,2})(?:ST|ND|RD|TH)?\b(?!\s*[/%.])",
            chunk,
            re.I,
        )
        if mday and _day_ok(mday.group(1)):
            return str(int(mday.group(1)))
        mord = re.search(r"\b(\d{1,2})(?:ST|ND|RD|TH)\b", chunk, re.I)
        if mord and _day_ok(mord.group(1)):
            return str(int(mord.group(1)))
        return ""

    m2 = re.search(
        r"billing\s+date\s+(?:the\s+)?(\d{1,2})(?:ST|ND|RD|TH)?\b",
        scrub,
        re.I,
    )
    if m2 and _day_ok(m2.group(1)):
        return str(int(m2.group(1)))

    m3 = re.search(
        r"\b(?:billing|bill\s*day|debited|draft)\b[^\n]{0,40}?\b(?:the\s+)?(\d{1,2})(?:ST|ND|RD|TH)?\b",
        scrub,
        re.I,
    )
    if m3 and _day_ok(m3.group(1)):
        return str(int(m3.group(1)))

    mp = re.search(r"(?i)\(\s*(\d{1,2})(?:ST|ND|RD|TH)\s*\)", t)
    if mp and _day_ok(mp.group(1)):
        return str(int(mp.group(1)))

    if re.search(r"(?i)\b1ST\s+of\s+the\s+month\b", scrub):
        return "1"

    mb = re.search(r"(?i)BILLING\s*/\s*(\d{1,2})(?:ST|ND|RD|TH)?(?:/\d+)?\b", t)
    if mb and _day_ok(mb.group(1)):
        return str(int(mb.group(1)))

    mb2 = re.search(r"(?i)\bBILLING\s+(\d{1,2})\b", scrub)
    if mb2 and _day_ok(mb2.group(1)):
        return str(int(mb2.group(1)))

    mb3 = re.search(r"(?i)(\d{1,2})\s+billing\s+date", scrub)
    if mb3 and _day_ok(mb3.group(1)):
        return str(int(mb3.group(1)))

    mb4 = re.search(r"(?i)BILLING\s+DATE\s+(\d{1,2})\b", t)
    if mb4 and _day_ok(mb4.group(1)):
        return str(int(mb4.group(1)))

    mb5 = re.search(r"(?i)\b(\d{1,2})(?:ST|ND|RD|TH)\s+credit\b", scrub)
    if mb5 and _day_ok(mb5.group(1)):
        return str(int(mb5.group(1)))

    return ""


def _collapse_digit_separators(text: str) -> str:
    """
    Join digits separated by spaces/dashes on the same line for PAN detection.
    Skip lines that look like DOB (e.g. 11 4 1944) so they are not merged into fake long numbers.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        if re.search(r"\b\d{1,2}\s+\d{1,2}\s+\d{4}\b", line):
            out.append(line)
        else:
            out.append(re.sub(r"(?<=\d)[ -]+(?=\d)", "", line))
    return "\n".join(out)


def _mask_exp_cvv_pan_for_pan_scan(text: str) -> str:
    """Insert a gap so CVV digits are not merged into the PAN (MM/YY-CVV-PAN lines)."""
    return re.sub(
        r"\b(\d{1,2})\s*/\s*(\d{2,4})\s*[-–]\s*(\d{3,4})\s*[-–]\s*(\d{15,19})\b",
        r"\1/\2 X \4",
        text or "",
    )


def _find_pan_from_dashed_groups(text: str) -> str:
    """Join dashed/spaced card segments on one line (e.g. 46490520-1093-9387). Skips CVV lines."""
    for line in (text or "").splitlines():
        if re.search(r"\bCVV\b|\bCVC\b", line, re.I):
            continue
        if "-" not in line and not re.search(r"\d{4}\s+\d{4}", line):
            continue
        parts = re.split(r"[\s-]+", line.strip())
        digit_parts = [p for p in parts if p.isdigit()]
        if len(digit_parts) < 2:
            continue
        pan = "".join(digit_parts)
        if 15 <= len(pan) <= 19:
            return pan
    return ""


def _find_pan(text: str) -> str:
    """Longest 15–19 digit sequence (payment card); handles dashed card groups."""
    t = _mask_exp_cvv_pan_for_pan_scan(text or "")
    g = _find_pan_from_dashed_groups(t)
    if g:
        return g
    raw = _collapse_digit_separators(t)
    best = ""
    for s in extract_digit_sequences(raw):
        if 15 <= len(s) <= 19 and len(s) > len(best):
            best = s
    return best


def _infer_card_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\bvisa\b|\bvise\b", t):
        return CARD_TYPE_VISA
    if re.search(r"\bmaster(?:card)?\b", t):
        return CARD_TYPE_MASTER
    if re.search(r"\bamex\b|\bamerican\s+express\b", t):
        return CARD_TYPE_AMEX
    if re.search(r"\bdiscover\b", t):
        return CARD_TYPE_DISCOVER
    return CARD_TYPE_MASTER


def _infer_payment_path(text: str) -> str:
    """Bank if explicit ACH/checking/savings/routing/account language; else card."""
    t = (text or "").lower()
    if re.search(
        r"\b(bank\s+account|checking|savings|direct\s+deposit|ach\b|wire\s+transfer)\b",
        t,
    ):
        return PAYMENT_BANK_ACCOUNT
    if "routing" in t and "account" in t:
        return PAYMENT_BANK_ACCOUNT
    if re.search(r"account\s*number", t) and re.search(r"routing", t):
        return PAYMENT_BANK_ACCOUNT
    if re.search(r"routing\s*number", t):
        return PAYMENT_BANK_ACCOUNT
    if re.search(r"account\s*number", t) and not re.search(
        r"\b(card|visa|master|cvv|debit\s+credit)\b",
        t,
    ):
        return PAYMENT_BANK_ACCOUNT
    if re.search(
        r"\bchime\b|\bwells\s+fargo\b|\bboa\b|\bbank\s+of\b|\bprofile\s+bank\b|"
        r"\bcheque\b|\bfederal\s+credit\b|\bcredit\s+union\b",
        t,
    ):
        return PAYMENT_BANK_ACCOUNT
    return PAYMENT_BANK_CARD


def _parse_labeled_routing(text: str) -> str:
    """Digits on the Routing line (US ABA = 9; if 10 digits, use first 9)."""
    m = re.search(
        r"routing\s*number\s*[/#:]*\s*(\d{9,11})\b",
        text,
        re.I,
    )
    if not m:
        m = re.search(r"\brouting\s*[/#:]*\s*(\d{9,11})\b", text, re.I)
    if m:
        d = m.group(1)
        if len(d) >= 9:
            return d[:9]
    return ""


def _parse_labeled_account(text: str) -> str:
    """Account number on labeled line (not card)."""
    m = re.search(
        r"account\s*numb(?:er)?\s*[/#:]*\s*([\d\s-]{6,24})",
        text,
        re.I,
    )
    if m:
        return re.sub(r"\D", "", m.group(1))
    return ""


def _parse_routing_account_split_line(text: str) -> tuple[str, str]:
    """e.g. '251579377-4171010' or '064204774 17250371114' (routing + account, no labels)."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if re.search(r"(?i)routing\s*number|account\s*number", line):
            continue
        if re.search(r"(?i)card|visa|master|cvv|exp", line):
            continue
        m = re.search(r"\b(\d{9})[\s-]+(\d{4,17})\b", line)
        if m:
            return m.group(1), m.group(2)
    return "", ""


def _parse_bank_institution_line(text: str) -> str:
    """Institution name on its own line (e.g. 'WEE Federal Credit Union') after checking/bank cues."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # Do not use bare "credit" — it matches "Credit" inside "Credit Union".
    skip_re = re.compile(
        r"(?i)routing|account\s*numb|cheque|check\s*no|cvv|exp\b|card\b|master|visa|debit|"
        r"credit\s+card|smart|necklace|billing|landline|cell",
    )
    for i, line in enumerate(lines):
        if re.match(r"(?i)^(checking|savings)(\s+account)?\s*$", line) or re.match(
            r"(?i)^profile\s+bank\s*$",
            line,
        ):
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j]
                if skip_re.search(cand) or re.match(r"^\d", cand):
                    continue
                if len(cand.split()) >= 2 and re.match(r"^[A-Za-z]", cand):
                    return _norm_ws(cand)
    return ""


def _parse_bank_name_from_note(text: str) -> str:
    """Lines like 'chime bank', 'WEE Federal Credit Union', 'Bank: Foo', or labeled bank name."""
    t = text or ""
    skip_cu = re.compile(
        r"(?i)routing|account\s*numb|cheque|check\s*no|cvv|exp\b|card\b|master|visa|debit|"
        r"credit\s+card|smart|necklace|billing|landline|cell(?:phone)?|\b\d{9}\b",
    )
    for raw_line in t.splitlines():
        line = raw_line.strip()
        if line and not skip_cu.search(line):
            if re.match(
                r"(?i)^[A-Za-z0-9][\w &'.-]{0,62}\s+(?:federal\s+)?credit\s+union\s*$",
                line,
            ):
                return _norm_ws(line)
            m_colon = re.match(r"(?i)^bank\s*[/#:]\s*(.+)$", line)
            if m_colon:
                got = _norm_ws(m_colon.group(1))
                if len(got) >= 3:
                    return got
        m = re.match(
            r"(?i)^([A-Za-z][A-Za-z &'.-]{0,30}?)\s+bank\s*$",
            line,
        )
        if not m:
            continue
        base = _norm_ws(m.group(1))
        stripped = re.sub(r"(?i)^(checking|savings?)\s+", "", base).strip()
        if stripped:
            return stripped.title() + " Bank"
        if re.match(r"(?i)^(checking|savings?)$", base):
            continue
        return base.title() + " Bank"
    m2 = re.search(
        r"(?:name\s+of\s+(?:the\s+)?bank|bank\s*name|institution\s*name)\s*[/#:]*\s*(.+?)(?:\n|$)",
        t,
        re.I,
    )
    if m2:
        return _norm_ws(m2.group(1))
    return ""


def _expand_zip_if_four_digits_after_state(s: str) -> str:
    """If line ends with 'ST 1234' (4 digits), treat as ZIP+leading zero (e.g. MA 1864 -> MA 01864)."""
    s = (s or "").strip()
    m = re.search(r"\b([A-Z]{2})\s+(\d{4})\s*$", s, re.I)
    if not m:
        return s
    z = m.group(2)
    if len(z) == 4 and z.isdigit():
        return s[: m.start(2)] + "0" + z
    return s


def _normalize_zip_plus_four_space(s: str) -> str:
    """ZIP+4 pasted as '26101 3739' → '26101-3739' so city/state parsers match."""
    return re.sub(r"\b(\d{5})\s+(\d{4})\b", r"\1-\2", s or "")


def _address_line_score(s: str) -> int:
    """How well a single line parses as street/city/state/ZIP (higher = better)."""
    s2 = _normalize_zip_plus_four_space(_expand_zip_if_four_digits_after_state(s.strip()))
    street, city, state, zipc = _parse_city_state_zip(s2)
    sc = 0
    if zipc:
        sc += 5
    if state:
        sc += 3
    if city:
        sc += 2
    if street and len(street) > 4:
        sc += 1
    return sc


def _looks_like_street_only_line(s: str) -> bool:
    """Street fragment with no city/state (e.g. '10 Mitchell Place')."""
    s = (s or "").strip()
    if len(s) < 6 or len(s) > 80:
        return False
    if re.search(r"(?i)\b(?:EXP|CVV|BILLING|VISA|MASTER|AMEX|NECKLACE|SMART|CONTACT|LANDLINE|CELL)\b", s):
        return False
    if re.search(r"\d{10}", s):
        return False
    if re.search(r"(?i)\b(?:PL|PLACE|ST|AVE|RD|DR|LN|BLVD|CT|WAY|CIR|BOULEVARD|ROAD|LANE)\b", s) and re.search(
        r"\d", s
    ):
        return True
    if re.match(r"^\d+\s+[A-Za-z]", s) and not re.search(r"\b[A-Z]{2}\s+\d{4,5}\s*$", s):
        return True
    return False


def _pick_best_address_line(lines: list[str]) -> str:
    """Choose one line that is most likely the physical address (never join the whole note)."""
    zip_re = re.compile(r"\b\d{5}(?:-\d{4})?\b")
    state_zip4 = re.compile(r"\b[A-Z]{2}\s+\d{4}\s*$", re.I)
    best = ""
    best_sc = -1
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"[\d\s+().-]+$", s):
            continue
        if re.match(
            r"^\d{10}\s*(?://|/)?\s*(?:cell|phone|mobile|cellphone|CELLPHONE|landline|LANDLINE)?\s*$",
            s,
            re.I,
        ):
            continue
        if re.fullmatch(r"\d{1,2}\s+\d{1,2}\s+\d{4}", s):
            continue
        if re.match(r"^\d{1,2}[/-]\d", s):
            continue
        if _is_payment_or_bank_line(s):
            continue
        if re.match(r"(?i)^(necklace|neclace|necklase|smart\s*watch|smartwatch)\b", s):
            continue
        if re.search(
            r"(?:EXP|CVV|BILLING|DEBIT|MASTER|VISA|CREDIT\s*CARD|EXPIRATION|AMERICAN\s+EXPRESS)\b",
            s,
            re.I,
        ):
            continue
        if re.search(r"(?i)^emergency\s+contact\b", s):
            continue
        sc = -1
        if zip_re.search(s) and re.search(r"[A-Za-z]", s):
            sc = max(sc, _address_line_score(s))
        if state_zip4.search(s):
            sc = max(sc, _address_line_score(s))
        if _looks_like_street_only_line(s):
            sc = max(sc, 1)
        if sc > best_sc:
            best_sc = sc
            best = s
    return best


def _is_payment_or_bank_line(s: str) -> bool:
    sl = (s or "").lower()
    return bool(
        re.search(
            r"\b(checking|savings|routing|account\s*number|chime\b|direct\s+deposit|\bach\b)\b",
            sl,
        )
    )


def _parse_name_line_candidates(lines: list[str]) -> str:
    """Pick full name line (no digits except maybe not). Prefer the last plausible line (name often at end)."""
    picked = ""
    for line in lines:
        s = _norm_ws(line)
        if not s or len(s) < 3:
            continue
        if re.match(r"^[\d\s+().-]+$", s):
            continue
        if re.match(r"^\d+[-.)]\s*", s) or re.match(r"^\d+\s*[-.)]\s*", s):
            continue
        if re.search(r"^\d{1,2}[/-]\d", s):
            continue
        if re.fullmatch(r"\d{1,2}\s+\d{1,2}\s+\d{4}", s):
            continue
        if re.match(
            r"(?i)^(have|had|got)\s+one\s+before\b|^(one\s+before)\b|^(have\s+one)\b",
            s,
        ):
            continue
        if re.search(
            r"(?:EXP|CVV|BILLING|NECKLACE|SMARTWATCH|DEBIT|MASTER|VISA|CREDIT\s+CARD|EXPIRATION)\b",
            s,
            re.I,
        ):
            continue
        if re.match(r"(?i)^(credit|debit)\b", s):
            continue
        if re.search(r"\d+\.\d{2}", s) or "$" in s:
            continue
        if re.match(r"(?i)^(smartwatch|necklace)\b", s):
            continue
        if re.match(r"(?i)^emergency\s+contact\b", s):
            continue
        if _is_payment_or_bank_line(s):
            continue
        if re.search(r"(?i)^p\.?\s*o\.?\s*box\b|\bbox\s+\d", s):
            continue
        parts = s.split()
        if 2 <= len(parts) <= 5 and not re.search(r"\d{5}", s):
            if re.match(r"^[A-Za-z'.-]+$", parts[0]):
                picked = s
    return picked


def _scrub_glued_zip_ordinal(s: str) -> str:
    """Fix typos like '448655th' (ZIP + ordinal) -> '44865'."""
    return re.sub(
        r"(?<!\d)(\d{5})\d+(?:st|nd|rd|th)\b",
        r"\1",
        s or "",
        flags=re.I,
    )


# Spelled-out state before ZIP (agents often omit the 2-letter abbreviation).
US_STATE_FULL_TO_ABBR: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def _parse_city_state_zip_full_state(t: str) -> tuple[str, str, str, str] | None:
    """Trailing '... City Full State Name 12345' without a 2-letter ST token."""
    t = _norm_ws(_scrub_glued_zip_ordinal(t.replace("\n", " "))).strip()
    mzip = re.search(r"\s+(\d{5}(?:-\d{4})?)\s*$", t)
    if not mzip:
        return None
    zipc = mzip.group(1)
    rest = t[: mzip.start()].strip().rstrip(",")
    if not rest:
        return None
    rl = rest.lower()
    for full, abbr in sorted(US_STATE_FULL_TO_ABBR.items(), key=lambda kv: -len(kv[0])):
        if not rl.endswith(full):
            continue
        city_street = rest[: -len(full)].strip().rstrip(",").strip()
        if not city_street:
            continue
        synthetic = f"{city_street} {abbr} {zipc}"
        sp = _parse_city_state_zip_space(synthetic)
        if sp and sp[2] == abbr and sp[3] == zipc:
            return sp[0], sp[1], sp[2], sp[3]
        return city_street, "", abbr, zipc
    return None


def _parse_city_state_zip_space(t: str) -> tuple[str, str, str, str] | None:
    """
    No commas: e.g. '1721 WASHINGTON ST GREAT BEND KS Zip 67530-2423'.
    """
    t = _norm_ws(t.replace("\n", " ")).strip()
    m = re.search(
        r"\s+([A-Z]{2})\s+(?:Zip\s*)?(\d{5}(?:-\d{4})?)\s*$",
        t,
        re.I,
    )
    if not m:
        return None
    state = m.group(1).upper()
    zipc = m.group(2)
    # "… Plymouth, OH 12345" leaves a trailing comma on the street+city blob.
    before = t[: m.start()].strip().rstrip(",").strip()
    if not before:
        return None
    po = re.match(
        r"(?i)^(p\.?\s*o\.?\s*box\s+[\w-]+)\s+(.+)$",
        before,
    )
    if po:
        return _norm_ws(po.group(1)), _norm_ws(po.group(2)), state, zipc
    street_types = (
        r"ST|AVE|RD|BLVD|DR|LN|WAY|CT|PL|CIR|PKWY|HWY|RT|ROUTE|POINT|"
        r"STREET|AVENUE|ROAD|DRIVE|LANE|BOULEVARD"
    )
    sm = re.search(rf"^(.+?)\s+({street_types})\s+(.+)$", before, re.I)
    if sm:
        street = _norm_ws(f"{sm.group(1)} {sm.group(2)}")
        city = _norm_ws(sm.group(3))
        return street, city, state, zipc
    words = before.split()
    if len(words) >= 4:
        city = " ".join(words[-2:])
        street = " ".join(words[:-2])
        return street, city, state, zipc
    if len(words) == 3:
        return words[0] + " " + words[1], words[2], state, zipc
    if len(words) == 2:
        return words[0], words[1], state, zipc
    return before, "", state, zipc


def _parse_city_state_zip(text: str) -> tuple[str, str, str, str]:
    """
    Returns (street_address, city, state, zip).
    """
    t = _norm_ws(_scrub_glued_zip_ordinal(text.replace("\n", " "))).strip()
    t = _expand_zip_if_four_digits_after_state(t)
    # Prefer trailing: ", City, ST, ZIP" or ", City, ST ZIP"
    m = re.search(
        r",\s*([^,]+?),\s*([A-Z]{2})\s*,?\s*(\d{5}(?:-\d{4})?)\s*$",
        t,
    )
    if m:
        city = _norm_ws(m.group(1)).rstrip(",").strip()
        state = m.group(2).upper()
        zipc = m.group(3)
        street = t[: m.start()].strip().rstrip(",").strip()
        # Fix odd leading token like "H52 Wichita" (typo/extra fragment before city)
        mfix = re.match(r"^(H\d+)\s+(.+)$", city)
        if mfix:
            street = _norm_ws(f"{street}, {mfix.group(1)}")
            city = _norm_ws(mfix.group(2))
        return street, city, state, zipc
    # One comma before ST: "425 Walker Rd Apt B Hodgdon, ME 04730-4041"
    m1 = re.match(
        r"^(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$",
        t,
        re.I,
    )
    if m1:
        rest = m1.group(1).strip().rstrip(",").strip()
        state = m1.group(2).upper()
        zipc = m1.group(3)
        words = rest.split()
        if len(words) >= 2:
            street = " ".join(words[:-1])
            city = words[-1]
            return street, city, state, zipc
        return rest, "", state, zipc
    # Prefer space-based parse before m2: m2's greedy group can swallow the whole
    # "… North Reading MA 01864" line and leave street blank.
    space = _parse_city_state_zip_space(t)
    if space:
        return space
    m2 = re.search(
        r"([A-Za-z0-9 .'#-]+)\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$",
        t,
        re.I,
    )
    if m2:
        before = t[: m2.start()].strip().rstrip(",")
        city = m2.group(1).strip().rstrip(",").strip()
        return before, city, m2.group(2).upper(), m2.group(3)
    fs = _parse_city_state_zip_full_state(t)
    if fs:
        return fs
    return t, "", "", ""


def _parse_old_company(text: str) -> str:
    m = re.search(r"(?i)old\s+company\s*/\s*(.+?)(?:\n|$)", text or "")
    if not m:
        return ""
    chunk = _norm_ws(m.group(1))
    chunk = re.split(r"\s+\d{3}[-.\s]?\d", chunk)[0].strip()
    return chunk or "NA"


def _hyphen_emergency_triples(text: str) -> list[tuple[str, str, str]]:
    """
    Lines like: Michael Meador- fiancee- 5406660923
    or: pam lewis-her friend-5403970552
    Returns list of (full_name, relation_text, phone10).
    """
    out: list[tuple[str, str, str]] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.search(r"(?i)visa|master(?:card)?|discover|\bamex\b|\bcvv\b|\bexp\b|necklace|smartwatch", s):
            continue
        if re.search(r"\d{13,19}", s):
            continue
        m = re.match(r"(?i)^(.+?)\s*[-–]\s*(.+?)\s*[-–]\s*(\d{10})\s*$", s)
        if not m:
            continue
        left = _norm_ws(m.group(1))
        mid = _norm_ws(m.group(2))
        dig = _digits_only(m.group(3))
        if len(dig) != 10:
            continue
        if re.match(r"^\d{1,2}\s*/\s*\d{2}", mid):
            continue
        if len(re.sub(r"[^A-Za-z]", "", left)) < 2 or len(mid) < 2:
            continue
        out.append((left, mid, dig))
    return out


def _parse_emergency(text: str, patient_phone: str = "") -> tuple[str, str, str, str]:
    """Emergency first, last, phone, relation (patient_phone used for 'same num' lines)."""
    t = text or ""
    ef, el = "", ""
    rel = ""
    phone = ""
    # emergency contact //NAME// RELATION // 9175551212
    m_sl = re.search(
        r"(?is)emergency\s+contact\s*//\s*([^/]+?)\s*//\s*([A-Za-z]{2,20})\s*//\s*(\d{10})\b",
        t,
    )
    m_ec = re.search(
        r"(?is)emergency\s+contact\s*:\s*(.+?)\s+(daughter|son|wife|husband|mother|father|brother|sister|"
        r"friend|spouse|fiancee?|grandson|granddaughter)\s+(\d{10})\b",
        t,
    )
    if m_sl:
        ef, el = first_name_for_form(_norm_ws(m_sl.group(1)))
        rel = _norm_ws(m_sl.group(2))
        phone = _digits_only(m_sl.group(3))
    elif m_ec:
        ef, el = first_name_for_form(_norm_ws(m_ec.group(1)))
        rel = _norm_ws(m_ec.group(2))
        phone = _digits_only(m_ec.group(3))
    if not (ef or el or phone):
        triples = _hyphen_emergency_triples(t)
        if triples:
            ppd = _digits_only(patient_phone or "")
            chosen = triples[0]
            for left, mid, dig in triples:
                if ppd and dig != ppd:
                    chosen = (left, mid, dig)
                    break
            left, mid, dig = chosen
            ef, el = first_name_for_form(left)
            rel = mid.title()
            phone = dig
    rm = re.search(
        r"EMERGENCY\s+CONTACT\s*,?\s*(.+?)(?:\n|$)|emrg\s+contact\s*:\s*(.+?)(?:\n|$)",
        t,
        re.I | re.DOTALL,
    )
    if rm and not m_sl and not m_ec and not (ef or el or phone):
        rel = _norm_ws(rm.group(1) or rm.group(2) or "")
    pm = re.search(r"(?:EMERGENCY|emrg)[^\d]{0,40}(\d{10})\b", t, re.I)
    if pm and not phone:
        phone = _digits_only(pm.group(1) or "")
    pm2 = re.search(
        r"(?i)(?:DAUGHTER|SON|WIFE|HUSBAND|FIANCEE?|SPOUSE)\s*-\s*(\d{10})\b",
        t,
    )
    if pm2 and not phone:
        phone = _digits_only(pm2.group(1))
    pm3 = re.search(
        r"(?i)(?:daughter|son|wife|husband)\s+[A-Za-z][A-Za-z'\s-]{2,35}\s+(\d{10})\b",
        t,
    )
    if pm3 and not phone:
        phone = _digits_only(pm3.group(1))
    if ef or el or phone:
        return ef, el, phone, rel
    rel_start = re.compile(
        r"(?i)^(wife|husband|son|daughter|friend|fiancee?|spouse|mother|father|brother|sister|"
        r"grandson|granddaughter)\b",
    )
    for line in t.splitlines():
        lm = re.match(
            r"(?i)^\s*([A-Za-z][A-Za-z\s'-]{1,35})\s*[-=]>\s*(.+)$",
            line.strip(),
        )
        if not lm:
            continue
        left = _norm_ws(lm.group(1))
        right = _norm_ws(lm.group(2))
        if rel_start.match(right):
            ef, el = first_name_for_form(left)
            mrel = rel_start.match(right)
            erel_word = mrel.group(1).title() if mrel else right[:48]
            rel = erel_word
            if re.search(r"(?i)same\s+(?:num|#|number|phone|cell)", right):
                if patient_phone:
                    phone = _digits_only(patient_phone)
                rel = (rel + " (same # as patient)").strip()
            else:
                pm4 = re.search(r"\b(\d{10})\b", right)
                if pm4:
                    phone = _digits_only(pm4.group(1))
        else:
            rel = right + " (" + left + ")"
        break
    if re.search(r"(?i)same\s+num", t) and not phone and not ef:
        rel = (rel + " (same # as patient)").strip() if rel else "Same # as patient"
    return ef, el, phone, rel


def parse_agent_note(note: str) -> dict[str, Any]:
    """
    Map a free-form agent note to form fields. Field order in the note can vary;
    we use labels (billing, routing, PO Box, etc.) and line heuristics rather than
    assuming a fixed template.
    """
    text = _truncate_concatenated_leads(_strip_agent_attribution_lines(note or ""))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    phones = _find_phones(text)
    primary_phone = _preferred_phone(text, phones)

    dates = _find_dates(text)
    dob = ""
    dm = re.search(
        r"(?i)(?:\d+\s*[-.)]\s*)?(?:DOB|date\s*of\s*birth)\s*[/#:]*\s*"
        r"(\d{1,2})\s*[/\s-]\s*(\d{1,2})\s*[/\s-]\s*(\d{2,4})\b",
        text,
    )
    if dm:
        yy = dm.group(3)
        if len(yy) == 2:
            y = int(yy)
            yy = str(2000 + y if y < 50 else 1900 + y)
        dob = f"{int(dm.group(1)):02d}/{int(dm.group(2)):02d}/{yy}"
    if not dob:
        for d in dates:
            parts = d.split("/")
            if len(parts) == 3 and int(parts[2]) > 1900:
                dob = d
                break
    if not dob and dates:
        dob = dates[0]

    full_name = ""
    nm = re.search(r"(?i)NAME\s*/\s*(.+?)(?:\n|$)", text)
    if nm:
        full_name = _norm_ws(nm.group(1))
    if not full_name:
        for line in lines:
            cm = re.match(
                r"(?i)^(?:\d+\s*[-.)]\s*)?(?:customer\s*name)\s*[/#:]*\s*(.+)$",
                line.strip(),
            )
            if cm:
                full_name = _norm_ws(cm.group(1))
                break
    if not full_name:
        full_name = _parse_name_line_candidates(lines)
    first, last = first_name_for_form(full_name) if full_name else ("", "")

    addr_line = ""
    zip_re = re.compile(r"\b\d{5}(?:-\d{4})?\b")
    for line in lines:
        s = line.strip()
        lam = re.match(
            r"(?i)^(?:\d+\s*[-.)]\s*)?(?:address|add)\s*[/#:]*\s*(.+)$",
            s,
        )
        if lam and zip_re.search(lam.group(1)):
            addr_line = _norm_ws(lam.group(1))
            break
    if not addr_line:
        for i, line in enumerate(lines):
            s = line.strip()
            if re.fullmatch(r"[\d\s+().-]+$", s):
                continue
            if re.match(
                r"^\d{10}\s*(?://|/)?\s*(?:cell|phone|mobile|cellphone|CELLPHONE|landline|LANDLINE)?\s*$",
                s,
                re.I,
            ):
                continue
            if re.search(r"^\d{1,2}[/-]\d", s):
                continue
            if re.fullmatch(r"\d{1,2}\s+\d{1,2}\s+\d{4}", s):
                continue
            if _is_payment_or_bank_line(s):
                continue
            if re.match(r"(?i)^(necklace|neclace|necklase|smart\s*watch|smartwatch)\b", s):
                continue
            if re.search(
                r"(?:EXP|CVV|BILLING|DEBIT|MASTER|VISA|CREDIT\s+CARD|EXPIRATION)\b",
                s,
                re.I,
            ):
                continue
            if zip_re.search(s) and re.search(r"[A-Za-z]{2,}", s):
                if i > 0 and re.match(r"(?i)^p\.?o\.?\s*box\b", lines[i - 1]):
                    addr_line = lines[i - 1] + " " + line
                else:
                    addr_line = line
                break
            if re.search(r"\b[A-Z]{2}\s+\d{4,5}\s*$", s, re.I) and re.search(r"\d", s):
                addr_line = line
                break
    if not addr_line:
        for line in lines:
            if _is_payment_or_bank_line(line):
                continue
            if re.search(r",\s*[A-Za-z]{2}\s*\d{5}", line, re.I):
                addr_line = line
                break
    if not addr_line:
        addr_line = _pick_best_address_line(lines)

    addr_line = _normalize_zip_plus_four_space(_expand_zip_if_four_digits_after_state(addr_line))
    street, city, state, zipc = _parse_city_state_zip(addr_line)

    device = normalize_device(text)
    payment = _infer_payment_path(text)
    card_type = _infer_card_type(text)
    exp = _parse_card_exp(text) or ""
    cvv = _parse_cvv(text) or ""
    pan = "" if payment == PAYMENT_BANK_ACCOUNT else _find_pan(text)

    billing = _parse_billing_date(text)

    ft = first_time_getting_device_yes_no(text)
    company = _parse_old_company(text) or current_company_name(text)

    ef, el, ep, erel = _parse_emergency(text, primary_phone)
    if phones and len(phones) >= 2 and not ep and "EMERGENCY" in text.upper():
        cand = phones[-1]
        if cand != primary_phone:
            ep = cand
    if ep == primary_phone and not re.search(
        r"(?i)same\s+(?:num|#|number|phone|cell)",
        text,
    ):
        ep = ""

    routing, account = "", ""
    bank_name = ""
    if payment == PAYMENT_BANK_ACCOUNT:
        routing = _parse_labeled_routing(text)
        account = _parse_labeled_account(text)
        if not routing or not account:
            cr, ca = classify_routing_and_account(extract_digit_sequences(text))
            routing = routing or cr
            account = account or ca
        if not routing or not account:
            r2, a2 = _parse_routing_account_split_line(text)
            routing = routing or r2
            account = account or a2
        bank_name = _parse_bank_name_from_note(text)
        if not bank_name:
            bank_name = _parse_bank_institution_line(text)
        if not bank_name:
            bm = re.search(
                r"(?:bank|name of the bank)\s*[:#]?\s*([A-Za-z][A-Za-z\s&'.-]{2,40})",
                text,
                re.I,
            )
            if bm:
                bank_name = _norm_ws(bm.group(1))

    # "If no, is service active?" — Yes → No active service; not first time → Yes
    if ft == FIRST_TIME_YES:
        svc = SERVICE_NO_ACTIVE
    else:
        svc = SERVICE_ACTIVE_YES

    comments = ""

    raw: dict[str, Any] = {
        "phone_number": primary_phone,
        "device": device,
        "first_name": first,
        "last_name": last,
        "date_of_birth": dob,
        "address": street,
        "city": city,
        "state": state,
        "zip_code": zipc,
        "emergency_first": ef,
        "emergency_last": el,
        "emergency_phone": ep,
        "emergency_relation": erel,
        "payment_method": payment,
        "card_type": card_type if payment == PAYMENT_BANK_CARD else CARD_TYPE_BANK_ACCOUNT_LABEL,
        "exp_date": exp if payment == PAYMENT_BANK_CARD else "NA",
        "cvv": cvv if payment == PAYMENT_BANK_CARD else "NA",
        "card_number": pan if payment == PAYMENT_BANK_CARD else "NA",
        "bank_account_type": (
            BANK_TYPE_OTHER
            if payment == PAYMENT_BANK_CARD
            else (
                BANK_TYPE_SAVING
                if re.search(r"\bsavings?\b", text, re.I)
                else BANK_TYPE_CHECKING
            )
        ),
        "bank_name": "NA" if payment == PAYMENT_BANK_CARD else (bank_name or ""),
        "routing_number": "NA" if payment == PAYMENT_BANK_CARD else (routing or ""),
        "account_number": "NA" if payment == PAYMENT_BANK_CARD else (account or ""),
        "billing_date": billing,
        "first_time_device": ft,
        "service_active": svc,
        "current_company": company,
        "center_name": CENTER_NAME,
        "comments": comments,
    }
    return normalize_submitted_fields(raw, text)


def normalize_submitted_fields(data: dict[str, Any], note: str) -> dict[str, Any]:
    """
    Enforce house rules and payment-path consistency (for LLM output or hybrid merge).
    Safe to run on heuristic parse output too.
    """
    out: dict[str, Any] = {
        k: ("" if v is None else str(v).strip()) for k, v in (data or {}).items()
    }
    for k in ENTRY_IDS:
        out.setdefault(k, "")

    out["center_name"] = CENTER_NAME
    if out.get("device") not in (DEVICE_SMARTWATCH, DEVICE_NECKLACE):
        out["device"] = normalize_device(note)

    pm = out.get("payment_method", "")
    if pm not in (PAYMENT_BANK_CARD, PAYMENT_BANK_ACCOUNT):
        out["payment_method"] = _infer_payment_path(note)
        pm = out["payment_method"]

    if pm == PAYMENT_BANK_ACCOUNT:
        out["exp_date"] = "NA"
        out["cvv"] = "NA"
        out["card_number"] = "NA"
        out["card_type"] = CARD_TYPE_BANK_ACCOUNT_LABEL
        if out.get("bank_account_type") not in (BANK_TYPE_CHECKING, BANK_TYPE_SAVING):
            out["bank_account_type"] = (
                BANK_TYPE_SAVING if re.search(r"\bsavings?\b", note, re.I) else BANK_TYPE_CHECKING
            )
    else:
        out["bank_account_type"] = BANK_TYPE_OTHER
        out["bank_name"] = "NA"
        out["routing_number"] = "NA"
        out["account_number"] = "NA"
        if not out.get("card_type"):
            out["card_type"] = _infer_card_type(note)

    ft = out.get("first_time_device") or ""
    if ft not in (FIRST_TIME_YES, FIRST_TIME_NO):
        ft = first_time_getting_device_yes_no(note)
    out["first_time_device"] = ft
    if ft == FIRST_TIME_YES:
        out["service_active"] = SERVICE_NO_ACTIVE
    else:
        out["service_active"] = SERVICE_ACTIVE_YES

    if not out.get("current_company"):
        out["current_company"] = current_company_name(note)

    pp = out.get("phone_number", "")
    ep = out.get("emergency_phone", "")
    if pp and ep and _digits_only(ep) == _digits_only(pp):
        # Keep duplicate when the note says EC uses the same number as the patient.
        if not re.search(r"(?i)same\s+(?:num|#|number|phone|cell)", note or ""):
            out["emergency_phone"] = ""

    return out


def build_prefill_url(data: dict[str, Any], *, omit_payment: bool = False) -> str:
    params: list[tuple[str, str]] = [("usp", "pp_url")]
    payment_keys = {
        "payment_method",
        "card_type",
        "exp_date",
        "cvv",
        "card_number",
        "bank_account_type",
        "bank_name",
        "routing_number",
        "account_number",
    }
    for key, val in data.items():
        if val is None or val == "":
            continue
        if omit_payment and key in payment_keys:
            continue
        eid = ENTRY_IDS.get(key)
        if not eid:
            continue
        sval = str(val).strip()
        params.append((f"entry.{eid}", sval))
    q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    return f"{FORM_BASE}?{q}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Open MLA Google Form prefilled from one agent note.")
    ap.add_argument(
        "note_file",
        nargs="?",
        help="UTF-8 text file with the note; if omitted, read stdin",
    )
    ap.add_argument(
        "--open",
        action="store_true",
        help="Open the prefill URL in the default browser",
    )
    ap.add_argument(
        "--no-payment",
        action="store_true",
        help="Omit payment-related fields from the URL (safer; type card/bank in the form)",
    )
    ap.add_argument(
        "--print-url-only",
        action="store_true",
        help="Print only the URL line",
    )
    args = ap.parse_args()
    if args.note_file:
        raw = Path(args.note_file).read_text(encoding="utf-8", errors="replace")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        print("No input text.", file=sys.stderr)
        sys.exit(1)
    data = parse_agent_note(raw)
    url = build_prefill_url(data, omit_payment=args.no_payment)
    if args.print_url_only:
        print(url)
    else:
        print(url)
        print("\n--- parsed (review) ---")
        for k in sorted(data.keys()):
            print(f"{k}: {data[k]}")
    if args.open:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
