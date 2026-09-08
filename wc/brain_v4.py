"""
brain_v4.py — v4 per-league Brain for winner-only pricing.

One BrainV4 instance per league (EPL, LaLiga, SerieA, Bundesliga, Ligue1, Other).
Each instance carries league-specific calibration (base_goals_per_side from
config/leagues.json), its own stacker weights, and its own LLM cache.

Pipeline per leg:
    ELO -> league-calibrated goal rates -> result_probs -> p_data
    LLM (league-aware prompt, per-game, cached)           -> p_llm   [gated]
    market mid (the book itself)                           -> p_mkt
    linear logit STACKER -> p_fair

Winner-only: prices only home win / draw / away win. Same public API as BrainV2
so existing callers (scanner, arena, strategy) compile unchanged.
"""
import json
import math
import re

from wc import paths
from wc.lib import market_data as gm
from wc.lib.elo_index import get_elo_index
import wc.markets as mv

# Cap on persisted LLM cache entries per brain. The cache is written to disk
# every cycle, and live keys are per (event, score) — unbounded over a season.
DEFAULT_LLM_CACHE_MAX = 5000


# ── per-league calibration ──────────────────────────────────────────────────

def _load_leagues():
    with open(paths.LEAGUES_FILE) as f:
        return json.load(f)

LEAGUES = _load_leagues()


# ── BrainV4 ─────────────────────────────────────────────────────────────────

