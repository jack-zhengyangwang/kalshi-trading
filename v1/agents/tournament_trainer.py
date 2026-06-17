#!/usr/bin/env python3
"""
tournament_trainer.py — Agent 3, per team.

Each tournament team learns its OWN params from its OWN resolved paper bets:
  • kelly_fraction — nudged by realized ROI (winners size up, losers size down)
  • min_edge       — nudged by calibration (overconfident → demand more edge)

Learned params persist to tournament/<team>_tuned.json and are merged over the
static tournament_config.json at load, so the tuning compounds across cycles.
Bounded, momentum-light nudges — NOT a fit to history (that would overfit); it
adapts gradually from clean, realized paper outcomes (no lookahead).

Run standalone:  python3 agents/tournament_trainer.py   (tunes all teams once)
Also called automatically at the end of each tournament cycle.
"""
import glob
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_DIR = os.path.join(BASE_DIR, "tournament")

KELLY_MIN, KELLY_MAX = 0.05, 0.60
EDGE_MIN, EDGE_MAX = 0.002, 0.10
MIN_CLOSED = 10          # need this many resolved bets before tuning


def _read_paper(name):
    rows = []
    path = os.path.join(LOG_DIR, f"paper_{name}.jsonl")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _stats(rows):
    cost = sum((r.get("contracts", 0) or 0) * (r.get("price", 0) or 0)
               for r in rows if r.get("event") == "ENTER")
    pnl = sum(r.get("pnl", 0) or 0 for r in rows if r.get("event") in ("EXIT", "SETTLE"))
    settled = [r for r in rows if r.get("event") == "SETTLE"]
    wins = sum(1 for r in settled if r.get("outcome") == 1)
    enters = [r for r in rows if r.get("event") == "ENTER" and r.get("p_fair") is not None]
    pred = (sum(r["p_fair"] for r in enters) / len(enters)) if enters else None
    closed = [r for r in rows if r.get("event") in ("EXIT", "SETTLE")]
    return {"cost": cost, "pnl": pnl, "roi": (pnl / cost if cost else 0.0),
            "n_closed": len(closed), "n_settled": len(settled),
            "win_rate": (wins / len(settled) if settled else None), "pred": pred}


def _state_path(name):
    return os.path.join(STATE_DIR, f"{name}_tuned.json")


def load_tuned(name):
    p = _state_path(name)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}


def tune_team(strategy, persist=True, min_closed=MIN_CLOSED):
    """Nudge one strategy's params from its paper results. Mutates strategy.p.

    `min_closed` gates how soon learning kicks in. Lower = learns faster off
    less data (noisier — the nudges stay small/bounded to limit overfitting)."""
    s = _stats(_read_paper(strategy.name))
    if s["n_closed"] < min_closed:
        return None   # not enough data yet

    p = strategy.p
    kelly = p.get("kelly_fraction", 0.25)
    min_edge = p.get("min_edge", 0.03)

    # 1. Kelly by ROI — winners size up, losers size down (down faster).
    if s["roi"] > 0.05:
        kelly = min(KELLY_MAX, kelly + 0.02)
    elif s["roi"] < -0.05:
        kelly = max(KELLY_MIN, kelly - 0.03)

    # 2. min_edge by calibration — if it wins far less than it predicted, it's
    #    overconfident → demand more edge; if far more, it can relax.
    if s["pred"] is not None and s["win_rate"] is not None and s["n_settled"] >= min_closed:
        gap = s["win_rate"] - s["pred"]
        if gap < -0.10:
            min_edge = min(EDGE_MAX, min_edge + 0.01)
        elif gap > 0.10:
            min_edge = max(EDGE_MIN, min_edge - 0.005)

    p["kelly_fraction"] = round(kelly, 4)
    p["min_edge"] = round(min_edge, 4)
    if persist:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _state_path(strategy.name) + ".tmp"
        json.dump({"kelly_fraction": p["kelly_fraction"], "min_edge": p["min_edge"]},
                  open(tmp, "w"), indent=2)
        os.replace(tmp, _state_path(strategy.name))
    return {**s, "kelly_fraction": p["kelly_fraction"], "min_edge": p["min_edge"]}


# ── Enhanced Agent 3 ────────────────────────────────────────────────────────
# A richer meta-step that runs after each resolved game. It diagnoses WHICH
# agent erred and routes the fix:
#   • over-conservatism (idle while +EV was on the table) → nudge the Trader to
#     PARTICIPATE: lower min_edge, raise kelly (the "regret of missed profit").
#   • sizing error (well-calibrated p_fair but poor ROI)  → fix the Trader's kelly.
#   • pricing error (p_fair poorly calibrated, high Brier) → fix the Brain: shift
#     brain_weights toward the lower-error source + flag an LLM reprompt / better
#     data. brain_weights is global (one Brain), so it's tuned from POOLED bets.

