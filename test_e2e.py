"""
End-to-end smoke test for the Vera bot.

Spawns uvicorn in a subprocess, pushes the real dataset, runs ticks against
every trigger, exercises /v1/reply with commit / auto-reply / hostile inputs,
and prints a summary. No jq, no shell glue — just Python + stdlib + requests.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urlreq, error as urlerr

ROOT = Path(__file__).resolve().parent
DATASET = Path("magicpin-ai-challenge/dataset")
EXPANDED = Path("magicpin-ai-challenge/expanded")
PORT = 8091
URL = f"http://127.0.0.1:{PORT}"


def http(method: str, path: str, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urlreq.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urlerr.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}


def push(scope, cid, payload, version=1):
    return http("POST", "/v1/context", {
        "scope": scope, "context_id": cid, "version": version,
        "payload": payload, "delivered_at": "2026-04-30T00:00:00Z",
    })


def main() -> int:
    print(f"Starting bot on port {PORT}...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bot:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        # wait for /healthz
        for _ in range(20):
            time.sleep(0.5)
            try:
                code, body = http("GET", "/v1/healthz", timeout=2)
                if code == 200:
                    print(f"  healthz OK: {body}")
                    break
            except Exception:
                continue
        else:
            print("  bot didn't come up", file=sys.stderr)
            return 1

        # metadata
        code, meta = http("GET", "/v1/metadata")
        assert code == 200, meta
        print(f"  metadata OK: {meta['model']}")

        # push categories
        n_cat = 0
        for cat_file in (DATASET / "categories").glob("*.json"):
            payload = json.loads(cat_file.read_text())
            slug = payload.get("slug", cat_file.stem)
            code, _ = push("category", slug, payload)
            assert code == 200, f"category push failed: {cat_file.name}"
            n_cat += 1
        print(f"  pushed {n_cat} categories")

        # push merchants
        merchants = json.loads((DATASET / "merchants_seed.json").read_text())["merchants"]
        for m in merchants:
            code, _ = push("merchant", m["merchant_id"], m)
            assert code == 200, f"merchant push failed: {m['merchant_id']}"
        print(f"  pushed {len(merchants)} merchants")

        # push customers
        customers = json.loads((DATASET / "customers_seed.json").read_text())["customers"]
        for c in customers:
            code, _ = push("customer", c["customer_id"], c)
            assert code == 200, f"customer push failed: {c['customer_id']}"
        print(f"  pushed {len(customers)} customers")

        # push triggers
        triggers = json.loads((DATASET / "triggers_seed.json").read_text())["triggers"]
        for t in triggers:
            code, _ = push("trigger", t["id"], t)
            assert code == 200, f"trigger push failed: {t['id']}"
        print(f"  pushed {len(triggers)} triggers")

        # idempotence: re-push at v1 → stale_version
        code, body = push("category", "dentists", {"slug": "dentists"}, version=1)
        assert body.get("accepted") is False and body.get("reason") == "stale_version", body
        print("  idempotence check OK (stale_version returned)")

        # higher version replaces
        code, body = push("category", "dentists",
                          json.loads((DATASET / "categories" / "dentists.json").read_text()),
                          version=2)
        assert body.get("accepted") is True, body
        print("  higher version replaces OK")

        # tick over EVERY trigger in batches of 10
        all_actions = []
        kind_seen = {}
        kind_failed = {}
        for i in range(0, len(triggers), 10):
            batch = [t["id"] for t in triggers[i:i + 10]]
            code, body = http("POST", "/v1/tick", {
                "now": "2026-04-30T10:00:00Z",
                "available_triggers": batch,
            })
            assert code == 200, body
            all_actions.extend(body.get("actions", []))

        # bucket by kind for inspection
        for a in all_actions:
            tid = a["trigger_id"]
            t = next((x for x in triggers if x["id"] == tid), {})
            kind = t.get("kind", "?")
            kind_seen.setdefault(kind, []).append((a["merchant_id"], a["body"]))

        # any trigger we didn't compose for?
        produced_ids = {a["trigger_id"] for a in all_actions}
        skipped = [t["id"] for t in triggers if t["id"] not in produced_ids]
        print(f"\n  tick produced {len(all_actions)} actions across {len(kind_seen)} kinds")
        print(f"  skipped {len(skipped)} triggers (suppression key match or missing context)")

        # show one example per kind
        print("\n--- one example per trigger kind ---")
        for kind in sorted(kind_seen):
            mid, body_text = kind_seen[kind][0]
            preview = body_text.replace("\n", " ⏎ ")
            if len(preview) > 200:
                preview = preview[:200] + "..."
            print(f"\n[{kind}]  ({mid})")
            print(f"  → {preview}")

        # check for the most obvious failure modes
        print("\n--- quality checks ---")
        problems = []
        for a in all_actions:
            b = a["body"]
            t = next(x for x in triggers if x["id"] == a["trigger_id"])
            kind = t.get("kind", "?")
            # body shouldn't be empty
            if not b.strip():
                problems.append((kind, a["trigger_id"], "empty body"))
            # body shouldn't have raw template artifacts
            for sentinel in ["{{", "}}", "None", "_README", "default=", "{ ", " }"]:
                if sentinel in b and sentinel != "{ ":  # allow currency
                    if sentinel == "None":
                        # only flag standalone None tokens
                        if " None " in f" {b} " or b.endswith(" None"):
                            problems.append((kind, a["trigger_id"], f"contains '{sentinel}'"))
                    else:
                        problems.append((kind, a["trigger_id"], f"contains '{sentinel}'"))
            # body shouldn't say 'unknown' (a default leaking)
            if " unknown" in b.lower() or "default" in b.lower():
                problems.append((kind, a["trigger_id"], "leaked default-ish word"))
            # body shouldn't have actual double spaces (after collapsing
            # paragraph breaks)
            collapsed = re.sub(r"\n+", " ", b)
            if "  " in collapsed:
                problems.append((kind, a["trigger_id"], "double spaces"))
            # body shouldn't end with '—' or whitespace
            if b.rstrip()[-1:] in ("—", "-", ","):
                problems.append((kind, a["trigger_id"], "trailing dash/comma"))

        if problems:
            print(f"  {len(problems)} potential issues:")
            for kind, tid, why in problems[:15]:
                print(f"    - [{kind}] {tid}: {why}")
        else:
            print("  no issues detected")

        # /v1/reply scenarios — fire after a real tick so conversation state exists
        print("\n--- reply scenarios ---")
        # find a tick action we can resume
        ref = next((a for a in all_actions if a["merchant_id"] == "m_001_drmeera_dentist_delhi"), None)
        if ref:
            conv_id = ref["conversation_id"]
            # commit
            code, body = http("POST", "/v1/reply", {
                "conversation_id": conv_id,
                "merchant_id": "m_001_drmeera_dentist_delhi",
                "from_role": "merchant",
                "message": "Yes please go ahead",
                "received_at": "2026-04-30T10:01:00Z",
                "turn_number": 2,
            })
            print(f"  commit ('Yes please go ahead'): action={body.get('action')}")
            print(f"    body: {(body.get('body') or '')[:120]}...")
            assert body.get("action") == "send", body
            assert any(w in (body.get("body") or "").lower()
                       for w in ("draft", "send", "share", "ship", "now", "min")), \
                "commit response should be action-mode, not qualifying"

        # auto-reply
        code, body = http("POST", "/v1/reply", {
            "conversation_id": "conv_auto_test",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Thank you for contacting us. Our team will respond shortly.",
            "received_at": "2026-04-30T10:02:00Z",
            "turn_number": 1,
        })
        print(f"  auto-reply (turn 1): action={body.get('action')}")
        assert body.get("action") == "send", body  # one polite nudge first
        # second auto-reply hit → end
        code, body = http("POST", "/v1/reply", {
            "conversation_id": "conv_auto_test",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Thank you for contacting us. Our team will respond shortly.",
            "received_at": "2026-04-30T10:02:30Z",
            "turn_number": 2,
        })
        print(f"  auto-reply (turn 2 same canned): action={body.get('action')}")
        assert body.get("action") == "end", body

        # hostile
        code, body = http("POST", "/v1/reply", {
            "conversation_id": "conv_hostile_test",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Stop messaging me, this is useless spam",
            "received_at": "2026-04-30T10:03:00Z",
            "turn_number": 1,
        })
        print(f"  hostile: action={body.get('action')}")
        assert body.get("action") == "end", body

        # Hindi commit
        code, body = http("POST", "/v1/reply", {
            "conversation_id": "conv_hi_commit_test",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Theek hai bhej do",
            "received_at": "2026-04-30T10:04:00Z",
            "turn_number": 2,
        })
        print(f"  hindi commit ('Theek hai bhej do'): action={body.get('action')}")
        assert body.get("action") == "send", body

        # busy / wait
        code, body = http("POST", "/v1/reply", {
            "conversation_id": "conv_busy_test",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Kal baat karte hain, abhi busy hoon",
            "received_at": "2026-04-30T10:05:00Z",
            "turn_number": 1,
        })
        print(f"  busy ('Kal baat karte hain'): action={body.get('action')}")
        assert body.get("action") == "wait", body

        print("\n  all reply scenarios passed.")

        # final healthz
        code, hz = http("GET", "/v1/healthz")
        print(f"\n  final healthz: {hz['contexts_loaded']}")

        # ----- 30 canonical test pairs from the expanded dataset -----
        print("\n--- 30 canonical test pairs (expanded dataset) ---")
        # Liveness check
        code, hz = http("GET", "/v1/healthz", timeout=5)
        print(f"  bot still alive? code={code} hz={hz}")
        if not EXPANDED.exists():
            print("  expanded/ not present — run dataset/generate_dataset.py first")
        else:
            # Reset the bot state cleanly so suppression keys are fresh.
            http("POST", "/v1/teardown", body={})

            # Push expanded data at version=1 (fresh after teardown).
            n_m = 0
            for f in (EXPANDED / "categories").glob("*.json"):
                payload = json.loads(f.read_text())
                push("category", payload.get("slug", f.stem), payload, version=1)
            for f in (EXPANDED / "merchants").glob("*.json"):
                payload = json.loads(f.read_text())
                push("merchant", payload["merchant_id"], payload, version=1)
                n_m += 1
            n_c = 0
            for f in (EXPANDED / "customers").glob("*.json"):
                payload = json.loads(f.read_text())
                push("customer", payload["customer_id"], payload, version=1)
                n_c += 1
            n_t = 0
            for f in (EXPANDED / "triggers").glob("*.json"):
                payload = json.loads(f.read_text())
                push("trigger", payload["id"], payload, version=1)
                n_t += 1
            print(f"  pushed expanded: {n_m} merchants, {n_c} customers, {n_t} triggers")

            # Run the 30 pairs
            pairs = json.loads((EXPANDED / "test_pairs.json").read_text())["pairs"]
            assert len(pairs) == 30, f"expected 30 pairs, got {len(pairs)}"
            results = []
            for pair in pairs:
                tid = pair["trigger_id"]
                code, body = http("POST", "/v1/tick", {
                    "now": "2026-04-30T10:00:00Z",
                    "available_triggers": [tid],
                })
                if code != 200 or not body.get("actions"):
                    results.append((pair["test_id"], tid, None, "no action"))
                    continue
                action = body["actions"][0]
                results.append((pair["test_id"], tid, action,
                                "OK" if action.get("body") else "empty body"))

            # Quality summary
            ok_count = sum(1 for r in results if r[3] == "OK")
            print(f"\n  composed for {sum(1 for r in results if r[2])}/30 test pairs")
            print(f"  bodies non-empty: {ok_count}/30")
            if ok_count:
                avg_len = sum(len(r[2]["body"]) for r in results if r[2]) / ok_count
                print(f"  average body length: {avg_len:.0f} chars")

            # Show a few showcase outputs
            print("\n  showcase compositions:")
            showcase_ids = ["T01", "T03", "T07", "T11"]
            for tid in showcase_ids:
                r = next((x for x in results if x[0] == tid), None)
                if r and r[2]:
                    trg_payload = json.loads((EXPANDED / "triggers" / (r[1] + ".json")).read_text())
                    kind = trg_payload.get("kind", "?")
                    print(f"\n  [{tid}] kind={kind}")
                    print(f"  → {r[2]['body']}")

            # Surface any failed pairs
            failed = [r for r in results if r[3] != "OK"]
            if failed:
                print(f"\n  {len(failed)} pair(s) had issues:")
                for tid, trg_id, _, reason in failed:
                    print(f"    {tid} ({trg_id}): {reason}")
            else:
                print("\n  all 30 pairs composed cleanly.")

        print("\nALL SMOKE TESTS PASSED")
        return 0

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
