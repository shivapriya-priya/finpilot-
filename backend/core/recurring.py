"""
Phase 1d: find payments that repeat (subscriptions, rent, EMIs, bills, salary).

What makes this harder than "same merchant, same amount":
  - A 28-day mobile recharge drifts through the calendar; it is NOT a monthly bill.
  - Rent keeps its date but the UPI note changes and then disappears.
  - Bills like electricity repeat every month with a different amount.
  - A subscription can raise its price (Netflix 649 -> 699).
  - An annual plan (Prime) is charged once in a 4-month statement, so history cannot prove it.

Everything here is plain date and number logic. No LLM. That keeps it testable and explainable.
"""
from __future__ import annotations

import calendar
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Optional

from .normalize import normalize
from .parse import Txn
from .rules import RuleResult

MONTH_TOL_DAYS = 4          # how far a monthly payment may sit from its usual date
NDAY_TOL_DAYS = 1           # allowed wobble for "every N days" cycles
AMOUNT_TOL = 0.02           # 2% = the same price
EXCLUDED_TYPES = {"cc_payment", "refund", "reversal", "split_reimbursement"}

KIND_BY_CATEGORY = {
    "Subscriptions": "subscription",
    "EMI & Loans": "emi",
    "Bills & Utilities": "bill",
    "Health & Fitness": "membership",
    "Savings Transfer": "transfer",
    "Salary": "income",
}


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


@dataclass
class Recurring:
    key: str
    name: str
    category: str
    kind: str                    # subscription emi bill membership transfer income fixed_payment
    source: str                  # bank or card
    direction: str               # out or in
    cadence: str                 # monthly or every_N_days
    interval_days: Optional[int]
    amount_type: str             # fixed or variable
    expected_amount: float
    last_date: str
    next_due: str
    occurrences: int
    confidence: float
    status: str                  # confirmed or tentative
    dates: list[str] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    price_changes: list[dict] = field(default_factory=list)
    txn_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def monthly_cost(self) -> float:
        if self.cadence == "monthly":
            return self.expected_amount
        return round(self.expected_amount * 30.4375 / (self.interval_days or 30), 2)


# ------------------------------------------------------------------ date patterns
def _is_monthly(dates: list[date]) -> bool:
    first = dates[0]
    ks = []
    for d in dates:
        k = round((d - first).days / 30.4375)
        if abs((d - add_months(first, k)).days) > MONTH_TOL_DAYS:
            return False
        ks.append(k)
    return all(b - a == 1 for a, b in zip(ks, ks[1:]))     # one per month, none skipped


def _every_n_days(dates: list[date]) -> Optional[int]:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if len(gaps) < 2:
        return None                                        # need 3 occurrences to trust a cycle
    med = statistics.median(gaps)
    if 5 <= med <= 60 and all(abs(g - med) <= NDAY_TOL_DAYS for g in gaps):
        return int(round(med))
    return None


# ------------------------------------------------------------------ amount patterns
def _levels(amounts: list[float]) -> list[list]:
    """Group consecutive amounts within 2% into price levels: [[value, count, start_index], ...]"""
    levels: list[list] = []
    for i, a in enumerate(amounts):
        if levels and abs(a - levels[-1][0]) <= AMOUNT_TOL * levels[-1][0]:
            levels[-1][1] += 1
        else:
            levels.append([a, 1, i])
    return levels


def _amount_profile(amounts: list[float], dates: list[str]):
    """Return (amount_type, expected_amount, price_changes) or None if the amounts are too erratic."""
    levels = _levels(amounts)
    stable = len(levels) == 1 or (len(levels) <= 3 and all(l[1] >= 2 for l in levels[:-1]))
    if stable:
        changes = []
        for prev, cur in zip(levels, levels[1:]):
            changes.append({
                "date": dates[cur[2]], "old": prev[0], "new": cur[0],
                "change": round(cur[0] - prev[0], 2), "pct": round((cur[0] - prev[0]) / prev[0] * 100, 1),
                "confirmed": cur[1] >= 2,
            })
        return "fixed", levels[-1][0], changes
    med = statistics.median(amounts)
    if len(amounts) >= 3 and all(0.4 * med <= a <= 1.6 * med for a in amounts):
        return "variable", round(sum(amounts[-3:]) / len(amounts[-3:]), 2), []
    return None


