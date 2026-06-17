"""
trainer.py — Agent 3 (Trainer).

Closes the feedback loop. After games resolve, it:
  1. Reads decision logs, pulls the YES/NO outcome from Kalshi for each bet.
  2. Re-weights the Brain's three sources by inverse Brier score (with momentum).
  3. Nudges the Kelly fraction up/down based on recent ROI.
  4. Nudges min_edge based on calibration (predicted vs realized win rate).
  5. Appends resolved soccer games to the training CSV and retrains the model.

All learned parameters live in trainer_state.json, which run.py merges into the
live config at startup.

Run standalone:  python3 agents/trainer.py
"""
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_PATH = os.path.join(BASE_DIR, "trainer_state.json")
MODELS_DIR = os.path.join(BASE_DIR, "models")
TRAIN_CSV = os.path.join(MODELS_DIR, "wc_matches_1990_2022.csv")
TRAIN_SCRIPT = os.path.join(MODELS_DIR, "train.py")
TEAM_ELO_JSON = os.path.join(MODELS_DIR, "team_elo.json")

DEFAULT_STATE = {
    "brain_weights": {"w_llm": 0.33, "w_model": 0.33, "w_data": 0.34},
    "kelly_fraction": 0.25,
    "min_edge": 0.03,
    "exit_take_profit_pct": 0.50,
    "exit_sell_margin": 0.06,
    "exit_stop_fair": 0.12,
    "exit_fraction": 0.65,
    "games_seen": 0,
    "brier_scores": {"llm": None, "model": None, "data": None},
    "processed_tickers": [],
    "processed_exits": [],
    "resolved_history": [],
    "last_updated": None,
}

# Bounds for the self-tuning parameters
KELLY_MIN, KELLY_MAX = 0.10, 0.35
EDGE_MIN, EDGE_MAX = 0.01, 0.10
TP_MIN, TP_MAX = 0.20, 1.50          # exit_take_profit_pct
MARGIN_MIN, MARGIN_MAX = 0.02, 0.20  # exit_sell_margin
MOMENTUM = 0.8  # weight on prior brain weights vs fresh estimate
EXITS_PATH = os.path.join(LOG_DIR, "exits.jsonl")


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── State I/O ────────────────────────────────────────────────────────────────


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
            # backfill any missing keys
            for k, v in DEFAULT_STATE.items():
                state.setdefault(k, v)
            return state
        except Exception as e:
            print(f"  [TRAINER WARN] state load failed: {e} — using defaults", flush=True)
    return json.loads(json.dumps(DEFAULT_STATE))  # deep copy


def save_state(state):
    """Atomic write (M4): write to a temp file then os.replace, so a crash
    mid-write can't truncate trainer_state.json and silently revert to defaults."""
    state["last_updated"] = _utcnow_iso()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


# ── Reading decisions + outcomes ──────────────────────────────────────────────


def _read_all_decisions():
    """Read all BET decisions across all daily logs."""
    decisions = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "group_*.jsonl"))):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Real BETs only — exclude dry-run preview rows (M5b).
                if d.get("action") == "BET" and not d.get("dry_run"):
                    decisions.append(d)
    return decisions


def _fetch_outcome(client, ticker):
    """
    Returns 1 (YES won), 0 (NO won), or None (unresolved/unknown).
    Kalshi market 'result' field is 'yes'/'no' once settled.
    """
    try:
        market = client.get_market(ticker)
    except Exception as e:
        print(f"  [TRAINER WARN] get_market({ticker}) failed: {e}", flush=True)
        return None
    status = (market.get("status") or "").lower()
    result = (market.get("result") or "").lower()
    if status in ("finalized", "settled", "determined") or result in ("yes", "no"):
        if result == "yes":
            return 1
        if result == "no":
            return 0
    return None


