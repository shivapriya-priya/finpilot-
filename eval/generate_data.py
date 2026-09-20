#!/usr/bin/env python3
"""
FinPilot synthetic dataset generator.

Creates a realistic Indian bank statement + credit card statement for
1 Jun 2026 to 18 Sep 2026, plus an ANSWER KEY (labels.csv) that says what
every row really is. The statements are what your app reads; the labels are
only for testing your Reader agent and the eval questions. Never show the
labels to the agent.

Deliberately tricky cases (each is a real gap in existing apps):
  - friend payments: lent and repaid, dinner split, pending split
  - refunds, failed-UPI reversal, suspected duplicate charge
  - credit card bill paid from the bank (must NOT be counted as spend twice)
  - own-account savings transfer (not spending)
  - rent with a different note every month, then a blank note
  - mobile recharge every 28 days (not monthly)
  - Netflix price hike, forgotten-style subscriptions, annual subscription
  - variable electricity bill, irregular freelance income
Run:  python generate_data.py     (writes ./data/*)
"""
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

START, END = date(2026, 6, 1), date(2026, 9, 18)
OPENING_BALANCE = 4000
MONTHS = [  # (first day, last day of data)
    (date(2026, 6, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 7, 31)),
    (date(2026, 8, 1), date(2026, 8, 31)),
    (date(2026, 9, 1), date(2026, 9, 18)),   # partial month (today is 19 Sep)
]

bank, card = [], []
IFSCS = ["HDFC0000123", "ICIC0000456", "UTIB0000789", "SBIN0001234", "YESB0000262", "KKBK0000958"]


def ref(n=12):
    return "".join(random.choice("0123456789") for _ in range(n))


def upi(payee, vpa, note=""):
    s = f"UPI-{payee}-{vpa}-{random.choice(IFSCS)}-{ref()}"
    return f"{s}-{note}" if note else s


def add(store, d, narration, amount, *, merchant, category, ttype, source,
        recurring="", link="", flag=""):
    """amount is signed from the user's side: negative = money out."""
    if d < START or d > END:
        return
    store.append(dict(
        date=d, narration=narration, amount=amount, merchant=merchant,
        category=category, ttype=ttype, source=source,
        recurring=recurring, link=link, flag=flag))


def b(d, narr, amount, **kw):
    add(bank, d, narr, amount, source="bank", **kw)


def c(d, narr, amount, **kw):
    add(card, d, narr, amount, source="card", **kw)


def rnd_days(lo, hi, n):
    span = (hi - lo).days
    return sorted(lo + timedelta(days=random.randint(0, span)) for _ in range(n))


def rnd_amt(lo, hi):
    return random.randint(lo, hi)


def scatter(counts, amt, merchants, store_fn, category, skip_days=()):
    """Random spends per month. merchants = [(NARR_NAME, vpa_or_text, clean_name)]"""
    for (lo, hi), n in zip(MONTHS, counts):
        for d in rnd_days(lo, hi, n):
            if d in skip_days:
                continue
            name, vpa, clean = random.choice(merchants)
            store_fn(d, vpa(name) if callable(vpa) else upi(name, vpa),
                     -rnd_amt(*amt), merchant=clean, category=category, ttype="expense")


# ---------------------------------------------------------------- INCOME
for m, (lo, _) in enumerate(MONTHS):
    b(lo, f"NEFT-CR-N{ref(9)}-ACME TECHNOLOGIES PVT LTD-SALARY {lo.strftime('%b').upper()} 2026",
      45000, merchant="Acme Technologies", category="Salary", ttype="income", recurring="salary")
b(date(2026, 7, 28), f"NEFT-CR-N{ref(9)}-PIXELCRAFT STUDIO-INV 214", 6000,
  merchant="Pixelcraft Studio", category="Freelance", ttype="income")
b(date(2026, 6, 20), upi("SUNITA DEVI", "sunita.devi@oksbi", "pocket money"), 2000,
  merchant="Sunita Devi", category="Family Support", ttype="income")
