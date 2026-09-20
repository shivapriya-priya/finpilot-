"""
Phase 1g: the calculation layer. AI interprets, CODE CALCULATES.

Every function here is plain Python: same input, same answer, no LLM. The Analyst agent (Phase 3)
will call these as tools, so it can explain a number but never invent one. Each result carries
`txn_ids` so the UI can show the exact transactions behind it ("Why?").

The flow:  statements -> parse -> rules -> (Reader for unknowns) -> linker -> user answers
           -> build_ledger()  -> the functions below.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .linker import Link, apply_overrides, link_all
from .normalize import normalize
from .parse import Txn
from .questions import apply_answers, build_questions, open_receivables, pair_evidence, spend_effect
from .recurring import Recurring, add_months, detect_recurring, upcoming
from .rules import RuleResult, classify_all


@dataclass
class Ledger:
    txns: list[Txn]
    results: dict[str, RuleResult]            # FINAL type/category per row
    recurring: list[Recurring]
    watchlist: list[dict]
    links: list[Link]
    questions: list[dict]
    pairs: list[dict]
    receivables: list[dict]
    profile: dict
    as_of: date
    balance: float                            # latest bank closing balance
    answers: dict = field(default_factory=dict)
    by_id: dict = field(default_factory=dict)


def build_ledger(txns: list[Txn], profile: dict, answers: Optional[dict] = None,
                 reader_overrides: Optional[dict] = None) -> Ledger:
    """
    reader_overrides: decisions of the Reader agent for merchants the rules could not place, keyed by
    the normalised merchant key, e.g. {"BARBEQUE NATION": {"merchant": ..., "category": ..., "type": "expense"}}
    """
    answers = answers or {}
    as_of = date.fromisoformat(profile["as_of"])
    rules = classify_all(txns)
    for t in txns:
        ov = (reader_overrides or {}).get(normalize(t.narration, t.source).key)
        if ov and not rules[t.id].resolved:
            rules[t.id] = RuleResult(ov.get("merchant", rules[t.id].merchant), ov["category"], ov.get("type", "expense"),
                                     ov.get("confidence", 0.85), "reader", True)
    linked = link_all(txns, rules)
    after_link = apply_overrides(rules, linked["overrides"])
    first_pass = detect_recurring(txns, after_link, as_of)["items"]          # used to suggest answers (rent...)
    questions = build_questions(txns, after_link, linked["links"], first_pass)
    final = apply_answers(after_link, questions, answers)
    found = detect_recurring(txns, final, as_of)                              # again, now with the user's answers
    bank = [t for t in txns if t.source == "bank" and t.balance is not None]
    return Ledger(txns=txns, results=final, recurring=found["items"], watchlist=found["watchlist"],
                  links=linked["links"], questions=questions, pairs=pair_evidence(txns, final),
                  receivables=open_receivables(linked["links"], questions, answers), profile=profile,
                  as_of=as_of, balance=bank[-1].balance if bank else 0.0, answers=answers,
                  by_id={t.id: t for t in txns})


# ------------------------------------------------------------------ helpers
def _r(x: float) -> float:
    return round(float(x), 2)


def _rows(L: Ledger, start: Optional[date] = None, end: Optional[date] = None):
    for t in L.txns:
        d = date.fromisoformat(t.date)
        if (start and d < start) or (end and d > end):
            continue
        yield t


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    return first, add_months(first, 1) - timedelta(days=1)


# ------------------------------------------------------------------ spending and income
def spend_summary(L: Ledger, start: date, end: date, category: Optional[str] = None,
                  merchant: Optional[str] = None) -> dict:
    """TRUE spend: purchases minus refunds/reversals/split repayments; excludes transfers, loans, card-bill payments."""
    by_cat: dict[str, float] = {}
    ids = []
    total = 0.0
    for t in _rows(L, start, end):
        r = L.results[t.id]
        eff = spend_effect(r.type, t.amount)
        if eff == 0:
            continue
        if category and r.category != category:
            continue
        if merchant and r.merchant != merchant:
            continue
        by_cat[r.category] = by_cat.get(r.category, 0.0) + eff
        total += eff
        ids.append(t.id)
    ranked = sorted(((k, _r(v)) for k, v in by_cat.items()), key=lambda kv: -kv[1])
    return {"total": _r(total), "by_category": dict(ranked), "top": ranked, "txn_ids": ids, "count": len(ids)}


def income_summary(L: Ledger, start: date, end: date) -> dict:
    ids, by = [], {}
    for t in _rows(L, start, end):
        r = L.results[t.id]
        if r.type == "income":
            by[r.category] = by.get(r.category, 0.0) + t.amount
            ids.append(t.id)
    return {"total": _r(sum(by.values())), "by_category": {k: _r(v) for k, v in by.items()}, "txn_ids": ids}


def compare_periods(L: Ledger, a: tuple[date, date], b: tuple[date, date]) -> dict:
    """Compare two equal-length periods (never full month vs part month)."""
    sa, sb = spend_summary(L, *a)["by_category"], spend_summary(L, *b)["by_category"]
    rows = []
    for cat in sorted(set(sa) | set(sb)):
        x, y = sa.get(cat, 0.0), sb.get(cat, 0.0)
        rows.append({"category": cat, "period_a": _r(x), "period_b": _r(y), "change": _r(y - x),
                     "change_pct": None if x == 0 else _r((y - x) / x * 100)})
    return {"rows": rows, "increased": sorted([r for r in rows if r["change"] > 0], key=lambda r: -r["change"]),
            "decreased": sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])}


def average_monthly(L: Ledger, category: str, months: list[str]) -> float:
    vals = [spend_summary(L, *_month_bounds(m), category=category)["total"] for m in months]
    return _r(sum(vals) / len(vals))


def naive_vs_true(L: Ledger, month: str) -> dict:
    """What a simple app would show vs the truth: the Money Truth headline."""
    s, e = _month_bounds(month)
    bank_out = _r(sum(-t.amount for t in _rows(L, s, e) if t.source == "bank" and t.amount < 0))
    card_out = _r(sum(-t.amount for t in _rows(L, s, e) if t.source == "card" and t.amount < 0))
    true = spend_summary(L, s, e)["total"]
    return {"month": month, "naive_bank_debits": bank_out, "naive_bank_plus_card": _r(bank_out + card_out),
            "true_spend": true, "naive_overstates_by": _r(bank_out + card_out - true)}


# ------------------------------------------------------------------ recurring, upcoming, committed, safe-to-spend
def subscriptions(L: Ledger) -> dict:
    subs = [i for i in L.recurring if i.kind == "subscription" and i.direction == "out"]
    monthly = sum(i.monthly_cost() for i in subs)
    annual_extra = sum(w["amount"] for w in L.watchlist)
    return {"items": [{"name": i.name, "monthly_amount": i.expected_amount, "source": i.source, "next_due": i.next_due,
                       "price_changes": i.price_changes, "txn_ids": i.txn_ids} for i in subs],
            "monthly_total": _r(monthly), "yearly_total": _r(monthly * 12),
            "yearly_including_annual_plans": _r(monthly * 12 + annual_extra), "watchlist": L.watchlist}


def _card_bill(L: Ledger) -> Optional[dict]:
    """Next credit-card bill: due one month after the last payment, for card spending since the last paid month."""
    pays = [t for t in L.txns if t.source == "bank" and L.results[t.id].type == "cc_payment"]
    if not pays:
        return None
    last = max(date.fromisoformat(t.date) for t in pays)
    due = add_months(last, 1)
    while due <= L.as_of:
        due = add_months(due, 1)
    paid_month_end = add_months(last, -1)
    paid_month_end = add_months(date(paid_month_end.year, paid_month_end.month, 1), 1) - timedelta(days=1)
    ids = [t.id for t in L.txns if t.source == "card" and date.fromisoformat(t.date) > paid_month_end
           and spend_effect(L.results[t.id].type, t.amount) != 0]
    amount = _r(sum(spend_effect(L.results[i].type, L.by_id[i].amount) for i in ids))
    return {"name": "Credit card bill (card spending so far)", "date": due.isoformat(), "amount": amount,
            "kind": "card_bill", "amount_type": "lower_bound", "txn_ids": ids}


def committed(L: Ledger, days: int = 30) -> dict:
    start, end = L.as_of + timedelta(days=1), L.as_of + timedelta(days=days)
    items = [w for w in upcoming(L.recurring, start, end)
             if w["direction"] == "out" and w["source"] == "bank" and w["kind"] != "transfer"]
    bill = _card_bill(L)
    if bill and start <= date.fromisoformat(bill["date"]) <= end:
        items.append(bill)
    items.sort(key=lambda x: x["date"])
    total = _r(sum(i["amount"] for i in items))
    salary = next_salary(L)
    planned = [w for w in upcoming(L.recurring, start, end) if w["kind"] == "transfer"]
    return {"window": [start.isoformat(), end.isoformat()], "total": total, "items": items,
            "share_of_next_salary_pct": _r(total / salary["amount"] * 100) if salary["amount"] else None,
            "planned_savings_not_included": _r(sum(p["amount"] for p in planned))}


def next_salary(L: Ledger) -> dict:
    inc = [i for i in L.recurring if i.kind == "income" and i.direction == "in"]
    if inc:
        s = max(inc, key=lambda i: i.expected_amount)
        return {"date": s.next_due, "amount": s.expected_amount, "source": "detected"}
    p = L.profile["next_salary"]
    return {"date": p["date"], "amount": p["amount"], "source": "profile"}


def next_days(L: Ledger, days: int) -> list[dict]:
    start, end = L.as_of + timedelta(days=1), L.as_of + timedelta(days=days)
    items = [w for w in upcoming(L.recurring, start, end) if w["direction"] == "out" and w["source"] == "bank"
             and w["kind"] != "transfer"]
    bill = _card_bill(L)
    if bill and start <= date.fromisoformat(bill["date"]) <= end:
        items.append(bill)
    return sorted(items, key=lambda x: x["date"])


def safe_to_spend(L: Ledger, extra_spend: float = 0.0) -> dict:
    """(balance - bills due before next salary - buffer - extra) / days until salary."""
    buffer = L.profile["safety_buffer"]
    salary = next_salary(L)
    sal_date = date.fromisoformat(salary["date"])
    start = L.as_of + timedelta(days=1)
    days_left = max(1, (sal_date - start).days)
    due = [i for i in committed(L, days=max(days_left, 1))["items"] if date.fromisoformat(i["date"]) < sal_date]
    due_total = _r(sum(i["amount"] for i in due))
    per_day = _r((L.balance - due_total - buffer - extra_spend) / days_left)
    return {"per_day": per_day, "days": days_left, "balance": _r(L.balance), "due_before_salary": due_total,
            "due_items": due, "buffer": buffer, "next_salary_date": salary["date"], "extra_spend": _r(extra_spend)}


def can_i_afford(L: Ledger, amount: float) -> dict:
    """Decision support, not a verdict: show what this purchase does to the safe daily amount."""
    before, after = safe_to_spend(L), safe_to_spend(L, amount)
    return {"amount": _r(amount), "per_day_before": before["per_day"], "per_day_after": after["per_day"],
            "drop_per_day": _r(before["per_day"] - after["per_day"]), "days": before["days"],
            "stays_within_safe_budget": after["per_day"] >= 0,
            "shortfall": _r(max(0.0, -after["per_day"] * after["days"]))}


# ------------------------------------------------------------------ budgets and goals
def budget_status(L: Ledger, month: str) -> dict:
    s, e = _month_bounds(month)
    e = min(e, L.as_of)
    cats = spend_summary(L, s, e)["by_category"]
    rows = []
    for cat, budget in L.profile["monthly_budgets"].items():
        used = cats.get(cat, 0.0)
        pct = used / budget * 100
        rows.append({"category": cat, "budget": budget, "spent": _r(used), "pct": _r(pct),
                     "remaining": _r(budget - used), "status": "over" if used > budget else "near" if pct >= 80 else "ok"})
    return {"month": month, "rows": rows, "over": [r for r in rows if r["status"] == "over"],
            "near_limit": [r for r in rows if r["status"] == "near"]}


def goal_projection(L: Ledger, extra_monthly: float = 0.0) -> dict:
    g = L.profile["goal"]
    remaining = g["target"] - g["saved"]
    base = math.ceil(remaining / g["monthly_contribution"])
    monthly = g["monthly_contribution"] + extra_monthly
    months = math.ceil(remaining / monthly)
    first_next = add_months(date(L.as_of.year, L.as_of.month, 1), 1)
    finish = add_months(first_next, months - 1)
    return {"goal": g["name"], "target": g["target"], "saved": g["saved"], "remaining": remaining,
            "monthly": monthly, "months": months, "months_saved_vs_base": base - months,
            "finish_month": finish.strftime("%b %Y")}


# ------------------------------------------------------------------ money truth summaries
def owed_to_me(L: Ledger) -> dict:
    lent = {}
    for t in L.txns:
        r = L.results[t.id]
        if r.type in ("p2p_lent", "p2p_repaid"):
            lent[r.merchant] = lent.get(r.merchant, 0.0) - t.amount      # lent: +, repaid: -, net = still owed
    loans_out = _r(sum(v for v in lent.values() if v > 0))       # lent (positive) minus repaid
    pend = _r(sum(x["pending_amount"] for x in L.receivables))
    return {"pending_from_splits": pend, "splits": L.receivables, "loans_outstanding": loans_out,
            "total_owed": _r(pend + loans_out)}


def split_summary(L: Ledger, expense_id: str) -> Optional[dict]:
    lk = next((x for x in L.links if x.kind == "split_reimbursement" and x.txn_ids[0] == expense_id), None)
    if not lk:
        return None
    d = lk.detail
    return {"paid_by_me": d["bill"], "friends_paid_back": d["received"], "my_share": d["my_share"],
            "pending_amount": d["pending_amount"], "txn_ids": lk.txn_ids}


def refunds_total(L: Ledger) -> dict:
    ids = [t.id for t in L.txns if L.results[t.id].type == "refund"]
    return {"total": _r(sum(L.by_id[i].amount for i in ids)), "txn_ids": ids}


def cash_withdrawn(L: Ledger) -> dict:
    ids = [t.id for t in L.txns if L.results[t.id].category == "Cash Withdrawal" and t.amount < 0]
    return {"total": _r(sum(-L.by_id[i].amount for i in ids)), "count": len(ids), "txn_ids": ids}


def card_spend(L: Ledger, month: str) -> dict:
    s, e = _month_bounds(month)
    ids = [t.id for t in _rows(L, s, e) if t.source == "card" and spend_effect(L.results[t.id].type, t.amount) != 0]
    return {"total": _r(sum(spend_effect(L.results[i].type, L.by_id[i].amount) for i in ids)), "txn_ids": ids}


def card_bill_payments(L: Ledger) -> dict:
    ids = [t.id for t in L.txns if t.source == "bank" and L.results[t.id].type == "cc_payment"]
    return {"paid_total": _r(sum(-L.by_id[i].amount for i in ids)), "counts_as_spending": False, "txn_ids": ids}


# ------------------------------------------------------------------ anomalies
def duplicates(L: Ledger) -> list[dict]:
    groups: dict[tuple, list[Txn]] = {}
    for t in L.txns:
        if t.amount < 0 and L.results[t.id].type == "expense":
            groups.setdefault((t.source, L.results[t.id].merchant, t.date, t.amount), []).append(t)
    return [{"merchant": k[1], "amount": abs(k[3]), "date": k[2], "count": len(v), "txn_ids": [t.id for t in v]}
            for k, v in groups.items() if len(v) >= 2]


def unusually_large(L: Ledger, min_amount: float = 5000, multiple: float = 4.0) -> list[dict]:
    """Big one-off purchases: far above what is normal for that category. Recurring payments are excluded."""
    recurring_ids = {i for r in L.recurring for i in r.txn_ids}
    by_cat: dict[str, list[Txn]] = {}
    for t in L.txns:
        if t.amount < 0 and L.results[t.id].type == "expense" and t.id not in recurring_ids:
            by_cat.setdefault(L.results[t.id].category, []).append(t)
    out = []
    for cat, rows in by_cat.items():
        med = statistics.median(abs(t.amount) for t in rows)
        for t in rows:
            if abs(t.amount) >= max(min_amount, multiple * med):
                out.append({"merchant": L.results[t.id].merchant, "amount": abs(t.amount), "date": t.date,
                            "source": t.source, "category": cat, "typical_in_category": _r(med), "txn_id": t.id})
    return sorted(out, key=lambda x: -x["amount"])