def _resolve_decisions(client, decisions, state):
    """
    Attach outcome + pnl to each newly-resolved BET. Returns list of resolved
    decision dicts (only ones not previously processed).
    """
    processed = set(state.get("processed_tickers", []))
    resolved = []
    for d in decisions:
        key = f"{d['ticker']}@{d['ts']}"
        if key in processed:
            continue
        outcome = _fetch_outcome(client, d["ticker"])
        if outcome is None:
            continue

        fill = d.get("fill_price")
        contracts = d.get("contracts") or 0
        if fill is not None and contracts:
            # YES contract pays $1 if outcome==1, else $0; cost = fill per contract
            payoff = (1.0 if outcome == 1 else 0.0) * contracts
            cost = fill * contracts
            d["pnl"] = round(payoff - cost, 4)
        else:
            d["pnl"] = None

        d["outcome"] = outcome
        d["resolved"] = True
        resolved.append(d)
        processed.add(key)

    state["processed_tickers"] = list(processed)
    return resolved


# ── Update rules ───────────────────────────────────────────────────────────────


def _update_brain_weights(state, resolved):
    """Inverse-Brier reweighting over the last 30 resolved bets, with momentum."""
    recent = resolved[-30:]
    sources = {"llm": "p_fair_llm", "model": "p_fair_model", "data": "p_fair_data"}

    briers = {}
    for short, field in sources.items():
        errs = [
            (d[field] - d["outcome"]) ** 2
            for d in recent
            if d.get(field) is not None and d.get("outcome") is not None
        ]
        briers[short] = (sum(errs) / len(errs)) if errs else None

    # Need at least two scorable sources to reweight
    scorable = {k: v for k, v in briers.items() if v is not None}
    if len(scorable) < 2:
        print("  [TRAINER] Not enough resolved data to reweight brain.", flush=True)
        state["brier_scores"] = briers
        return

    raw = {k: 1.0 / (v + 1e-6) for k, v in scorable.items()}
    total = sum(raw.values())
    fresh = {k: raw[k] / total for k in raw}

    cur = state["brain_weights"]
    key_map = {"llm": "w_llm", "model": "w_model", "data": "w_data"}

    # Blend fresh estimate with prior (momentum). Sources missing this round keep prior.
    new = dict(cur)
    for short, wkey in key_map.items():
        if short in fresh:
            new[wkey] = MOMENTUM * cur.get(wkey, 0.33) + (1 - MOMENTUM) * fresh[short]
    # renormalize
    s = sum(new.values())
    if s > 0:
        new = {k: v / s for k, v in new.items()}

    state["brain_weights"] = new
    state["brier_scores"] = briers
    print(f"  [TRAINER] brain_weights → {json.dumps({k: round(v,3) for k,v in new.items()})}", flush=True)


def _update_kelly_fraction(state, resolved):
    """Nudge Kelly fraction by recent ROI over the last 20 resolved bets."""
    recent = [d for d in resolved[-20:] if d.get("pnl") is not None and d.get("fill_price") and d.get("contracts")]
    if not recent:
        return
    pnl_sum = sum(d["pnl"] for d in recent)
    cost_basis = sum(d["fill_price"] * d["contracts"] for d in recent)
    if cost_basis <= 0:
        return
    roi = pnl_sum / cost_basis

    kf = state["kelly_fraction"]
    if roi > 0.05:
        kf = min(KELLY_MAX, kf + 0.01)
    elif roi < -0.05:
        kf = max(KELLY_MIN, kf - 0.01)
    state["kelly_fraction"] = round(kf, 4)
    print(f"  [TRAINER] recent ROI {roi:+.3f} → kelly_fraction {kf:.3f}", flush=True)


