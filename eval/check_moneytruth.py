#!/usr/bin/env python3
"""
Phase 1f check: rules + linker + user answers -> every row ends up correct, and true spend matches.

    python eval/check_moneytruth.py

The Reader agent (LLM) does not exist yet, so this test plays its part for the ONE restaurant the
rules cannot place (Barbeque Nation), and plays the user for the 5 questions FinPilot asks.
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pandas as pd  # noqa: E402

from core.linker import apply_overrides, link_all  # noqa: E402
from core.normalize import normalize  # noqa: E402
from core.parse import parse_statement  # noqa: E402
from core.questions import (apply_answers, build_questions, open_receivables,  # noqa: E402
                            pair_evidence, spend_effect, true_spend)
from core.recurring import detect_recurring  # noqa: E402
from core.rules import RuleResult, classify_all  # noqa: E402

DEMO = ROOT / "backend" / "demo"
labels = pd.read_csv(ROOT / "eval" / "labels.csv").set_index("txn_id")
eval_q = {q["id"]: q for q in json.loads((ROOT / "eval" / "eval_questions.json").read_text())["questions"]}
profile = json.loads((DEMO / "profile.json").read_text())
AS_OF = date.fromisoformat(profile["as_of"])

bank = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8"))
card = parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))
txns = bank + card

results_list = []


def check(name, ok, detail=""):
    results_list.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# ---- 1. rules, then the Reader's answer for the unknown restaurant -------------------
rules = classify_all(txns)
for t in txns:
    if normalize(t.narration, t.source).key == "BARBEQUE NATION":
        rules[t.id] = RuleResult("Barbeque Nation", "Food & Dining", "expense", 0.85, "reader_simulated", True)

# ---- 2. linker (loans, splits, card-bill pairs) ---------------------------------------
linked = link_all(txns, rules)
after_link = apply_overrides(rules, linked["overrides"])
recurring = detect_recurring(txns, after_link, AS_OF)["items"]

# ---- 3. evidence pairs ------------------------------------------------------------------
pairs = pair_evidence(txns, after_link)
kinds = {(p["kind"], p["matched"]) for p in pairs}
rev = next(p for p in pairs if p["kind"] == "reversal")
check("failed-UPI reversal paired with the DMART debit", rev["matched"] and abs(rev["amount"]) == 640, str(rev["txn_ids"]))
myn = next(p for p in pairs if p["kind"] == "refund" and p["txn_ids"][-1].startswith("C"))
check("Myntra refund paired with the purchase and marked partial", myn["matched"] and myn["partial"] and myn["amount"] == 1799)
check("every refund and reversal found its charge", all(p["matched"] for p in pairs), f"{len(pairs)} pairs")

# ---- 4. questions -------------------------------------------------------------------------
questions = build_questions(txns, after_link, linked["links"], recurring)
qids = [q["id"] for q in questions]
expected_q = {"q_out_ravinder_kumar", "q_out_neha_verma", "q_out_sunita_devi", "q_in_sunita_devi"}
check("asks exactly the 4 person questions + 1 split confirmation", len(questions) == 5 and expected_q <= set(qids)
      and any(q["kind"] == "split_confirm" for q in questions), str(qids))
rq = next(q for q in questions if q["id"] == "q_out_ravinder_kumar")
check("rent question suggests 'rent' because the same amount repeats monthly", rq["suggested"] == "rent" and rq["count"] == 4, rq["why"])
check("no question asked about the loan or the dinner split (settled automatically)",
      not any(x in " ".join(qids) for x in ("rahul", "ananya_rao_out", "karthik", "meera")))

# ---- 5. before any answers -------------------------------------------------------------
pending = [t for t in txns if after_link[t.id].type == "p2p_pending"]
check("before answers, 7 rows are held back from spending totals", len(pending) == 7, f"{len(pending)} rows")

# ---- 6. the user answers ----------------------------------------------------------------
answers = {"q_out_ravinder_kumar": "rent", "q_out_neha_verma": "gift_family",
           "q_out_sunita_devi": "gift_family", "q_in_sunita_devi": "family_income",
           "q_split_c0045": "split_yes"}
check("the split question id is what the test expects", "q_split_c0045" in qids)
final = apply_answers(after_link, questions, answers)

bad = []
for t in txns:
    lab, got = labels.loc[t.id], final[t.id]
    if got.type != lab.true_type or got.category != lab.true_category:
        bad.append((t.id, t.narration[:36], got.type, got.category, lab.true_type, lab.true_category))
check("all 229 rows end with the right type AND category", not bad, f"{len(bad)} wrong {bad[:3]}")

diff = [t.id for t in txns if abs(spend_effect(final[t.id].type, t.amount) - labels.loc[t.id, "spend_effect"]) > 0.01]
check("true spend per row matches the answer key", not diff, f"{len(diff)} differ")

aug = true_spend(final, txns, "2026-08")
check("August true spend = answer key Q01", abs(aug - eval_q["Q01"]["expected"]["true_spend"]) < 0.01,
      f"{aug:,.0f} vs {eval_q['Q01']['expected']['true_spend']:,.0f}")
before = true_spend(after_link, txns, "2026-08")
print(f"      (August true spend before the user answers: {before:,.0f}; after: {aug:,.0f})")

# ---- 7. money friends still owe ---------------------------------------------------------
rec = open_receivables(linked["links"], questions, answers)
check("still owed by friends = Rs 560 (2 people x Rs 280)", len(rec) == 1 and rec[0]["pending_amount"] == 560 and rec[0]["pending_people"] == 2, str(rec))
rec_no = open_receivables(linked["links"], questions, {**answers, "q_split_c0045": "split_no"})
check("saying 'not a split' removes the receivable", rec_no == [])
alt = apply_answers(after_link, questions, {**answers, "q_split_c0045": "split_no"})
check("...and turns that credit into plain income", alt["B0170"].type == "income")

# ---- 8. answers are pure -----------------------------------------------------------------
check("apply_answers does not modify its input", after_link["B0013"].type == after_link["B0013"].type and
      all(after_link[t.id].type == "p2p_pending" for t in pending))

print(f"\n{sum(results_list)}/{len(results_list)} checks passed")
sys.exit(0 if all(results_list) else 1)