# ------------------------------------------------------------------ main entry points
def detect_recurring(txns: list[Txn], results: dict[str, RuleResult], as_of: date) -> dict:
    groups: dict[tuple, list[tuple[Txn, RuleResult, str]]] = {}
    for t in txns:
        r = results[t.id]
        if r.type in EXCLUDED_TYPES:
            continue
        n = normalize(t.narration, t.source)
        groups.setdefault((t.source, n.key or n.counterparty, t.amount < 0), []).append((t, r, n.key))

    items: list[Recurring] = []
    for (source, key, is_out), rows in groups.items():
        rows.sort(key=lambda x: x[0].date)
        if len(rows) < 2:
            continue
        dts = [date.fromisoformat(t.date) for t, _, _ in rows]
        amts = [abs(t.amount) for t, _, _ in rows]

        cadence, interval, conf = None, None, 0.0
        if _is_monthly(dts):
            cadence = "monthly"
            conf = 0.95 if len(rows) >= 4 else 0.85 if len(rows) == 3 else 0.6
        else:
            n_days = _every_n_days(dts)
            if n_days:
                cadence, interval = f"every_{n_days}_days", n_days
                conf = 0.9 if len(rows) >= 4 else 0.75
        if not cadence:
            continue

        iso = [d.isoformat() for d in dts]
        profile = _amount_profile(amts, iso)
        if profile is None:
            continue
        if len(rows) == 2 and abs(amts[1] - amts[0]) > 0.05 * amts[0]:
            continue                                     # two payments alone cannot show a pattern
        amount_type, expected, changes = profile

        # next due date after "today"
        last = dts[-1]
        step = 0
        nxt = last
        while nxt <= as_of:
            step += 1
            nxt = add_months(last, step) if cadence == "monthly" else last + timedelta(days=interval * step)

        _, r0, _ = rows[-1]
        resolved = r0.resolved
        category = r0.category if resolved else "Uncategorised"
        name = r0.merchant if resolved else normalize(rows[-1][0].narration, source).counterparty.title()
        if r0.type == "own_transfer":
            kind = "transfer"
        elif not is_out:
            kind = "income"
        else:
            kind = KIND_BY_CATEGORY.get(category, "fixed_payment")
        note = ""
        if not resolved and is_out:
            note = "Same amount to the same person every month; this looks like rent or a fixed payment. Ask the user."
        if any(not c["confirmed"] for c in changes):
            note = (note + " Latest price change seen only once.").strip()

        items.append(Recurring(
            key=key, name=name, category=category, kind=kind, source=source,
            direction="out" if is_out else "in", cadence=cadence, interval_days=interval,
            amount_type=amount_type, expected_amount=round(expected, 2), last_date=iso[-1],
            next_due=nxt.isoformat(), occurrences=len(rows), confidence=conf,
            status="confirmed" if conf >= 0.8 else "tentative", dates=iso, amounts=amts,
            price_changes=changes, txn_ids=[t.id for t, _, _ in rows], note=note))

    items.sort(key=lambda i: i.next_due)

    # subscriptions charged once: maybe annual, maybe new. Cannot be proven from history.
    seen_keys = {i.key for i in items}
    watch = []
    for t in txns:
        r = results[t.id]
        n = normalize(t.narration, t.source)
        if r.category == "Subscriptions" and r.type == "expense" and n.key not in seen_keys:
            d = date.fromisoformat(t.date)
            watch.append({"name": r.merchant, "date": t.date, "amount": abs(t.amount), "txn_id": t.id,
                          "note": "Charged once in this statement. May be an annual plan; "
                                  f"if so it renews around {add_months(d, 12).isoformat()} (estimate).",
                          "estimated_monthly_cost": round(abs(t.amount) / 12, 2)})
    return {"items": items, "watchlist": watch}


def upcoming(items: list[Recurring], start: date, end: date) -> list[dict]:
    """Every expected occurrence between start and end (inclusive), for the Radar and committed totals."""
    out = []
    for it in items:
        if it.confidence < 0.6:
            continue
        last = date.fromisoformat(it.last_date)
        j = 1
        while True:
            d = add_months(last, j) if it.cadence == "monthly" else last + timedelta(days=it.interval_days * j)
            if d > end:
                break
            if d >= start:
                out.append({"date": d.isoformat(), "name": it.name, "amount": it.expected_amount,
                            "direction": it.direction, "kind": it.kind, "source": it.source,
                            "amount_type": it.amount_type, "confidence": it.confidence})
            j += 1
    return sorted(out, key=lambda x: (x["date"], x["name"]))
