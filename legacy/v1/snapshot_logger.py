"""
snapshot_logger.py — log live in-play box-score snapshots for future GBT training.

Each run finds WC games ESPN reports as in-progress and appends one row per live
game to logs/inplay_snapshots.jsonl with the minute, score, and per-team shots /
shots-on-target / possession / corners. Over a tournament this builds the
time-series dataset that "GBT proper" needs (features at minute X -> final
corners/goals), which our end-of-match historical corpus lacks.

Standalone + read-only (ESPN, no auth, no Kalshi, no orders). Run on a cron:
    */2 * * * * cd /root/ebk-personal && flock -n /tmp/snap.lock ./venv/bin/python3 snapshot_logger.py >> logs/snapshot.out 2>&1
"""
import datetime as dt
import json
import os

from group import live_feed as lf

OUT = os.path.join(os.path.dirname(__file__), "logs", "inplay_snapshots.jsonl")


def snapshot():
    events = lf.scoreboard_events()
    rows = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) != 2:
            continue
        state = (ev.get("status", {}).get("type", {}).get("state") or "").lower()
        if state != "in":
            continue
        by = {c.get("homeAway"): c for c in cs}
        h, a = by.get("home"), by.get("away")
        if not h or not a:
            continue
        home = h.get("team", {}).get("displayName", "")
        away = a.get("team", {}).get("displayName", "")
        try:
            stats = lf.get_game_stats(home, away)
        except Exception:
            stats = None
        rows.append({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": ev.get("id"),
            "home": home, "away": away,
            "minute": lf._parse_minute(ev.get("status", {})),
            "home_score": int(h.get("score") or 0),
            "away_score": int(a.get("score") or 0),
            "stats": stats,
        })
    if rows:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return len(rows)


if __name__ == "__main__":
    n = snapshot()
    print(f"logged {n} live-game snapshot(s) -> {OUT}")
