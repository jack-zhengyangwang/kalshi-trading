#!/usr/bin/env python3
"""
run.py — Orchestrator / entry point for the 3-agent betting system.

Wires together:
    Trader (agents/trader.py)  — scan, size, bet, launch exit monitor
    Brain  (group/brain.py)    — LLM + trained model + market data → p_fair
    Trainer (agents/trainer.py)— update learned params from resolved outcomes

Usage:
    python3 group/run.py                # dry run, continuous loop
    python3 group/run.py --once         # single scan, then exit
    python3 group/run.py --execute      # LIVE — places real orders
    python3 group/run.py --train        # run Trainer once, then exit
    python3 group/run.py --fetch-stats  # refresh team ELO, then exit

Environment:
    KALSHI_KEY_ID      Kalshi RSA key ID
    ANTHROPIC_API_KEY  Claude API key (for the Brain's LLM component)
"""
import argparse
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "group"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # v1/: legacy agents/ live here

from kalshi_client import KalshiClient            # noqa: E402
from group.brain import Brain                     # noqa: E402
from agents.trader import Trader                   # noqa: E402
from agents import trainer as trainer_mod          # noqa: E402

CONFIG_PATH = os.path.join(BASE_DIR, "group", "config.json")
STATE_PATH = os.path.join(BASE_DIR, "trainer_state.json")
FETCH_STATS = os.path.join(BASE_DIR, "models", "fetch_stats.py")


def _load_env_from_zshenv():
    """
    launchd doesn't source the shell profile, so when a key isn't already in the
    environment, pull it from ~/.zshenv. Lets the launchd agent run python
    directly (no zsh wrapper, which macOS TCC blocks from reading ~/Desktop).
    """
    import re
    path = os.path.expanduser("~/.zshenv")
    if not os.path.exists(path):
        return
    pat = re.compile(r'^\s*export\s+(KALSHI_KEY_ID|ANTHROPIC_API_KEY|KALSHI_API_KEY)\s*=\s*["\']?([^"\'\n]+)')
    try:
        for line in open(path):
            m = pat.match(line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2)
    except Exception as e:
        print(f"  [RUN WARN] could not load ~/.zshenv: {e}", flush=True)


def load_config():
    """Load base config, then overlay any learned params from trainer_state.json."""
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
            for key in ("brain_weights", "kelly_fraction", "min_edge",
                        "exit_take_profit_pct", "exit_sell_margin",
                        "exit_stop_fair", "exit_fraction"):
                if key in state and state[key] is not None:
                    config[key] = state[key]
            print(f"  [RUN] merged learned params from trainer_state.json", flush=True)
        except Exception as e:
            print(f"  [RUN WARN] could not merge trainer_state: {e}", flush=True)

    return config


def main():
    p = argparse.ArgumentParser(description="3-agent Kalshi betting system")
    p.add_argument("--once", action="store_true", help="single scan then exit")
    p.add_argument("--execute", action="store_true", help="LIVE mode (real orders)")
    p.add_argument("--train", action="store_true", help="run Trainer once then exit")
    p.add_argument("--fetch-stats", action="store_true", help="refresh team ELO then exit")
    args = p.parse_args()

    _load_env_from_zshenv()  # ensure keys present when launched by launchd

    if args.fetch_stats:
        subprocess.run([sys.executable, FETCH_STATS], check=True)
        return

    client = KalshiClient()

    if args.train:
        trainer_mod.train(client)
        return

    config = load_config()
    brain = Brain(config)
    dry_run = not args.execute
    trader = Trader(client, config, brain, dry_run=dry_run)

    mode = "LIVE" if args.execute else "DRY RUN"
    print(f"\n=== Trading group starting [{mode}] ===", flush=True)

    while True:
        trader.run_once()

        if args.once:
            break

        interval = config.get("scan_interval_mins", 30) * 60
        print(f"  Sleeping {interval // 60} min until next scan...\n", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