b(date(2026, 6, 30), "INT.PD:01-04-2026 TO 30-06-2026", 96,
  merchant="Bank interest", category="Interest", ttype="income")

# ---------------------------------------------------------------- FIXED / RECURRING (BANK)
rent_notes = ["RENT JUN", "rent", "house", ""]           # note changes, then disappears
for (lo, _), note in zip(MONTHS, rent_notes):
    b(date(lo.year, lo.month, 5), upi("RAVINDER KUMAR", "ravinderk@ybl", note), -12000,
      merchant="Landlord (Ravinder Kumar)", category="Rent", ttype="expense", recurring="rent")
for lo, _ in MONTHS:
    b(date(lo.year, lo.month, 10), f"ACH D- BAJAJ FINANCE LTD-{ref(8)}", -5500,
      merchant="Bajaj Finance EMI", category="EMI & Loans", ttype="expense", recurring="phone_emi")
    b(date(lo.year, lo.month, 3), f"ACH D- FITZONE GYMS PVT LTD-{ref(8)}", -1000,
      merchant="FitZone Gym", category="Health & Fitness", ttype="expense", recurring="gym")
    b(date(lo.year, lo.month, 12), f"BBPS-BROADBAND SERVICES-{ref(10)}-BILL PAY", -999,
      merchant="Broadband", category="Bills & Utilities", ttype="expense", recurring="broadband")
    b(date(lo.year, lo.month, 7), upi("MANDATE-SPOTIFY INDIA", "spotify@axisbank", ""), -119,
      merchant="Spotify", category="Subscriptions", ttype="expense", recurring="spotify")
    b(date(lo.year, lo.month, 2), f"IMPS-{ref(12)}-TRANSFER TO SELF SAVINGS A/C", -4000,
      merchant="Own savings account", category="Savings Transfer", ttype="own_transfer",
      recurring="savings_transfer")
for (lo, _), amt in zip(MONTHS, [1420, 1780, 1910, 1650]):
    b(date(lo.year, lo.month, 16), f"BBPS-ELECTRICITY BOARD-{ref(10)}-BILL PAY", -amt,
      merchant="Electricity", category="Bills & Utilities", ttype="expense", recurring="electricity")
# Mobile recharge every 28 days (NOT monthly)
d = date(2026, 6, 2)
while d <= END:
    b(d, upi("JIO PREPAID", "jioprepaid@icici", "RECHARGE"), -299,
      merchant="Jio recharge", category="Bills & Utilities", ttype="expense", recurring="mobile_recharge")
    d += timedelta(days=28)
for dd in [date(2026, 6, 8), date(2026, 7, 6), date(2026, 8, 3), date(2026, 9, 5)]:
    b(dd, upi("METRO RAIL TRAVEL CARD", "metrocard@ybl", "RECHARGE"), -500,
      merchant="Metro card", category="Transport", ttype="expense")

# ---------------------------------------------------------------- VARIABLE SPEND (BANK / UPI)
scatter((5, 6, 6, 8), (180, 520),
        [("SWIGGY", "swiggy@icici", "Swiggy"), ("ZOMATO", "zomato@hdfcbank", "Zomato")],
        b, "Food & Dining")
scatter((2, 2, 2, 3), (250, 850),
        [("CHAI POINT", "chaipoint@ybl", "Chai Point"), ("DOMINOS PIZZA", "dominos@paytm", "Dominos"),
         ("BEHROUZ BIRYANI", "behrouz@icici", "Behrouz Biryani"), ("STARBUCKS", "starbucks@hdfcbank", "Starbucks")],
        b, "Food & Dining")
scatter((8, 8, 8, 10), (20, 140),
        [("SRI LAXMI TEA STALL", "paytmqr281@paytm", "Tea stall"),
         ("ANNAPURNA JUICE CENTRE", "annapurna.j@ybl", "Juice centre"),
         ("MAA TIFFIN CENTER", "maatiffin@okaxis", "Tiffin centre")],
        b, "Food & Dining")