def _update_min_edge(state, resolved):
    """Nudge min_edge by calibration gap (realized vs predicted win rate)."""
    scored = [d for d in resolved if d.get("outcome") is not None and d.get("p_fair_lo") is not None]
    if len(scored) < 5:
        return
    actual_wr = sum(d["outcome"] for d in scored) / len(scored)
    expected_wr = sum(d["p_fair_lo"] for d in scored) / len(scored)
    gap = actual_wr - expected_wr

    me = state["min_edge"]
    if gap < -0.10:      # we lose more than predicted → demand more edge
        me = min(EDGE_MAX, me + 0.005)
    elif gap > 0.10:     # we win more than predicted → can relax
        me = max(EDGE_MIN, me - 0.005)
    state["min_edge"] = round(me, 4)
    print(f"  [TRAINER] calibration gap {gap:+.3f} (act {actual_wr:.2f} vs exp {expected_wr:.2f}) → min_edge {me:.3f}", flush=True)


# ── Model retraining ────────────────────────────────────────────────────────────

_VS_RE = re.compile(
    r"([A-Z][A-Za-z .'-]+?)\s+(?:vs\.?|v\.?)\s+([A-Z][A-Za-z .'-]+?)(?:\s+Winner)?\??$",
    re.IGNORECASE,
)

_ALIASES = {
    "czechia": "Czech Republic", "turkiye": "Turkey",
    "korea republic": "South Korea", "republic of korea": "South Korea",
    "ir iran": "Iran", "usa": "USA", "united states": "USA",
}
_DRAW_LABELS = {"tie", "draw"}


def _load_team_elo():
    if os.path.exists(TEAM_ELO_JSON):
        try:
            with open(TEAM_ELO_JSON) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _resolve_team(name, team_elo):
    if not name:
        return None
    name = name.strip().strip("?.,")
    low = name.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    for team in team_elo:
        if team.lower() == low:
            return team
    for team in team_elo:
        tl = team.lower()
        if tl in low or low in tl:
            return team
    return None


