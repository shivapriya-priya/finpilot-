"""
Phase 1a: read a bank or credit-card statement CSV into clean transactions.

Design goals
- Tolerant: real bank CSVs have junk lines above the header, BOM characters,
  commas inside amounts, different column names and date formats.
- Honest: if we cannot understand the file we raise a clear error instead of guessing.
- Same output for every bank: a list of Txn objects.

Sign convention (always from the USER's side):
    amount < 0  -> money went out (purchase, bill, transfer out)
    amount > 0  -> money came in  (salary, refund, repayment)
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Txn:
    id: str
    date: str                 # ISO yyyy-mm-dd
    source: str               # "bank" or "card"
    narration: str            # exactly as printed on the statement
    amount: float             # negative = out, positive = in
    balance: Optional[float] = None   # closing balance after this row (bank only)

    def to_dict(self) -> dict:
        return asdict(self)


DATE_FORMATS = (
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y",
    "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %B %Y",
)

# column-name fragments (matched against lower-cased header cells)
NARRATION_KEYS = ("narration", "description", "particulars", "details", "remarks")
DEBIT_KEYS = ("withdrawal", "debit")
CREDIT_KEYS = ("deposit", "credit")
BALANCE_KEYS = ("balance",)
AMOUNT_KEYS = ("amount",)
DRCR_KEYS = ("dr/cr", "cr/dr", "type")


def parse_date(text: str) -> str:
    text = (text or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {text!r}")


def parse_amount(text: str) -> Optional[float]:
    """'1,234.50' -> 1234.5 ; '' -> None ; 'Rs. 99' -> 99.0"""
    if text is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(text))
    if cleaned in ("", "-", ".", "-."):
        return None
    return float(cleaned)


def _find_col(header: list[str], keys: tuple[str, ...], exclude: tuple[int, ...] = ()) -> Optional[int]:
    for i, cell in enumerate(header):
        if i in exclude:
            continue
        low = cell.lower()
        if any(k in low for k in keys):
            return i
    return None


def _find_header_row(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:40]):
        lows = [c.strip().lower() for c in row]
        has_date = any("date" in c for c in lows)
        has_narr = any(any(k in c for k in NARRATION_KEYS) for c in lows)
        if has_date and has_narr:
            return i
    raise ValueError(
        "Could not find the header row. Expected columns like Date and Narration/Description."
    )


def parse_statement(text: str, source: Optional[str] = None, start: int = 1) -> list[Txn]:
    """
    Parse one statement. `source` can be "bank" or "card"; if omitted it is detected
    from the columns (Withdrawal/Deposit columns -> bank, Amount + Dr/Cr -> card).
    """
    text = text.lstrip("\ufeff")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    h = _find_header_row(rows)
    header = [c.strip() for c in rows[h]]

    i_date = _find_col(header, ("date",))
    i_narr = _find_col(header, NARRATION_KEYS)
    i_debit = _find_col(header, DEBIT_KEYS)
    i_credit = _find_col(header, CREDIT_KEYS)
    i_bal = _find_col(header, BALANCE_KEYS)
    i_amt = _find_col(header, AMOUNT_KEYS)
    i_drcr = _find_col(header, DRCR_KEYS, exclude=tuple(x for x in (i_amt,) if x is not None))

    if source is None:
        if i_debit is not None and i_credit is not None:
            source = "bank"
        elif i_amt is not None and i_drcr is not None:
            source = "card"
        else:
            raise ValueError(
                "Could not tell whether this is a bank or card statement. "
                "Bank files need Withdrawal and Deposit columns; card files need Amount and Dr/Cr."
            )

    prefix = "B" if source == "bank" else "C"
    out: list[Txn] = []
    n = start
    for row in rows[h + 1:]:
        if len(row) <= i_narr or not row[i_date].strip():
            continue
        try:
            iso = parse_date(row[i_date])
        except ValueError:
            continue    # footer lines such as "Statement summary"
        narration = re.sub(r"\s+", " ", row[i_narr]).strip()

        if source == "bank":
            debit = parse_amount(row[i_debit]) if i_debit is not None and i_debit < len(row) else None
            credit = parse_amount(row[i_credit]) if i_credit is not None and i_credit < len(row) else None
            amount = (credit or 0.0) - (debit or 0.0)
            balance = parse_amount(row[i_bal]) if i_bal is not None and i_bal < len(row) else None
        else:
            value = parse_amount(row[i_amt]) or 0.0
            drcr = row[i_drcr].strip().lower() if i_drcr is not None and i_drcr < len(row) else "dr"
            amount = -abs(value) if drcr.startswith("d") else abs(value)
            balance = None

        out.append(Txn(id=f"{prefix}{n:04d}", date=iso, source=source,
                       narration=narration, amount=round(amount, 2), balance=balance))
        n += 1
    return out


def find_balance_breaks(txns: list[Txn], tolerance: float = 0.01) -> list[dict]:
    """
    Data-quality check for bank statements: each closing balance should equal the
    previous balance plus the row's amount. A break usually means a missing row.
    """
    breaks = []
    prev = None
    for t in txns:
        if t.balance is None:
            continue
        if prev is not None and abs((prev + t.amount) - t.balance) > tolerance:
            breaks.append({"id": t.id, "date": t.date, "expected": round(prev + t.amount, 2), "found": t.balance})
        prev = t.balance
    return breaks
