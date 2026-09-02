"""
brain.py — Agent 2 (Brain).

Produces a fair-probability estimate for a Kalshi market by blending three
independent sources:

  1. p_fair_llm   — Claude (LLM) estimate from the market title + price
  2. p_fair_model — trained soccer-stats logistic regression (ELO-based)
  3. p_fair_data  — orderbook mid-price + volume skew

The three are combined with learnable weights (from config["brain_weights"],
which the Trainer updates). Disagreement between sources becomes the uncertainty
sigma, used to form a conservative lower bound p_fair_lo.
"""
import difflib
import json
import math
import os
from wc import paths
import pickle
import re
import statistics
import unicodedata

MODELS_DIR = paths.MODELS_DIR
MODEL_PKL = os.path.join(MODELS_DIR, "model.pkl")
TEAM_ELO_JSON = os.path.join(MODELS_DIR, "team_elo.json")
CLUB_ELO_JSON = os.path.join(MODELS_DIR, "club_elo.json")


class Brain:
    def __init__(self, config):
        self.config = config
        self.model = self._load_model()
        self.team_elo = self._load_team_elo()
        self.team_elo.update(self._load_club_elo())   # merge club ratings
        self._elo_index = {self._canon(k): k for k in self.team_elo}
        self._resolve_cache = {}
        self._anthropic = None  # lazy

    # ── Loading ─────────────────────────────────────────────────────────────

    def _load_model(self):
        if os.path.exists(MODEL_PKL):
            try:
                with open(MODEL_PKL, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                print(f"  [BRAIN WARN] model load failed: {e}", flush=True)
        else:
            print("  [BRAIN WARN] model.pkl not found — run models/train.py", flush=True)
        return None

    def _load_team_elo(self):
        if os.path.exists(TEAM_ELO_JSON):
            try:
                with open(TEAM_ELO_JSON) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  [BRAIN WARN] team_elo load failed: {e}", flush=True)
        else:
            print("  [BRAIN WARN] team_elo.json not found — run models/fetch_stats.py", flush=True)
        return {}

    def _load_club_elo(self):
        if os.path.exists(CLUB_ELO_JSON):
            try:
                with open(CLUB_ELO_JSON) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  [BRAIN WARN] club_elo load failed: {e}", flush=True)
        return {}

    @staticmethod
    def _canon(s):
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    # ── Component A: LLM ──────────────────────────────────────────────────────

    def _get_anthropic(self):
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._anthropic

    def _llm_estimate(self, market):
        """Returns (p_fair_llm: float|None, reasoning: str)."""
        title = market["title"]
        yes_bid = market.get("yes_bid_cents")
        category = market.get("category", "")
        sub = (market.get("yes_sub_title") or "").strip()
        # State explicitly what YES means so the LLM prices the correct leg
        # (home win / draw / away win), not just the headline matchup.
        if sub:
            if sub.lower() in self._DRAW_LABELS:
                yes_clause = f'YES resolves if the match ends in a draw/tie ("{title}").'
            else:
                yes_clause = f'YES resolves if {sub} wins ("{title}").'
        else:
            yes_clause = f"YES resolves per the market: {title}"
        # Agent 3 injects a learned calibration note here when the LLM is the
        # pricing culprit (the "reprompt the LLM" lever).
        addendum = (self.config.get("llm_addendum") or "").strip()
        note = (f"Calibration note from your own past performance: {addendum}\n\n"
                if addendum else "")
        prompt = (
            "You are a prediction market analyst.\n"
            f"Market: {title}\n"
            f"{yes_clause}\n"
            f"Current YES price: {yes_bid}¢\n"
            f"Category: {category}\n\n"
            f"{note}"
            "Give your honest probability that this market resolves YES.\n"
            'Respond with ONLY a JSON object: {"p_fair": 0.XX, "reasoning": "one sentence"}'
        )
        try:
            client = self._get_anthropic()
        except Exception as e:
            print(f"  [BRAIN WARN] no anthropic client: {e}", flush=True)
            return None, ""
        import time as _t
        for attempt in range(4):                       # temp=0 for reproducibility; retry 429/overload
            try:
                resp = client.messages.create(
                    model=self.config.get("claude_model", "claude-haiku-4-5-20251001"),
                    max_tokens=200, temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                data = self._parse_json(resp.content[0].text.strip())
                return min(0.99, max(0.01, float(data.get("p_fair")))), str(data.get("reasoning", ""))
            except Exception as e:
                es = str(e).lower()
                if attempt < 3 and ("429" in es or "rate" in es or "overload" in es or "timeout" in es):
                    _t.sleep(1.5 * (attempt + 1)); continue
                print(f"  [BRAIN WARN] LLM estimate failed: {e}", flush=True)
                return None, ""

    @staticmethod
    def _parse_json(text):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return json.loads(text)

    # ── Component B: trained model ─────────────────────────────────────────────

    # Kalshi WC game titles look like "Czechia vs Mexico Winner?".
    _VS_RE = re.compile(
        r"([A-Z][A-Za-z .'-]+?)\s+(?:vs\.?|v\.?)\s+([A-Z][A-Za-z .'-]+?)(?:\s+Winner)?\??$",
        re.IGNORECASE,
    )

    # Kalshi spelling -> team_elo.json key. Anything not listed falls through to
    # the fuzzy matcher below.
    _ALIASES = {
        "czechia": "Czech Republic",
        "turkiye": "Turkey",
        "korea republic": "South Korea",
        "republic of korea": "South Korea",
        "ir iran": "Iran",
        "usa": "USA",
        "united states": "USA",
    }

    # Markets whose YES side is a draw, not a team win.
    _DRAW_LABELS = {"tie", "draw"}

    # Canonicalized Kalshi/ESPN name -> canonicalized clubelo key, for clubs whose
    # spellings differ enough that substring/fuzzy matching misses them.
    _CLUB_ALIASES = {
        "psg": "parissg", "parissaintgermain": "parissg",
        "intermilan": "inter", "internazionale": "inter",
        "manchesterunited": "manunited", "manunited": "manunited",
        "manchestercity": "mancity", "mancity": "mancity",
        "tottenhamhotspur": "tottenham", "spurs": "tottenham",
        "wolverhampton": "wolves", "atleticomadrid": "atletico",
        "hellasverona": "verona", "sportingcp": "sporting", "sportinglisbon": "sporting",
    }

    def _resolve_team(self, name):
        """Match a parsed/sub-title team name to a key in team_elo (national + club)."""
        if not name:
            return None
        if name in self._resolve_cache:
            return self._resolve_cache[name]
        low = name.strip().strip("?.,").lower()
        result = None
        if low in self._ALIASES:
            result = self._ALIASES[low]
        if result is None:
            c = self._canon(name)
            c = self._CLUB_ALIASES.get(c, c)
            if c in self._elo_index:                          # exact (canonical)
                result = self._elo_index[c]
        if result is None:
            for team in self.team_elo:                        # substring either way
                tl = self._canon(team)
                if tl and (tl in c or c in tl):
                    result = team
                    break
        if result is None:                                    # fuzzy last resort
            m = difflib.get_close_matches(c, list(self._elo_index), n=1, cutoff=0.86)
            if m:
                result = self._elo_index[m[0]]
        self._resolve_cache[name] = result
        return result

    def _model_estimate(self, market):
        """
        Returns p_fair_model: float|None — P(the YES side resolves YES).

        Handles all three legs of a Kalshi "Winner?" event (home win / draw /
        away win). The multinomial model predicts [P(away win), P(draw),
        P(home win)] from the home-vs-away ELO gap, where home = the first team
        in the title. The YES side is identified authoritatively from
        `yes_sub_title` ("Czechia" / "Mexico" / "Tie"), then mapped to the
        matching class. Returns None (so the Brain reweights onto its other
        sources) if teams can't be resolved.
        """
        if self.model is None or not self.team_elo:
            return None

        m = self._VS_RE.search(market.get("title", ""))
        if not m:
            return None
        home = self._resolve_team(m.group(1))   # first-listed team
        away = self._resolve_team(m.group(2))
        if not home or not away:
            return None

        elo_diff = self.team_elo[home] - self.team_elo[away]
        try:
            probs = self.model.predict_proba([[elo_diff, abs(elo_diff)]])[0]
        except Exception as e:
            print(f"  [BRAIN WARN] model predict failed: {e}", flush=True)
            return None
        p_away, p_draw, p_home = float(probs[0]), float(probs[1]), float(probs[2])

        sub = (market.get("yes_sub_title") or "").strip()
        if sub.lower() in self._DRAW_LABELS:
            p = p_draw
        else:
            yes_team = self._resolve_team(sub) if sub else None
            if yes_team == home:
                p = p_home
            elif yes_team == away:
                p = p_away
            else:
                # No / unrecognized sub_title — fall back to ticker suffix.
                side = self._side_from_ticker(market, home, away)
                if side == "home":
                    p = p_home
                elif side == "away":
                    p = p_away
                else:
                    return None  # can't orient — don't risk an inverted bet

        return self._apply_calibration(min(0.99, max(0.01, p)))

    def _apply_calibration(self, p):
        """Per-team model calibration in logit space: logit' = (logit(p)+shift)/T.
        Agent 3 sets `model_calibration` {shift, temperature} when the trained
        model is the pricing culprit (the "refine the data model" lever): `shift`
        corrects a directional bias, `temperature`>1 tempers over-confidence."""
        cal = self.config.get("model_calibration") or {}
        shift = cal.get("shift", 0.0)
        T = cal.get("temperature", 1.0)
        if shift == 0.0 and T == 1.0:
            return p
        p = min(0.99, max(0.01, p))
        z = (math.log(p / (1.0 - p)) + shift) / max(0.1, T)
        return min(0.99, max(0.01, 1.0 / (1.0 + math.exp(-z))))

    def _side_from_ticker(self, market, home, away):
        """
        Fallback YES-side detection from the ticker suffix when sub_title is
        absent. Ticker form: KXWCGAME-26JUN24CZEMEX-CZE (suffix = YES team code).
        Returns "home" / "away" / None.
        """
        ticker = market.get("ticker", "")
        suffix = ticker.rsplit("-", 1)[-1].lower() if "-" in ticker else ""
        if not suffix:
            return None
        if suffix in self._DRAW_LABELS:
            return None
        if home and home.lower().replace(" ", "").startswith(suffix):
            return "home"
        if away and away.lower().replace(" ", "").startswith(suffix):
            return "away"
        return None

    # ── Component C: market data ──────────────────────────────────────────────

    def _data_estimate(self, market):
        """Returns p_fair_data: float — orderbook mid blended with volume skew."""
        bid = market.get("yes_bid_cents")
        ask = market.get("yes_ask_cents")

        if bid is not None and ask is not None:
            mid = (bid + ask) / 200.0
        elif bid is not None:
            mid = bid / 100.0
        else:
            return 0.5

        vol_yes = market.get("volume_yes", 0) or 0
        vol_no = market.get("volume_no", 0) or 0
        total = vol_yes + vol_no
        if total > 0:
            volume_skew = vol_yes / total
            p = 0.6 * mid + 0.4 * volume_skew
        else:
            p = mid

        return min(0.99, max(0.01, p))

    # ── Blend ─────────────────────────────────────────────────────────────────

    def evaluate(self, market):
        """
        Returns a dict with the blended p_fair plus per-component values and a
        conservative lower bound.
        """
        p_llm, reasoning = self._llm_estimate(market)
        p_model = self._model_estimate(market)
        p_data = self._data_estimate(market)

        weights = self.config.get(
            "brain_weights", {"w_llm": 0.33, "w_model": 0.33, "w_data": 0.34}
        )
        components = {
            "w_llm": p_llm,
            "w_model": p_model,
            "w_data": p_data,
        }

        # Drop unavailable components and renormalize their weights onto the rest.
        active = {k: v for k, v in components.items() if v is not None}
        if not active:
            # Total fallback — should not happen since data is always present
            p_final = 0.5
        else:
            wsum = sum(weights.get(k, 0.0) for k in active)
            if wsum <= 0:
                # equal weight if config has zeros for the active set
                p_final = sum(active.values()) / len(active)
            else:
                p_final = sum(
                    (weights.get(k, 0.0) / wsum) * v for k, v in active.items()
                )

        present = [v for v in (p_llm, p_model, p_data) if v is not None]
        sigma = statistics.pstdev(present) if len(present) >= 2 else 0.15

        p_fair_lo = max(0.01, p_final - sigma)
        p_fair_hi = min(0.99, p_final + sigma)

        return {
            "p_fair": p_final,
            "p_fair_lo": p_fair_lo,
            "p_fair_hi": p_fair_hi,
            "p_fair_llm": p_llm,
            "p_fair_model": p_model,
            "p_fair_data": p_data,
            "sigma": sigma,
            "reasoning": reasoning,
        }

    # ══ LIVE (in-play) evaluation — used by the Keeper agent ═══════════════════

    # Pre-game scoring assumptions for the Poisson in-play model.
    _BASE_GOALS = 1.35          # league-ish per-side expected goals at parity
    _SUPREMACY_SCALE = 600.0    # ELO points → tanh supremacy scale

    def _team_elos_from_title(self, market):
        """(home_elo, away_elo) or (None, None) if teams can't be resolved."""
        m = self._VS_RE.search(market.get("title", ""))
        if not m:
            return None, None
        home = self._resolve_team(m.group(1))
        away = self._resolve_team(m.group(2))
        if not home or not away:
            return None, None
        return self.team_elo[home], self.team_elo[away]

    def _inplay_wp(self, elo_diff, margin, mins_left, dom_home=1.0, dom_away=1.0):
        """
        Analytic in-play win probabilities via a Skellam (difference-of-Poisson)
        model. Returns (p_away, p_draw, p_home) for the FINAL result given the
        current goal `margin` (home_score - away_score) and minutes remaining.

        Remaining goals per side ~ Poisson(rate * time_fraction), with the rate
        split by an ELO-derived supremacy and scaled by live-dominance
        multipliers (shots/possession; 1.0 = no adjustment). At mins_left=0 the
        result is locked to the current margin.
        """
        frac = max(0.0, min(1.0, mins_left / 90.0))
        base = self.config.get("base_goals", self._BASE_GOALS)   # calibrated
        s = math.tanh(elo_diff / self._SUPREMACY_SCALE)      # -1..1 supremacy
        mu_home = base * (1.0 + s) * frac * dom_home
        mu_away = base * (1.0 - s) * frac * dom_away
        mu_home = max(1e-6, mu_home)
        mu_away = max(1e-6, mu_away)

        # Poisson pmf over remaining goals, summed into final-margin distribution.
        K = 12
        ph = [math.exp(-mu_home) * mu_home ** k / math.factorial(k) for k in range(K + 1)]
        pa = [math.exp(-mu_away) * mu_away ** k / math.factorial(k) for k in range(K + 1)]

        p_home = p_draw = p_away = 0.0
        for gh in range(K + 1):
            for ga in range(K + 1):
                final = margin + gh - ga
                prob = ph[gh] * pa[ga]
                if final > 0:
                    p_home += prob
                elif final == 0:
                    p_draw += prob
                else:
                    p_away += prob
        total = p_home + p_draw + p_away
        if total <= 0:
            return (1 / 3, 1 / 3, 1 / 3)
        return (p_away / total, p_draw / total, p_home / total)

    def _dominance(self, game_state):
        """(dom_home, dom_away) live-dominance goal-rate multipliers from ESPN
        box-score stats, or (1.0, 1.0) if disabled / unavailable. Each value is
        ~0.65–1.35; >1 for the team controlling the run of play."""
        if not self.config.get("use_dominance", True):
            return 1.0, 1.0
        # Caller may precompute the multipliers once per cycle and stash them on
        # game_state to avoid an ESPN fetch per leg/team (high-freq in-play loop).
        if game_state.get("_dom") is not None:
            return game_state["_dom"]
        try:
            from wc.lib import live_feed, dominance
            stats = live_feed.get_game_stats(
                game_state["home_team"], game_state["away_team"]
            )
            if not stats:
                return 1.0, 1.0
            return dominance.dominance_multipliers(stats["home"], stats["away"])
        except Exception as e:
            print(f"  [BRAIN WARN] dominance fetch failed: {e}", flush=True)
            return 1.0, 1.0

    def evaluate_market(self, market, home_name, away_name, game_state=None,
                        market_total=None):
        """
        Phase 4 — fair value for a game-ADJACENT market (spread / total /
        team-total / BTTS) from the same ELO→Poisson goal model as the winner
        market. `home_name`/`away_name` are the full matchup team names (the
        adjacent market's own title names only one team, so the caller supplies
        the pairing — e.g. from the sibling winner market). Pass `game_state`
        to price it live (uses current score + remaining minutes + dominance).

        Returns {p_fair, p_market, edge_vs_ask, type, line, leg} or None if the
        market can't be priced (unknown type / unresolved teams).
        """
        from wc.lib import market_data as markets

        home = self._resolve_team(home_name)
        away = self._resolve_team(away_name)
        if not home or not away:
            return None
        elo_diff = self.team_elo[home] - self.team_elo[away]

        parsed = markets.parse_market(market.get("ticker", ""),
                                      market.get("yes_sub_title", ""))
        if parsed["type"] == "unknown":
            return None

        # Orient the leg's team (if any) to home/away.
        leg_is_home = True
        if parsed["team"]:
            leg = self._resolve_team(parsed["team"])
            leg_is_home = (leg == home)

        # Live tallies + remaining time (pre-game: full match, 0-0).
        margin_so_far = home_so_far = away_so_far = 0
        mins_left = 90
        dom_home = dom_away = 1.0
        if game_state and game_state.get("status") == "in":
            home_so_far = game_state["home_score"]
            away_so_far = game_state["away_score"]
            margin_so_far = home_so_far - away_so_far
            mins_left = max(0, 90 - game_state["minute"])
            dom_home, dom_away = self._dominance(game_state)

        # Anchor the expected total to the market's implied total for this game
        # (calibration: the model's flat total is right on average but blind to
        # matchup scoring); ELO supremacy still does the home/away split.
        base = self.config.get("base_goals", self._BASE_GOALS)
        total = market_total if (market_total and self.config.get(
            "anchor_total_to_market", True)) else None
        mu_home, mu_away = markets.goal_rates(
            elo_diff, mins_left, dom_home, dom_away, base_goals=base, total_goals=total)
        p_fair = markets.fair_yes(parsed, mu_home, mu_away, leg_is_home,
                                  margin_so_far, home_so_far, away_so_far)
        if p_fair is None:
            return None
        p_model = min(0.99, max(0.01, p_fair))   # the ELO/Poisson view
        p_market = self._data_estimate(market)    # the market mid

        # Blend the model vs the market by brain_weights (w_model/w_data — no LLM
        # for adjacent markets), then apply the per-category calibration. This is
        # what lets Agent 3 LEARN adjacent pricing: reweighting toward whichever
        # source is more accurate, and calibrating the model's bias.
        weights = self.config.get("brain_weights",
                                  {"w_llm": 0.0, "w_model": 0.5, "w_data": 0.5})
        active = {"w_model": p_model}
        if p_market is not None:
            active["w_data"] = p_market
        wsum = sum(weights.get(k, 0.0) for k in active)
        blend = (sum((weights.get(k, 0.0) / wsum) * v for k, v in active.items())
                 if wsum > 0 else sum(active.values()) / len(active))
        p_blend = self._apply_calibration(min(0.99, max(0.01, blend)))

        # Large model-vs-market gaps are ALLOWED by default (they can be genuine
        # edge, and the per-bet $ cap bounds the downside). `adjacent_disagree_cap`
        # is an optional clamp (None = off): if set and the gap exceeds it, defer
        # to the market. Left off — risk is handled by sizing + Agent 3 calibration.
        cap = self.config.get("adjacent_disagree_cap")
        if cap and p_market is not None and abs(p_model - p_market) > cap:
            p_blend = p_market

        ask_c = market.get("yes_ask_cents")
        edge = (p_blend - ask_c / 100.0) if ask_c else None
        return {
            "p_fair": p_blend,
            "p_model": p_model,
            "p_market": p_market,
            "p_fair_model": p_model,
            "p_fair_data": p_market,
            "edge_vs_ask": edge,
            "type": parsed["type"],
            "line": parsed["line"],
            "leg": "home" if leg_is_home else "away",
        }

    _SCORE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
    _WINS_SPLIT_RE = re.compile(r"\s+wins?\s+", re.I)

    def evaluate_scoreline(self, market, home_name, away_name, game_state=None,
                           market_total=None):
        """Fair YES for a KXWCSCORE exact-score leg ("<team> wins a-b" / "Draw c-c").

        Prices one cell of the SAME independent-Poisson goal grid the winner/
        spread/total markets use (markets.exact_score_prob), oriented to home/away
        from the leg's named winner, then blends with the market mid and applies
        the per-category calibration — identical machinery to evaluate_market, so
        the scoreline view never disagrees with the winner view. Returns the
        adjacent-shaped dict {p_fair, p_model, p_market, ...} or None if the leg
        can't be parsed / teams don't resolve.
        """
        from wc.lib import market_data as markets

        home = self._resolve_team(home_name)
        away = self._resolve_team(away_name)
        if not home or not away:
            return None

        sub = market.get("yes_sub_title") or ""
        m = self._SCORE_RE.search(sub)
        if not m:
            return None
        g1, g2 = int(m.group(1)), int(m.group(2))
        low = sub.lower()
        if "draw" in low or "tie" in low:
            home_goals = away_goals = g1                 # c-c (g1 == g2)
        else:
            win_name = self._WINS_SPLIT_RE.split(sub, 1)[0].strip()
            win_team = self._resolve_team(win_name)
            if win_team == home:
                home_goals, away_goals = g1, g2          # "<home> wins g1-g2"
            elif win_team == away:
                home_goals, away_goals = g2, g1          # "<away> wins g1-g2"
            else:
                return None

        elo_diff = self.team_elo[home] - self.team_elo[away]

        home_so_far = away_so_far = 0
        mins_left = 90
        dom_home = dom_away = 1.0
        if game_state and game_state.get("status") == "in":
            home_so_far = game_state["home_score"]
            away_so_far = game_state["away_score"]
            mins_left = max(0, 90 - game_state["minute"])
            dom_home, dom_away = self._dominance(game_state)

        base = self.config.get("base_goals", self._BASE_GOALS)
        total = market_total if (market_total and self.config.get(
            "anchor_total_to_market", True)) else None
        mu_home, mu_away = markets.goal_rates(
            elo_diff, mins_left, dom_home, dom_away, base_goals=base, total_goals=total)
        p_model = markets.exact_score_prob(
            mu_home, mu_away, home_goals, away_goals, home_so_far, away_so_far)
        p_model = min(0.99, max(1e-4, p_model))          # exact scores are legit-small
        p_market = self._data_estimate(market)

        # Blend model vs market by brain_weights (no LLM for scoreline), then
        # calibrate — same as the adjacent path so Agent 3 can learn the pricing.
        weights = (self.config.get("brain_weights")
                   or {"w_llm": 0.0, "w_model": 0.5, "w_data": 0.5})
        active = {"w_model": p_model}
        if p_market is not None:
            active["w_data"] = p_market
        wsum = sum(weights.get(k, 0.0) for k in active)
        blend = (sum((weights.get(k, 0.0) / wsum) * v for k, v in active.items())
                 if wsum > 0 else sum(active.values()) / len(active))
        p_blend = self._apply_calibration(min(0.99, max(1e-4, blend)))

        ask_c = market.get("yes_ask_cents")
        edge = (p_blend - ask_c / 100.0) if ask_c else None
        return {
            "p_fair": p_blend,
            "p_model": p_model,
            "p_market": p_market,
            "p_fair_model": p_model,
            "p_fair_data": p_market,
            "edge_vs_ask": edge,
            "type": "score",
            "line": f"{home_goals}-{away_goals}",
            "leg": "home",
        }

    def _live_llm_estimate(self, market, game_state):
        """LLM read of the live situation. Returns (p|None, reasoning)."""
        title = market["title"]
        sub = (market.get("yes_sub_title") or "").strip()
        if sub.lower() in self._DRAW_LABELS:
            leg = "the match ends in a draw"
        elif sub:
            leg = f"{sub} wins"
        else:
            leg = "this market resolves YES"
        gs = (f"Live: {game_state['home_team']} {game_state['home_score']}-"
              f"{game_state['away_score']} {game_state['away_team']}, "
              f"minute {game_state['minute']}.")
        prompt = (
            "You are a live in-play soccer analyst.\n"
            f"Market: {title}\n{gs}\n"
            f"Estimate the probability that {leg}, given the live score and time left.\n"
            'Respond with ONLY JSON: {"p_fair": 0.XX, "reasoning": "one sentence"}'
        )
        try:
            client = self._get_anthropic()
            resp = client.messages.create(
                model=self.config.get("claude_model", "claude-haiku-4-5-20251001"),
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            data = self._parse_json(resp.content[0].text.strip())
            p = min(0.99, max(0.01, float(data["p_fair"])))
            return p, str(data.get("reasoning", ""))
        except Exception as e:
            print(f"  [BRAIN WARN] live LLM failed: {e}", flush=True)
            return None, ""

    def evaluate_live(self, market, game_state, prev_score=None, prev_llm=None):
        """
        Live fair value for OUR leg, blending the analytic in-play model, the
        live market mid, and a throttled LLM read (only re-queried on a score
        change — pass prev_score/prev_llm to reuse the last LLM value).

        Returns a dict including `live_p_fair` and `llm_for_cache`/`score_key`
        so the caller can cache the LLM result across polls.
        """
        sub = (market.get("yes_sub_title") or "").strip()
        home_elo, away_elo = self._team_elos_from_title(market)

        # 1. analytic in-play WP for the relevant leg
        p_inplay = None
        if home_elo is not None:
            margin = game_state["home_score"] - game_state["away_score"]
            mins_left = max(0, 90 - game_state["minute"])
            # Phase 3: scale remaining-goal rates by live dominance (shots on
            # target / possession / corners) so a dominant-but-level favorite is
            # priced above a passive one. Falls back to no-op (1.0, 1.0) on any
            # fetch failure, so the score-only model still works.
            dom_home, dom_away = self._dominance(game_state)
            p_away, p_draw, p_home = self._inplay_wp(
                home_elo - away_elo, margin, mins_left, dom_home, dom_away
            )
            if sub.lower() in self._DRAW_LABELS:
                p_inplay = p_draw
            else:
                home_name = self._VS_RE.search(market["title"]).group(1)
                away_name = self._VS_RE.search(market["title"]).group(2)
                yes_team = self._resolve_team(sub) if sub else None
                home_team = self._resolve_team(home_name)
                away_team = self._resolve_team(away_name)
                # H5: only assign a leg we can positively identify. If the YES
                # side can't be matched, drop the in-play component (None) rather
                # than silently defaulting to away → inverted fair value.
                if yes_team and yes_team == home_team:
                    p_inplay = p_home
                elif yes_team and yes_team == away_team:
                    p_inplay = p_away
                else:
                    side = self._side_from_ticker(market, home_team, away_team)
                    p_inplay = p_home if side == "home" else (p_away if side == "away" else None)

        # 2. live market mid
        p_market = self._data_estimate(market)

        # 3. throttled LLM (reuse cached value unless the score changed)
        score_key = (game_state["home_score"], game_state["away_score"])
        if prev_score == score_key and prev_llm is not None:
            p_llm, reasoning = prev_llm, "(cached)"
        else:
            p_llm, reasoning = self._live_llm_estimate(market, game_state)

        weights = self.config.get(
            "brain_weights", {"w_llm": 0.33, "w_model": 0.33, "w_data": 0.34}
        )
        active = {}
        if p_inplay is not None:
            active["w_model"] = p_inplay      # in-play model takes the model slot
        active["w_data"] = p_market
        if p_llm is not None:
            active["w_llm"] = p_llm

        wsum = sum(weights.get(k, 0.0) for k in active)
        if wsum <= 0:
            live = sum(active.values()) / len(active)
        else:
            live = sum((weights.get(k, 0.0) / wsum) * v for k, v in active.items())

        present = [v for v in (p_inplay, p_market, p_llm) if v is not None]
        sigma = statistics.pstdev(present) if len(present) >= 2 else 0.15

        return {
            "live_p_fair": min(0.99, max(0.01, live)),
            "p_inplay": p_inplay,
            "p_market": p_market,
            "p_llm": p_llm,
            "sigma": sigma,
            "reasoning": reasoning,
            "score_key": score_key,
            "llm_for_cache": p_llm,
        }


# Module-level convenience wrapper (matches TRADING_GROUP_PLAN.md signature)
_DEFAULT_BRAIN = None


def evaluate(market, config):
    global _DEFAULT_BRAIN
    if _DEFAULT_BRAIN is None:
        _DEFAULT_BRAIN = Brain(config)
    return _DEFAULT_BRAIN.evaluate(market)
