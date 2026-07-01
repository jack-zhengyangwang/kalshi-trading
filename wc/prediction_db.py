#!/usr/bin/env python3
"""Predictions database — the running record of every RIGHT and WRONG call.

Each time the brain prices a market we append a prediction; when the market
resolves we grade it (outcome, correctness, Brier, realized P&L if we entered).
Over ~weeks this becomes our OWN dataset so we can price partly from history
instead of leaning only on the LLM.

Storage: append-only JSONL, no deps.
  logs/predictions.jsonl         one row per (ticker, ts) at prediction time
  logs/predictions_graded.jsonl  one row per resolved ticker after grading

Grade from the CLI:  ./venv/bin/python3 prediction_db.py --grade
Summary:             ./venv/bin/python3 prediction_db.py --summary
"""
import collections
import datetime as dt
import json
import os
from wc import paths
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRED_LOG = os.path.join(paths.LOGS_DIR, "predictions.jsonl")
GRADED_LOG = os.path.join(paths.LOGS_DIR, "predictions_graded.jsonl")


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log_prediction(rec, ts=None):
    """Append one prediction. Expected keys (extra keys are kept verbatim):
      ticker, series, event, sub, type, period, in_play, minute,
      p_fair, source ('analytic'|'llm'|'blend'), sources {data,llm,market},
      ask, bid, edge, resolves_at, entered (bool), team, cat.
    `ts` overrides the timestamp (tests/replay); else UTC now."""
    rec = dict(rec)
    rec.setdefault("ts", ts or _utc())
    os.makedirs(os.path.dirname(PRED_LOG), exist_ok=True)
    with open(PRED_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def grade(client, ts=None):
    """Settle ungraded predictions against Kalshi results. One market fetch per
    distinct unresolved ticker. Writes graded rows: outcome (1/0), correct,
    brier, and pnl (if the prediction was entered). Returns (#graded, #still_open).
    Idempotent: tickers already in predictions_graded.jsonl are skipped."""
    preds = _read(PRED_LOG)
    if not preds:
        return 0, 0
    already = {g["ticker"] for g in _read(GRADED_LOG)}
    # keep the LATEST prediction per ticker (the one we'd act on)
    latest = {}
    for p in preds:
        tk = p.get("ticker")
        if tk and (tk not in latest or p.get("ts", "") >= latest[tk].get("ts", "")):
            latest[tk] = p
    graded = still_open = 0
    with open(GRADED_LOG, "a") as out:
        for tk, p in latest.items():
            if tk in already:
                continue
            try:
                mk = client.get_market(tk)
                res = (mk.get("result") or "").lower()
                st = mk.get("status")
            except Exception:
                still_open += 1
                continue
            if st not in ("settled", "finalized") or res not in ("yes", "no"):
                still_open += 1
                continue
            outcome = 1 if res == "yes" else 0
            pf = float(p.get("p_fair") or 0.0)
            row = {
                "ticker": tk, "series": p.get("series"), "event": p.get("event"),
                "sub": p.get("sub"), "type": p.get("type"), "source": p.get("source"),
                "p_fair": round(pf, 4), "outcome": outcome,
                "correct": int((pf >= 0.5) == (outcome == 1)),
                "brier": round((pf - outcome) ** 2, 4),
                "ask": p.get("ask"), "edge": p.get("edge"),
                "entered": bool(p.get("entered")),
                "pnl": (round((outcome - (p.get("ask") or 0) / 100.0)
                              * (p.get("n") or 0), 2) if p.get("entered") else None),
                "predicted_ts": p.get("ts"), "graded_ts": ts or _utc(),
            }
            out.write(json.dumps(row) + "\n")
            graded += 1
    return graded, still_open


def summary():
    g = _read(GRADED_LOG)
    if not g:
        print("no graded predictions yet"); return
    print(f"graded predictions: {len(g)}")
    by_src = collections.defaultdict(lambda: [0, 0, 0.0])     # source -> [n, correct, brier]
    by_type = collections.defaultdict(lambda: [0, 0, 0.0])
    for r in g:
        for d, k in ((by_src, r.get("source") or "?"), (by_type, r.get("type") or "?")):
            d[k][0] += 1; d[k][1] += r.get("correct", 0); d[k][2] += r.get("brier", 0.0)
    for title, d in (("by source", by_src), ("by market type", by_type)):
        print(f"\n{title}:")
        for k, (n, c, b) in sorted(d.items(), key=lambda x: -x[1][0]):
            print(f"  {k:<16} n={n:<5} acc={100*c/n:4.0f}%  brier={b/n:.3f}")


if __name__ == "__main__":
    if "--grade" in sys.argv:
        import wc.core.arena_base as A
        gd, op = grade(A.KalshiClientV2(req_per_sec=6))
        print(f"graded {gd} newly-resolved, {op} still open")
    elif "--summary" in sys.argv:
        summary()
    else:
        print("usage: prediction_db.py --grade | --summary")
