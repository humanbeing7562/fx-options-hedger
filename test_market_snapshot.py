"""
Checks for MarketSnapshot and the Portfolio that now prices off it.
"""
import sys
sys.path.insert(0, "build")

import pandas as pd
import fxengine
from simulation.market_loader import MarketLoader, years_between

market = MarketLoader()
problems = 0

start = pd.Timestamp("2026-09-30 16:00")
snap = market.get_snapshot(start, sim_start=start)          # time = 0
cny, usd = market.get_curves(start)
smile = market.get_smile_vol(start)
spot = market.get_spot(start)

# ---- 1. Snapshot gives the same numbers as the pieces it bundles ----
worst = 0.0
for days in [5, 7, 30, 47, 91, 200, 365]:
    T = days / 365
    diffs = [
        snap.domestic_rate(T) - cny.zero_rate(T),
        snap.foreign_rate(T) - usd.zero_rate(T),
        snap.forward(T) - market.get_forward(start, T),
        snap.vol(6.70, T) - smile.get_vol(6.70, T),
    ]
    for d in diffs:
        worst = max(worst, abs(d))
if worst > 1e-14:
    problems += 1
print(f"1. snapshot matches curves / loader forward / smile: max diff {worst:.1e}")

# ---- 2. Snapshot price = GKPricer built by hand with that T's rates ----
T = 91 / 365
by_hand = fxengine.GKPricer(cny.zero_rate(T), usd.zero_rate(T)).price(
    spot, 6.70, T, smile.get_vol(6.70, T), True)
from_snap = snap.price(6.70, T, True)
diff = abs(from_snap.price - by_hand.price) + abs(from_snap.delta - by_hand.delta)
if diff > 1e-14:
    problems += 1
print(f"2. 3M call 6.70: snapshot {from_snap.price:.6f}, by hand {by_hand.price:.6f}  (expect 0.014107)")

# ---- 3. Portfolio: each position uses ITS OWN rates ----
book = fxengine.Portfolio()
positions = [
    (7 / 365, 6.70, True, -10e6),       # short 10m 1W call
    (91 / 365, 6.65, False, 5e6),       # long 5m 3M put
    (365 / 365, 6.60, True, -20e6),     # short 20m 1Y call
]
for expiry, strike, is_call, notional in positions:
    p = fxengine.Position()
    p.expiry = expiry
    p.strike = strike
    p.is_call = is_call
    p.notional = notional
    book.add_option(p)

total = book.aggregate_greeks(snap)
expected_price = 0.0
expected_delta = 0.0
for expiry, strike, is_call, notional in positions:
    r = snap.price(strike, expiry, is_call)
    expected_price += r.price * notional
    expected_delta += r.delta * notional
diff = abs(total.price - expected_price) + abs(total.delta - expected_delta)
if diff > 1e-6:
    problems += 1
print(f"3. book value {total.price:,.0f} CNY, delta {total.delta:,.0f} USD  "
      f"(sum of positions: {expected_price:,.0f} / {expected_delta:,.0f})")

# What the OLD design would have done: one pricer with the 3M rates for everything
old_pricer = fxengine.GKPricer(cny.zero_rate(91 / 365), usd.zero_rate(91 / 365))
old_value = 0.0
for expiry, strike, is_call, notional in positions:
    r = old_pricer.price(spot, strike, expiry, smile.get_vol(strike, expiry), is_call)
    old_value += r.price * notional
print(f"   old one-pricer design would say {old_value:,.0f} CNY  "
      f"(off by {old_value - total.price:,.0f})")

# ---- 4. Buckets add up to the total ----
buckets = book.greeks_by_expiry(snap)
bucket_price = 0.0
for b in buckets:
    bucket_price += buckets[b].price
if abs(bucket_price - total.price) > 1e-6 or len(buckets) != 3:
    problems += 1
print(f"4. {len(buckets)} expiry buckets, sum {bucket_price:,.0f} = total {total.price:,.0f}")

# ---- 5. Time moves on: a week later the 1W option has expired and is skipped ----
later = pd.Timestamp("2026-09-30 16:00")
start_early = pd.Timestamp("2026-09-22 10:00")
snap_week = market.get_snapshot(later, sim_start=start_early)
print(f"5. snapshot 30 Sep 16:00 with sim start 22 Sep 10:00: time = {snap_week.time()*365:.2f} days")
book2 = fxengine.Portfolio()
p = fxengine.Position()
p.expiry = 7 / 365          # expired relative to this snapshot's clock
p.strike = 6.70
p.is_call = True
p.notional = -10e6
book2.add_option(p)
if book2.aggregate_greeks(snap_week).price != 0.0:
    problems += 1
print(f"   expired option skipped: value {book2.aggregate_greeks(snap_week).price}")

try:
    snap.price(6.70, -0.01, True)
    problems += 1
except ValueError:
    print("   pricing with negative T rejected")

# ---- 6. Every fixing: snapshot builds and prices the book ----
times = market.snapshot_times()
first = times[0]
bad = 0
for t in times:
    s = market.get_snapshot(t, sim_start=first)
    if abs(s.time() - years_between(first, t)) > 1e-15:
        bad += 1
    shifted = fxengine.Portfolio()
    for expiry, strike, is_call, notional in positions:
        p = fxengine.Position()
        p.expiry = s.time() + expiry      # always live
        p.strike = strike
        p.is_call = is_call
        p.notional = notional
        shifted.add_option(p)
    g = shifted.aggregate_greeks(s)
    if g.price != g.price:                # NaN check
        bad += 1
if bad > 0:
    problems += 1
print(f"6. {len(times)} snapshots built and priced, problems: {bad}")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")