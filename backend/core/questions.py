"""
Phase 1f: the human-in-the-loop half of Money Truth.

linker.py settles what it can prove (card-bill pairs, loans, split repayments).
Some rows cannot be proven from the statement alone:
    "You paid RAVINDER KUMAR Rs 12,000 every month"   -> rent? a regular payment?
    "You paid NEHA VERMA Rs 1,500 once"                -> a gift? a loan?
    "SUNITA DEVI sent you Rs 2,000"                    -> family money? someone paying you back?
So we ask - once per person, with sensible options - and remember the answer.

Everything here is a pure function. The browser keeps the answers ({question_id: option_id})
and sends them with each request, so the server needs no database.
"""
from __future__ import annotations

from typing import Optional

from .linker import Link
from .normalize import normalize
from .parse import Txn
from .recurring import Recurring
from .rules import RuleResult

SPEND_TYPES = {"expense", "refund", "reversal", "split_reimbursement"}
REVERSAL_WINDOW_DAYS = 7
REFUND_WINDOW_DAYS = 90

# what each answer means: option id -> (type, category, label shown to the user)
OUT_OPTIONS = {
    "rent": ("expense", "Rent", "Rent or a regular payment"),
    "gift_family": ("expense", "Family & Gifts", "A gift or family support"),
    "lent": ("p2p_lent", "Friends (loan)", "I lent it (expecting it back)"),
    "other_expense": ("expense", "Other", "Some other expense"),
}
IN_OPTIONS = {
    "family_income": ("income", "Family Support", "Money from family"),
    "repayment": ("p2p_repaid", "Friends (loan)", "Someone paying me back"),
    "other_income": ("income", "Other income", "Other income"),
}


def spend_effect(rtype: str, amount: float) -> float:
    """How much this row changes TRUE spending. Purchases add; refunds, reversals and split
    repayments subtract; transfers, loans, income and card-bill payments do nothing."""
    return round(-amount, 2) if rtype in SPEND_TYPES else 0.0


def _d(t: Txn):
    from datetime import date
    return date.fromisoformat(t.date)


def _slug(text: str) -> str:
    return "_".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


# ------------------------------------------------------------------ evidence pairs
def pair_evidence(txns: list[Txn], results: dict[str, RuleResult]) -> list[dict]:
    """Point every reversal and refund back at the charge it cancels (for the 'Why?' panel)."""
    norms = {t.id: normalize(t.narration, t.source) for t in txns}
    pairs = []
    for t in txns:
        typ = results[t.id].type
        if typ not in ("reversal", "refund") or t.amount <= 0:
            continue
        window = REVERSAL_WINDOW_DAYS if typ == "reversal" else REFUND_WINDOW_DAYS
        # A refund line is often printed differently from the purchase ("MYNTRA REFUND" vs
        # "MYNTRA DESIGNS PVT LTD"), so compare the merchant the rules recognised, not raw text.
        def same_merchant(c: Txn) -> bool:
            rc, rt = results[c.id], results[t.id]
            return (rc.resolved and rt.resolved and rc.merchant == rt.merchant) or norms[c.id].key == norms[t.id].key

        earlier = [c for c in txns if c.amount < 0 and results[c.id].type == "expense" and same_merchant(c)
                   and 0 <= (_d(t) - _d(c)).days <= window and abs(c.amount) >= t.amount]
        if not earlier:
            pairs.append({"kind": typ, "txn_ids": [t.id], "matched": False,
                          "note": f"{typ.title()} with no earlier charge found"})
            continue
        exact = [c for c in earlier if abs(c.amount) == t.amount]
        c = exact[-1] if exact else max(earlier, key=lambda x: x.date)
        partial = abs(c.amount) > t.amount
        if typ == "reversal":
            conf, note = 0.95, "Payment reversed: the two cancel out."
        elif exact:
            conf, note = 0.95, "Full refund of an earlier purchase."
        elif len(earlier) == 1:
            conf, note = 0.9, "Refund of part of an earlier purchase." if partial else "Refund."
        else:
            conf, note = 0.5, "Refund; several earlier charges could match, so this pairing is a best guess."
        pairs.append({"kind": typ, "matched": True, "txn_ids": [c.id, t.id], "amount": t.amount,
                      "partial": partial, "confidence": conf, "note": note})
    return pairs


