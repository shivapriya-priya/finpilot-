"""
Phase 1d: link transactions that are two views of the same real-world event.

rules.py classifies one row at a time and is deliberately blind to history. Three
kinds of Money Truth can only be seen by looking ACROSS rows:

  1. Credit-card bill payments  - a debit on the bank statement and a credit on the
     card statement are the SAME rupee moving between your own accounts. Both sides
     are already typed "cc_payment" by rules.py; linking them just proves the pairing
     and lets calc.py show "this settles your July card spending of Rs X".

  2. Friend loans - money sent to a person (p2p_pending, unresolved) that comes back
     later from the same person is a loan, not a gift and not spend. rules.py cannot
     know this at the time the money goes out; only the repayment reveals it.

  3. Split reimbursements - you pay the full bill, friends pay back their share.
     Each incoming share looks identical to a random p2p credit until it is placed
     next to the expense it is repaying. What is left over after known repayments
     is money still owed to you.

Nothing here overrides a row rules.py already resolved with confidence (refunds and
reversals are handled fine by rules.py's dictionary+flags path, since a refund/reversal
carries its own keyword). Linker only touches rows rules.py left as p2p_pending, plus
it pairs the two already-correct cc_payment sides so the story can be told.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Optional

from .normalize import normalize
from .parse import Txn
from .rules import RuleResult

CC_AMOUNT_TOL = 0.5          # rupees; settlement amounts should match almost exactly
CC_DATE_WINDOW_DAYS = 5       # bank debit and card credit are usually the same day or next day

LOAN_AMOUNT_TOL = 0.02        # 2% - a friend rounds a repayment sometimes
LOAN_WINDOW_DAYS = 120        # a loan can take a while to come back

SPLIT_LOOKBACK_DAYS = 14      # how long after the group expense a share can still land
SPLIT_MIN_SHARE_FRACTION = 0.15   # a repayment must be a real fraction of the bill, not pocket change
SPLIT_MAX_SHARE_FRACTION = 0.9    # and less than the whole bill, or it isn't a "split"

SHARE_WORDS = ("SPLIT", "SHARE", "DINNER", "MOVIE", "BBQ", "TRIP", "PARTY", "OUTING", "LUNCH")

# When a repayment's note names the occasion, prefer an expense in the matching
# category over a merely closer-in-time one from an unrelated category.
WORD_CATEGORY_HINTS = {
    "MOVIE": "Entertainment", "DINNER": "Food & Dining", "BBQ": "Food & Dining",
    "LUNCH": "Food & Dining", "TRIP": "Travel", "OUTING": "Travel",
}

# Fixed personal obligations are never the kind of expense friends chip in for.
NOT_SPLITTABLE_CATEGORIES = {
    "Bills & Utilities", "EMI & Loans", "Subscriptions", "Health & Fitness",
    "Savings Transfer", "Interest", "Credit Card Payment",
}


@dataclass
class Link:
    link_id: str
    kind: str                 # cc_settlement, loan, split_reimbursement
    txn_ids: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Override:
    """A correction to a single row's RuleResult, produced by linking evidence."""
    type: str
    merchant: Optional[str] = None
    category: Optional[str] = None
    confidence: float = 0.9
    rule: str = "linker"
    link_id: str = ""

    def apply(self, r: RuleResult) -> RuleResult:
        return RuleResult(
            merchant=self.merchant or r.merchant,
            category=self.category or r.category,
            type=self.type,
            confidence=self.confidence,
            rule=self.rule,
            resolved=True,
            hints={**r.hints, "link_id": self.link_id},
        )


def _d(t: Txn) -> date:
    return date.fromisoformat(t.date)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# --------------------------------------------------------------------- 1. cc settlements
