# BTC Race Bot — cbBTC minute-bar trend bot

[![Competition](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![Rank](https://make.fun/badge/btc-race-24h-20261123-0511/owlvans/btc-race-24h/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![Reward](https://make.fun/badge/btc-race-24h-20261123-0511/owlvans/btc-race-24h/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

A trading bot for make.fun's [btc-race-24h](https://make.fun/bounty/btc-race-24h-20261123-0511): it reads one 1-minute OHLC bar of cbBTC/USD (Orca Whirlpool, Solana) per minute over the `agent-fun/bars-replay/v1` protocol and answers with buy/sell orders. `main.py`, Python 3.13, standard library only, fully deterministic — no network, no wall clock, no files.

## Strategy

- Entry is an event, not a level: an EMA(12/26) cross-up, taken only with the close above SMA(100), the trend SMA rising (≥ +0.08% over 30 bars), a minimum EMA gap, RSI(14) inside [45, 80], and a break of the previous 20 bars' high — filters that keep sideways chop from re-firing entries.
- Sizing: 90% of cash up front, then two pyramid add-ons at +0.4% and +1.0% gains, so a confirmed trend is traded at near-full weight.
- Partial take-profits sell 34% of the position at peak gains of +1.0% and +1.8%; the rest runs.
- Exits are deliberately slower than entries: five consecutive closes under EMA(26), a trend-SMA break with momentum rolled over, a −1.0% hard stop, or a 2.5% trailing stop armed at +0.5%. Once the peak gain passes +1%, signal exits switch off so winners run (profit lock).
- Risk: a 60-bar cooldown after every exit, new entries blocked at 12% equity drawdown, and an exit-and-lockout at 18% — far inside the race's 25% drawdown bar.

## Dev-window results

Graded on the kit's frozen 24-hour dev window (1,440 bars, $1,000 start):

- Net PnL: **+$16.37**
- Trades: **5** fills
- Max drawdown: **1.29%**
- Final equity: $1,016.37