# ------------------------------------------------------------------ questions
def build_questions(txns: list[Txn], results: dict[str, RuleResult], links: list[Link],
                    recurring: list[Recurring]) -> list[dict]:
    """`results` must already have linker overrides applied. Returns questions for what is still open."""
    norms = {t.id: normalize(t.narration, t.source) for t in txns}
    rent_like = {r.key: r for r in recurring if r.direction == "out" and r.kind == "fixed_payment"}

    groups: dict[tuple, list[Txn]] = {}
    for t in txns:
        if results[t.id].type == "p2p_pending":
            groups.setdefault((norms[t.id].key, t.amount < 0), []).append(t)

    questions = []
    for (key, is_out), rows in groups.items():
        who = norms[rows[0].id].counterparty.title()
        total = round(sum(abs(t.amount) for t in rows), 2)
        rec = rent_like.get(key) if is_out else None
        opts = OUT_OPTIONS if is_out else IN_OPTIONS
        if is_out:
            title = f"You paid {who} Rs {total:,.0f}" + (f" in {len(rows)} payments" if len(rows) > 1 else "") + ". What was it?"
        else:
            title = f"{who} sent you Rs {total:,.0f}. What was it?"
        questions.append({
            "id": f"q_{'out' if is_out else 'in'}_{_slug(key)}",
            "kind": "person_payment_out" if is_out else "person_payment_in",
            "counterparty": who, "txn_ids": [t.id for t in rows], "count": len(rows), "total": total,
            "title": title,
            "why": f"Same Rs {rec.expected_amount:,.0f} to the same person every month." if rec else "",
            "suggested": "rent" if rec else None,
            "confidence": 0.7 if rec else 0.0,
            "options": [{"id": k, "label": v[2]} for k, v in opts.items()],
        })

    # A split with only ONE share back rests on thin evidence: confirm it.
    for lk in links:
        if lk.kind != "split_reimbursement" or len(lk.txn_ids) != 2:
            continue
        exp_id, share_id = lk.txn_ids
        who = norms[share_id].counterparty.title()
        d = lk.detail
        questions.append({
            "id": f"q_split_{exp_id.lower()}", "kind": "split_confirm", "txn_ids": lk.txn_ids,
            "counterparty": who, "link_id": lk.link_id,
            "title": f"{who} sent Rs {d['per_head']:,.0f} after you paid Rs {d['bill']:,.0f} at {d['merchant']}. "
                     f"Is that their share of a {d['parties_estimated']}-way split?",
            "why": "One share came back; the rest would still be owed to you." if d["pending_people"] else "",
            "suggested": "split_yes", "confidence": 0.7,
            "options": [{"id": "split_yes", "label": f"Yes, a {d['parties_estimated']}-way split"},
                        {"id": "split_no", "label": "No, unrelated"}],
        })
    questions.sort(key=lambda q: q["id"])
    return questions


# ------------------------------------------------------------------ applying answers
def _user_result(old: RuleResult, rtype: str, category: str, note: str) -> RuleResult:
    return RuleResult(merchant=old.merchant, category=category, type=rtype, confidence=1.0,
                      rule="user_answer", resolved=True, hints={**old.hints, "answer": note})


def apply_answers(results: dict[str, RuleResult], questions: list[dict], answers: dict[str, str]) -> dict[str, RuleResult]:
    """Return NEW results with the user's answers applied. Unanswered questions change nothing."""
    out = dict(results)
    for q in questions:
        choice = answers.get(q["id"])
        if not choice:
            continue
        if q["kind"] == "split_confirm":
            if choice == "split_no":                      # the credit was not a split share after all
                share_id = q["txn_ids"][1]
                out[share_id] = _user_result(out[share_id], "income", "Other income", "not_a_split")
            continue
        table = OUT_OPTIONS if q["kind"] == "person_payment_out" else IN_OPTIONS
        if choice not in table:
            continue
        rtype, category, _ = table[choice]
        for tid in q["txn_ids"]:
            out[tid] = _user_result(out[tid], rtype, category, choice)
    return out


def open_receivables(links: list[Link], questions: list[dict], answers: dict[str, str]) -> list[dict]:
    """Money friends still owe you, skipping any split the user said was unrelated."""
    rejected = {q["link_id"] for q in questions if q["kind"] == "split_confirm" and answers.get(q["id"]) == "split_no"}
    return [{"link_id": lk.link_id, "merchant": lk.detail["merchant"], "pending_people": lk.detail["pending_people"],
             "per_head": lk.detail["per_head"], "pending_amount": lk.detail["pending_amount"],
             "confirmed": lk.link_id not in {q["link_id"] for q in questions if q["kind"] == "split_confirm"}
                          or answers.get(f"q_split_{lk.txn_ids[0].lower()}") == "split_yes"}
            for lk in links if lk.kind == "split_reimbursement" and lk.detail["pending_amount"] > 0
            and lk.link_id not in rejected]


def true_spend(results: dict[str, RuleResult], txns: list[Txn], month: Optional[str] = None) -> float:
    rows = [t for t in txns if not month or t.date.startswith(month)]
    return round(sum(spend_effect(results[t.id].type, t.amount) for t in rows), 2)