def _link_cc_payments(txns: list[Txn], results: dict[str, RuleResult]) -> list[Link]:
    bank_side = [t for t in txns if t.source == "bank" and results[t.id].type == "cc_payment"]
    card_side = [t for t in txns if t.source == "card" and results[t.id].type == "cc_payment"]
    used_card: set[str] = set()
    links: list[Link] = []
    for i, b in enumerate(sorted(bank_side, key=lambda t: t.date)):
        best = None
        for c in card_side:
            if c.id in used_card:
                continue
            if abs(abs(b.amount) - abs(c.amount)) > CC_AMOUNT_TOL:
                continue
            if abs((_d(b) - _d(c)).days) > CC_DATE_WINDOW_DAYS:
                continue
            if best is None or abs((_d(b) - _d(c)).days) < abs((_d(b) - _d(best)).days):
                best = c
        if best is not None:
            used_card.add(best.id)
            bill_month = add_months(_d(b), -1)   # the payment settles the PRIOR month's card spend
            links.append(Link(
                link_id=f"cc_{bill_month.year}_{bill_month.month:02d}", kind="cc_settlement",
                txn_ids=[b.id, best.id],
                detail={"amount": round(abs(b.amount), 2), "bank_date": b.date, "card_date": best.date},
            ))
    return links


# --------------------------------------------------------------------- 2 & 3. p2p
def _person_groups(txns: list[Txn], results: dict[str, RuleResult]) -> dict[str, list[Txn]]:
    """Group rows rules.py left as p2p_pending by the payee's VPA (or name if no VPA)."""
    groups: dict[str, list[Txn]] = {}
    for t in txns:
        r = results[t.id]
        if r.type != "p2p_pending":
            continue
        n = normalize(t.narration, t.source)
        person = n.vpa or n.counterparty.upper()
        groups.setdefault(person, []).append(t)
    for g in groups.values():
        g.sort(key=lambda t: t.date)
    return groups


def _link_loans(person_txns: list[Txn], results: dict[str, RuleResult],
                 claimed: set[str]) -> list[Link]:
    """Within one person's p2p_pending rows, pair an outgoing payment with a later
    same-amount incoming payment from the same person: that is a loan and its repayment."""
    links = []
    outgoing = [t for t in person_txns if t.amount < 0 and t.id not in claimed]
    incoming = [t for t in person_txns if t.amount > 0 and t.id not in claimed]
    for out in outgoing:
        match = None
        for inc in incoming:
            if inc.id in claimed:
                continue
            if _d(inc) < _d(out) or (_d(inc) - _d(out)).days > LOAN_WINDOW_DAYS:
                continue
            if abs(abs(inc.amount) - abs(out.amount)) > LOAN_AMOUNT_TOL * abs(out.amount):
                continue
            if match is None or _d(inc) < _d(match):
                match = inc
        if match is not None:
            claimed.add(out.id)
            claimed.add(match.id)
            links.append(Link(
                link_id=f"loan_{out.id.lower()}", kind="loan",
                txn_ids=[out.id, match.id],
                detail={"amount": round(abs(out.amount), 2), "lent_on": out.date, "repaid_on": match.date},
            ))
    return links


