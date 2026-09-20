#!/usr/bin/env python3
"""
Phase 1g check: does the CALCULATION layer reproduce the answer key from the raw statements?

    python eval/check_calc.py

Pipeline under test (no labels are used to compute anything):
    statements -> parse -> rules -> linker -> user answers -> calc.py
Only the Reader agent (LLM) and the user are simulated, for one restaurant and 5 questions.
Q28 (no flights), Q29 (refuse investment advice) and Q30 (Hinglish) need the LLM; they come in Phase 3.
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from core import calc  # noqa: E402
from core.parse import parse_statement  # noqa: E402

DEMO = ROOT / "backend" / "demo"
Q = {q["id"]: q["expected"] for q in json.loads((ROOT / "eval" / "eval_questions.json").read_text())["questions"]}
profile = json.loads((DEMO / "profile.json").read_text())
txns = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8")) + \
       parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))

READER = {"BARBEQUE NATION": {"merchant": "Barbeque Nation", "category": "Food & Dining", "type": "expense"}}
ANSWERS = {"q_out_ravinder_kumar": "rent", "q_out_neha_verma": "gift_family", "q_out_sunita_devi": "gift_family",
           "q_in_sunita_devi": "family_income", "q_split_c0045": "split_yes"}
L = calc.build_ledger(txns, profile, ANSWERS, READER)

results = []


def check(qid, name, got, want, tol=0.01):
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) else got == want
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {qid} {name}: got {got!r}" + ("" if ok else f"  expected {want!r}"))


def rng(y, m, d1, d2):
    return date(y, m, d1), date(y, m, d2)


aug = calc._month_bounds("2026-08")
sep = calc._month_bounds("2026-09")

# ---- Money truth, income, categories
check("Q01", "true spend in August", calc.spend_summary(L, *aug)["total"], Q["Q01"]["true_spend"])
nt = calc.naive_vs_true(L, "2026-08")
print(f"       naive bank debits {nt['naive_bank_debits']:,.0f} | naive bank+card {nt['naive_bank_plus_card']:,.0f} | truth {nt['true_spend']:,.0f}")
check("Q02", "July income", calc.income_summary(L, *calc._month_bounds("2026-07"))["total"], Q["Q02"]["income"])
top = calc.spend_summary(L, *aug)["top"]
check("Q03", "top category August", top[0][0], Q["Q03"]["category"])
check("Q03", "top category amount", top[0][1], Q["Q03"]["amount"])
check("Q04", "top 3 categories", [{"category": c, "amount": a} for c, a in top[:3]], Q["Q04"]["top3"])

# ---- comparison (same days)
cmp_ = calc.compare_periods(L, rng(2026, 8, 1, 18), rng(2026, 9, 1, 18))
got_inc = [(r["category"], r["change"], r["change_pct"]) for r in cmp_["increased"]]
want_inc = [(r["category"], r["change"], r["change_pct"]) for r in Q["Q05"]["increased"]]
check("Q05", "categories that increased (same days)", got_inc, want_inc)
food = next(r for r in cmp_["rows"] if r["category"] == "Food & Dining")
check("Q06", "food change % (1-18 Sep vs 1-18 Aug)", food["change_pct"], Q["Q06"]["change_pct"])

# ---- subscriptions
subs = calc.subscriptions(L)
check("Q07", "subscription monthly amounts", sorted(i["monthly_amount"] for i in subs["items"]),
      sorted(s["monthly_amount"] for s in Q["Q07"]["must_include"]))
check("Q08", "monthly subscription cost", subs["monthly_total"], Q["Q08"]["monthly_total"])
nf = next(i for i in subs["items"] if i["name"] == "Netflix")["price_changes"][0]
check("Q09", "Netflix old -> new", (nf["old"], nf["new"]), (Q["Q09"]["old"], Q["Q09"]["new"]))
check("Q10", "yearly (monthly only)", subs["yearly_total"], Q["Q10"]["monthly_only"])
check("Q10", "yearly incl. annual Prime", subs["yearly_including_annual_plans"], Q["Q10"]["including_annual_prime"])

# ---- upcoming, committed, safe to spend
n7 = [(i["name"], i["date"], i["amount"]) for i in calc.next_days(L, 7)]
check("Q11", "next 7 days", [(d, a) for _, d, a in n7], [(p["due"], p["amount"]) for p in Q["Q11"]["payments"]])
com = calc.committed(L)
check("Q12", "committed next 30 days", com["total"], Q["Q12"]["committed_total"])
check("Q12", "share of next salary %", com["share_of_next_salary_pct"], Q["Q12"]["share_of_next_salary_pct"])
check("Q12", "due dates match", sorted(i["date"] for i in com["items"]), sorted(b["due"] for b in Q["Q12"]["breakdown"]))
sts = calc.safe_to_spend(L)
check("Q13", "safe to spend per day", sts["per_day"], Q["Q13"]["per_day"])
check("Q13", "days until salary", sts["days"], Q["Q13"]["days"])
afford = calc.can_i_afford(L, 4500)
check("Q14", "per day after Rs 4,500", afford["per_day_after"], Q["Q14"]["safe_per_day_after"])
check("Q14", "drop per day", afford["drop_per_day"], Q["Q14"]["drop_per_day"])

# ---- budgets and goals
bs = calc.budget_status(L, "2026-09")
check("Q15", "over budget categories", sorted(r["category"] for r in bs["over"]), sorted(r["category"] for r in Q["Q15"]["over"]))
check("Q15", "near limit categories", sorted(r["category"] for r in bs["near_limit"]), sorted(r["category"] for r in Q["Q15"]["near_limit"]))
fb = next(r for r in bs["rows"] if r["category"] == "Food & Dining")
check("Q16", "food budget % used", fb["pct"], Q["Q16"]["pct"])
check("Q16", "food spent", fb["spent"], Q["Q16"]["spent"])
gp = calc.goal_projection(L)
check("Q17", "months to laptop goal", gp["months"], Q["Q17"]["months"])
gw = calc.goal_projection(L, 1500)
check("Q18", "months with +1,500/month", gw["months"], Q["Q18"]["months"])
check("Q18", "months saved", gw["months_saved_vs_base"], Q["Q18"]["months_saved"])
print(f"       goal finish (base): {gp['finish_month']}   with +1,500: {gw['finish_month']}")

# ---- money-truth cases
dinner = calc.split_summary(L, "B0112")
check("Q19", "dinner paid / paid back / my share", (dinner["paid_by_me"], dinner["friends_paid_back"], dinner["my_share"]),
      (Q["Q19"]["paid_by_me"], Q["Q19"]["friends_paid_back"], Q["Q19"]["my_share"]))
owed = calc.owed_to_me(L)
check("Q20", "pending from friends", owed["pending_from_splits"], Q["Q20"]["pending_total"])
check("Q20", "loans outstanding", owed["loans_outstanding"], Q["Q20"]["loans_outstanding"])
dups = calc.duplicates(L)
check("Q21", "duplicate charge", [(d["merchant"], d["amount"], d["date"], d["count"]) for d in dups],
      [(x["merchant"], x["amount"], x["date"], x["count"]) for x in Q["Q21"]["suspected"]])
big = calc.unusually_large(L)
check("Q22", "unusually large purchase", [(b["merchant"], b["amount"], b["date"]) for b in big],
      [(Q["Q22"]["merchant"], Q["Q22"]["amount"], Q["Q22"]["date"])])
check("Q23", "card spend in August", calc.card_spend(L, "2026-08")["total"], Q["Q23"]["card_spend"])
cc = calc.card_bill_payments(L)
check("Q24", "card bills paid", cc["paid_total"], Q["Q24"]["paid_total"])
check("Q24", "counts as spending?", cc["counts_as_spending"], Q["Q24"]["counts_as_spending"])
check("Q25", "ATM cash", calc.cash_withdrawn(L)["total"], Q["Q25"]["total"])
check("Q26", "refunds received", calc.refunds_total(L)["total"], Q["Q26"]["total"])
check("Q27", "average monthly food Jun-Aug", calc.average_monthly(L, "Food & Dining", ["2026-06", "2026-07", "2026-08"]), Q["Q27"]["average"])
sw = calc.spend_summary(L, *aug, merchant="Swiggy")["total"]
zo = calc.spend_summary(L, *aug, merchant="Zomato")["total"]
check("Q31", "Swiggy vs Zomato August", (sw, zo), (Q["Q31"]["swiggy"], Q["Q31"]["zomato"]))
fl = calc.spend_summary(L, date(2026, 6, 1), L.as_of, category="Travel")
check("Q28", "no flight/travel spend (numeric part)", (fl["total"], fl["count"]), (0.0, 0))

print(f"\n{sum(results)}/{len(results)} checks passed   (Q28 wording, Q29 and Q30 need the LLM: Phase 3)")
sys.exit(0 if all(results) else 1)
