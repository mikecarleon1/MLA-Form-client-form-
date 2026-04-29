"""
LLM-assisted MLA Google Form prefill — reads messy agent notes and returns a prefill URL.

Why: regex rules miss many real-world layouts. This script uses a model + *your* few-shot
examples (training data you control) in `mla_few_shot_examples.json`, then applies the same
house rules as `mla_form_prefill.py` (`normalize_submitted_fields`).

Setup:
  pip install openai
  set OPENAI_API_KEY=...   (Windows: setx OPENAI_API_KEY "sk-...")
  Optional: set OPENAI_MODEL=gpt-4o-mini

Usage (from the `mla-form-prefill` folder):
  python scripts/mla_form_llm_prefill.py path/to/note.txt --open
  python scripts/mla_form_llm_prefill.py path/to/note.txt --no-merge   # do not fill gaps from regex parser
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FEW_SHOT = SCRIPT_DIR / "mla_few_shot_examples.json"

FIELD_INSTRUCTIONS = """
Return ONE JSON object only. Keys must match exactly (use "" for unknown optional fields):

phone_number, device, first_name, last_name, date_of_birth, address, city, state, zip_code,
emergency_first, emergency_last, emergency_phone, emergency_relation,
payment_method, card_type, exp_date, cvv, card_number,
bank_account_type, bank_name, routing_number, account_number,
billing_date, first_time_device, service_active, current_company, center_name, comments

Rules:
- device: exactly "Smartwatch $44.95$" OR "Necklace $39.95" (Google Form radio labels).
- center_name: "HS".
- payment_method: exactly "Bank card" OR "Bank account".
- If Bank account: exp_date, cvv, card_number must be "NA"; card_type "Bank Account";
  bank_account_type "Checking account" or "Saving account"; bank_name/routing/account as given.
- If Bank card: bank_name, routing_number, account_number "NA"; bank_account_type "Other".
- first_time_device: "Yes" or "No" (first time getting a device).
- If first_time_device is "Yes", service_active must be "No active service".
  If "No", service_active must be "Yes".
- current_company: "NA" if not stated.
- Middle initial belongs in first_name (e.g. "Rebecca L").
- Do not put patient phone in emergency_phone unless the note clearly gives a different emergency number.
- billing_date: day of month 1-31 only if clearly stated; otherwise "".
- Do not invent card or bank numbers; only digits explicitly in the note.
"""


def _load_few_shot(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("examples") or [])


def _few_shot_block(examples: list[dict[str, Any]]) -> str:
    if not examples:
        return ""
    parts = ["Few-shot examples (match this style):\n"]
    for i, ex in enumerate(examples, 1):
        parts.append(f"--- Example {i} ---\nNOTE:\n{ex.get('note', '')}\n\nJSON:\n")
        parts.append(json.dumps(ex.get("fields", {}), indent=2))
        parts.append("\n")
    return "\n".join(parts)


def _call_openai(note: str, examples: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise SystemExit("Install: pip install openai") from e

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set environment variable OPENAI_API_KEY")

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    system = (
        "You extract structured lead data for a medical alert transfer Google Form. "
        + FIELD_INSTRUCTIONS
        + "\n"
        + _few_shot_block(examples)
    )
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "Extract fields from this agent note. Reply with JSON only.\n\n"
                + note.strip(),
            },
        ],
        temperature=0.1,
    )
    content = resp.choices[0].message.content
    if not content:
        return {}
    return json.loads(content)


def _pick_llm_fields(raw: dict[str, Any]) -> dict[str, Any]:
    from mla_form_prefill import ENTRY_IDS

    out: dict[str, Any] = {}
    for k in ENTRY_IDS:
        v = raw.get(k)
        if v is None:
            out[k] = ""
        else:
            out[k] = str(v).strip()
    return out


def _merge_heuristic(llm_flat: dict[str, Any], note: str) -> dict[str, Any]:
    from mla_form_prefill import parse_agent_note

    h = parse_agent_note(note)
    out = dict(llm_flat)
    for k, v in h.items():
        if k not in out:
            continue
        cur = str(out.get(k, "")).strip()
        hv = str(v).strip()
        if not cur and hv:
            out[k] = v
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="MLA form prefill via OpenAI + few-shot JSON.")
    ap.add_argument("note_file", nargs="?", help="UTF-8 file with the agent note")
    ap.add_argument("--open", action="store_true", help="Open prefill URL in browser")
    ap.add_argument("--no-payment", action="store_true", help="Omit payment fields from URL")
    ap.add_argument("--no-merge", action="store_true", help="Do not fill empty fields from regex parser")
    ap.add_argument(
        "--few-shot",
        type=Path,
        default=DEFAULT_FEW_SHOT,
        help="Path to mla_few_shot_examples.json",
    )
    args = ap.parse_args()

    if args.note_file:
        note = Path(args.note_file).read_text(encoding="utf-8", errors="replace")
    else:
        note = sys.stdin.read()

    if not note.strip():
        print("No note text.", file=sys.stderr)
        sys.exit(1)

    examples = _load_few_shot(args.few_shot)
    raw = _call_openai(note, examples)
    llm_flat = _pick_llm_fields(raw)

    from mla_form_prefill import build_prefill_url, normalize_submitted_fields, parse_agent_note

    if args.no_merge:
        merged = llm_flat
    else:
        merged = _merge_heuristic(llm_flat, note)

    final = normalize_submitted_fields(merged, note)
    url = build_prefill_url(final, omit_payment=args.no_payment)
    print(url)
    if args.open:
        import webbrowser

        webbrowser.open(url)


if __name__ == "__main__":
    main()