scatter((5, 5, 5, 5), (250, 1100),
        [("BLINKIT", "blinkit@icici", "Blinkit"), ("ZEPTO", "zepto@hdfcbank", "Zepto"),
         ("BIGBASKET", "bigbasket@icici", "BigBasket"), ("DMART", "dmart@sbi", "DMart"),
         ("SRI LAXMI KIRANA", "laxmikirana@ybl", "Local kirana")],
        b, "Groceries")
scatter((6, 7, 6, 3), (70, 320),
        [("UBER INDIA", "uber@axisbank", "Uber"), ("RAPIDO", "rapido@ybl", "Rapido"), ("OLA CABS", "olacabs@icici", "Ola")],
        b, "Transport")
scatter((1, 1, 2, 1), (120, 650),
        [("APOLLO PHARMACY", "apollopharmacy@icici", "Apollo Pharmacy"), ("MEDPLUS", "medplus@ybl", "MedPlus")],
        b, "Health & Fitness")
scatter((1, 1, 1, 1), (399, 1500),
        [("FLIPKART INTERNET", "flipkart@axisbank", "Flipkart")], b, "Shopping")

# ---------------------------------------------------------------- FRIENDS, FAMILY, CASH, REFUNDS (BANK)
# 1) Lent to Rahul, repaid (net zero)
b(date(2026, 7, 20), upi("RAHUL SHARMA", "rahul.sharma21@okhdfcbank", "hostel adv"), -2000,
  merchant="Rahul Sharma", category="Friends (loan)", ttype="p2p_lent", link="loan_rahul")
b(date(2026, 8, 6), upi("RAHUL SHARMA", "rahul.sharma21@okhdfcbank", "returned"), 2000,
  merchant="Rahul Sharma", category="Friends (loan)", ttype="p2p_repaid", link="loan_rahul")
# 2) Dinner Rs 3,200 paid by me, split 4 ways (my share Rs 800)
b(date(2026, 8, 16), upi("BARBEQUE NATION", "bbqn.pay@icici", "DINNER"), -3200,
  merchant="Barbeque Nation", category="Food & Dining", ttype="expense", link="dinner_aug16")
b(date(2026, 8, 17), upi("ANANYA RAO", "ananya.rao@okicici", "dinner"), 800,
  merchant="Ananya Rao", category="Food & Dining", ttype="split_reimbursement", link="dinner_aug16")
b(date(2026, 8, 17), upi("KARTHIK N", "karthik.n@ybl", "bbq"), 800,
  merchant="Karthik N", category="Food & Dining", ttype="split_reimbursement", link="dinner_aug16")
b(date(2026, 8, 19), upi("MEERA IYER", "meera.iyer@okaxis", "dinner share"), 800,
  merchant="Meera Iyer", category="Food & Dining", ttype="split_reimbursement", link="dinner_aug16")
# 3) Failed UPI payment debited then reversed (net zero)
b(date(2026, 8, 19), upi("DMART", "dmart@sbi", "payment"), -640,
  merchant="DMart", category="Groceries", ttype="reversal_debit_placeholder", link="dmart_fail")
b(date(2026, 8, 19), f"UPI-REV-DMART-{ref()}-FAILED TXN REFUND", 640,
  merchant="DMart", category="Groceries", ttype="reversal", link="dmart_fail")
# 4) Swiggy refund
b(date(2026, 8, 9), upi("SWIGGY REFUND", "swiggy@icici", "refund order"), 342,
  merchant="Swiggy", category="Food & Dining", ttype="refund")
# 5) Birthday gift to a friend (looks like a P2P transfer but IS spending)
b(date(2026, 8, 24), upi("NEHA VERMA", "neha.v@okaxis", "bday"), -1500,
  merchant="Neha Verma", category="Family & Gifts", ttype="expense")