class BrainV4:
    def __init__(self, league, config=None):
        if league not in LEAGUES:
            raise KeyError(f"Unknown league '{league}'. Known: {list(LEAGUES)}")
        self.league = league
        self.calibration = LEAGUES[league]
        self.config = config or {}
        # One read-only index shared by all six league brains (injectable for tests).
        self._elo = self.config.get("elo_index") or get_elo_index()
        self.use_llm = self.config.get("use_llm", False)
        # stacker weights in logit space. Market gets a modest prior so a wildly
        # off model leg can't show a fake edge on a thin/stale book.
        default_w = ({"data": 0.6, "llm": 0.2, "market": 0.2} if self.use_llm
                     else {"data": 0.8, "llm": 0.0, "market": 0.2})
        # Copy, never alias: `config` may be shared across leagues or be the dict
        # the orchestrator loaded from disk. update_stacker() mutates in place.
        self.weights, self.weights_version = self._read_weights(default_w)
        if self.use_llm and self.weights.get("llm", 0) <= 0:
            self.weights["llm"] = 0.2
        self.llm_cache_max = self.config.get("llm_cache_max", DEFAULT_LLM_CACHE_MAX)
        self.llm_cache = dict(self.config.get("llm_cache") or {})
        if "_leg" in self.llm_cache:                      # don't alias the sub-dict either
            self.llm_cache["_leg"] = dict(self.llm_cache["_leg"])
        self._evict_cache()                               # a stale oversized file is trimmed on load
        self._anthropic = None                            # lazy

    # ── weight state (versioned for optimistic locking — Ground Rule 9) ────

    def _read_weights(self, default_w):
        """Accept either a bare {src: w} dict (v3 on-disk format) or a versioned
        {"weights": {...}, "version": n} payload. Returns (weights, version)."""
        raw = self.config.get("stacker_weights")
        if raw is None:
            return dict(default_w), 0
        if isinstance(raw, dict) and "weights" in raw:
            return dict(raw["weights"]), int(raw.get("version", 0))
        return dict(raw), 0

    def weights_payload(self):
        """Versioned snapshot for the supervisor to persist. Compare `version`
        against what was loaded to detect a concurrent write before saving."""
        return {"weights": dict(self.weights), "version": self.weights_version}

    # ── features ──────────────────────────────────────────────────────────

    def elos(self, home, away):
        return self._elo.elo(home), self._elo.elo(away)

    def game_prior(self, home, away, market_total=None):
        """Per-game goal rates. ELO supremacy splits a total that is ANCHORED to
        the market when available (the edge is the split, not the total).
        Uses league-specific base_goals_per_side instead of the old hardcoded 1.35."""
        eh, ea = self.elos(home, away)
        elo_diff = (eh - ea) if (eh is not None and ea is not None) else 0.0
        mu_home, mu_away = gm.goal_rates(
            elo_diff,
            base_goals=self.calibration["base_goals_per_side"],
            total_goals=market_total)
        return {"home": home, "away": away, "elo_diff": elo_diff,
                "mu_home": mu_home, "mu_away": mu_away,
                "elo_known": eh is not None and ea is not None}

    def live_prior(self, home, away, minute, home_score, away_score, stats=None,
                   market_total=None):
        """In-play prior: goal rates scaled to the REMAINING minutes, plus live
        dominance multipliers from ESPN box-score stats."""
        eh, ea = self.elos(home, away)
        elo_diff = (eh - ea) if (eh is not None and ea is not None) else 0.0
        mins_left = max(1.0, 90.0 - float(minute or 0))
        mu_home, mu_away = gm.goal_rates(
            elo_diff, mins_left=mins_left,
            base_goals=self.calibration["base_goals_per_side"],
            total_goals=market_total)
        from wc.lib import dominance as dom
        if stats:
            dom_h, dom_a = dom.dominance_multipliers(stats.get("home"), stats.get("away"))
        else:
            dom_h = dom_a = 1.0
        return {"home": home, "away": away, "elo_diff": elo_diff,
                "mu_home": mu_home, "mu_away": mu_away,
                "minute": minute, "home_score": home_score, "away_score": away_score,
                "dom_home": dom_h, "dom_away": dom_a,
                "elo_known": eh is not None and ea is not None}

    # ── data-model pricing (winner-only) ───────────────────────────────────

    def data_pfair(self, parsed, prior, leg_is_home):
        """Analytic p_fair for winner legs only. Returns None for non-winner types."""
        if parsed["type"] != "winner":
            return None
        mh, ma = prior["mu_home"], prior["mu_away"]
        ph, pt, pa = mv.result_probs(mh, ma)
        return pt if parsed["team"] is None else (ph if leg_is_home else pa)

    def live_pfair(self, parsed, lprior, leg_is_home):
        """In-play p_fair: winner only, full-period only."""
        if parsed["period"] != "full" or parsed["type"] != "winner":
            return None
        mh, ma = lprior["mu_home"], lprior["mu_away"]
        hs, as_ = lprior["home_score"], lprior["away_score"]
        ph, pt, pa = mv.result_probs(mh, ma, margin_so_far=hs - as_)
        return pt if parsed["team"] is None else (ph if leg_is_home else pa)

    # ── stacker ───────────────────────────────────────────────────────────

    def pfair(self, parsed, prior, leg_is_home, market_mid=None, llm=None):
        sources = {}
        d = self.data_pfair(parsed, prior, leg_is_home)
        if d is not None:
            sources["data"] = d
        if llm is not None:
            sources["llm"] = llm
        if market_mid is not None:
            sources["market"] = market_mid
        return self._stack(sources), sources

    def llm_price_market(self, market, market_mid=None):
        """Generic LLM p_fair for ANY market — the fallback for leg types the data
        model can't price. Cached per ticker. Returns (p_fair, sources)."""
        if not self.use_llm:
            return None, {}
        tk = market.get("ticker")
        cache = self.llm_cache.setdefault("_leg", {})
        if tk not in cache:
            try:
                p, _ = self._llm_estimate(market)
            except Exception:
                p = None
            self._leg_cache_set(tk, p)
        p = self.llm_cache.get("_leg", {}).get(tk)
        if p is None:
            return None, {}
        sources = {"llm": p}
        if market_mid is not None:
            sources["market"] = market_mid
        return self._stack(sources), sources

    def prewarm_leg_llm(self, markets, max_workers=8):
        """Concurrently LLM-price a batch of novel markets (bounded thread pool).
        No-op if the LLM is off."""
        if not self.use_llm or not markets:
            return
        cache = self.llm_cache.setdefault("_leg", {})
        todo, seen = [], set()
        for m in markets:
            tk = m.get("ticker")
            if tk and tk not in cache and tk not in seen:
                seen.add(tk); todo.append(m)
        if not todo:
            return
        import concurrent.futures as _cf

        def _price(m):
            try:
                p, _ = self._llm_estimate(m)
            except Exception:
                p = None
            return m.get("ticker"), p
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for tk, p in ex.map(_price, todo):
                self._leg_cache_set(tk, p)

    @staticmethod
    def _logit_mean(weighted):
        """(p, w) pairs -> weighted mean in logit space, back to a probability."""
        num = den = 0.0
        for p, w in weighted:
            p = min(max(p, 1e-6), 1 - 1e-6)
            num += w * math.log(p / (1 - p))
            den += w
        if den == 0:
            return None
        return 1.0 / (1.0 + math.exp(-num / den))

    def _stack(self, sources):
        """Weighted blend in logit space over available sources (renormalized).

        If no source carries positive weight, fall back to an EQUAL-weight blend
        of whatever is present — still in logit space, so the fallback is the
        same estimator as the main path rather than a different one."""
        avail = [(p, self.weights.get(k, 0.0))
                 for k, p in sources.items() if p is not None]
        if not avail:
            return None
        if all(w <= 0 for _, w in avail):
            return self._logit_mean([(p, 1.0) for p, _ in avail])
        return self._logit_mean([(p, w) for p, w in avail if w > 0])

    # ── LLM cache (bounded — it is persisted to disk every cycle) ──────────

    def _evict_cache(self):
        """Trim both buckets to `llm_cache_max`, oldest inserted first. FIFO, not
        LRU: dict insertion order is the age order, and a re-write of an existing
        key does not refresh it. The `_leg` bucket is never evicted as a whole —
        it is capped independently."""
        cap = self.llm_cache_max
        if not cap or cap <= 0:
            return
        leg = self.llm_cache.get("_leg")
        if isinstance(leg, dict) and len(leg) > cap:
            for k in list(leg)[:len(leg) - cap]:
                del leg[k]
        games = [k for k in self.llm_cache if k != "_leg"]
        if len(games) > cap:
            for k in games[:len(games) - cap]:
                del self.llm_cache[k]

    def _cache_set(self, key, value):
        """Write a game-level cache entry (pre-game / HT / live), then evict."""
        self.llm_cache[key] = value
        self._evict_cache()
        return value

    def _leg_cache_set(self, ticker, value):
        """Write a per-ticker leg cache entry, then evict."""
        self.llm_cache.setdefault("_leg", {})[ticker] = value
        self._evict_cache()
        return value

    # ── Brain Assistant (meta-learner) ────────────────────────────────────

    def update_stacker(self, resolved, min_sample=15, lr=0.3):
        """Re-tune stacker weights from resolved bets after a round.
        `resolved` = [{sources:{src:p}, outcome:0/1}, ...]. Shifts weight toward
        the lower-Brier source. No-op below min_sample (thin-sample guard)."""
        if len(resolved) < min_sample:
            return self.weights
        brier = {k: [] for k in self.weights}
        for r in resolved:
            y = r["outcome"]
            for k, p in r.get("sources", {}).items():
                if k in brier and p is not None:
                    brier[k].append((p - y) ** 2)
        scores = {k: (sum(v) / len(v)) for k, v in brier.items() if v}
        if len(scores) < 2:
            return self.weights
        inv = {k: 1.0 / (s + 1e-6) for k, s in scores.items()}
        tot = sum(inv.values())
        for k in self.weights:
            if k in inv:
                target = inv[k] / tot
                self.weights[k] = (1 - lr) * self.weights[k] + lr * target
        self.weights_version += 1                         # only on an actual write
        return self.weights

    # ── LLM infrastructure ────────────────────────────────────────────────

    def _get_anthropic(self):
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic()       # reads ANTHROPIC_API_KEY
        return self._anthropic

    @staticmethod
    def _parse_json(text):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return json.loads(text)

    def _llm_estimate(self, market):
        """Returns (p_fair_llm: float|None, reasoning: str). Generic per-market
        LLM fallback for novel/unknown leg types."""
        title = market["title"]
        yes_bid = market.get("yes_bid_cents")
        category = market.get("category", "")
        sub = (market.get("yes_sub_title") or "").strip()
        if sub:
            if sub.lower() in ("tie", "draw"):
                yes_clause = f'YES resolves if the match ends in a draw/tie ("{title}").'
            else:
                yes_clause = f'YES resolves if {sub} wins ("{title}").'
        else:
            yes_clause = f"YES resolves per the market: {title}"
        prompt = (
            "You are a prediction market analyst.\n"
            f"Market: {title}\n"
            f"{yes_clause}\n"
            f"Current YES price: {yes_bid}¢\n"
            f"Category: {category}\n\n"
            "Give your honest probability that this market resolves YES.\n"
            'Respond with ONLY a JSON object: {"p_fair": 0.XX, "reasoning": "one sentence"}'
        )
        try:
            client = self._get_anthropic()
        except Exception as e:
            print(f"  [BRAINv4 WARN] no anthropic client: {e}", flush=True)
            return None, ""
        import time as _t
        for attempt in range(4):                          # temp=0 + retry on 429/overload
            try:
                resp = client.messages.create(
                    model=self.config.get("claude_model", "claude-haiku-4-5-20251001"),
                    # temperature was removed from messages.create in anthropic>=1.4;

                    # passing it raised on EVERY call, and the caller swallowed the

                    # exception into an empty dict, so the LLM leg silently did nothing.

                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                data = self._parse_json(resp.content[0].text.strip())
                return min(0.99, max(0.01, float(data.get("p_fair")))), \
                    str(data.get("reasoning", ""))
            except Exception as e:
                es = str(e).lower()
                if attempt < 3 and ("429" in es or "rate" in es or "overload" in es
                                    or "timeout" in es):
                    _t.sleep(1.5 * (attempt + 1)); continue
                print(f"  [BRAINv4 WARN] LLM estimate failed: {e}", flush=True)
                return None, ""

    def _llm_request(self, prompt):
        """Call Haiku with `prompt`, parse + sanitize to {total_goals,
        p_home/draw/away (normalized)}. {} on failure."""
        try:
            client = self._get_anthropic()
        except Exception as e:
            print(f"  [BRAINv4 WARN] no anthropic client: {e}", flush=True)
            return {}
        import time as _t
        data = None
        for attempt in range(4):                          # temp=0 + retry on 429/overload
            try:
                resp = client.messages.create(
                    model=self.config.get("claude_model", "claude-haiku-4-5-20251001"),
                    # temperature was removed from messages.create in anthropic>=1.4;

                    # passing it raised on EVERY call, and the caller swallowed the

                    # exception into an empty dict, so the LLM leg silently did nothing.

                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}])
                data = self._parse_json(resp.content[0].text.strip())
                break
            except Exception as e:
                es = str(e).lower()
                if attempt < 3 and ("429" in es or "rate" in es or "overload" in es
                                    or "timeout" in es):
                    _t.sleep(1.5 * (attempt + 1)); continue
                print(f"  [BRAINv4 WARN] LLM call failed: {e}", flush=True)
                return {}
        if data is None:
            return {}
        out = {"overrides": {}}
        for k in ("total_goals",):
            try:
                out[k] = float(data[k])
            except (KeyError, TypeError, ValueError):
                pass
        s = 0.0
        for k in ("p_home", "p_draw", "p_away"):
            try:
                out[k] = max(0.0, float(data[k]))
                s += out[k]
            except (KeyError, TypeError, ValueError):
                pass
        if s > 0:                                         # normalize the 3-way
            for k in ("p_home", "p_draw", "p_away"):
                if k in out:
                    out[k] /= s
        return out

    # ── LLM game-level calls (cached) ─────────────────────────────────────

    def _call_llm(self, home, away, prior):
        """Pre-game: league-aware prompt for independent result estimate."""
        league_name = self.calibration["name"]
        prompt = (
            f"{league_name} match: {home} (home) vs {away} (away).\n"
            f"A statistical model expects ~{prior['mu_home'] + prior['mu_away']:.2f} "
            f"total goals (home {prior['mu_home']:.2f}, away {prior['mu_away']:.2f}; "
            f"ELO diff {prior['elo_diff']:.0f}).\n"
            "Give your INDEPENDENT pre-game estimate as JSON ONLY:\n"
            '{"p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX}\n'
            "p_home+p_draw+p_away should sum to ~1."
        )
        return self._llm_request(prompt)

    def _call_llm_ht(self, home, away, lprior):
        """Halftime: re-estimate the FINAL outcome given the live score."""
        league_name = self.calibration["name"]
        hs, as_ = lprior["home_score"], lprior["away_score"]
        prompt = (
            f"{league_name} match: {home} (home) vs {away} (away). It is roughly "
            f"HALFTIME, current score {hs}-{as_} (home-away).\n"
            "Re-estimate the FINAL match outcome given this state, as JSON ONLY:\n"
            '{"p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX}\n'
            "p_* sum ~1."
        )
        return self._llm_request(prompt)

    def _call_llm_live(self, home, away, minute, hs, as_):
        """In-play: re-estimate the FINAL outcome from the live minute + score.
        CAUSAL — the prompt carries only what is known at `minute`, never the result."""
        league_name = self.calibration["name"]
        prompt = (
            f"{league_name} match: {home} (home) vs {away} (away). It is about minute "
            f"{int(minute)} of 90, current score {hs}-{as_} (home-away).\n"
            "Re-estimate the FINAL match outcome given this live state, as JSON ONLY:\n"
            '{"p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX}\n'
            "p_* sum ~1."
        )
        return self._llm_request(prompt)

    def llm_for_game(self, home, away, event_key, prior):
        """Cached one-call-per-game pre-game LLM priors. None if LLM disabled."""
        if not self.use_llm:
            return None
        if event_key not in self.llm_cache:
            self._cache_set(event_key, self._call_llm(home, away, prior))
        return self.llm_cache.get(event_key)

    def llm_halftime(self, home, away, event_key, lprior):
        """Cached one-call halftime re-estimate (key `<event>:HT`). None if off."""
        if not self.use_llm:
            return None
        key = f"{event_key}:HT"
        if key not in self.llm_cache:
            self._cache_set(key, self._call_llm_ht(home, away, lprior))
        return self.llm_cache.get(key)

    def llm_live(self, home, away, event_key, minute, home_score, away_score):
        """Cached CAUSAL in-play LLM estimate, re-queried only when the SCORE changes."""
        if not self.use_llm:
            return None
        key = f"{event_key}:L:{home_score}-{away_score}"
        if key not in self.llm_cache:
            self._cache_set(key, self._call_llm_live(
                home, away, minute, home_score, away_score))
        return self.llm_cache.get(key)

    # ── LLM pricing helpers ───────────────────────────────────────────────

    def _llm_prior(self, prior, llm_data):
        """A prior re-anchored to the LLM's total goals (ELO split kept)."""
        lp = dict(prior)
        tg = llm_data.get("total_goals")
        tot = prior["mu_home"] + prior["mu_away"]
        if tg and tot > 0:
            r = prior["mu_home"] / tot
            lp["mu_home"], lp["mu_away"] = tg * r, tg * (1 - r)
        return lp

    def llm_pfair(self, parsed, prior, leg_is_home, llm_data, ticker=None):
        """LLM's p_fair for a winner leg: explicit override > direct 3-way > re-price."""
        if not llm_data:
            return None
        ov = llm_data.get("overrides") or {}
        if ticker and ticker in ov:
            try:
                return min(0.99, max(0.01, float(ov[ticker])))
            except (TypeError, ValueError):
                pass
        if parsed["type"] == "winner" and parsed["period"] == "full":
            ph, pd, pa = llm_data.get("p_home"), llm_data.get("p_draw"), llm_data.get("p_away")
            if None in (ph, pd, pa):
                return None
            return pd if parsed["team"] is None else (ph if leg_is_home else pa)
        return self.data_pfair(parsed, self._llm_prior(prior, llm_data), leg_is_home)

    def _llm_live_prior(self, lprior, llm_ht):
        """Live prior re-anchored to the LLM's FINAL total (remaining = final − scored)."""
        lp = dict(lprior)
        ft = llm_ht.get("total_goals")
        tot = lprior["mu_home"] + lprior["mu_away"]
        if ft is not None and tot > 0:
            rem = max(0.0, ft - (lprior["home_score"] + lprior["away_score"]))
            r = lprior["mu_home"] / tot
            lp["mu_home"], lp["mu_away"] = rem * r, rem * (1 - r)
        return lp

    def llm_live_pfair(self, parsed, lprior, leg_is_home, llm_ht):
        """LLM-anchored p_fair for a live leg: direct 3-way for the full winner."""
        if not llm_ht:
            return None
        if parsed["type"] == "winner" and parsed["period"] == "full":
            ph, pd, pa = llm_ht.get("p_home"), llm_ht.get("p_draw"), llm_ht.get("p_away")
            if None in (ph, pd, pa):
                return None
            return pd if parsed["team"] is None else (ph if leg_is_home else pa)
        return self.live_pfair(parsed, self._llm_live_prior(lprior, llm_ht), leg_is_home)
