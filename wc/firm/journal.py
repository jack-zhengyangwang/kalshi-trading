"""A portfolio manager's own journal: what IT has learned, day by day.

The shared fact store holds what is true of the world. A journal holds what is
true of THIS PM — how its own calls have actually gone, per league and per
price band. Two desks reading identical facts accumulate different journals,
because they bet on different games and are wrong in different places.

WHY IT EXISTS. A PM entering a league it has never traded should know that it
knows nothing, and should find out by trading it — not by inheriting somebody
else's record. After twenty settled bets on Liga MX a desk has earned an opinion
about its own reliability there, and can size accordingly. That is a fact about
the manager, not about Mexican football, so it cannot live in the shared store.

POINT-IN-TIME BY CONSTRUCTION. A journal only ever contains what the PM has
already observed: an entry is written when a bet settles, and reads are bounded
by the day being priced. There is no way to consult it about a match that has
not finished, because nothing has been written yet.
"""
from __future__ import annotations

from collections import defaultdict

MIN_SAMPLE = 8          # below this, a hit rate is noise, not a record


class Journal:
    """One PM's accumulated experience."""

    __slots__ = ("pm", "entries", "_by_segment")

    def __init__(self, pm):
        self.pm = pm
        self.entries = []
        self._by_segment = defaultdict(lambda: {"n": 0, "correct": 0,
                                                "pnl": 0.0, "brier": 0.0})

    # ── writing ──────────────────────────────────────────────────────────────

    def record(self, ts, series, model_prob, outcome, net_pnl):
        """Log one SETTLED bet. Called after the fact, never before."""
        if outcome is None:
            return                                # a void teaches nothing
        entry = {"ts": int(ts), "series": series, "model_prob": model_prob,
                 "outcome": outcome, "net_pnl": net_pnl}
        self.entries.append(entry)
        for seg in self._segments(series, model_prob):
            s = self._by_segment[seg]
            s["n"] += 1
            s["correct"] += int((model_prob or 0.5) >= 0.5) == outcome
            s["pnl"] += net_pnl
            if model_prob is not None:
                s["brier"] += (model_prob - outcome) ** 2

    @staticmethod
    def _segments(series, model_prob):
        segs = ["all"]
        if series:
            segs.append(f"series:{series}")
        if model_prob is not None:
            lo = int(model_prob * 5) / 5.0        # 0.0, 0.2, 0.4, 0.6, 0.8
            segs.append(f"band:{lo:.1f}")
        return segs

    # ── reading ──────────────────────────────────────────────────────────────

    def experience(self, series):
        """How many settled bets this PM has in `series`. Zero means it is
        trading blind there, which is worth knowing before sizing."""
        return self._by_segment[f"series:{series}"]["n"]

    def reliability(self, series, min_sample=MIN_SAMPLE):
        """This PM's Brier in `series`, or None when it has not earned an
        opinion yet. None means "no view", never "average" — a default would be
        a belief the record does not support.
        """
        s = self._by_segment[f"series:{series}"]
        if s["n"] < min_sample:
            return None
        return s["brier"] / s["n"]

    def size_multiplier(self, series, min_sample=MIN_SAMPLE, floor=0.25):
        """How much to scale a stake in `series`, given this PM's own record.

        1.0 until the desk has a real sample — an untested league is not
        assumed bad, only unproven. After that, a Brier worse than a coin flip
        (0.25) scales down toward `floor`. It never scales ABOVE 1.0: a good
        run is not evidence for betting bigger, and letting it be would turn
        this into a martingale.
        """
        b = self.reliability(series, min_sample)
        if b is None:
            return 1.0
        if b <= 0.25:
            return 1.0
        return max(floor, 1.0 - (b - 0.25) * 2.0)

    def summary(self):
        out = {}
        for seg, s in sorted(self._by_segment.items()):
            if not s["n"]:
                continue
            out[seg] = {"n": s["n"],
                        "hit_rate": round(s["correct"] / s["n"], 4),
                        "net_pnl": round(s["pnl"], 4),
                        "brier": round(s["brier"] / s["n"], 4)}
        return out

    def __len__(self):
        return len(self.entries)
