"""BTC race bot: trend-following on 1-minute cbBTC bars (bars-replay/v1).

Protocol: agent-fun/bars-replay/v1 (hello / batch / done on stdin, one
reply per batch on stdout). Long/flat spot, at most one position.

Strategy (v3, built from dev-window evidence):
  - Entry is an EVENT, not a level: the fast EMA crosses above the slow
    EMA within the last few bars, the close is above the long trend SMA
    (and above a second, 360-bar trend SMA once enough history exists,
    which keeps counter-trend bounces in a falling market out), RSI sits
    in a healthy momentum band, and the close confirms by breaking the
    recent short high by a clear margin (0.1%), so marginal range-top
    breakouts do not trigger. Event entries cannot re-fire every few
    bars in sideways chop the way level checks do.
  - Exits are deliberately SLOWER than entries (the dev window punishes
    twitchy exits with fee bleed): a fall back under the slow EMA held
    for several consecutive bars, a close under the trend SMA with
    momentum rolled over, a percent hard stop under the entry, or a
    percent trailing stop under the running peak once in profit. The
    one fast exit is a failed-breakout cut: a close 0.35% under the
    entry within the first 20 bars ends the trade at once, so a
    breakout that does not follow through costs a little, not a lot.
  - Risk: an equity drawdown breaker stops new entries after a deep
    drawdown from the peak (the race bars entries above 25%); a harder
    floor exits and locks out entirely. A cooldown follows every exit.
Deterministic: values from the bars only; no clock, no network, no
files, no randomness. Stdlib only.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- strategy parameters (tuned on the dev window, kept on robust plateaus) ---
EMA_FAST = 12
EMA_SLOW = 26
TREND_SMA = 100
RSI_PERIOD = 14
RSI_MIN = 45.0
RSI_MAX = 80.0
CROSS_WINDOW = 3      # entry allowed this many bars after the cross up
CONFIRM_N = 20        # close must break the previous CONFIRM_N bars' high
BREAK_MARGIN_PCT = 0.001  # breakout must clear the recent high by this margin
TREND_SMA2 = 360      # second, longer trend SMA the close must sit above (0 = off)
FAIL_BARS = 20        # failed-breakout exit window after entry (0 = off)
FAIL_PCT = 0.0035     # exit if the close falls this far under the entry early on
SLOPE_BARS = 30       # trend SMA must rise over this lookback to enter
SLOPE_MIN_PCT = 0.0   # SMA slope gate OFF: redundant once the breakout
                      # margin + 360-bar trend filter landed (sim identical
                      # with it on or off on dev/rotated/inverted windows)
GAP_MIN_PCT = 0.00015  # minimum EMA fast-slow gap at entry (fraction of price)
ENTRY_EDGE = 0        # 1: enter on the rising edge of the full condition set
REGIME_SLOPE_PCT = 0.0015  # at/above this SMA slope, level entries are allowed
ROC_BARS = 60         # momentum lookback for the optional ROC entry filter
ROC_MIN_PCT = 0.0     # minimum close ROC over ROC_BARS at entry (0 = off)
EXIT_EMA_BARS = 5     # consecutive closes under EMA(slow) before signal exit
STOP_PCT = 0.010      # hard stop under entry
TRAIL_ACT_PCT = 0.005  # trailing stop arms after this gain
TRAIL_PCT = 0.025     # trailing stop distance under the peak
PROFIT_LOCK_PCT = 0.01  # once the peak gain passes this, signal exits switch off
SIZE_FRAC = 0.97      # fraction of cash spent per entry
INIT_FRAC = 0.90      # first tranche of an entry (fraction of cash)
MAX_EXPOSURE = 1.0    # cap on total position cost as a fraction of equity
VOL_ADJ = 0           # 1: scale MAX_EXPOSURE down when recent bar ranges run hot
VOL_REF = 0.0006      # reference mean (high-low)/close over the last 20 bars
VOL_FLOOR = 0.4       # lowest exposure multiplier the vol scaler may reach
ADD_GAINS = (0.004, 0.010)  # gains at which the two add-on tranches buy
ADD_DELAY = 10        # ... and at least this many bars after entry
TP_STEPS = (0.010, 0.018)  # peak gains at which partial profits are taken
TP_FRAC = 0.34        # fraction of the position sold at each take-profit
COOLDOWN = 60         # bars after an exit before a new entry
MIN_HOLD = 15         # bars before signal exits (stops still apply)
DD_BREAKER = 0.12     # stop opening new trades beyond this equity drawdown
DD_HARD = 0.18        # exit + lock out beyond this drawdown
MAX_SLIPPAGE_BPS = 200


class Ema:
    def __init__(self, span):
        self.span = span
        self.k = 2.0 / (span + 1)
        self.warm = []
        self.value = None

    def step(self, x):
        if self.value is not None:
            self.value += self.k * (x - self.value)
        else:
            self.warm.append(x)
            if len(self.warm) == self.span:
                self.value = sum(self.warm) / self.span
        return self.value


class Bot:
    def __init__(self):
        self.closes = []
        self.highs = []
        self.ranges = []
        self.ema_fast = Ema(EMA_FAST)
        self.ema_slow = Ema(EMA_SLOW)
        self.sma_sum = 0.0
        self.prev_gap = None
        self.last_cross_up = -10 ** 9
        self.prev_all_cond = False
        self.under_slow = 0
        # Wilder RSI state
        self.prev_close = None
        self.rsi_changes = []
        self.avg_gain = None
        self.avg_loss = None
        self.rsi = None
        # trade state
        self.entry_price = None
        self.entry_bar = -1
        self.peak_price = None
        self.last_exit_bar = -10 ** 9
        self.pending_buy = False
        self.adds_done = 0
        self.tps_done = 0
        self.pending_add = False
        self.peak_equity = None
        self.locked = False
        self.bar_no = -1

    # -- indicators ------------------------------------------------------
    def exposure_cap(self, equity):
        cap = MAX_EXPOSURE
        if VOL_ADJ and len(self.ranges) >= 20:
            vol = sum(self.ranges[-20:]) / 20
            if vol > 0:
                cap = MAX_EXPOSURE * min(1.0, max(VOL_FLOOR, VOL_REF / vol))
        return int(equity * cap)

    def step_indicators(self, bar):
        c = int(bar["c"])
        h = int(bar["h"])
        self.closes.append(c)
        self.highs.append(h)
        if c > 0:
            self.ranges.append((h - int(bar["l"])) / c)
        self.sma_sum += c
        if len(self.closes) > TREND_SMA:
            self.sma_sum -= self.closes[-TREND_SMA - 1]
        self.ema_fast.step(c)
        self.ema_slow.step(c)
        if self.ema_fast.value is not None and self.ema_slow.value is not None:
            gap = self.ema_fast.value - self.ema_slow.value
            if self.prev_gap is not None and self.prev_gap <= 0 < gap:
                self.last_cross_up = self.bar_no
            self.prev_gap = gap
        # RSI (Wilder)
        if self.prev_close is not None:
            ch = c - self.prev_close
            up = float(ch) if ch > 0 else 0.0
            dn = float(-ch) if ch < 0 else 0.0
            if self.avg_gain is None:
                self.rsi_changes.append((up, dn))
                if len(self.rsi_changes) == RSI_PERIOD:
                    self.avg_gain = sum(u for u, _ in self.rsi_changes) / RSI_PERIOD
                    self.avg_loss = sum(d for _, d in self.rsi_changes) / RSI_PERIOD
            else:
                self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) + up) / RSI_PERIOD
                self.avg_loss = (self.avg_loss * (RSI_PERIOD - 1) + dn) / RSI_PERIOD
            if self.avg_gain is not None:
                tot = self.avg_gain + self.avg_loss
                self.rsi = 50.0 if tot == 0 else 100.0 * self.avg_gain / tot
        self.prev_close = c

    def trend_sma(self):
        if len(self.closes) < TREND_SMA:
            return None
        return self.sma_sum / TREND_SMA

    def trend_sma_lagged(self, lag):
        n = len(self.closes)
        if n < TREND_SMA + lag:
            return None
        window = self.closes[n - TREND_SMA - lag:n - lag]
        return sum(window) / TREND_SMA

    # -- main decision ---------------------------------------------------
    def on_batch(self, state_cash, state_equity, positions, fills):
        for f in fills:
            if f.get("status") != "filled":
                if f.get("side") == "buy":
                    self.pending_buy = False
                    self.pending_add = False
                continue
            if f["side"] == "buy":
                fill_price = int(f["price"])
                fill_qty = int(f["qty"])
                if self.pending_add and self.entry_price is not None:
                    held_qty = positions.get(SYMBOL, (0, 0))[0]
                    old_qty = held_qty - fill_qty
                    if old_qty > 0:
                        self.entry_price = int(
                            (self.entry_price * old_qty + fill_price * fill_qty) / held_qty)
                    self.adds_done += 1
                    self.pending_add = False
                else:
                    self.pending_buy = False
                    self.entry_price = fill_price
                    self.entry_bar = self.bar_no
                    self.peak_price = fill_price
                    self.under_slow = 0
                    self.adds_done = 0
                    self.tps_done = 0
            elif f["side"] == "sell":
                held_after = positions.get(SYMBOL)
                if held_after is not None and held_after[0] > 0:
                    self.tps_done += 1
                if held_after is None or held_after[0] == 0:
                    self.entry_price = None
                    self.peak_price = None
                    self.last_exit_bar = self.bar_no
                # a partial sell (take-profit) keeps the entry reference
        held = positions.get(SYMBOL)
        close = self.closes[-1] if self.closes else None
        if close is None:
            return []
        if held is not None and self.peak_price is not None and close > self.peak_price:
            self.peak_price = close
        if self.peak_equity is None or state_equity > self.peak_equity:
            self.peak_equity = state_equity
        dd = 0.0
        if self.peak_equity:
            dd = 1.0 - state_equity / self.peak_equity
        if dd >= DD_HARD:
            self.locked = True
        ef, es, sma = self.ema_fast.value, self.ema_slow.value, self.trend_sma()
        # --- exits first ---
        if held is not None and self.entry_price is not None:
            if es is not None and close < es:
                self.under_slow += 1
            else:
                self.under_slow = 0
            held_bars = self.bar_no - self.entry_bar
            stop_hit = close <= self.entry_price * (1.0 - STOP_PCT)
            fail_hit = (
                FAIL_BARS > 0 and held_bars <= FAIL_BARS
                and close <= self.entry_price * (1.0 - FAIL_PCT)
            )
            trail_hit = (
                self.peak_price is not None
                and self.peak_price >= self.entry_price * (1.0 + TRAIL_ACT_PCT)
                and close <= self.peak_price * (1.0 - TRAIL_PCT)
            )
            in_profit_lock = (
                self.peak_price is not None
                and self.peak_price >= self.entry_price * (1.0 + PROFIT_LOCK_PCT)
            )
            ema_exit = (held_bars >= MIN_HOLD and self.under_slow >= EXIT_EMA_BARS
                        and not in_profit_lock)
            trend_exit = (
                held_bars >= MIN_HOLD and sma is not None and close < sma
                and ef is not None and es is not None and ef < es
                and not in_profit_lock
            )
            if stop_hit or fail_hit or trail_hit or ema_exit or trend_exit or self.locked:
                return [{"side": "sell", "symbol": SYMBOL, "qty": str(held[0]),
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            # partial take-profits: bank a third at each peak-gain step
            if (self.tps_done < len(TP_STEPS) and self.peak_price is not None
                    and self.peak_price >= self.entry_price * (1.0 + TP_STEPS[self.tps_done])):
                part = int(held[0] * TP_FRAC)
                if part > 0:
                    return [{"side": "sell", "symbol": SYMBOL, "qty": str(part),
                             "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            # pyramid add-ons: further tranches into a confirming trend
            if (self.adds_done < len(ADD_GAINS) and not self.pending_add
                    and self.bar_no - self.entry_bar >= ADD_DELAY
                    and close >= self.entry_price * (1.0 + ADD_GAINS[self.adds_done])
                    and ef is not None and es is not None and ef > es):
                frac = 0.5 if self.adds_done == 0 else SIZE_FRAC
                # position value at the latest close is equity minus cash
                room = self.exposure_cap(state_equity) - (state_equity - state_cash)
                notional = min(int(state_cash * frac), room)
                if notional >= 1_000_000:
                    self.pending_add = True
                    return [{"side": "buy", "symbol": SYMBOL,
                             "notional_quote_units": str(notional),
                             "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []
        if held is not None:
            return []
        # --- entries ---
        if self.locked or self.pending_buy:
            return []
        if dd >= DD_BREAKER:
            return []
        if self.bar_no - self.last_exit_bar < COOLDOWN:
            return []
        if sma is None or ef is None or es is None or self.rsi is None:
            return []
        sma_lag = self.trend_sma_lagged(SLOPE_BARS)
        all_cond = (
            close > sma and ef > es and RSI_MIN <= self.rsi <= RSI_MAX
            and sma_lag is not None and sma >= sma_lag * (1.0 + SLOPE_MIN_PCT)
            and ef - es >= close * GAP_MIN_PCT
            and not (CONFIRM_N > 0 and len(self.highs) > CONFIRM_N and close <= max(self.highs[-CONFIRM_N - 1:-1]) * (1.0 + BREAK_MARGIN_PCT))
        )
        if TREND_SMA2 > 0 and len(self.closes) >= TREND_SMA2:
            # second trend filter, applied only once enough history exists
            all_cond = all_cond and close > sum(self.closes[-TREND_SMA2:]) / TREND_SMA2
        if ROC_MIN_PCT > 0 and len(self.closes) > ROC_BARS:
            all_cond = all_cond and close >= self.closes[-ROC_BARS - 1] * (1.0 + ROC_MIN_PCT)
        edge = all_cond and not self.prev_all_cond
        self.prev_all_cond = all_cond
        if ENTRY_EDGE:
            if not edge:
                return []
        else:
            if not all_cond:
                return []
            strong_regime = sma >= sma_lag * (1.0 + REGIME_SLOPE_PCT)
            if not strong_regime and self.bar_no - self.last_cross_up > CROSS_WINDOW:
                return []
        notional = min(int(state_cash * INIT_FRAC), self.exposure_cap(state_equity))
        if notional < 1_000_000:
            return []
        self.pending_buy = True
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": str(notional),
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    bot = Bot()
    out = sys.stdout
    cid = [0]

    def send_orders(seq, orders):
        wire = []
        for o in orders:
            cid[0] += 1
            w = {"side": o["side"], "client_id": f"o{cid[0]}", "symbol": o["symbol"]}
            if o["side"] == "buy":
                w["notional_quote_units"] = o["notional_quote_units"]
            else:
                w["qty"] = o["qty"]
            w["max_slippage_bps"] = o["max_slippage_bps"]
            wire.append(w)
        out.write(json.dumps({"type": "reply", "seq": seq, "orders": wire},
                             separators=(",", ":")) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            continue
        if kind == "done":
            return
        if kind != "batch":
            continue
        bot.bar_no = msg["seq"]
        for bar in msg["bars"]:
            if bar["symbol"] == SYMBOL:
                bot.step_indicators(bar)
        account = msg["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        positions = {p["symbol"]: (int(p["qty"]), int(p["cost_quote_units"]))
                     for p in account["positions"]}
        orders = bot.on_batch(cash, equity, positions, msg.get("fills", []))
        send_orders(msg["seq"], orders)


if __name__ == "__main__":
    main()
