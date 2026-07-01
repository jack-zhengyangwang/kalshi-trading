#!/bin/zsh
# Launch a background Keeper to manage the in-play exit of ONE position.
# Usage: ./watch_exit.sh TICKER YES_SUBTITLE ENTRY_PRICE HOME AWAY
#   e.g. ./watch_exit.sh KXWCGAME-26JUN12CANBIH-CAN Canada 0.54 Canada "Bosnia and Herzegovina"
[ -f "$HOME/.zshenv" ] && source "$HOME/.zshenv"
cd "$HOME/Desktop/EBK/ebk-personal" || exit 1

ticker="$1"; sub="$2"; entry="$3"; home="$4"; away="$5"
log="logs/keeper_${ticker}.log"
nohup caffeinate -i /usr/local/bin/python3 agents/keeper.py \
    --ticker "$ticker" --yes-sub-title "$sub" --entry-price "$entry" \
    --home "$home" --away "$away" --title "$home vs $away Winner?" \
    --execute > "$log" 2>&1 &
echo "Keeper launched for $ticker (pid $!). Watch: tail -f $log"
