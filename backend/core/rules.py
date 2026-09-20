"""
Phase 1c: rules that decide what each transaction IS.

Order of thinking (most certain first):
  1. Structure   - interest, ATM, own-account transfer, card-bill payment, salary
  2. Dictionary  - known merchants and keywords (Swiggy, "KIRANA", "GYM", ...)
  3. People      - unknown UPI payee that looks like a person -> "p2p_pending"
                   (Money Truth: is it a loan, a split, rent, a gift? ask the user)
  4. Everything else -> "unknown"  (goes to the Reader agent, the LLM, in Phase 2)

A row is "resolved" only when we are confident. The rest is handed on, never guessed.
The dictionary is deliberately small: rules handle the common cases cheaply and
predictably, the LLM handles the long tail.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .normalize import Norm, normalize
from .parse import Txn

RESOLVE_THRESHOLD = 0.8

# Category-style labels are too vague to show a user ("Utility bill" x2). For these we show the
# cleaned name from the statement instead ("Broadband Services", "Electricity Board").
GENERIC_NAMES = {
    "Utility bill", "Mobile recharge", "Loan EMI", "Gym", "Pharmacy / clinic", "Cafe / restaurant",
    "Tea / snacks", "Local grocery", "Fast food", "Supermarket", "Online shopping", "Travel booking",
    "Train / bus", "Fuel / toll", "Streaming",
}

# (regex on the merchant key, display name, category). First match wins, so specific goes first.
MERCHANTS: list[tuple[str, str, str]] = [
    # subscriptions
    (r"\bAMAZON PRIME\b", "Amazon Prime", "Subscriptions"),
    (r"\bNETFLIX\b", "Netflix", "Subscriptions"),
    (r"\bSPOTIFY\b", "Spotify", "Subscriptions"),
    (r"\bAPPLE\b", "iCloud / Apple", "Subscriptions"),
    (r"\bCANVA\b", "Canva", "Subscriptions"),
    (r"\b(HOTSTAR|DISNEY)\b", "Hotstar", "Subscriptions"),
    (r"\bYOUTUBE\b", "YouTube", "Subscriptions"),
    (r"\b(SONYLIV|ZEE5|JIOCINEMA)\b", "Streaming", "Subscriptions"),
    # groceries (before Swiggy so Instamart lands here)
    (r"\bINSTAMART\b", "Swiggy Instamart", "Groceries"),
    (r"\bBLINKIT\b", "Blinkit", "Groceries"),
    (r"\bZEPTO\b", "Zepto", "Groceries"),
    (r"\bBIGBASKET\b", "BigBasket", "Groceries"),
    (r"\bDMART\b", "DMart", "Groceries"),
    (r"\b(KIRANA|SUPERMARKET|PROVISION|GROCERY)\b", "Local grocery", "Groceries"),
    (r"\b(RELIANCE FRESH|RELIANCE SMART|MORE RETAIL|NATURES BASKET)\b", "Supermarket", "Groceries"),
    # food and dining
    (r"\bSWIGGY\b", "Swiggy", "Food & Dining"),
    (r"\bZOMATO\b", "Zomato", "Food & Dining"),
    (r"\bDOMINOS\b", "Dominos", "Food & Dining"),
    (r"\bSTARBUCKS\b", "Starbucks", "Food & Dining"),
    (r"\bCHAI POINT\b", "Chai Point", "Food & Dining"),
    (r"\bBEHROUZ\b", "Behrouz Biryani", "Food & Dining"),
    (r"\b(MCDONALD|MCDONALDS|KFC|SUBWAY|BURGER KING|PIZZA HUT|HALDIRAM)\b", "Fast food", "Food & Dining"),
    (r"\b(CAFE|RESTAURANT|BAKERY|DHABA|BIRYANI)\b", "Cafe / restaurant", "Food & Dining"),
    (r"\b(TEA|CHAI|JUICE|TIFFIN)\b", "Tea / snacks", "Food & Dining"),
    # transport
    (r"\bUBER\b", "Uber", "Transport"),
    (r"\bOLA\b", "Ola", "Transport"),
    (r"\bRAPIDO\b", "Rapido", "Transport"),
    (r"\bMETRO\b", "Metro", "Transport"),
    (r"\b(FASTAG|HPCL|BPCL|IOCL|PETROL|FUEL|INDIANOIL)\b", "Fuel / toll", "Transport"),
    (r"\b(IRCTC|REDBUS)\b", "Train / bus", "Transport"),
    (r"\b(MAKEMYTRIP|GOIBIBO|CLEARTRIP|INDIGO|AIR INDIA|AKASA)\b", "Travel booking", "Travel"),
    # shopping
    (r"\bAMAZON\b", "Amazon", "Shopping"),
    (r"\bMYNTRA\b", "Myntra", "Shopping"),
    (r"\bFLIPKART\b", "Flipkart", "Shopping"),
    (r"\b(AJIO|NYKAA|MEESHO|DECATHLON|LENSKART|IKEA|CROMA)\b", "Online shopping", "Shopping"),
    # entertainment
    (r"\b(BOOKMYSHOW|PVR|INOX)\b", "BookMyShow", "Entertainment"),
    # health and fitness
    (r"\b(APOLLO|MEDPLUS|PHARMEASY|NETMEDS|PRACTO|PHARMACY|CLINIC|HOSPITAL)\b", "Pharmacy / clinic", "Health & Fitness"),
    (r"\b(GYM|GYMS|FITNESS|CULT)\b", "Gym", "Health & Fitness"),
    # bills and utilities
    (r"\b(JIO|AIRTEL|BSNL|VODAFONE)\b", "Mobile recharge", "Bills & Utilities"),
    (r"\b(ELECTRICITY|BROADBAND|FIBERNET|WATER|PIPED GAS|DTH)\b", "Utility bill", "Bills & Utilities"),
    # loans
    (r"\b(FINANCE|LOAN|EMI)\b", "Loan EMI", "EMI & Loans"),
]
_COMPILED = [(re.compile(p), name, cat) for p, name, cat in MERCHANTS]

# words that show a UPI payee is a business, not a person
BUSINESS_WORDS = {
    "STORE", "STORES", "STALL", "CENTER", "CENTRE", "TIFFIN", "JUICE", "KIRANA", "PAY", "CAFE",
    "RESTAURANT", "BAKERY", "MEDICAL", "PHARMACY", "TRADERS", "ENTERPRISES", "SERVICES", "SHOP",
    "MART", "FOODS", "HOTEL", "CABS", "TRAVEL", "CARD", "RECHARGE", "SALON", "CLINIC",
}


@dataclass
class RuleResult:
    merchant: str
    category: str
    type: str            # expense income refund reversal own_transfer cc_payment p2p_pending unknown
    confidence: float
    rule: str            # which rule fired (for the evidence trail)
    resolved: bool
    hints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def person_hint(n: Norm) -> dict:
    """Does this UPI payee look like a private person? A hint for later steps, not a verdict."""
    tokens = [t for t in re.split(r"[^A-Za-z]+", n.counterparty.upper()) if t]
    handle = n.vpa.split("@")[-1].lower() if "@" in n.vpa else ""
    local = re.sub(r"[^a-z0-9._]", "", n.vpa.split("@")[0].lower()) if n.vpa else ""
    reasons = []
    ok = 1 <= len(tokens) <= 3 and not (set(tokens) & BUSINESS_WORDS)
    if not ok:
        return {"person_like": False, "handle": handle}
    first = tokens[0].lower()
    if handle.startswith("ok"):
        reasons.append("personal-style UPI handle")
    if len(first) >= 4 and first in local and (re.search(r"[._\d]", local) or local.startswith(first) and len(local) <= len(first) + 2):
        reasons.append("VPA looks like the person's name")
    return {"person_like": bool(reasons), "handle": handle, "reasons": reasons}


def _dictionary(key: str):
    for rx, name, cat in _COMPILED:
        if rx.search(key):
            return name, cat
    return None


def classify(t: Txn, n: Norm | None = None) -> RuleResult:
    n = n or normalize(t.narration, t.source)
    u = t.narration.upper()
    amt = t.amount
    flags = set(n.flags)

    # ---- 1. structure ----------------------------------------------------------
    if t.source == "card" and "PAYMENT RECEIVED" in u and amt > 0:
        return RuleResult("Credit card bill", "Credit Card Payment", "cc_payment", 0.98, "card_payment_received", True)
    if n.channel == "INT":
        return RuleResult("Bank interest", "Interest", "income", 0.98, "interest_credit", True)
    if n.channel == "ATM":
        return RuleResult("ATM cash", "Cash Withdrawal", "expense", 0.98, "atm_withdrawal", True)
    if "self" in flags and n.channel in ("IMPS", "NEFT", "RTGS", "UPI") and amt < 0:
        return RuleResult("Own savings account", "Savings Transfer", "own_transfer", 0.95, "self_transfer_keyword", True)
    if n.channel == "BILLDESK" and re.search(r"CREDIT CARD|CC PAYMENT", u) and amt < 0:
        return RuleResult("Credit card bill", "Credit Card Payment", "cc_payment", 0.95, "billdesk_card_bill", True)
    if n.channel in ("NEFT", "RTGS", "IMPS") and amt > 0:
        if "SALARY" in u:
            return RuleResult(n.counterparty.title() or "Employer", "Salary", "income", 0.95, "salary_keyword", True)
        if re.search(r"\bINV\b|INVOICE|FREELANCE", u):
            return RuleResult(n.counterparty.title() or "Client", "Freelance", "income", 0.9, "invoice_keyword", True)
        return RuleResult(n.counterparty.title(), "Other income", "unknown", 0.4, "unknown_credit", False,
                          {"note": "credit with no clear label"})

    # ---- 2. dictionary ---------------------------------------------------------
    hit = _dictionary(n.key)
    if hit:
        name, cat = hit
        if name in GENERIC_NAMES and n.key:
            name = n.key.title()
        if amt < 0:
            typ, conf, rule = "expense", 0.95, "merchant_dictionary"
        elif "reversal" in flags:
            typ, conf, rule = "reversal", 0.95, "reversal_keyword"
        elif "refund" in flags:
            typ, conf, rule = "refund", 0.95, "refund_keyword"
        else:
            typ, conf, rule = "refund", 0.6, "credit_from_known_merchant"   # probable refund, not sure
        return RuleResult(name, cat, typ, conf, rule, conf >= RESOLVE_THRESHOLD)

    if n.channel == "BBPS":
        return RuleResult(n.counterparty.title(), "Bills & Utilities", "expense", 0.85, "bbps_bill", True)

    # ---- 3. people --------------------------------------------------------------
    if n.channel == "UPI":
        hint = person_hint(n)
        if hint["person_like"]:
            return RuleResult(n.counterparty.title(), "Friends & Family", "p2p_pending", 0.5, "person_like_upi", False, hint)
        return RuleResult(n.counterparty.title(), "Uncategorised", "unknown", 0.0, "unknown_upi_payee", False, hint)

    return RuleResult(n.counterparty.title(), "Uncategorised", "unknown", 0.0, "no_rule", False)


def classify_all(txns: list[Txn]) -> dict[str, RuleResult]:
    return {t.id: classify(t) for t in txns}


def unresolved_groups(txns: list[Txn], results: dict[str, RuleResult]) -> list[dict]:
    """Group unresolved rows by merchant key so the LLM / user is asked once per payee, not per row."""
    groups: dict[str, dict] = {}
    for t in txns:
        r = results[t.id]
        if r.resolved:
            continue
        n = normalize(t.narration, t.source)
        g = groups.setdefault(n.key or n.counterparty, {
            "key": n.key or n.counterparty, "counterparty": n.counterparty, "channel": n.channel,
            "status": r.type, "hints": r.hints, "txn_ids": [], "total_out": 0.0, "total_in": 0.0})
        g["txn_ids"].append(t.id)
        if t.amount < 0:
            g["total_out"] = round(g["total_out"] - t.amount, 2)
        else:
            g["total_in"] = round(g["total_in"] + t.amount, 2)
    return sorted(groups.values(), key=lambda g: -(g["total_out"] + g["total_in"]))
