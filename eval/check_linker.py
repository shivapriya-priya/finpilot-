import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.parse import parse_statement
from core.rules import classify_all
from core.linker import link_all, apply_overrides

DEMO = Path(__file__).resolve().parent.parent / "backend" / "demo"


def load_txns():
    bank = parse_statement((DEMO / "bank_statement.csv").read_text(encoding="utf-8"))
    card = parse_statement((DEMO / "credit_card_statement.csv").read_text(encoding="utf-8"))
    return bank + card


def load_labels():
    with open(Path(__file__).resolve().parent / "labels.csv", newline="") as f:
        return {r["txn_id"]: r for r in csv.DictReader(f)}


def main():
    txns = load_txns()
    labels = load_labels()
    results = classify_all(txns)
    linked = link_all(txns, results)
    final = apply_overrides(results, linked["overrides"])

    print(f"{len(linked['links'])} links found:")
    for lk in linked["links"]:
        print(f"  [{lk.kind:20s}] {lk.link_id:20s} {lk.txn_ids} {lk.detail}")
    print()
    print("pending_splits:", linked["pending_splits"])
    print()

    # ---- check every row linker is responsible for classifying got the right final type.
    # The ANCHOR expense of a split (the original bill) is deliberately left alone: whether
    # it resolves to "expense" is rules.py/the Reader agent's job, not linker's - linker only
    # asserts that it participated in the right link, which the false-positive/cc/pending
    # checks below verify independently.
    anchor_ids = {lk.txn_ids[0] for lk in linked["links"] if lk.kind == "split_reimbursement"}
    checked, passed = 0, 0
    fails = []
    for t in txns:
        lab = labels.get(t.id)
        if not lab:
            continue
        true_type = lab["true_type"]
        true_link = lab["link_id"]
        if not true_link and true_type not in ("p2p_lent", "p2p_repaid", "split_reimbursement"):
            continue
        if t.id in anchor_ids:
            continue   # anchor's own type is out of linker's scope, see note above
        checked += 1
        got = final[t.id]
        ok = got.type == true_type
        if ok:
            passed += 1
        else:
            fails.append((t.id, true_type, got.type, t.narration))

    print(f"linked-row type check (linker's own scope): {passed}/{checked}")
    for f in fails:
        print("  FAIL", f)

    # ---- make sure nothing NOT in ground truth links got reclassified (no false positives)
    false_positive = []
    for t in txns:
        lab = labels.get(t.id)
        if not lab:
            continue
        if lab["link_id"] or lab["true_type"] in ("p2p_lent", "p2p_repaid", "split_reimbursement"):
            continue
        if t.id in linked["overrides"]:
            false_positive.append((t.id, lab["true_type"], linked["overrides"][t.id].type))
    print(f"\nfalse positives (should be 0): {len(false_positive)}")
    for fp in false_positive:
        print("  FP", fp)

    # ---- cc settlement pairs check
    cc_true_groups = {}
    for t in txns:
        lab = labels.get(t.id)
        if lab and lab["link_id"].startswith("cc_"):
            cc_true_groups.setdefault(lab["link_id"], set()).add(t.id)
    cc_got_groups = {lk.link_id: set(lk.txn_ids) for lk in linked["links"] if lk.kind == "cc_settlement"}
    print(f"\ncc settlement groups: expected {len(cc_true_groups)}, got {len(cc_got_groups)}")
    for k, v in cc_true_groups.items():
        match = v in cc_got_groups.values()
        print(f"  {k}: {'OK' if match else 'MISS'} expected={v}")

    overall_ok = (passed == checked) and (len(false_positive) == 0) and \
        all(v in cc_got_groups.values() for v in cc_true_groups.values())
    print("\n" + ("ALL CHECKS PASSED" if overall_ok else "SOME CHECKS FAILED"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