REGRET_STAKE = 5.0          # hypothetical $ per skipped +edge leg (opportunity cost)


def tune_from_ledger(strategy, ledger):
    """Per-team nudge from an in-memory ledger (no file I/O — keeps the live paper
    logs clean). Mutates strategy.p. ledger = {"bets":[...], "skipped":[...]}.
      bets:    {p_fair, outcome(0/1), pnl, dollars}
      skipped: {p_fair, ask, outcome}  (legs the team passed on)
    Returns a diagnostics dict.
    """
    p = strategy.p
    kelly = p.get("kelly_fraction", 0.25)
    min_edge = p.get("min_edge", 0.03)
    bets, skipped = ledger["bets"], ledger["skipped"]
    diag = []

    # opportunity cost: realized P&L it would have earned on +model-edge legs it
    # skipped (the "hypothetical world of losing out on profit").
    missed = sum(REGRET_STAKE * (s["outcome"] - s["ask"]) / s["ask"]
                 for s in skipped if s["p_fair"] > s["ask"] and s["ask"])

    cost = sum(b["dollars"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    roi = pnl / cost if cost else 0.0
    brier = (sum((b["p_fair"] - b["outcome"]) ** 2 for b in bets) / len(bets)
             if bets else None)

    # 1. Over-conservative: IDLE while leaving real profit on the table. Gated to
    #    zero-bet teams so we never push an active loser to size up (that's the
    #    sizing route's job, below).
    if missed > 1.0 and len(bets) == 0:
        min_edge = max(EDGE_MIN, round(min_edge - 0.01, 4))
        kelly = min(KELLY_MAX, round(kelly + 0.02, 4))
        diag.append(f"REGRET +${missed:.1f} missed/{len(skipped)} skipped "
                    f"→ min_edge↓{min_edge} kelly↑{kelly} (take some risk)")
    # 2/3. For bets it DID make: sizing (pricing handled globally via reweight_brain).
    if bets and (brier is None or brier <= 0.25):
        if roi < -0.05:
            kelly = max(KELLY_MIN, round(kelly - 0.03, 4))
            diag.append(f"SIZING↓ ROI {roi:+.0%} → kelly {kelly}")
        elif roi > 0.05:
            kelly = min(KELLY_MAX, round(kelly + 0.02, 4))
            diag.append(f"SIZING↑ ROI {roi:+.0%} → kelly {kelly}")
    elif bets and brier and brier > 0.25:
        diag.append(f"PRICING Brier={brier:.2f} (Brain-level fix — see reweight)")

    p["kelly_fraction"], p["min_edge"] = kelly, min_edge
    return {"n": len(bets), "roi": roi, "brier": brier, "missed": missed,
            "kelly": kelly, "min_edge": min_edge, "diag": diag}


def reweight_brain(pooled_bets, brain_config, step=0.05):
    """Pricing fix (global): from pooled bets across all teams, compute each Brain
    source's Brier (w_llm/w_model/w_data) and shift weight from the worst to the
    best. Returns a human-readable note + whether an LLM reprompt is advised."""
    keys = ("w_llm", "w_model", "w_data")
    briers = {}
    for k in keys:
        vals = [(b["comp"].get(k), b["outcome"]) for b in pooled_bets
                if b.get("comp", {}).get(k) is not None]
        if vals:
            briers[k] = sum((pv - o) ** 2 for pv, o in vals) / len(vals)
    if len(briers) < 2:
        return None
    best, worst = min(briers, key=briers.get), max(briers, key=briers.get)
    if briers[worst] - briers[best] < 0.05:
        return None    # sources agree — leave the blend alone
    w = dict(brain_config.get("brain_weights",
                              {"w_llm": 0.33, "w_model": 0.33, "w_data": 0.34}))
    w[worst] = max(0.05, round(w[worst] - step, 4))
    w[best] = round(w[best] + step, 4)
    tot = sum(w.values())
    w = {k: round(v / tot, 4) for k, v in w.items()}
    brain_config["brain_weights"] = w
    reprompt = (briers.get("w_llm", 0) == briers[worst])
    note = (f"reweight {worst}↓ {best}↑ (Brier {briers[worst]:.2f}→{briers[best]:.2f}) "
            f"→ {w}" + ("  [advise LLM reprompt]" if reprompt else ""))
    return note


def seed_params_via_llm(brain, name, kind):
    """Ask Claude for a ROUGH untrained starting param guess for one team, given
    its strategy archetype. Returns {kelly_fraction, min_edge, brain_weights} or
    {} on failure (caller falls back to config defaults)."""
    desc = {
        "aggressive_hold": "Aggressive: bets the mean fair value with a low edge bar, sizes hard, holds to resolution.",
        "conservative_active": "Conservative: bets a discounted (uncertainty-haircut) fair value, small size, exits actively.",
        "late_scalp": "Late scalper: only buys near-certain outcomes late in a game for small reliable gains.",
        "momentum": "In-play momentum: enters during the game when the live model sees edge the market hasn't priced.",
    }.get(kind, kind)
    prompt = (
        "You are initializing a soccer-betting strategy team. Give a ROUGH starting "
        "parameter guess (it will be refined by learning, so approximate is fine).\n"
        f"Team: {name} — {desc}\n\n"
        "Parameters:\n"
        "- kelly_fraction (0.05-0.60): fraction of Kelly used to size bets\n"
        "- min_edge (0.005-0.10): minimum edge over the ask required to bet\n"
        "- brain_weights: how much to trust each fair-value source; three numbers "
        "summing to 1.0 — w_llm (an LLM read), w_model (an ELO/Poisson model), "
        "w_data (the market price)\n\n"
        'Respond with ONLY JSON: {"kelly_fraction":0.X,"min_edge":0.0X,'
        '"brain_weights":{"w_llm":0.33,"w_model":0.33,"w_data":0.34}}'
    )
    try:
        client = brain._get_anthropic()
        resp = client.messages.create(
            model=brain.config.get("claude_model", "claude-haiku-4-5-20251001"),
            max_tokens=200, messages=[{"role": "user", "content": prompt}])
        d = brain._parse_json(resp.content[0].text.strip())
        out = {}
        if "kelly_fraction" in d:
            out["kelly_fraction"] = min(KELLY_MAX, max(KELLY_MIN, float(d["kelly_fraction"])))
        if "min_edge" in d:
            out["min_edge"] = min(EDGE_MAX, max(EDGE_MIN, float(d["min_edge"])))
        bw = d.get("brain_weights")
        if isinstance(bw, dict):
            s = sum(float(bw.get(k, 0)) for k in ("w_llm", "w_model", "w_data")) or 1.0
            out["brain_weights"] = {k: round(float(bw.get(k, 0)) / s, 3)
                                    for k in ("w_llm", "w_model", "w_data")}
        return out
    except Exception as e:
        print(f"  [SEED WARN] LLM seed failed for {name}: {e}", flush=True)
        return {}


def _source_briers(bets):
    out = {}
    for k in ("w_llm", "w_model", "w_data"):
        v = [(b["comp"].get(k), b["outcome"]) for b in bets
             if b.get("comp", {}).get(k) is not None and b.get("outcome") is not None]
        if v:
            out[k] = sum((pv - o) ** 2 for pv, o in v) / len(v)
    return out


MIN_PRICE_SAMPLE = 15   # don't move Brain weights until this many resolved bets


def price_update(brain, cum_bets):
    """PRICING lever (shared per category): from cumulative resolved bets, if one
    Brain source's Brier clearly exceeds another, shift weight worst→best and fix
    the culprit (LLM → prompt addendum; model → calibration shift). Mutates
    brain.config. Returns a list of diagnostic strings.

    Gated by MIN_PRICE_SAMPLE so a handful of lucky games can't overfit the blend
    (e.g. cranking model weight to 0.9 off 7 games)."""
    cfg = brain.config
    cbets = [b for b in cum_bets if b.get("outcome") is not None]
    if len(cbets) < MIN_PRICE_SAMPLE:
        return []
    briers = _source_briers(cbets)
    if len(briers) < 2:
        return []

    def _bias(src):
        v = [b["comp"][src] - b["outcome"] for b in cbets if b.get("comp", {}).get(src) is not None]
        return sum(v) / len(v) if v else 0.0   # >0 = source over-estimates YES

    worst = max(briers, key=briers.get)
    best = min(briers, key=briers.get)
    spread = briers[worst] - briers[best]
    if spread <= 0.03:
        return []
    step = min(0.05, round(spread, 3))
    w = dict(cfg.get("brain_weights", {"w_llm": 0.34, "w_model": 0.33, "w_data": 0.33}))
    w[worst] = max(0.05, round(w[worst] - step, 3))
    w[best] = round(w[best] + step, 3)
    tot = sum(w.values()) or 1.0
    cfg["brain_weights"] = {k: round(v / tot, 3) for k, v in w.items()}
    if worst == "w_llm":
        b = _bias("w_llm")
        if b > 0.05:
            cfg["llm_addendum"] = (f"You have OVER-estimated YES by ~{b:.0%} on past games; "
                                   "shade your probabilities DOWN.")
        elif b < -0.05:
            cfg["llm_addendum"] = (f"You have UNDER-estimated YES by ~{abs(b):.0%} on past games; "
                                   "shade your probabilities UP.")
        return [f"PRICING: LLM worst (Brier {briers['w_llm']:.2f}, spread {spread:.2f}) "
                f"→ w_llm↓ + reprompt (bias {b:+.0%})"]
    if worst == "w_model":
        b = _bias("w_model")
        cal = dict(cfg.get("model_calibration") or {"shift": 0.0, "temperature": 1.0})
        cal["shift"] = max(-1.5, min(1.5, round(cal.get("shift", 0.0) - 2.0 * b, 3)))
        cfg["model_calibration"] = cal
        return [f"PRICING: model worst (Brier {briers['w_model']:.2f}, spread {spread:.2f}) "
                f"→ w_model↓ + calib shift={cal['shift']}"]
    return [f"PRICING: market worst (Brier {briers['w_data']:.2f}, spread {spread:.2f}) → w_data↓"]


def full_update(strategy, brain, cum_ledger, game_bets, game_skipped, do_pricing=True):
    """FULL Agent 3 — after a resolved game, diagnose WHICH agent erred and fix it.
      • PRICING (cumulative, shared) — via price_update; skipped when do_pricing
        is False (e.g. the category already did one shared pricing pass).
      • SIZING (this game only): nudge kelly by THIS game's ROI.
      • PARTICIPATION (this game): idle while +EV was on the table → lower
        min_edge, raise kelly.
    Mutates strategy.p (and brain.config when do_pricing). Returns diagnostics.
    """
    p = strategy.p
    kelly = p.get("kelly_fraction", 0.25)
    min_edge = p.get("min_edge", 0.03)
    diag = []
    if do_pricing:
        diag += price_update(brain, cum_ledger["bets"])

    # ── SIZING: THIS game's bets only. ──
    g = [b for b in game_bets if b.get("outcome") is not None]
    gcost = sum(b["dollars"] for b in g)
    groi = (sum(b["pnl"] for b in g) / gcost) if gcost else None
    if groi is not None:
        if groi < -0.05:
            kelly = max(KELLY_MIN, round(kelly - 0.03, 4)); diag.append(f"SIZING↓ game ROI {groi:+.0%} → kelly {kelly}")
        elif groi > 0.05:
            kelly = min(KELLY_MAX, round(kelly + 0.02, 4)); diag.append(f"SIZING↑ game ROI {groi:+.0%} → kelly {kelly}")

    # ── PARTICIPATION: idle this game while +EV was available. ──
    missed = sum(5.0 * (s["outcome"] - s["ask"]) / s["ask"] for s in game_skipped
                 if s.get("outcome") is not None and s["p_fair"] > s["ask"] and s["ask"])
    if missed > 1.0 and not g:
        min_edge = max(EDGE_MIN, round(min_edge - 0.01, 4))
        kelly = min(KELLY_MAX, round(kelly + 0.02, 4))
        diag.append(f"REGRET +${missed:.1f} missed → min_edge↓{min_edge} kelly↑{kelly} (participate)")

    p["kelly_fraction"], p["min_edge"] = kelly, min_edge
    return {"n_game": len(g), "game_roi": groi, "kelly": kelly, "min_edge": min_edge,
            "diag": diag}


def tune_all(teams, min_closed=MIN_CLOSED):
    """Tune every team in place; return {name: summary} for those that updated."""
    out = {}
    for t in teams:
        r = tune_team(t["strategy"], min_closed=min_closed)
        if r:
            out[t["name"]] = r
    return out


if __name__ == "__main__":
    import json as _j
    tcfg = _j.load(open(os.path.join(BASE_DIR, "tournament_config.json")))
    from group.strategy import build_strategy
    teams = [{"name": t["name"],
              "strategy": build_strategy(t["kind"], t["name"],
                                         {**t["params"], **load_tuned(t["name"])})}
             for t in tcfg["teams"]]
    res = tune_all(teams)
    if not res:
        print("No team has enough resolved paper bets to tune yet "
              f"(need ≥{MIN_CLOSED}). Run the paper tournament first.")
    for name, r in res.items():
        print(f"{name}: ROI {r['roi']:+.1%} over {r['n_closed']} closed → "
              f"kelly {r['kelly_fraction']}, min_edge {r['min_edge']}")