def _event_prefix(ticker):
    """KXWCGAME-26JUN24CZEMEX-CZE -> KXWCGAME-26JUN24CZEMEX (the game)."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def _append_training_rows(resolved):
    """
    Append one 3-class training row per resolved GAME.

    Each Kalshi game is three contracts (home / draw / away). A contract that
    resolved YES (outcome == 1) unambiguously identifies the game result:
        YES on home leg  -> class 2 (home win)
        YES on draw leg  -> class 1 (draw)
        YES on away leg  -> class 0 (away win)
    We group resolved bets by game and emit a row whenever we observed the
    winning leg. Games where we only held losing legs are skipped (the result
    can't be determined from our data alone). Returns count appended.
    """
    team_elo = _load_team_elo()
    if not team_elo or not os.path.exists(TRAIN_CSV):
        return 0

    # Group by game, keep the leg that resolved YES (if any).
    games = {}
    for d in resolved:
        if d.get("outcome") != 1:
            continue  # only the winning leg pins the 3-class label
        games.setdefault(_event_prefix(d["ticker"]), d)

    import csv
    appended = 0
    year = datetime.now(timezone.utc).year
    with open(TRAIN_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        for d in games.values():
            m = _VS_RE.search(d.get("title", ""))
            if not m:
                continue
            home = _resolve_team(m.group(1), team_elo)
            away = _resolve_team(m.group(2), team_elo)
            if not home or not away:
                continue

            sub = (d.get("yes_sub_title") or "").strip()
            if sub.lower() in _DRAW_LABELS:
                cls = 1
            else:
                winner = _resolve_team(sub, team_elo)
                if winner == home:
                    cls = 2
                elif winner == away:
                    cls = 0
                else:
                    continue  # can't orient — skip rather than mislabel

            writer.writerow([year, "group", home, away,
                             team_elo[home], team_elo[away], cls])
            appended += 1
    return appended


def _retrain_model():
    print("  [TRAINER] retraining model...", flush=True)
    try:
        subprocess.run([sys.executable, TRAIN_SCRIPT], check=True, timeout=120)
    except Exception as e:
        print(f"  [TRAINER WARN] retrain failed: {e}", flush=True)


# ── Exit counterfactuals: did selling early beat holding to maturity? ──────────


def _read_exits():
    exits = []
    if not os.path.exists(EXITS_PATH):
        return exits
    with open(EXITS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                exits.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return exits


def _update_exit_params(client, state):
    """
    For each resolved Keeper exit, compute the per-contract advantage of having
    SOLD vs HELD to resolution: adv = bid_received - final_outcome ($1/$0).
      avg(adv) > 0  → selling beat holding → sell MORE eagerly (tighten thresholds)
      avg(adv) < 0  → holding beat selling → sell LESS eagerly (loosen thresholds)
    """
    exits = _read_exits()
    if not exits:
        return
    processed = set(state.get("processed_exits", []))
    advantages = []
    SELL_ACTIONS = {"OVERPRICED", "TAKE_PROFIT", "STOP"}
    for e in exits:
        # Only actual sells are counterfactuals — skip HALFTIME_REC (advisory).
        if e.get("action") not in SELL_ACTIONS:
            continue
        key = f"{e['ticker']}@{e['ts']}"
        if key in processed:
            continue
        outcome = _fetch_outcome(client, e["ticker"])
        if outcome is None:
            continue  # not resolved yet — revisit next run
        advantages.append(e["bid"] - outcome)
        processed.add(key)
    state["processed_exits"] = list(processed)
    if not advantages:
        return

    avg = sum(advantages) / len(advantages)
    tp = state["exit_take_profit_pct"]
    mg = state["exit_sell_margin"]
    if avg > 0.02:        # selling won → be quicker to sell
        tp = max(TP_MIN, tp - 0.05)
        mg = max(MARGIN_MIN, mg - 0.005)
    elif avg < -0.02:     # holding won → be slower to sell
        tp = min(TP_MAX, tp + 0.05)
        mg = min(MARGIN_MAX, mg + 0.005)
    state["exit_take_profit_pct"] = round(tp, 4)
    state["exit_sell_margin"] = round(mg, 4)
    print(f"  [TRAINER] exit counterfactual avg(sell−hold)={avg:+.3f} over "
          f"{len(advantages)} exit(s) → take_profit={tp:.2f}, sell_margin={mg:.3f}",
          flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────


def train(client):
    """Run one full training pass. Returns the updated state dict."""
    state = load_state()
    decisions = _read_all_decisions()
    resolved = _resolve_decisions(client, decisions, state)

    # Exit counterfactual tuning runs independently of new bet resolutions.
    _update_exit_params(client, state)

    if not resolved:
        print("  [TRAINER] No newly-resolved bets. Exit params updated if any.", flush=True)
        save_state(state)
        return state

    print(f"  [TRAINER] {len(resolved)} newly-resolved bet(s).", flush=True)

    # M3: accumulate a rolling history so the tuning windows span real recent
    # history, not just the few bets resolved in this single run.
    KEEP = ("p_fair_llm", "p_fair_model", "p_fair_data", "p_fair_lo",
            "outcome", "pnl", "fill_price", "contracts")
    history = state.get("resolved_history", [])
    history.extend({k: d.get(k) for k in KEEP} for d in resolved)
    state["resolved_history"] = history[-300:]   # cap memory

    _update_brain_weights(state, state["resolved_history"])
    _update_kelly_fraction(state, state["resolved_history"])
    _update_min_edge(state, state["resolved_history"])

    state["games_seen"] = state.get("games_seen", 0) + len(resolved)

    appended = _append_training_rows(resolved)
    if appended:
        print(f"  [TRAINER] appended {appended} new training row(s).", flush=True)
        _retrain_model()

    save_state(state)
    print(f"  [TRAINER] state saved → {STATE_PATH}", flush=True)
    return state


if __name__ == "__main__":
    from kalshi_client import KalshiClient
    client = KalshiClient()
    train(client)