# 6) Money sent to mother (family support, an expense for budgeting)
b(date(2026, 9, 2), upi("SUNITA DEVI", "sunita.devi@oksbi", "for mummy"), -2500,
  merchant="Sunita Devi", category="Family & Gifts", ttype="expense")
# 7) ATM cash
b(date(2026, 7, 15), f"ATM WDL-{ref(6)}-ATM HDFC0001", -2000,
  merchant="ATM cash", category="Cash Withdrawal", ttype="expense")
b(date(2026, 9, 10), f"ATM WDL-{ref(6)}-ATM ICIC0002", -2000,
  merchant="ATM cash", category="Cash Withdrawal", ttype="expense")

# ---------------------------------------------------------------- CREDIT CARD
NARR = {
    "netflix": "NETFLIX.COM MUMBAI IN", "icloud": "APPLE.COM/BILL ITUNES.COM IN",
    "canva": "CANVA PTY LTD SYDNEY", "prime": "AMAZON PRIME MEMBERSHIP",
    "amazon": "AMAZON PAY IN E-COMMERCE", "myntra": "MYNTRA DESIGNS PVT LTD BENGALURU",
    "zomato": "ZOMATO LTD GURGAON", "uber": "UBER INDIA SYSTEMS PVT LTD", "bms": "BOOKMYSHOW MUMBAI",
}
for (lo, _), nf in zip(MONTHS, [649, 649, 699, 699]):          # Netflix price hike in Aug
    c(date(lo.year, lo.month, 5), NARR["netflix"], -nf, merchant="Netflix", category="Subscriptions",
      ttype="expense", recurring="netflix")
    c(date(lo.year, lo.month, 9), NARR["icloud"], -75, merchant="iCloud", category="Subscriptions",
      ttype="expense", recurring="icloud")
    c(date(lo.year, lo.month, 14), NARR["canva"], -499, merchant="Canva", category="Subscriptions",
      ttype="expense", recurring="canva")
c(date(2026, 6, 21), NARR["prime"], -1499, merchant="Amazon Prime", category="Subscriptions",
  ttype="expense", recurring="prime_annual")

c(date(2026, 6, 1), NARR["amazon"], -1299, merchant="Amazon", category="Shopping", ttype="expense")
c(date(2026, 7, 1), NARR["amazon"], -899, merchant="Amazon", category="Shopping", ttype="expense")
c(date(2026, 8, 2), NARR["amazon"], -12899, merchant="Amazon", category="Shopping", ttype="expense",
  flag="unusually_large")
c(date(2026, 9, 2), NARR["amazon"], -3499, merchant="Amazon", category="Shopping", ttype="expense")
c(date(2026, 8, 21), NARR["amazon"], -640, merchant="Amazon", category="Shopping", ttype="expense")
c(date(2026, 7, 12), NARR["myntra"], -4298, merchant="Myntra", category="Shopping", ttype="expense",
  link="myntra_jul")
c(date(2026, 7, 25), "MYNTRA REFUND", 1799, merchant="Myntra", category="Shopping",
  ttype="refund", link="myntra_jul")
c(date(2026, 9, 15), NARR["myntra"], -2299, merchant="Myntra", category="Shopping", ttype="expense")
c(date(2026, 6, 14), NARR["bms"], -600, merchant="BookMyShow", category="Entertainment", ttype="expense")
c(date(2026, 9, 12), NARR["bms"], -1120, merchant="BookMyShow", category="Entertainment",
  ttype="expense", link="movie_sep12")
# friend repays part of the movie (bank credit) -> 2 friends still pending
b(date(2026, 9, 13), upi("ANANYA RAO", "ananya.rao@okicici", "movie"), 280,
  merchant="Ananya Rao", category="Entertainment", ttype="split_reimbursement", link="movie_sep12")
# Suspected duplicate Zomato charge
c(date(2026, 9, 6), NARR["zomato"], -489, merchant="Zomato", category="Food & Dining", ttype="expense")
c(date(2026, 9, 6), NARR["zomato"], -489, merchant="Zomato", category="Food & Dining", ttype="expense",
  flag="possible_duplicate")
