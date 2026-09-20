"""
Phase 1b: turn a raw narration into structured facts.

    "UPI-MANDATE-SPOTIFY INDIA-spotify@axisbank-UTIB0000789-612345678901"
        -> channel=UPI, counterparty="SPOTIFY INDIA", key="SPOTIFY", flags=["mandate"]

This file only extracts what is literally written. It does NOT decide whether a payee is
a person or a shop, or which category it belongs to. That is the job of rules.py and the
Reader agent (Phase 2). Keeping those apart makes both easier to test.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
LONG_DIGITS_RE = re.compile(r"^\d{9,14}$")
REF_RE = re.compile(r"^[A-Z]?\d{6,}$")

# Words that add no information when grouping the same merchant across months.
NOISE_WORDS = {
    "PVT", "LTD", "LIMITED", "PRIVATE", "INDIA", "IN", "IND", "SYSTEMS", "PTY", "COM",
    "MUMBAI", "BENGALURU", "BANGALORE", "GURGAON", "SYDNEY", "DELHI", "CHENNAI", "PUNE",
    "BILL", "ITUNES", "REFUND", "REVERSAL", "REV", "PAYMENT", "ONLINE", "THE", "OF",
}


@dataclass
class Norm:
    channel: str                 # UPI NEFT IMPS RTGS ACH BBPS BILLDESK ATM INT CARD OTHER
    counterparty: str            # payee or merchant text as written (trimmed)
    key: str                     # stable text used to group the same merchant across months
    note: str = ""               # free-text note (UPI note, NEFT remarks, ...)
    vpa: str = ""
    ifsc: str = ""
    ref: str = ""
    flags: list[str] = field(default_factory=list)   # lexical facts: mandate, refund, reversal, self

    def to_dict(self) -> dict:
        return asdict(self)


def make_key(text: str) -> str:
    """Upper-case letters only, noise words and one-letter tokens removed."""
    tokens = re.split(r"[^A-Za-z]+", text.upper())
    kept = [t for t in tokens if len(t) > 1 and t not in NOISE_WORDS]
    return " ".join(kept)


def _flags(text_upper: str) -> list[str]:
    flags = []
    if "MANDATE" in text_upper or "AUTOPAY" in text_upper:
        flags.append("mandate")
    if re.search(r"\bREFUND", text_upper):
        flags.append("refund")
    if re.search(r"\b(REV|REVERSAL|REVERSED)\b", text_upper):
        flags.append("reversal")
    if re.search(r"\bSELF\b", text_upper):
        flags.append("self")
    return flags


def _parse_upi(n: str) -> Norm:
    parts = n.split("-")
    vpa_i = next((i for i in range(1, len(parts)) if "@" in parts[i]), None)
    ifsc = ref = note = vpa = ""

    if vpa_i is not None:
        payee = "-".join(parts[1:vpa_i])
        vpa = parts[vpa_i]
        rest = parts[vpa_i + 1:]
        if rest and IFSC_RE.match(rest[0].upper()):
            ifsc, rest = rest[0], rest[1:]
        if rest and LONG_DIGITS_RE.match(rest[0]):
            ref, rest = rest[0], rest[1:]
        note = "-".join(rest)
    else:
        # e.g. UPI-REV-DMART-123456789012-FAILED TXN REFUND  (no VPA on reversals)
        ref_i = next((i for i in range(1, len(parts)) if LONG_DIGITS_RE.match(parts[i])), None)
        if ref_i is not None:
            payee = "-".join(parts[1:ref_i])
            ref = parts[ref_i]
            note = "-".join(parts[ref_i + 1:])
        else:
            payee = "-".join(parts[1:])

    flags = _flags((payee + " " + note).upper())
    payee = re.sub(r"^(MANDATE|AUTOPAY|REV|REVERSAL)-", "", payee, flags=re.I).strip()
    return Norm("UPI", payee, make_key(payee) or make_key(note), note, vpa, ifsc, ref, flags)


def _parse_generic(channel: str, parts: list[str], whole_upper: str) -> Norm:
    toks = [p.strip() for p in parts[1:] if p.strip()]
    if toks and toks[0].upper() in ("CR", "DR"):
        toks.pop(0)
    ref = ""
    for i, t in enumerate(toks):
        if REF_RE.match(t.replace(" ", "")) and " " not in t:
            ref = t
            toks.pop(i)
            break

    if channel in ("NEFT", "RTGS", "BBPS", "BILLDESK"):
        payee = toks[0] if toks else ""
        note = "-".join(toks[1:])
    else:                                   # IMPS and anything else
        if len(toks) >= 2:
            payee, note = toks[0], "-".join(toks[1:])
        else:
            payee, note = "", (toks[0] if toks else "")
    return Norm(channel, payee, make_key(payee) or make_key(note), note, "", "", ref, _flags(whole_upper))


def normalize(narration: str, source: str = "bank") -> Norm:
    n = re.sub(r"\s+", " ", (narration or "").strip())
    u = n.upper()

    if source == "card":
        return Norm("CARD", n, make_key(n), "", flags=_flags(u))

    if u.startswith("UPI-"):
        return _parse_upi(n)

    m = re.match(r"^ACH\s*D-\s*(?P<payee>.+?)(?:-(?P<ref>[A-Z0-9]+))?$", n, flags=re.I)
    if m:
        payee = m.group("payee").strip()
        return Norm("ACH", payee, make_key(payee), "", ref=m.group("ref") or "", flags=_flags(u))

    if u.startswith("ATM WDL") or u.startswith("ATM-") or u.startswith("NFS"):
        ref = next((p for p in n.split("-") if LONG_DIGITS_RE.match(p) or re.fullmatch(r"\d{6,}", p)), "")
        return Norm("ATM", "ATM CASH WITHDRAWAL", "ATM CASH WITHDRAWAL", "", ref=ref, flags=_flags(u))

    if u.startswith("INT.PD") or u.startswith("INT PD") or "INTEREST" in u[:12]:
        return Norm("INT", "BANK INTEREST", "BANK INTEREST", n)

    head = u.split("-")[0].strip()
    if head in ("NEFT", "IMPS", "RTGS", "BBPS", "BILLDESK"):
        return _parse_generic(head, n.split("-"), u)

    return Norm("OTHER", n, make_key(n), flags=_flags(u))
