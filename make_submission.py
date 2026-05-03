"""
Produce submission.jsonl per the challenge brief §7.2.

Reads the 30 canonical test pairs from expanded/test_pairs.json and
runs each through the composer to produce one JSONL line per pair.

Usage:
    python make_submission.py [--expanded-dir DIR] [--out submission.jsonl]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from composer import compose


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expanded-dir", default="/home/claude/magicpin/expanded")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    EX = Path(args.expanded_dir)
    pairs_doc = json.loads((EX / "test_pairs.json").read_text())
    pairs = pairs_doc.get("pairs") or pairs_doc

    out_path = Path(args.out)
    n = 0
    with out_path.open("w") as f:
        for pair in pairs:
            tid = pair["trigger_id"]
            mid = pair["merchant_id"]
            cid = pair.get("customer_id")

            trigger = json.loads((EX / "triggers" / f"{tid}.json").read_text())
            merchant = json.loads((EX / "merchants" / f"{mid}.json").read_text())
            cat_path = EX / "categories" / f"{merchant.get('category_slug', '')}.json"
            category = json.loads(cat_path.read_text()) if cat_path.exists() else {}
            customer = None
            if cid:
                cust_path = EX / "customers" / f"{cid}.json"
                if cust_path.exists():
                    customer = json.loads(cust_path.read_text())

            composed = compose(category, merchant, trigger, customer)

            line = {
                "test_id": pair["test_id"],
                "merchant_id": mid,
                "trigger_id": tid,
                "customer_id": cid,
                "body": composed["body"],
                "cta": composed["cta"],
                "send_as": composed["send_as"],
                "suppression_key": composed["suppression_key"],
                "rationale": composed["rationale"],
                "template_name": composed["template_name"],
                "template_params": composed["template_params"],
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            n += 1

    print(f"Wrote {n} test-pair compositions to {out_path}")


if __name__ == "__main__":
    main()
