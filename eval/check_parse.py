#!/usr/bin/env python3
"""
Phase 1 check: does the parser + normalizer understand every row correctly?

Run from the repo root (C:\\dev\\finpilot) with the venv active:
    python eval/check_parse.py
It needs pandas only for reading labels.csv:  pip install pandas
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pandas as pd  # noqa: E402

from core.normalize import normalize  # noqa: E402
from core.parse import find_balance_breaks, parse_statement  # noqa: E402

DEMO = ROOT / "backend" / "demo"
labels = pd.read_csv(ROOT / "eval" / "labels.csv").set_index("txn_id")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


bank = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8"))
card = parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))
allx = bank + card

check("bank rows parsed", len(bank) == 179, f"{len(bank)} rows")
check("card rows parsed", len(card) == 50, f"{len(card)} rows")
check("source auto-detected", {t.source for t in bank} == {"bank"} and {t.source for t in card} == {"card"})

# every parsed row must match the answer key on date and amount
bad = [t.id for t in allx if t.id not in labels.index
       or abs(labels.loc[t.id, "amount_signed"] - t.amount) > 0.01
       or labels.loc[t.id, "date"] != t.date]
check("dates and amounts match the answer key", not bad, f"{len(bad)} mismatches {bad[:5]}")

breaks = find_balance_breaks(bank)
check("bank balance chain is unbroken (no missing rows)", not breaks, f"{len(breaks)} breaks")

norm = {t.id: normalize(t.narration, t.source) for t in allx}
other = [t.narration for t in allx if norm[t.id].channel == "OTHER"]
check("every narration has a known channel", not other, f"unknown: {other[:3]}")

upi = [t for t in bank if norm[t.id].channel == "UPI"]
no_payee = [t.narration for t in upi if not norm[t.id].counterparty]
check("every UPI row has a payee", not no_payee, f"{len(upi)} UPI rows")
no_vpa = [t.narration for t in upi if not norm[t.id].vpa and "reversal" not in norm[t.id].flags]
check("every UPI row (except reversals) has a VPA", not no_vpa, f"{no_vpa[:2]}")


def keys_for(recurring_id):
    ids = labels[labels.recurring_id == recurring_id].index
    return {norm[i].key for i in ids if i in norm}


for rid in ["rent", "netflix", "spotify", "mobile_recharge", "phone_emi", "electricity", "gym"]:
    ks = keys_for(rid)
    check(f"recurring '{rid}' gets ONE stable key across months", len(ks) == 1, f"{ks}")

check("Netflix and Amazon shopping get different keys",
      norm[labels[labels.recurring_id == 'netflix'].index[0]].key
      != norm[labels[(labels.true_merchant == 'Amazon') & (labels.recurring_id.isna())].index[0]].key)

flag_checks = {
    "reversal flag on failed-UPI reversal": ("UPI-REV-DMART", "reversal"),
    "mandate flag on Spotify autopay": ("UPI-MANDATE-SPOTIFY", "mandate"),
    "self flag on own-account transfer": ("TRANSFER TO SELF", "self"),
    "refund flag on Swiggy refund": ("SWIGGY REFUND", "refund"),
}
for name, (needle, flag) in flag_checks.items():
    hits = [t for t in bank if needle in t.narration.upper()]
    check(name, bool(hits) and all(flag in norm[t.id].flags for t in hits), f"{len(hits)} rows")

print("\nRows per channel:", dict(Counter(n.channel for n in norm.values())))
print("Sample:")
for t in [bank[0], bank[5], bank[40], card[0]]:
    print(" ", t.narration[:70], "->", {k: v for k, v in norm[t.id].to_dict().items() if v})

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