for (lo, hi), n in zip(MONTHS, (3, 3, 3, 3)):
    for d in rnd_days(lo, hi, n):
        if d == date(2026, 9, 6):
            continue
        c(d, NARR["zomato"], -rnd_amt(380, 750), merchant="Zomato", category="Food & Dining", ttype="expense")
for (lo, hi), n in zip(MONTHS, (3, 2, 3, 2)):
    for d in rnd_days(lo, hi, n):
        c(d, NARR["uber"], -rnd_amt(180, 450), merchant="Uber", category="Transport", ttype="expense")

# ---------------------------------------------------------------- CARD BILL PAYMENTS (bank debit + card credit)
def card_month_total(year, month):
    tot = 0
    for t in card:
        if t["date"].year == year and t["date"].month == month and t["ttype"] != "cc_payment":
            tot += -t["amount"]
    return tot

for pay_date, (y, m) in [(date(2026, 7, 8), (2026, 6)), (date(2026, 8, 8), (2026, 7)), (date(2026, 9, 8), (2026, 8))]:
    amt = card_month_total(y, m)
    b(pay_date, f"BILLDESK-HDFC CREDIT CARD-{ref(10)}-CC PAYMENT", -amt,
      merchant="Credit card bill", category="Credit Card Payment", ttype="cc_payment", link=f"cc_{y}_{m:02d}")
    c(pay_date, "PAYMENT RECEIVED - THANK YOU", amt,
      merchant="Credit card bill", category="Credit Card Payment", ttype="cc_payment", link=f"cc_{y}_{m:02d}")

# ---------------------------------------------------------------- FINALISE
for t in bank:
    if t["ttype"] == "reversal_debit_placeholder":
        t["ttype"] = "expense"          # the debit is a normal expense until the reversal cancels it
ORDER = {"income": 0}
bank.sort(key=lambda t: t["date"])
card.sort(key=lambda t: t["date"])
for i, t in enumerate(bank, 1):
    t["id"] = f"B{i:04d}"
for i, t in enumerate(card, 1):
    t["id"] = f"C{i:04d}"

bal = OPENING_BALANCE
for t in bank:
    bal += t["amount"]
    t["balance"] = bal
min_bal = min(t["balance"] for t in bank)
assert min_bal > 0, f"balance went negative: {min_bal}"

SPEND_TYPES = {"expense", "refund", "reversal", "split_reimbursement"}
for t in bank + card:
    t["spend_effect"] = -t["amount"] if t["ttype"] in SPEND_TYPES else 0
    t["income_effect"] = t["amount"] if t["ttype"] == "income" else 0

def fdate(d):
    return d.strftime("%d/%m/%Y")

with open(OUT / "bank_statement.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
    for t in bank:
        w.writerow([fdate(t["date"]), t["narration"],
                    f"{-t['amount']:.2f}" if t["amount"] < 0 else "",
                    f"{t['amount']:.2f}" if t["amount"] > 0 else "",
                    f"{t['balance']:.2f}"])

with open(OUT / "credit_card_statement.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Amount (INR)", "Dr/Cr"])
    for t in card:
        w.writerow([fdate(t["date"]), t["narration"], f"{abs(t['amount']):.2f}",
                    "Dr" if t["amount"] < 0 else "Cr"])

with open(OUT / "labels.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["txn_id", "source", "date", "narration", "amount_signed", "true_merchant",
                "true_category", "true_type", "spend_effect", "income_effect",
                "recurring_id", "link_id", "flag"])
    for t in bank + card:
        w.writerow([t["id"], t["source"], t["date"].isoformat(), t["narration"], t["amount"],
                    t["merchant"], t["category"], t["ttype"], t["spend_effect"], t["income_effect"],
                    t["recurring"], t["link"], t["flag"]])

