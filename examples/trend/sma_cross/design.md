# sma_cross: design

## 1. Idea

### 1.1 Core idea

A trend is under way when a short average of closing prices rises above a longer
one. The example uses that as its only signal.

## 2. Signals

Inputs are the closing prices of 1-hour bars. Let `gap = SMA(fast) - SMA(slow)`.

### 2.1 Entry

**Long:** on the bar where `gap` turns from `<= 0` to `> 0`.

**Short:** none; the example trades long only.

### 2.2 Exit

On the bar where `gap` turns from `>= 0` to `< 0`. Stop loss and take profit come
from the toolkit defaults (section 4).

### 2.3 Filters

None; the toolkit's filters stay off.

## 3. Position size

### 3.1 Per trade

`config.yaml` has no `position` section, so each position is the toolkit default
of 10% of equity.

### 3.2 Scaling in and position limits

The strategy holds at most one position, which makes its largest exposure 10% of
equity.

## 4. Risk

### 4.1 Stop loss

The toolkit default, an ATR-based stop.

### 4.2 Take profit

The toolkit default, an ATR-based target.

### 4.4 Exposure ceiling

On the `10000 USDT` sandbox account the largest exposure is `1000 USDT` at entry,
and `run.yaml` sets `risk_config.max_total_notional` to the same `1000`.

## 6. Parameters

| Parameter | Default | Plausible range | Sensitivity | Notes |
|---|---|---|---|---|
| `fast_period` | 10 | 5 to 20 | not measured | |
| `slow_period` | 30 | 20 to 100 | not measured | always longer than `fast_period` |

## 9. Where it works and where it does not

**Works in:** long, steady trends.

**Fails in:** sideways markets, where the averages cross back and forth.
