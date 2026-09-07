"""Real-money safety caps.

Caps FAIL CLOSED. A missing, unparseable, or non-positive cap means the system
refuses to place real orders — it never places them uncapped.

This exists because the original readers were `master.get(key, 0.0)` guarded by
`if cap and ...`. A missing key yielded 0.0, which is falsy, so the guard was
skipped and the cap silently disappeared: the exact opposite of what a safety
limit is for. `per_game_cap_dollars` was absent from the committed switchboard
for two months this way.
"""


def read_cap(master, key):
    """Return (value, ok) for a switchboard master cap.

    ok is False when the key is missing, not a number, or <= 0. Callers must
    refuse real orders when ok is False. Paper trading is unaffected.

    The value is 0.0 whenever ok is False, so a caller that ignores the flag
    still cannot spend against a bad cap.
    """
    raw = master.get(key)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0, False
    return (val, True) if val > 0 else (0.0, False)


def check_caps(master, keys):
    """Validate several caps at once.

    Returns (values, missing): values maps key -> float, missing lists the keys
    that failed. `missing` empty means every cap is usable.
    """
    values, missing = {}, []
    for k in keys:
        v, ok = read_cap(master, k)
        values[k] = v
        if not ok:
            missing.append(k)
    return values, missing