# ---------------------------------------------------------------- ANSWER KEY FOR RECURRING DETECTION
recurring_truth = [
    {"id": "rent", "name": "Rent", "category": "Rent", "source": "bank", "cadence": "monthly", "day": 5, "amount": 12000, "amount_type": "fixed", "note": "UPI note changes then disappears"},
    {"id": "phone_emi", "name": "Bajaj Finance EMI", "category": "EMI & Loans", "source": "bank", "cadence": "monthly", "day": 10, "amount": 5500, "amount_type": "fixed"},
    {"id": "gym", "name": "FitZone Gym", "category": "Health & Fitness", "source": "bank", "cadence": "monthly", "day": 3, "amount": 1000, "amount_type": "fixed"},
    {"id": "broadband", "name": "Broadband", "category": "Bills & Utilities", "source": "bank", "cadence": "monthly", "day": 12, "amount": 999, "amount_type": "fixed"},
    {"id": "electricity", "name": "Electricity", "category": "Bills & Utilities", "source": "bank", "cadence": "monthly", "day": 16, "amount": None, "amount_type": "variable", "recent_amounts": [1780, 1910, 1650]},
    {"id": "mobile_recharge", "name": "Jio recharge", "category": "Bills & Utilities", "source": "bank", "cadence": "every_28_days", "last_date": "2026-08-25", "next_due": "2026-09-22", "amount": 299, "amount_type": "fixed", "note": "NOT monthly"},
    {"id": "spotify", "name": "Spotify", "category": "Subscriptions", "source": "bank", "cadence": "monthly", "day": 7, "amount": 119, "amount_type": "fixed"},
    {"id": "netflix", "name": "Netflix", "category": "Subscriptions", "source": "card", "cadence": "monthly", "day": 5, "amount": 699, "amount_type": "fixed", "price_history": [649, 649, 699, 699], "note": "price rose from 649 to 699 in Aug"},
    {"id": "icloud", "name": "iCloud", "category": "Subscriptions", "source": "card", "cadence": "monthly", "day": 9, "amount": 75, "amount_type": "fixed"},
    {"id": "canva", "name": "Canva", "category": "Subscriptions", "source": "card", "cadence": "monthly", "day": 14, "amount": 499, "amount_type": "fixed"},
    {"id": "prime_annual", "name": "Amazon Prime", "category": "Subscriptions", "source": "card", "cadence": "annual", "amount": 1499, "amount_type": "fixed", "note": "only ONE charge in the data; cannot be detected from history"},
    {"id": "savings_transfer", "name": "Transfer to own savings", "category": "Savings Transfer", "source": "bank", "cadence": "monthly", "day": 2, "amount": 4000, "amount_type": "fixed", "note": "own transfer, not spending"},
    {"id": "salary", "name": "Salary", "category": "Salary", "source": "bank", "cadence": "monthly", "day": 1, "amount": 45000, "amount_type": "fixed", "direction": "income"},
]
(OUT / "recurring_truth.json").write_text(json.dumps(recurring_truth, indent=2))

profile = {
    "as_of": "2026-09-18",
    "currency": "INR",
    "safety_buffer": 2000,
    "next_salary": {"date": "2026-10-01", "amount": 45000},
    "monthly_budgets": {"Food & Dining": 6000, "Groceries": 3000, "Transport": 2500,
                        "Shopping": 4000, "Entertainment": 1500},
    "goal": {"name": "Laptop", "target": 60000, "saved": 15000, "monthly_contribution": 4000},
    "pending_splits": [
        {"link_id": "movie_sep12", "description": "Movie tickets for 4, paid by me", "total": 1120,
         "my_share": 280, "received": 280, "pending_from": ["friend 2", "friend 3"], "pending_amount": 560}
    ],
}
(OUT / "profile.json").write_text(json.dumps(profile, indent=2))

print(f"bank rows: {len(bank)}  card rows: {len(card)}  lowest bank balance: {min_bal:,.0f}  closing: {bal:,.0f}")
