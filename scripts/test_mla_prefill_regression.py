"""
Run parser against examples in mla_few_shot_examples.json (no Google Form, no network).

Usage (from the `mla-form-prefill` folder):
  python scripts/test_mla_prefill_regression.py

Add more cases by appending { "note": "...", "fields": { ... } } to that JSON.
Only keys present in each example's "fields" are asserted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    path = root / "mla_few_shot_examples.json"
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).expanduser().resolve()

    if not path.is_file():
        print(f"Missing examples file: {path}", file=sys.stderr)
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    examples = data.get("examples") or []
    if not examples:
        print("No examples in JSON.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(root))
    from mla_form_prefill import parse_agent_note  # noqa: E402

    failures: list[str] = []
    for i, ex in enumerate(examples, start=1):
        note = (ex.get("note") or "").strip()
        expected = ex.get("fields") or {}
        if not note:
            failures.append(f"Example #{i}: empty note")
            continue
        if not isinstance(expected, dict):
            failures.append(f"Example #{i}: fields must be an object")
            continue

        got = parse_agent_note(note)
        for key, exp_val in expected.items():
            exp_s = "" if exp_val is None else str(exp_val).strip()
            got_s = "" if got.get(key) is None else str(got.get(key, "")).strip()
            if got_s != exp_s:
                failures.append(
                    f"Example #{i} field {key!r}:\n  expected: {exp_s!r}\n  got:      {got_s!r}"
                )

    if failures:
        print(f"FAILED {len(failures)} assertion(s) across {len(examples)} example(s):\n")
        for line in failures:
            print(line)
            print()
        return 1

    print(f"OK - all {len(examples)} example(s) match on declared fields.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
