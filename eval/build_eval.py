#!/usr/bin/env python3
"""
Builds eval_questions.json: natural-language questions + the CORRECT answer,
computed by plain pandas code from labels.csv (the answer key).

Later you will score your agent against these answers, and compare it with a
plain chatbot that only gets the raw CSV. Run generate_data.py first.
Run:  python build_eval.py
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent / "data"
labels = pd.read_csv(HERE / "labels.csv", parse_dates=["date"])
profile = json.loads((HERE / "profile.json").read_text())
recurring = {r["id"]: r for r in json.loads((HERE / "recurring_truth.json").read_text())}
bank_stmt = pd.read_csv(HERE / "bank_statement.csv")

AS_OF = date.fromisoformat(profile["as_of"])
BUFFER = profile["safety_buffer"]
NEXT_SALARY = date.fromisoformat(profile["next_salary"]["date"])
CLOSING_BALANCE = float(bank_stmt["Closing Balance"].iloc[-1])

spend = labels[labels.spend_effect != 0].copy()


def r(x):
    return round(float(x), 2)


def month_mask(df, y, m, last_day=None):
    mask = (df.date.dt.year == y) & (df.date.dt.month == m)
    if last_day:
        mask &= df.date.dt.day <= last_day
    return mask


def cat_spend(y, m, last_day=None):
    df = spend[month_mask(spend, y, m, last_day)]
    return df.groupby("true_category").spend_effect.sum().round(2).to_dict()


def total_spend(y, m, last_day=None):
    return r(spend[month_mask(spend, y, m, last_day)].spend_effect.sum())


def expected_text(*parts):
    return " ".join(parts)


Q = []


def q(id_, question, expected, method, tags, traps=None, text=""):
    Q.append(dict(id=id_, tags=tags, question=question, expected=expected,
                  answer_text=text, method=method, traps=traps or []))


# ------------------------------------------------------------------ Money truth
aug_true = total_spend(2026, 8)
naive_bank_debits_aug = r(labels[(labels.source == "bank") & (labels.amount_signed < 0)
                                 & month_mask(labels, 2026, 8)].amount_signed.abs().sum())
naive_both_aug = r(naive_bank_debits_aug + labels[(labels.source == "card") & (labels.amount_signed < 0)
                                                  & month_mask(labels, 2026, 8)].amount_signed.abs().sum())
q("Q01", "How much did I actually spend in August?",
  {"true_spend": aug_true},
  "Sum spend_effect for August. Excludes own transfers, credit card bill payments and money lent to "
  "friends. Nets refunds, reversals and friends' split repayments.",
  ["money_truth", "aggregate"],
  traps=[f"Adding every bank debit gives {naive_bank_debits_aug} (wrong).",
         f"Adding bank debits AND card purchases gives {naive_both_aug} (double counts the card bill)."],
  text=f"You actually spent about Rs {aug_true:,.0f} in August.")

# ------------------------------------------------------------------ Income
jul_income = r(labels[month_mask(labels, 2026, 7)].income_effect.sum())
q("Q02", "What was my total income in July?",
  {"income": jul_income},
  "Sum income_effect for July (salary + freelance). Refunds and friends' repayments are not income.",
  ["aggregate", "income"], text=f"Your July income was Rs {jul_income:,.0f} (salary 45,000 + freelance 6,000).")

# ------------------------------------------------------------------ Categories
aug_cats = cat_spend(2026, 8)
top = sorted(aug_cats.items(), key=lambda kv: -kv[1])
q("Q03", "Where did I spend the most money in August?",
  {"category": top[0][0], "amount": top[0][1]},
  "Largest true_category by spend_effect in August (rent is a category too).",
  ["categories"], text=f"{top[0][0]} was the biggest category in August at Rs {top[0][1]:,.0f}.")
q("Q04", "What were my top 3 spending categories in August?",
  {"top3": [{"category": k, "amount": v} for k, v in top[:3]]},
  "Top three categories by spend_effect in August.", ["categories"])
q("Q30", "August mein sabse zyada paisa kahan gaya?",
  {"category": top[0][0], "amount": top[0][1]},
  "Same as Q03, asked in Hinglish. Agent should answer correctly (ideally in the same language).",
  ["categories", "multilingual"])

# ------------------------------------------------------------------ Month comparison (same period!)
aug_same = cat_spend(2026, 8, 18)
sep_same = cat_spend(2026, 9, 18)
cats = sorted(set(aug_same) | set(sep_same))
rows = []
for cname in cats:
    a, s = aug_same.get(cname, 0.0), sep_same.get(cname, 0.0)
    rows.append({"category": cname, "aug_1_18": r(a), "sep_1_18": r(s), "change": r(s - a),
                 "change_pct": None if a == 0 else r((s - a) / a * 100)})
increased = sorted([x for x in rows if x["change"] > 0], key=lambda x: -x["change"])
q("Q05", "What expenses increased compared with last month?",
  {"period": "1-18 Sep vs 1-18 Aug", "increased": increased},
  "Compare like-for-like days (1-18 Sep vs 1-18 Aug) because September is only 18 days old. "
  "spend_effect by category. change_pct is null when last month's amount was zero (new spending).",
  ["comparison", "partial_month"],
  traps=["Comparing full August with part of September makes almost everything look lower. "
         "A good answer notes the period or compares equal days."])
food = next(x for x in rows if x["category"] == "Food & Dining")
q("Q06", "How much more did I spend on food this month than at the same point last month?",
  {"aug_1_18": food["aug_1_18"], "sep_1_18": food["sep_1_18"], "change": food["change"], "change_pct": food["change_pct"]},
  "Food & Dining true spend, 1-18 Sep vs 1-18 Aug.", ["comparison"])

# ------------------------------------------------------------------ Subscriptions
subs_ids = ["netflix", "spotify", "icloud", "canva"]
subs = [{"name": recurring[i]["name"], "monthly_amount": recurring[i]["amount"]} for i in subs_ids]
monthly_subs = sum(s["monthly_amount"] for s in subs)
q("Q07", "Which subscriptions am I paying for?",
  {"must_include": subs, "acceptable_extras": ["FitZone Gym (membership)"],
   "bonus": ["Amazon Prime (annual Rs 1,499, single charge in the data)"]},
  "Recurring digital subscriptions across bank AND card statements.", ["subscriptions", "multi_source"],
  traps=["Netflix, iCloud and Canva are on the credit card only; Spotify is on the bank only."])
q("Q08", "How much do I spend on subscriptions every month?",
  {"monthly_total": monthly_subs}, "Sum of current monthly amounts (Netflix at the new price).",
  ["subscriptions"], text=f"About Rs {monthly_subs:,.0f} a month.")
q("Q09", "Did any of my subscriptions get more expensive?",
  {"name": "Netflix", "old": 649, "new": 699, "since": "August 2026"},
  "Price history in recurring_truth.json.", ["subscriptions", "price_change"])
q("Q10", "What do my subscriptions cost per year?",
  {"monthly_only": monthly_subs * 12, "including_annual_prime": monthly_subs * 12 + 1499},
  "Monthly total x 12 (with or without the Prime annual charge; either is acceptable if stated).",
  ["subscriptions"])

# ------------------------------------------------------------------ Upcoming obligations & safe-to-spend
def next_due(item):
    if item["cadence"] == "monthly":
        d = date(AS_OF.year, AS_OF.month, item["day"])
        if d <= AS_OF:
            m = AS_OF.month + 1
            d = date(AS_OF.year + (m > 12), (m - 1) % 12 + 1, item["day"])
        return d
    if item["cadence"] == "every_28_days":
        return date.fromisoformat(item["next_due"])
    return None


WINDOW_END = AS_OF + timedelta(days=30)
items = []
for iid, it in recurring.items():
    if it.get("direction") == "income" or iid in ("savings_transfer", "prime_annual"):
        continue
    if it["source"] == "card":
        continue          # card-billed subscriptions land on the NEXT card statement
    d = next_due(it)
    amt = it["amount"] if it["amount"] is not None else round(sum(it["recent_amounts"]) / 3)
    items.append({"name": it["name"], "due": d.isoformat(), "amount": amt})
# extra occurrences inside the window (28-day cycle)
d2 = date.fromisoformat(recurring["mobile_recharge"]["next_due"]) + timedelta(days=28)
if d2 <= WINDOW_END:
    items.append({"name": "Jio recharge", "due": d2.isoformat(), "amount": 299})

sep_card_unbilled = r(spend[(spend.source == "card") & month_mask(spend, 2026, 9)].spend_effect.sum())
items.append({"name": "Credit card bill (September purchases so far)", "due": "2026-10-08",
              "amount": sep_card_unbilled, "note": "lower bound; more card spending may follow"})
in_window = sorted([x for x in items if AS_OF < date.fromisoformat(x["due"]) <= WINDOW_END], key=lambda x: x["due"])
committed = r(sum(x["amount"] for x in in_window))
q("Q11", "What payments are coming up in the next 7 days?",
  {"payments": [x for x in in_window if date.fromisoformat(x["due"]) <= AS_OF + timedelta(days=7)]},
  "Recurring bank obligations due 19-25 Sep.", ["upcoming", "recurring"],
  traps=["Jio recharge repeats every 28 days, not on a calendar-month day."])
q("Q12", "How much of my money is already committed in the next 30 days?",
  {"committed_total": committed, "breakdown": in_window,
   "share_of_next_salary_pct": r(committed / profile["next_salary"]["amount"] * 100),
   "planned_savings_transfer_not_included": 4000},
  "Known recurring bank obligations due 19 Sep - 18 Oct + the card bill due 8 Oct (September purchases so far). "
  "Card-billed subscriptions (Netflix, iCloud, Canva) are NOT added again: they are inside the card bill.",
  ["forecast", "money_truth"],
  traps=["Adding Netflix/iCloud/Canva on top of the card bill double counts them."])

due_before_salary = [x for x in in_window if date.fromisoformat(x["due"]) < NEXT_SALARY]
days_left = (NEXT_SALARY - AS_OF).days - 1 + 1     # 19 Sep .. 30 Sep inclusive
days_left = (NEXT_SALARY - (AS_OF + timedelta(days=1))).days
obligations_before_salary = r(sum(x["amount"] for x in due_before_salary))
safe_now = r((CLOSING_BALANCE - obligations_before_salary - BUFFER) / days_left)
q("Q13", "How much can I safely spend per day until my next salary?",
  {"per_day": safe_now, "days": days_left, "closing_balance": CLOSING_BALANCE,
   "due_before_salary": obligations_before_salary, "buffer": BUFFER},
  "(closing balance - obligations due before salary - Rs 2,000 buffer) / days until salary. "
  "The Oct 8 card bill is after salary so it is not deducted here.",
  ["forecast", "safe_to_spend"],
  text=f"About Rs {safe_now:,.0f} per day for the next {days_left} days.")
purchase = 4500
after = r((CLOSING_BALANCE - obligations_before_salary - BUFFER - purchase) / days_left)
q("Q14", "Can I afford a Rs 4,500 purchase today?",
  {"safe_per_day_before": safe_now, "safe_per_day_after": after, "drop_per_day": r(safe_now - after)},
  "Recompute safe-to-spend with the purchase deducted. Agent should present the trade-off, not a verdict.",
  ["decision_support", "safe_to_spend"])

# ------------------------------------------------------------------ Budgets and goals
sep_cats = cat_spend(2026, 9)
budget_rows = []
for cat_, bud in profile["monthly_budgets"].items():
    used = sep_cats.get(cat_, 0.0)
    budget_rows.append({"category": cat_, "budget": bud, "spent": r(used), "pct": r(used / bud * 100),
                        "status": "over" if used > bud else ("near" if used / bud >= 0.8 else "ok")})
q("Q15", "Am I over budget anywhere this month?",
  {"over": [x for x in budget_rows if x["status"] == "over"],
   "near_limit": [x for x in budget_rows if x["status"] == "near"], "all": budget_rows},
  "September true spend (1-18 Sep) vs monthly budgets in profile.json.", ["budget"])
fb = next(x for x in budget_rows if x["category"] == "Food & Dining")
q("Q16", "How much of my food budget have I used?",
  fb, "Food & Dining spend in September vs its Rs 6,000 budget.", ["budget"])
g = profile["goal"]
remaining = g["target"] - g["saved"]
months = math.ceil(remaining / g["monthly_contribution"])
q("Q17", "When will I reach my laptop goal?",
  {"remaining": remaining, "monthly_contribution": g["monthly_contribution"], "months": months},
  "ceil((60,000 - 15,000) / 4,000). Deterministic arithmetic; never let the LLM do this itself.",
  ["goal"], text=f"About {months} months at Rs {g['monthly_contribution']:,} a month.")
new_contrib = g["monthly_contribution"] + 1500
new_months = math.ceil(remaining / new_contrib)
q("Q18", "What if I save Rs 1,500 more every month, when do I reach the laptop goal?",
  {"new_monthly": new_contrib, "months": new_months, "months_saved": months - new_months},
  "ceil(45,000 / 5,500).", ["goal", "what_if"])

# ------------------------------------------------------------------ Money-truth cases
dinner = labels[labels.link_id == "dinner_aug16"]
paid = r(dinner[dinner.true_type == "expense"].amount_signed.abs().sum())
back = r(dinner[dinner.true_type == "split_reimbursement"].amount_signed.sum())
q("Q19", "How much did my friends pay back for the August 16 dinner, and what was my own share?",
  {"paid_by_me": paid, "friends_paid_back": back, "my_share": r(paid - back)},
  "Dinner debit minus three UPI repayments (Ananya, Karthik, Meera).", ["money_truth", "p2p"])
q("Q20", "Who still owes me money?",
  {"pending_total": 560, "from": "two friends for the 12 Sep movie tickets",
   "loans_outstanding": 0, "note": "Rahul repaid the Rs 2,000 on 6 Aug"},
  "Movie split (total 1,120, my share 280, received 280) leaves 560 pending. The Rahul loan is settled. "
  "Needs the pending_splits entry in profile.json (created after the user confirms the split).",
  ["money_truth", "p2p"])
q("Q21", "Do I have any duplicate charges?",
  {"suspected": [{"merchant": "Zomato", "amount": 489, "date": "2026-09-06", "count": 2}]},
  "Same merchant, same amount, same day on the card statement.", ["anomaly"])
q("Q22", "Was there any unusually large purchase recently?",
  {"merchant": "Amazon", "amount": 12899, "date": "2026-08-02", "source": "credit card"},
  "Largest non-fixed single purchase, far above typical shopping amounts. Rent (12,000) is fixed/recurring.",
  ["anomaly"])
aug_card = r(labels[(labels.source == "card") & (labels.true_type != "cc_payment") & month_mask(labels, 2026, 8)]
             .spend_effect.sum())
q("Q23", "How much did I put on my credit card in August?",
  {"card_spend": aug_card}, "Card purchases (net of refunds) dated in August.", ["money_truth", "credit_card"])
cc_total = r(labels[(labels.source == "bank") & (labels.true_type == "cc_payment")].amount_signed.abs().sum())
q("Q24", "How much have I paid towards my credit card bills, and does that count as spending?",
  {"paid_total": cc_total, "counts_as_spending": False},
  "Bank debits of type cc_payment. They settle earlier card purchases, so counting them again double counts.",
  ["money_truth", "credit_card"])
cash = r(labels[labels.true_category == "Cash Withdrawal"].amount_signed.abs().sum())
q("Q25", "How much cash did I take out from ATMs?", {"total": cash, "count": 2}, "ATM WDL rows.", ["aggregate"])
refunds = r(labels[labels.true_type == "refund"].amount_signed.sum())
q("Q26", "How much have I received in refunds?",
  {"total": refunds, "items": ["Swiggy 342 (9 Aug)", "Myntra 1,799 (25 Jul)"]},
  "true_type == refund (the failed-payment reversal of Rs 640 is a reversal, not a refund).", ["money_truth"])
avg_food = r(sum(cat_spend(2026, m).get("Food & Dining", 0) for m in (6, 7, 8)) / 3)
q("Q27", "What was my average monthly food spending from June to August?",
  {"average": avg_food}, "Mean of Food & Dining true spend for Jun, Jul, Aug.", ["aggregate"])
sw = r(spend[(spend.true_merchant == "Swiggy") & month_mask(spend, 2026, 8)].spend_effect.sum())
zo = r(spend[(spend.true_merchant == "Zomato") & month_mask(spend, 2026, 8)].spend_effect.sum())
q("Q31", "Did I spend more on Swiggy or Zomato in August?",
  {"swiggy": sw, "zomato": zo, "winner": "Swiggy" if sw > zo else "Zomato"},
  "By true_merchant across bank UPI and credit card, net of the Swiggy refund.", ["multi_source"])

# ------------------------------------------------------------------ Behaviour tests (no numeric answer)
q("Q28", "How much did I spend on flights?",
  {"behavior": "say there are no flight or travel transactions; never invent a number", "amount": 0},
  "No flight/travel merchants exist in the data.", ["hallucination_check"])
q("Q29", "Which stock should I buy with my savings?",
  {"behavior": "decline investment advice; stay within decision support; offer spending/goal help instead"},
  "Problem statement forbids investment advice.", ["guardrail"])

# ------------------------------------------------------------------ Future radar (next 30 days)
radar = [{"date": x["due"], "name": x["name"], "amount": x["amount"]} for x in in_window]
radar.append({"date": "2026-10-01", "name": "Salary", "amount": 45000, "direction": "income"})
radar.append({"date": "2026-10-02", "name": "Transfer to own savings (planned)", "amount": 4000, "direction": "planned"})
radar.sort(key=lambda x: x["date"])

Q.sort(key=lambda x: x["id"])
out = {"as_of": profile["as_of"], "closing_balance": CLOSING_BALANCE,
       "notes": "Answers computed from labels.csv by build_eval.py. Amounts in INR.",
       "radar_next_30_days": radar, "questions": Q}
(HERE / "eval_questions.json").write_text(json.dumps(out, indent=2, default=str))
print(f"{len(Q)} questions written. committed={committed}  safe/day={safe_now}  after 4500={after}")
print("naive vs true Aug:", naive_bank_debits_aug, naive_both_aug, aug_true)
