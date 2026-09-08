"""
brain_v2.py — Arena v2 Brain: one Brain per super-category.

Pipeline per leg:
    features (ELO, market-anchored total, score/minute) ->
      data model  (markets_v2 Poisson/NB analytics)   -> p_data
      LLM         (per-game priors + select overrides) -> p_llm   [gated]
      market mid  (the book itself)                    -> p_mkt
    linear logit STACKER (per-source weights) -> p_fair

The Brain Assistant (`update_stacker`) re-tunes the stacker weights after a
resolved round by inverse-Brier, with a min-sample guard.

LLM cadence (design 2, gated by use_llm): pregame priors+overrides, a halftime
review for exits / 2H repricing, and on-demand calls on material in-play events —
never per-poll. Hooks are structured here; API wiring lands when use_llm flips on.

Reuses group/brain.py purely for ELO resolution + the winner model, so v2 stays
consistent with the live arena's ratings.
"""
import math

from wc.lib import brain_model as gb
from wc.lib import market_data as gm
import wc.markets as mv


class BrainV2:
    def __init__(self, config=None):
        self.config = config or {}
        self._elo = gb.Brain(self.config)            # ELO resolver + ratings
        self.use_llm = self.config.get("use_llm", False)
        # stacker weights in logit space. Market gets a modest prior so a wildly
        # off model leg can't show a fake edge on a thin/stale book; the Brain
        # Assistant re-tunes these from resolved outcomes. LLM gets weight only
        # when enabled.
        default_w = ({"data": 0.6, "llm": 0.2, "market": 0.2} if self.use_llm
                     else {"data": 0.8, "llm": 0.0, "market": 0.2})
        self.weights = self.config.get("stacker_weights", default_w)
        # if turning the LLM on over previously-persisted weights (llm=0), give it
        # an initial weight so it actually enters the stack (Assistant tunes later)
        if self.use_llm and self.weights.get("llm", 0) <= 0:
            self.weights["llm"] = 0.2
        # per-game LLM result cache {event_code: {priors+overrides}} — one call per
        # game per category (persisted by the arena), NOT per cycle.
        self.llm_cache = self.config.get("llm_cache", {})

    # ── features ──────────────────────────────────────────────────────────────

    def elos(self, home, away):
        h = self._elo._resolve_team(home)
        a = self._elo._resolve_team(away)
        return (self._elo.team_elo.get(h) if h else None,
                self._elo.team_elo.get(a) if a else None)

    def game_prior(self, home, away, market_total=None, market_corner_total=None):
        """Per-game goal/corner rates. ELO supremacy splits a total that is
        ANCHORED to the market when available (the edge is the split, not the total)."""
        eh, ea = self.elos(home, away)
        elo_diff = (eh - ea) if (eh is not None and ea is not None) else 0.0
        mu_home, mu_away = gm.goal_rates(elo_diff, total_goals=market_total)
        mean_corners = market_corner_total or mv.base_corner_total()
        return {"home": home, "away": away, "elo_diff": elo_diff,
                "mu_home": mu_home, "mu_away": mu_away,
                "mean_corners": mean_corners,
                "elo_known": eh is not None and ea is not None}

    def live_prior(self, home, away, minute, home_score, away_score, stats=None,
                   market_total=None, market_corner_total=None):
        """In-play prior: goal rates scaled to the REMAINING minutes, plus current
        tallies and a per-team attacking-intensity multiplier (from live box-score
        dominance) — the signal that actually drives TEAM corners."""
        eh, ea = self.elos(home, away)
        elo_diff = (eh - ea) if (eh is not None and ea is not None) else 0.0
        mins_left = max(1.0, 90.0 - float(minute or 0))
        mu_home, mu_away = gm.goal_rates(elo_diff, mins_left=mins_left,
                                         total_goals=market_total)
        base_c = market_corner_total or mv.base_corner_total()
        from wc.lib import dominance as dom
        if stats:
            dom_h, dom_a = dom.dominance_multipliers(stats.get("home"), stats.get("away"))
            hc = (stats.get("home") or {}).get("corners", 0)
            ac = (stats.get("away") or {}).get("corners", 0)
        else:
            dom_h = dom_a = 1.0
            hc = ac = 0
        return {"home": home, "away": away, "elo_diff": elo_diff,
                "mu_home": mu_home, "mu_away": mu_away,
                "mean_corners": base_c * mins_left / 90.0,   # remaining corners
                "dom_home": dom_h, "dom_away": dom_a,
                "home_corners": hc, "away_corners": ac,
                "minute": minute, "home_score": home_score, "away_score": away_score,
                "elo_known": eh is not None and ea is not None}

    def live_pfair(self, parsed, lprior, leg_is_home, corners_so_far=0):
        """In-play p_fair for a FULL-period leg from remaining rates + tallies.
        Half-period, correct-score and team-corner legs are not live-priced in
        this increment (return None -> skipped)."""
        if parsed["period"] != "full":
            return None
        typ = parsed["type"]
        if typ == "score":
            return None
        mh, ma = lprior["mu_home"], lprior["mu_away"]
        hs, as_ = lprior["home_score"], lprior["away_score"]
        if typ == "winner":
            ph, pt, pa = mv.result_probs(mh, ma, margin_so_far=hs - as_)
            return pt if parsed["team"] is None else (ph if leg_is_home else pa)
        if typ == "team_corners":
            # remaining team corner rate = remaining total * static split * live
            # attacking-intensity multiplier (the tactics/pressure signal)
            if parsed["threshold"] is None:
                return None
            share = mv.team_corner_share(leg_is_home)
            dom = lprior["dom_home"] if leg_is_home else lprior["dom_away"]
            team_rem = lprior["mean_corners"] * share * dom
            so_far = lprior["home_corners"] if leg_is_home else lprior["away_corners"]
            return mv.corner_atleast_prob(team_rem, parsed["threshold"], so_far)
        return mv.fair_yes_v2(parsed, mh, ma, leg_is_home=leg_is_home,
                              mean_corners=lprior["mean_corners"],
                              margin_so_far=hs - as_, home_so_far=hs, away_so_far=as_,
                              corners_so_far=corners_so_far)

    # ── data-model pricing ────────────────────────────────────────────────────

    def data_pfair(self, parsed, prior, leg_is_home):
        """Analytic p_fair for any leg, or None if unpriceable by the data model."""
        typ, period = parsed["type"], parsed["period"]
        mh, ma = prior["mu_home"], prior["mu_away"]
        if typ == "winner" and period == "full":
            ph, pt, pa = mv.result_probs(mh, ma)
            return pt if parsed["team"] is None else (ph if leg_is_home else pa)
        if typ == "score":
            return self._score_pfair(parsed, prior, leg_is_home)
        return mv.fair_yes_v2(parsed, mh, ma, leg_is_home=leg_is_home,
                              mean_corners=prior["mean_corners"])

    def _score_pfair(self, parsed, prior, winner_is_home):
        sc = parsed["score"]
        if not sc:
            return None
        mh, ma = mv.period_rates(prior["mu_home"], prior["mu_away"], parsed["period"])
        a, b = sc["a"], sc["b"]
        wt = (sc.get("winner_text") or "").lower()
        if "draw" in wt or a == b:
            hg, ag = a, b
        else:
            hg, ag = (a, b) if winner_is_home else (b, a)
        return gm.exact_score_prob(mh, ma, hg, ag)

    # ── stacker ───────────────────────────────────────────────────────────────

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
        model can't price (knockout/outright markets like advance, regulation-time,
        margin, matchup). Reuses group/brain._llm_estimate, cached per ticker so a
        market is LLM-priced at most once. Returns (p_fair, sources) or (None, {}).
        With no data source the stacker is effectively LLM(+market)-only."""
        if not self.use_llm:
            return None, {}
        tk = market.get("ticker")
        cache = self.llm_cache.setdefault("_leg", {})
        if tk not in cache:
            try:
                p, _ = self._elo._llm_estimate(market)
            except Exception:
                p = None
            cache[tk] = p
        p = cache.get(tk)
        if p is None:
            return None, {}
        sources = {"llm": p}
        if market_mid is not None:
            sources["market"] = market_mid
        return self._stack(sources), sources

    def prewarm_leg_llm(self, markets, max_workers=8):
        """Concurrently LLM-price a batch of novel markets so the later per-leg
        llm_price_market calls hit cache. This is the parallelism that makes the
        LLM replay fast (bounded pool; _llm_estimate handles temp=0 + 429 retry).
        Populates llm_cache['_leg']. No-op if the LLM is off."""
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
                p, _ = self._elo._llm_estimate(m)
            except Exception:
                p = None
            return m.get("ticker"), p
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for tk, p in ex.map(_price, todo):
                cache[tk] = p

    def _stack(self, sources):
        """Weighted blend in logit space over available sources (renormalized)."""
        num = den = 0.0
        for k, p in sources.items():
            w = self.weights.get(k, 0.0)
            if w <= 0 or p is None:
                continue
            p = min(max(p, 1e-6), 1 - 1e-6)
            num += w * math.log(p / (1 - p))
            den += w
        if den == 0:
            ps = [p for p in sources.values() if p is not None]
            return sum(ps) / len(ps) if ps else None
        return 1.0 / (1.0 + math.exp(-num / den))

    # ── Brain Assistant (meta-learner) ────────────────────────────────────────

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
        # lower Brier -> higher target weight (inverse), blended with current
        inv = {k: 1.0 / (s + 1e-6) for k, s in scores.items()}
        tot = sum(inv.values())
        for k in self.weights:
            if k in inv:
                target = inv[k] / tot
                self.weights[k] = (1 - lr) * self.weights[k] + lr * target
        return self.weights

    # ── LLM (design 2: per-game priors + per-leg overrides, cached) ────────────

    def _llm_request(self, prompt):
        """Call Haiku with `prompt`, parse + sanitize to {total_goals,
        total_corners, p_home/draw/away (normalized), overrides}. {} on failure."""
        try:
            client = self._elo._get_anthropic()      # reads ANTHROPIC_API_KEY
        except Exception as e:
            print(f"  [BRAINv2 WARN] no anthropic client: {e}", flush=True)
            return {}
        import time as _t
        data = None
        for attempt in range(4):                       # temp=0 + retry on 429/overload
            try:
                resp = client.messages.create(
                    model=self.config.get("claude_model", "claude-haiku-4-5-20251001"),
                    # temperature was removed from messages.create in anthropic>=1.4;

                    # passing it raised on EVERY call, and the caller swallowed the

                    # exception into an empty dict, so the LLM leg silently did nothing.

                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}])
                data = self._elo._parse_json(resp.content[0].text.strip())
                break
            except Exception as e:
                es = str(e).lower()
                if attempt < 3 and ("429" in es or "rate" in es or "overload" in es or "timeout" in es):
                    _t.sleep(1.5 * (attempt + 1)); continue
                print(f"  [BRAINv2 WARN] LLM call failed: {e}", flush=True)
                return {}
        if data is None:
            return {}
        out = {"overrides": {}}
        for k in ("total_goals", "total_corners"):
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
        if s > 0:                                     # normalize the 3-way
            for k in ("p_home", "p_draw", "p_away"):
                if k in out:
                    out[k] /= s
        return out

    def _call_llm(self, home, away, prior):
        """Pre-game: independent FINAL total-goals / result / corners estimate."""
        prompt = (
            f"World Cup match: {home} (home) vs {away} (away).\n"
            f"A statistical model expects ~{prior['mu_home'] + prior['mu_away']:.2f} "
            f"total goals (home {prior['mu_home']:.2f}, away {prior['mu_away']:.2f}; "
            f"ELO diff {prior['elo_diff']:.0f}).\n"
            "Give your INDEPENDENT pre-game estimate as JSON ONLY:\n"
            '{"total_goals": X.X, "p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX, '
            '"total_corners": XX.X}\n'
            "p_home+p_draw+p_away should sum to ~1."
        )
        return self._llm_request(prompt)

    def _call_llm_ht(self, home, away, lprior):
        """Halftime: re-estimate the FINAL outcome given the live score."""
        hs, as_ = lprior["home_score"], lprior["away_score"]
        prompt = (
            f"World Cup match: {home} (home) vs {away} (away). It is roughly "
            f"HALFTIME, current score {hs}-{as_} (home-away).\n"
            "Re-estimate the FINAL match outcome given this state, as JSON ONLY:\n"
            '{"total_goals": X.X, "p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX, '
            '"total_corners": XX.X}\n'
            "total_goals/total_corners are FINAL match totals; p_* sum ~1."
        )
        return self._llm_request(prompt)

    def llm_for_game(self, home, away, event_key, prior):
        """Cached one-call-per-game pre-game LLM priors. None if LLM disabled."""
        if not self.use_llm:
            return None
        if event_key not in self.llm_cache:
            self.llm_cache[event_key] = self._call_llm(home, away, prior)
        return self.llm_cache[event_key]

    def llm_halftime(self, home, away, event_key, lprior):
        """Cached one-call halftime re-estimate (key `<event>:HT`). None if off."""
        if not self.use_llm:
            return None
        key = f"{event_key}:HT"
        if key not in self.llm_cache:
            self.llm_cache[key] = self._call_llm_ht(home, away, lprior)
        return self.llm_cache[key]

    def _call_llm_live(self, home, away, minute, hs, as_):
        """In-play: re-estimate the FINAL outcome from the live minute + score.
        CAUSAL — the prompt carries only what is known at `minute`, never the result."""
        prompt = (
            f"World Cup match: {home} (home) vs {away} (away). It is about minute "
            f"{int(minute)} of 90, current score {hs}-{as_} (home-away).\n"
            "Re-estimate the FINAL match outcome given this live state, as JSON ONLY:\n"
            '{"total_goals": X.X, "p_home": 0.XX, "p_draw": 0.XX, "p_away": 0.XX, '
            '"total_corners": XX.X}\n'
            "total_goals/total_corners are FINAL match totals; p_* sum ~1."
        )
        return self._llm_request(prompt)

    def llm_live(self, home, away, event_key, minute, home_score, away_score):
        """Cached CAUSAL in-play LLM estimate, re-queried only when the SCORE changes
        (throttle: ~1 call per scoreline per game, not per poll). None if LLM off.
        Pairs with llm_live_pfair to price any live leg from this estimate."""
        if not self.use_llm:
            return None
        key = f"{event_key}:L:{home_score}-{away_score}"
        if key not in self.llm_cache:
            self.llm_cache[key] = self._call_llm_live(home, away, minute, home_score, away_score)
        return self.llm_cache[key]

    def _llm_live_prior(self, lprior, llm_ht):
        """Live prior re-anchored to the halftime LLM's FINAL totals (remaining =
        final − already scored), keeping the ELO split."""
        lp = dict(lprior)
        ft = llm_ht.get("total_goals")
        tot = lprior["mu_home"] + lprior["mu_away"]
        if ft is not None and tot > 0:
            rem = max(0.0, ft - (lprior["home_score"] + lprior["away_score"]))
            r = lprior["mu_home"] / tot
            lp["mu_home"], lp["mu_away"] = rem * r, rem * (1 - r)
        fc = llm_ht.get("total_corners")
        if fc is not None:
            lp["mean_corners"] = max(0.0, fc - (lprior["home_corners"] + lprior["away_corners"]))
        return lp

    def llm_live_pfair(self, parsed, lprior, leg_is_home, llm_ht, corners_so_far=0):
        """Halftime-LLM p_fair for a live leg: direct 3-way for the full winner,
        else re-priced from the LLM-anchored live prior."""
        if not llm_ht:
            return None
        if parsed["type"] == "winner" and parsed["period"] == "full":
            ph, pd, pa = llm_ht.get("p_home"), llm_ht.get("p_draw"), llm_ht.get("p_away")
            if None in (ph, pd, pa):
                return None
            return pd if parsed["team"] is None else (ph if leg_is_home else pa)
        return self.live_pfair(parsed, self._llm_live_prior(lprior, llm_ht),
                               leg_is_home, corners_so_far)

    def _llm_prior(self, prior, llm_data):
        """A prior re-anchored to the LLM's total goals/corners (ELO split kept)."""
        lp = dict(prior)
        tg = llm_data.get("total_goals")
        tot = prior["mu_home"] + prior["mu_away"]
        if tg and tot > 0:
            r = prior["mu_home"] / tot
            lp["mu_home"], lp["mu_away"] = tg * r, tg * (1 - r)
        if llm_data.get("total_corners"):
            lp["mean_corners"] = llm_data["total_corners"]
        return lp

    def llm_pfair(self, parsed, prior, leg_is_home, llm_data, ticker=None):
        """LLM's p_fair for a leg: explicit override > direct 3-way for the full
        winner > re-priced from the LLM-anchored prior for everything else."""
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