def _link_splits(txns: list[Txn], results: dict[str, RuleResult],
                  person_groups: dict[str, list[Txn]], claimed: set[str]) -> list[Link]:
    """Find a group expense followed by several people's shares coming back for it.

    Matching runs per-incoming-row (not per-expense): each candidate repayment is
    assigned to its nearest eligible expense first, so an earlier unrelated bill
    that happens to also fit the amount window never steals a later, better match.
    """
    from collections import Counter

    # A group bill doesn't have to be a merchant rules.py recognised yet (an unresolved
    # "unknown" debit, headed for the Reader agent, is just as valid a split candidate as
    # a dictionary-matched "expense") - only structurally non-bill types are excluded.
    expenses = [t for t in txns if t.amount < 0 and results[t.id].type in ("expense", "unknown")
                and results[t.id].category not in NOT_SPLITTABLE_CATEGORIES]
    all_incoming = sorted(
        (t for g in person_groups.values() for t in g if t.amount > 0 and t.id not in claimed),
        key=lambda t: t.date,
    )

    assigned: dict[str, list[Txn]] = {}   # expense.id -> candidate repayment rows
    for t in all_incoming:
        note = normalize(t.narration, t.source).note.upper()
        has_share_word = any(w in note for w in SHARE_WORDS)
        hinted_category = next((cat for w, cat in WORD_CATEGORY_HINTS.items() if w in note), None)
        candidates = []
        for exp in expenses:
            bill = abs(exp.amount)
            if not (_d(exp) <= _d(t) <= _d(exp) + timedelta(days=SPLIT_LOOKBACK_DAYS)):
                continue
            frac = t.amount / bill
            if not (SPLIT_MIN_SHARE_FRACTION <= frac <= SPLIT_MAX_SHARE_FRACTION):
                continue
            gap = (_d(t) - _d(exp)).days
            candidates.append((exp, gap))
        best = None
        if hinted_category:
            in_category = [c for c in candidates if results[c[0].id].category == hinted_category]
            if in_category:
                candidates = in_category
        if candidates:
            best = min(candidates, key=lambda c: c[1])
        if best is not None:
            assigned.setdefault(best[0].id, []).append(t)
            # stash the share-word flag on the txn id for the filtering pass below
            assigned[best[0].id][-1] = t
            if has_share_word:
                claimed.add(f"__shareword__{t.id}")   # sentinel, cleaned up below

    links = []
    for exp_id, rows in assigned.items():
        exp = next(t for t in expenses if t.id == exp_id)
        bill = abs(exp.amount)
        amt_counts = Counter(round(t.amount, 2) for t in rows)
        modal_amount, modal_n = amt_counts.most_common(1)[0]
        kept = [t for t in rows if f"__shareword__{t.id}" in claimed
                or (modal_n >= 2 and round(t.amount, 2) == modal_amount)]
        for t in rows:
            claimed.discard(f"__shareword__{t.id}")
        if not kept:
            continue
        kept.sort(key=lambda t: t.date)
        per_head = modal_amount if modal_n >= 2 else kept[0].amount
        received = round(sum(t.amount for t in kept), 2)
        parties = max(2, round(bill / per_head)) if per_head else 2   # me + everyone who paid a share
        pending_people = max(0, parties - 1 - len(kept))
        pending_amount = round(pending_people * per_head, 2)
        for t in kept:
            claimed.add(t.id)
        links.append(Link(
            link_id=f"split_{exp.id.lower()}", kind="split_reimbursement",
            txn_ids=[exp.id] + [t.id for t in kept],
            detail={
                "bill": round(bill, 2), "per_head": round(per_head, 2),
                "parties_estimated": parties, "received": received,
                "my_share": round(bill - received - pending_amount, 2),
                "pending_amount": pending_amount, "pending_people": pending_people,
                "merchant": results[exp.id].merchant, "category": results[exp.id].category,
            },
        ))
    return links


# --------------------------------------------------------------------- entry point
def link_all(txns: list[Txn], results: dict[str, RuleResult]) -> dict:
    """
    Returns:
      {
        "links": [Link, ...],
        "overrides": {txn_id: Override, ...},   # apply with Override.apply(old_result)
        "pending_splits": [ ... amounts still owed to the user ... ],
      }
    """
    links: list[Link] = []
    overrides: dict[str, Override] = {}

    links += _link_cc_payments(txns, results)

    claimed: set[str] = set()
    groups = _person_groups(txns, results)
    for person_txns in groups.values():
        loan_links = _link_loans(person_txns, results, claimed)
        links += loan_links
        for lk in loan_links:
            out_id, in_id = lk.txn_ids
            overrides[out_id] = Override("p2p_lent", category="Friends (loan)", link_id=lk.link_id)
            overrides[in_id] = Override("p2p_repaid", category="Friends (loan)", link_id=lk.link_id)

    split_links = _link_splits(txns, results, groups, claimed)
    links += split_links
    pending_splits = []
    for lk in split_links:
        exp_id, *share_ids = lk.txn_ids
        for sid in share_ids:
            overrides[sid] = Override(
                "split_reimbursement", category=lk.detail["category"], link_id=lk.link_id)
        if lk.detail["pending_amount"] > 0:
            pending_splits.append({
                "link_id": lk.link_id, "merchant": lk.detail["merchant"],
                "amount": lk.detail["pending_amount"], "people": lk.detail["pending_people"],
                "since": [t for t in txns if t.id == exp_id][0].date,
            })

    return {"links": links, "overrides": overrides, "pending_splits": pending_splits}


def apply_overrides(results: dict[str, RuleResult], overrides: dict[str, Override]) -> dict[str, RuleResult]:
    """Return a new results dict with linker overrides applied. Never mutates the input."""
    out = dict(results)
    for txn_id, ov in overrides.items():
        out[txn_id] = ov.apply(out[txn_id])
    return out
