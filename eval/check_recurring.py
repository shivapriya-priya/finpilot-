#!/usr/bin/env python3
"""
Phase 1d check: does recurring detection find exactly the real recurring payments?

    python eval/check_recurring.py
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pandas as pd  # noqa: E402

from core.parse import parse_statement  # noqa: E402
from core.recurring import add_months, detect_recurring, upcoming  # noqa: E402
from core.rules import classify_all  # noqa: E402

DEMO = ROOT / "backend" / "demo"
labels = pd.read_csv(ROOT / "eval" / "labels.csv")
truth = {r["id"]: r for r in json.loads((ROOT / "eval" / "recurring_truth.json").read_text())}
profile = json.loads((DEMO / "profile.json").read_text())
eval_q = {q["id"]: q for q in json.loads((ROOT / "eval" / "eval_questions.json").read_text())["questions"]}
AS_OF = date.fromisoformat(profile["as_of"])

bank = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8"))
card = parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))
txns = bank + card
res = classify_all(txns)
out = detect_recurring(txns, res, AS_OF)
items = out["items"]

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


expected_ids = [i for i in truth if i != "prime_annual"]
by_ids = {frozenset(i.txn_ids): i for i in items}

for rid in expected_ids:
    ids = frozenset(labels[labels.recurring_id == rid].txn_id)
    it = by_ids.get(ids)
    check(f"'{rid}' found with exactly the right transactions", it is not None,
          f"{len(ids)} rows" if it else "not found")
    if not it:
        continue
    t = truth[rid]
    want_cadence = "every_28_days" if t["cadence"] == "every_28_days" else "monthly"
    check(f"  '{rid}' cadence = {want_cadence}", it.cadence == want_cadence, it.cadence)
    if t["cadence"] == "monthly":
        want_next = add_months(date.fromisoformat(it.last_date), 1)
    else:
        want_next = date.fromisoformat(t["next_due"])
    check(f"  '{rid}' next due {want_next}", it.next_due == want_next.isoformat(), it.next_due)

extra = [i.name for i in items if frozenset(i.txn_ids) not in {frozenset(labels[labels.recurring_id == r].txn_id) for r in expected_ids}]
check("no false positives (nothing extra detected)", not extra, f"extra: {extra}")

nf = next(i for i in items if i.name == "Netflix")
check("Netflix price rise 649 -> 699 detected", len(nf.price_changes) == 1 and nf.price_changes[0]["old"] == 649
      and nf.price_changes[0]["new"] == 699 and nf.price_changes[0]["date"].startswith("2026-08"), str(nf.price_changes))
el = next(i for i in items if "ELECTRICITY" in i.key)
check("electricity is a variable monthly bill, expected ~1,780", el.amount_type == "variable" and el.expected_amount == 1780,
      f"{el.amount_type} {el.expected_amount}")
rent = next(i for i in items if i.key == "RAVINDER KUMAR")
check("rent to a person found although the note changes and disappears", rent.expected_amount == 12000 and rent.occurrences == 4)

subs = [i for i in items if i.kind == "subscription" and i.direction == "out"]
monthly_subs = sum(i.monthly_cost() for i in subs)
check("monthly subscription cost = 1,392", round(monthly_subs) == eval_q["Q08"]["expected"]["monthly_total"], f"{monthly_subs}")
check("Amazon Prime (single charge) is on the watchlist, not detected as monthly",
      any(w["name"] == "Amazon Prime" and w["amount"] == 1499 for w in out["watchlist"]) and "Amazon Prime" not in [i.name for i in items])

# upcoming bank obligations must equal the answer key's committed total minus the card bill
window = upcoming(items, AS_OF + timedelta(days=1), AS_OF + timedelta(days=30))
bank_out = [w for w in window if w["direction"] == "out" and w["source"] == "bank" and w["kind"] != "transfer"]
q12 = eval_q["Q12"]["expected"]
card_bill = next(b["amount"] for b in q12["breakdown"] if "Credit card bill" in b["name"])
check("upcoming bank obligations = answer-key committed total (minus card bill)",
      abs(sum(w["amount"] for w in bank_out) - (q12["committed_total"] - card_bill)) < 0.01,
      f"{sum(w['amount'] for w in bank_out):,.0f} vs {q12['committed_total'] - card_bill:,.0f}")
next7 = [w for w in bank_out if w["date"] <= (AS_OF + timedelta(days=7)).isoformat()]
check("next 7 days = only the Jio recharge on 22 Sep", [(w["name"], w["date"]) for w in next7] == [("Jio Prepaid", "2026-09-22")], str(next7))
check("Jio recharge is NOT double-listed on 20 Oct (outside 30 days)", sum(1 for w in window if w["name"] == "Jio Prepaid") == 1)

print("\nDetected:")
for i in items:
    print(f"  {i.name[:26]:26} {i.kind:14} {i.cadence:14} {i.amount_type:8} {i.expected_amount:>9,.0f}  next {i.next_due}  conf {i.confidence}")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
