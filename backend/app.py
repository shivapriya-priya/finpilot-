import json
import os
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from core.normalize import normalize
from core.parse import find_balance_breaks, parse_statement
from core.linker import apply_overrides, link_all
from core.questions import apply_answers, build_questions, open_receivables, pair_evidence, spend_effect
from core.recurring import detect_recurring, upcoming
from core.rules import classify, classify_all, unresolved_groups

app = FastAPI(title="FinPilot API")

origins = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "").split(",") if o.strip()]
origins.append("http://localhost:5173")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_DIR = Path(__file__).resolve().parent / "demo"
MAX_UPLOAD_BYTES = 2_000_000


def _load_demo():
    bank = parse_statement((DEMO_DIR / "bank_statement.csv").read_text(encoding="utf-8"))
    card = parse_statement((DEMO_DIR / "credit_card_statement.csv").read_text(encoding="utf-8"))
    return bank, card


def _row(t):
    n = normalize(t.narration, t.source)
    return {**t.to_dict(), "norm": n.to_dict(), "rule": classify(t, n).to_dict()}


def _summary(txns):
    dates = [t.date for t in txns]
    return {
        "rows": len(txns),
        "from": min(dates) if dates else None,
        "to": max(dates) if dates else None,
        "channels": dict(Counter(normalize(t.narration, t.source).channel for t in txns)),
    }


@app.get("/")
def root():
    return {"app": "FinPilot API", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok", "app": "FinPilot"}


@app.get("/demo/summary")
def demo_summary():
    bank, card = _load_demo()
    return {
        "bank": {**_summary(bank), "closing_balance": bank[-1].balance,
                 "balance_breaks": find_balance_breaks(bank)},
        "card": _summary(card),
    }


@app.get("/demo/classified")
def demo_classified():
    bank, card = _load_demo()
    txns = bank + card
    res = classify_all(txns)
    resolved = sum(1 for r in res.values() if r.resolved)
    return {
        "rows": len(txns),
        "resolved_by_rules": resolved,
        "coverage": round(resolved / len(txns), 3),
        "needs_reader_or_user": unresolved_groups(txns, res),
    }


@app.get("/demo/recurring")
def demo_recurring():
    bank, card = _load_demo()
    txns = bank + card
    as_of = date.fromisoformat(json.loads((DEMO_DIR / "profile.json").read_text())["as_of"])
    found = detect_recurring(txns, classify_all(txns), as_of)
    items = found["items"]
    subs = [i for i in items if i.kind == "subscription" and i.direction == "out"]
    monthly = round(sum(i.monthly_cost() for i in subs), 2)
    return {
        "as_of": as_of.isoformat(),
        "items": [i.to_dict() for i in items],
        "watchlist": found["watchlist"],
        "subscriptions": {"count": len(subs), "monthly_total": monthly, "yearly_total": round(monthly * 12, 2)},
        "upcoming_30_days": upcoming(items, as_of + timedelta(days=1), as_of + timedelta(days=30)),
    }


def _moneytruth(answers: dict):
    bank, card = _load_demo()
    txns = bank + card
    as_of = date.fromisoformat(json.loads((DEMO_DIR / "profile.json").read_text())["as_of"])
    rules = classify_all(txns)
    linked = link_all(txns, rules)
    after_link = apply_overrides(rules, linked["overrides"])
    recurring = detect_recurring(txns, after_link, as_of)["items"]
    questions = build_questions(txns, after_link, linked["links"], recurring)
    final = apply_answers(after_link, questions, answers)
    months = sorted({t.date[:7] for t in txns})
    by_month = {m: round(sum(spend_effect(final[t.id].type, t.amount) for t in txns if t.date.startswith(m)), 2) for m in months}
    return {
        "questions": [{**q, "answer": answers.get(q["id"])} for q in questions],
        "answered": sum(1 for q in questions if answers.get(q["id"])),
        "links": [lk.to_dict() for lk in linked["links"]],
        "evidence_pairs": pair_evidence(txns, after_link),
        "receivables": open_receivables(linked["links"], questions, answers),
        "held_back_rows": sum(1 for t in txns if final[t.id].type == "p2p_pending"),
        "true_spend_by_month": by_month,
        "still_needs_reader": [g for g in unresolved_groups(txns, rules) if g["status"] == "unknown"],
    }


@app.get("/demo/moneytruth")
def demo_moneytruth_get():
    return _moneytruth({})


@app.post("/demo/moneytruth")
def demo_moneytruth_post(body: dict = Body(default={})):
    """Body: {"answers": {"q_out_ravinder_kumar": "rent", ...}}. Nothing is stored on the server."""
    return _moneytruth(body.get("answers", {}))


@app.get("/demo/transactions")
def demo_transactions(source: str = "bank", limit: int = 20):
    bank, card = _load_demo()
    rows = bank if source == "bank" else card
    return [_row(t) for t in rows[: max(1, min(limit, 500))]]


@app.post("/parse")
async def parse_upload(file: UploadFile = File(...)):
    """Parse an uploaded statement CSV and return clean rows (nothing is stored)."""
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large for this demo (limit 2 MB).")
    try:
        txns = parse_statement(data.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "summary": _summary(txns),
        "balance_breaks": find_balance_breaks(txns) if txns and txns[0].source == "bank" else [],
        "transactions": [_row(t) for t in txns],
    }
