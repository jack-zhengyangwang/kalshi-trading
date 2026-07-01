#!/bin/zsh
# run_loop.sh — entry point for the launchd agent `tech.ebk.wc-betting`.
#
# launchd does NOT read your shell profile, so we source ~/.zshenv here to load
# ANTHROPIC_API_KEY and KALSHI_KEY_ID. The loop then runs under `caffeinate` so
# idle sleep doesn't pause scanning or in-play exit management.
#
# Manual use is fine too:  zsh run_loop.sh

[ -f "$HOME/.zshenv" ] && source "$HOME/.zshenv"

cd "$HOME/Desktop/EBK/ebk-personal" || exit 1

if [ -z "$ANTHROPIC_API_KEY" ] || [ -z "$KALSHI_KEY_ID" ]; then
  echo "[run_loop] Missing ANTHROPIC_API_KEY or KALSHI_KEY_ID in env — check ~/.zshenv" >&2
  exit 1
fi

# -i: prevent idle sleep while running. (A closed lid on battery can still sleep.)
exec caffeinate -i /usr/local/bin/python3 group/run.py --execute
