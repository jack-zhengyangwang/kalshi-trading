"""Pricing views: what a portfolio manager BELIEVES a market is worth.

A view is the PM's own opinion, and it is the thing that must not be shared.
Two PMs may read the same prices and the same results — that is one market, and
there is no choice about it — but the moment they share a fitted model their
P&Ls re-correlate and the firm is one opinion wearing several hats.

    SHARE OBSERVATIONS. NEVER SHARE BELIEFS.

That distinction is the whole reason this file exists. Before it, every arena
strategist read `p_fair` from one shared per-league brain and differed only in
risk parameters, so when the brain was wrong about a match everyone lost on it
together. Ranking sixteen correlated agents is close to ranking one.

Every view implements:

    price(bars) -> {(ticker, ts): p_fair}

Bars a view cannot price are simply absent, and the interpreter's fail-closed
rule turns any condition on `model_prob` or `edge` into False. A market a PM
cannot price is a market it does not trade.

NO LOOKAHEAD. A view is handed bars in chronological order and may use only
what precedes each bar. `EloView` is the interesting case: it builds its ratings
by walking settled matches forward, so a rating used to price a bar reflects
only games that had already finished. That is what `wc/backtest/pricing.py`
cannot do with the live brains, whose Elo is a present-day snapshot.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

REGISTRY = {}


def register(cls):
    REGISTRY[cls.name] = cls
    return cls


def build(spec):
    """{'view': 'momentum', 'params': {...}} -> a View instance."""
    name = spec.get("view") if isinstance(spec, dict) else spec
    if name not in REGISTRY:
        raise ValueError(f"unknown view {name!r}. Known: {sorted(REGISTRY)}")
    params = (spec.get("params") or {}) if isinstance(spec, dict) else {}
    return REGISTRY[name](**params)


def _mid(bar):
    if bar["yes_bid"] is not None and bar["yes_ask"] is not None:
        return (bar["yes_bid"] + bar["yes_ask"]) / 200.0
    if bar["close"] is not None:
        return bar["close"] / 100.0
    return None


class View:
    name = "base"

    def price(self, bars):
        raise NotImplementedError

    def describe(self):
        return self.name


# ── the benchmark ─────────────────────────────────────────────────────────────

@register
class MarketView(View):
    """The book is right. p_fair == mid, so edge is exactly zero everywhere.

    This is not a filler view. It is the do-nothing baseline that promotion
    gate 2.5 requires: a PM on this view can only lose the spread and fees, so
    any PM that fails to beat it has no edge, only variance. Every firm needs
    one honest zero to measure against.
    """
    name = "market"

    def price(self, bars):
        out = {}
        for b in bars:
            m = _mid(b)
            if m is not None:
                out[(b["ticker"], b["ts"])] = m
        return out


# ── pure microstructure: no external data, fully independent ──────────────────

@register
class MomentumView(View):
    """Recent direction persists. Extrapolates the trailing move forward.

    Owes nothing to Elo, to a goals model, or to any fitted state — which makes
    it genuinely uncorrelated with the structural PMs by construction, not by
    hope.
    """
    name = "momentum"

    def __init__(self, lookback=12, strength=0.5, floor=0.02, cap=0.98):
        self.lookback, self.strength = int(lookback), float(strength)
        self.floor, self.cap = float(floor), float(cap)

    def price(self, bars):
        hist, out = defaultdict(list), {}
        for b in bars:
            m = _mid(b)
            if m is None:
                continue
            h = hist[b["ticker"]]
            if len(h) >= 2:
                drift = m - h[0]                    # trailing window only
                p = m + self.strength * drift
                out[(b["ticker"], b["ts"])] = min(self.cap, max(self.floor, p))
            h.append(m)
            if len(h) > self.lookback:
                h.pop(0)
        return out


@register
class MeanReversionView(View):
    """Recent moves overshoot. Pulls price back toward its trailing average.

    The deliberate opposite of MomentumView: when one is right the other is
    wrong, so a firm holding both learns which regime it is in from their
    divergence rather than from a backtest average that hides both.
    """
    name = "mean_reversion"

    def __init__(self, lookback=24, strength=0.5, floor=0.02, cap=0.98):
        self.lookback, self.strength = int(lookback), float(strength)
        self.floor, self.cap = float(floor), float(cap)

    def price(self, bars):
        hist, out = defaultdict(list), {}
        for b in bars:
            m = _mid(b)
            if m is None:
                continue
            h = hist[b["ticker"]]
            if len(h) >= 3:
                p = m + self.strength * (statistics.fmean(h) - m)
                out[(b["ticker"], b["ts"])] = min(self.cap, max(self.floor, p))
            h.append(m)
            if len(h) > self.lookback:
                h.pop(0)
        return out


# ── team strength, built point-in-time from the data itself ───────────────────

@register
class EloView(View):
    """Own Elo ratings, learned by walking settled matches forward in time.

    Deliberately NOT `models/team_elo.json`. That file is a present-day
    snapshot: using it to price a bar from three weeks ago tells the strategy
    who went on to win, which is the model-fitted lookahead stamped on every
    `--brains` backtest. Here a rating is updated only AFTER a match settles, so
    a rating used at time t reflects only matches finished before t.

    Ratings are also this PM's own. Two EloView PMs with different k or
    home_bonus genuinely disagree, rather than reading one shared table.
    """
    name = "elo"

    def __init__(self, k=24.0, base=1500.0, home_bonus=60.0, scale=400.0,
                 draw_share=0.26):
        self.k, self.base = float(k), float(base)
        self.home_bonus, self.scale = float(home_bonus), float(scale)
        self.draw_share = float(draw_share)

    def _expected(self, rh, ra):
        return 1.0 / (1.0 + 10 ** (-((rh + self.home_bonus) - ra) / self.scale))

    def price(self, bars):
        rating = defaultdict(lambda: self.base)
        settled_seen = set()
        out = {}

        for b in bars:
            home, away = b["home"], b["away"]
            sub = b["sub_title"]
            if not home or not away:
                continue

            # ── price this bar from ratings as they stand BEFORE any update ──
            exp_home = self._expected(rating[home], rating[away])
            live = 1.0 - self.draw_share
            if sub and _same(sub, home):
                p = exp_home * live
            elif sub and _same(sub, away):
                p = (1.0 - exp_home) * live
            elif sub:                                   # the draw leg
                p = self.draw_share
            else:
                p = None
            if p is not None:
                out[(b["ticker"], b["ts"])] = min(0.98, max(0.02, p))

            # ── learn only from matches that have ALREADY finished ───────────
            key = b["event_ticker"] or b["ticker"]
            if (b["result"] in ("yes", "no") and b["close_time"] is not None
                    and b["ts"] >= b["close_time"] and key not in settled_seen
                    and sub and _same(sub, home)):
                settled_seen.add(key)
                score = 1.0 if b["result"] == "yes" else 0.0
                delta = self.k * (score - exp_home)
                rating[home] += delta
                rating[away] -= delta
        return out


def _same(a, b):
    na = "".join(c for c in (a or "").lower() if c.isalnum())
    nb = "".join(c for c in (b or "").lower() if c.isalnum())
    return bool(na) and bool(nb) and (na in nb or nb in na)


# ── the existing structural brain, with state this PM owns ────────────────────

@register
class StructuralView(View):
    """The Poisson/Elo goals model from `wc/brain_v4.py`.

    Wrapped rather than reimplemented — it is well-tested and it is what the
    live system already trades. What changes is ownership: the PM supplies its
    own `state_dir`, so its fitted stacker weights are its own and two
    StructuralView PMs can genuinely disagree.

    It still inherits the live brains' Elo snapshot, so a PM using this view
    carries model-fitted lookahead in backtest. `lookahead` says so, and the
    report stamps it. EloView is the honest alternative.
    """
    name = "structural"
    lookahead = True

    def __init__(self, state_dir=None, use_llm=False):
        self.state_dir, self.use_llm = state_dir, bool(use_llm)

    def price(self, bars):
        from wc.backtest import pricing
        from wc import brains as brain_set
        brains = brain_set.load(use_llm=self.use_llm, state_dir=self.state_dir)
        probs, _skips = pricing.build_model_probs(bars, brains=brains)
        return probs

    def describe(self):
        return f"{self.name}(state_dir={self.state_dir or 'default'})"


# ── views built on the firm's shared facts ────────────────────────────────────

class FactsView(View):
    """Base for views that read the firm's shared fact store.

    Facts are shared; what a view makes of them is not. Two FactsViews reading
    the same form numbers can disagree completely — one weighting recency, one
    the table, one ignoring both — and that disagreement is the diversity the
    firm exists to produce.

    Every read goes through an `AsOfIndex`, which has no method that returns a
    current value. A view therefore CANNOT accidentally read tomorrow's form
    while pricing today's bar: the API does not permit it.
    """

    features = ()

    def __init__(self, facts_db=None):
        self.facts_db = facts_db
        self._idx = {}
        self._names = {}

    def _load(self):
        from wc.firm import facts as F
        con = F.connect(self.facts_db) if self.facts_db else F.connect()
        self._idx = {f: F.index(con, f) for f in self.features}
        known = set()
        for ix in self._idx.values():
            known |= ix.entities()
        self._names = {_norm(e): e for e in known}

    def _resolve(self, name):
        """Kalshi and ESPN spell teams differently ('Man United' vs
        'Manchester United'). An unresolved name yields no fact, and the view
        then declines to price — which is the correct direction to fail."""
        if not name:
            return None
        n = _norm(name)
        if n in self._names:
            return self._names[n]
        for key, original in self._names.items():
            if key and n and (key in n or n in key):
                return original
        return None

    def fact(self, name, feature, ts):
        entity = self._resolve(name)
        ix = self._idx.get(feature)
        return None if (entity is None or ix is None) else ix.at(entity, ts)


def _norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


@register
class BookOddsView(FactsView):
    """Believes the sportsbooks. p_fair is their de-vigged consensus.

    The strongest fact we hold: an independent market, priced by people with
    real money at stake and no sight of Kalshi's book. Where the two disagree
    is the cleanest edge candidate in the firm — and unlike a model, this view
    has no parameters to overfit.

    It is NOT the same as MarketView. That one believes Kalshi; this one
    believes a different market, and trades the gap between them.
    """
    name = "book_odds"
    features = ("book_prob_home", "book_prob_away", "book_prob_draw")

    def price(self, bars):
        self._load()
        out = {}
        for b in bars:
            home, away, sub = b["home"], b["away"], b["sub_title"]
            if not home or not sub:
                continue
            key = "|".join(sorted([home, away or ""])).strip("|")
            ts = b["ts"]
            if _same(sub, home):
                p = self.fact(key, "book_prob_home", ts)
            elif away and _same(sub, away):
                p = self.fact(key, "book_prob_away", ts)
            else:
                p = self.fact(key, "book_prob_draw", ts)
            if p is not None and 0.0 < p < 1.0:
                out[(b["ticker"], b["ts"])] = p
        return out


@register
class FormView(FactsView):
    """Believes recent form and league position over long-run strength.

    A deliberately different belief from EloView, which weighs a whole season
    equally. This one thinks a team on a four-game run is genuinely better
    right now than its rating says — a claim that is either true or false, and
    that the firm can now measure separately rather than blending away.
    """
    name = "form"
    features = ("form_last5_ppg", "form_last5_gd", "table_rank")

    def __init__(self, facts_db=None, form_weight=0.6, home_edge=0.08,
                 draw_share=0.26):
        super().__init__(facts_db)
        self.form_weight = float(form_weight)
        self.home_edge = float(home_edge)
        self.draw_share = float(draw_share)

    def price(self, bars):
        self._load()
        out = {}
        for b in bars:
            home, away, sub = b["home"], b["away"], b["sub_title"]
            if not home or not away or not sub:
                continue
            ts = b["ts"]
            hp = self.fact(home, "form_last5_ppg", ts)
            ap = self.fact(away, "form_last5_ppg", ts)
            if hp is None or ap is None:
                continue                      # no fact, no view, no trade

            hg = self.fact(home, "form_last5_gd", ts) or 0.0
            ag = self.fact(away, "form_last5_gd", ts) or 0.0
            # points-per-game runs 0..3; goal difference is the tiebreak
            edge = ((hp - ap) / 3.0) * self.form_weight \
                + ((hg - ag) / 6.0) * (1.0 - self.form_weight) \
                + self.home_edge

            live = 1.0 - self.draw_share
            p_home = min(0.97, max(0.03, 0.5 + edge / 2.0)) * live
            if _same(sub, home):
                p = p_home
            elif _same(sub, away):
                p = live - p_home
            else:
                p = self.draw_share
            out[(b["ticker"], b["ts"])] = min(0.98, max(0.02, p))
        return out


@register
class KnowledgeView(FactsView):
    """Believes the soccer knowledge base: Elo, form, home advantage, H2H.

    Everything it reads was rebuilt by walking finished matches forward, so a
    fact dated day D reflects only matches finished by day D. That makes a
    backtest using this view genuine promotion evidence rather than ranking
    evidence — the distinction that separates it from `structural`.

    A team the knowledge base has never seen has no facts, so this view
    declines to price it. That is deliberate: a desk entering an unfamiliar
    league should know that it knows nothing, and earn a record one match at a
    time rather than inherit a default.
    """
    name = "knowledge"
    features = ("elo", "form_ppg", "home_win_rate", "away_win_rate",
                "matches_seen")

    def __init__(self, facts_db=None, elo_weight=0.6, form_weight=0.25,
                 home_weight=0.15, draw_share=0.26, min_matches=3):
        super().__init__(facts_db)
        self.elo_weight = float(elo_weight)
        self.form_weight = float(form_weight)
        self.home_weight = float(home_weight)
        self.draw_share = float(draw_share)
        self.min_matches = int(min_matches)

    def price(self, bars):
        self._load()
        out = {}
        for b in bars:
            home, away, sub = b["home"], b["away"], b["sub_title"]
            if not home or not away or not sub:
                continue
            ts = b["ts"]

            # Refuse to price a fixture we have barely seen. A thin record is
            # not a small edge, it is no edge.
            if (self.fact(home, "matches_seen", ts) or 0) < self.min_matches:
                continue
            if (self.fact(away, "matches_seen", ts) or 0) < self.min_matches:
                continue

            eh = self.fact(home, "elo", ts)
            ea = self.fact(away, "elo", ts)
            if eh is None or ea is None:
                continue
            elo_p = 1.0 / (1.0 + 10 ** (-((eh + 60.0) - ea) / 400.0))

            fh = self.fact(home, "form_ppg", ts)
            fa = self.fact(away, "form_ppg", ts)
            form_p = 0.5 if (fh is None or fa is None) else \
                min(0.95, max(0.05, 0.5 + (fh - fa) / 6.0))

            hw = self.fact(home, "home_win_rate", ts)
            aw = self.fact(away, "away_win_rate", ts)
            venue_p = 0.5 if (hw is None or aw is None) else \
                min(0.95, max(0.05, 0.5 + (hw - aw) / 2.0))

            blended = (self.elo_weight * elo_p + self.form_weight * form_p
                       + self.home_weight * venue_p)
            total = self.elo_weight + self.form_weight + self.home_weight
            blended /= total

            live = 1.0 - self.draw_share
            if _same(sub, home):
                p = blended * live
            elif _same(sub, away):
                p = (1.0 - blended) * live
            else:
                p = self.draw_share
            out[(b["ticker"], b["ts"])] = min(0.98, max(0.02, p))
        return out
