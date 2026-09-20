#!/usr/bin/env python3
"""
Phase 1c check: how many rows do the rules resolve, and are they right?

    python eval/check_rules.py

Two numbers matter:
  precision - of the rows the rules were confident about, how many are correct? (must be 100%)
  coverage  - how many rows the rules resolved on their own (the rest go to the Reader agent / the user)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pandas as pd  # noqa: E402

from core.parse import parse_statement  # noqa: E402
from core.rules import classify_all, unresolved_groups  # noqa: E402

DEMO = ROOT / "backend" / "demo"
labels = pd.read_csv(ROOT / "eval" / "labels.csv").set_index("txn_id")

bank = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8"))
card = parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))
allx = bank + card
res = classify_all(allx)

# label types that the rules are NOT expected to settle alone (they need the user or the LLM)
HARD_MERCHANTS = {"Landlord (Ravinder Kumar)", "Rahul Sharma", "Ananya Rao", "Karthik N", "Meera Iyer",
                  "Neha Verma", "Sunita Devi", "Barbeque Nation"}

resolved = [t for t in allx if res[t.id].resolved]
unresolved = [t for t in allx if not res[t.id].resolved]

wrong = []
for t in resolved:
    r, lab = res[t.id], labels.loc[t.id]
    type_ok = r.type == lab.true_type
    cat_ok = r.category == lab.true_category
    if not (type_ok and cat_ok):
        wrong.append((t.id, t.narration[:45], f"rule=({r.type},{r.category})", f"truth=({lab.true_type},{lab.true_category})"))

precision = 1 - len(wrong) / max(1, len(resolved))
coverage = len(resolved) / len(allx)

unexpected = [t for t in unresolved if labels.loc[t.id].true_merchant not in HARD_MERCHANTS]
hard_but_resolved = [t for t in resolved if labels.loc[t.id].true_merchant in HARD_MERCHANTS]

print(f"rows: {len(allx)}   resolved by rules: {len(resolved)} ({coverage:.0%})   passed on: {len(unresolved)}")
print(f"precision on resolved rows: {precision:.1%}  ({len(wrong)} wrong)")
for w in wrong[:10]:
    print("  WRONG:", *w)

print("\nRows passed on to the Reader agent / user, grouped by payee:")
for g in unresolved_groups(allx, res):
    print(f"  {g['counterparty'][:28]:28} {g['status']:12} rows={len(g['txn_ids']):2}  out={g['total_out']:>9,.0f}  in={g['total_in']:>9,.0f}")

ok = not wrong and not unexpected and not hard_but_resolved
print("\n[{}] no wrong answers among resolved rows".format("PASS" if not wrong else "FAIL"))
print("[{}] every passed-on row is a genuine hard case".format("PASS" if not unexpected else "FAIL"), [t.narration[:40] for t in unexpected[:3]])
print("[{}] no hard case was guessed".format("PASS" if not hard_but_resolved else "FAIL"), [t.narration[:40] for t in hard_but_resolved[:3]])
sys.exit(0 if ok else 1)
