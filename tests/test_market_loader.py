import sys
import math
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
from simulation.market_loader import MarketLoader
import fxengine

market = MarketLoader("data/clean")

timestamp = pd.Timestamp("2026-09-30 16:00")
T = 91 / 365

# Everything below now comes from the CSVs
spot = market.get_spot(timestamp)
cny, usd = market.get_curves(timestamp)
vol = market.get_vol_quote(timestamp, "3M", "ATM")

rd = cny.zero_rate(T)
rf = usd.zero_rate(T)
forward = spot * usd.discount(T) / cny.discount(T)

pricer = fxengine.GKPricer(rd, rf)
call = pricer.price(spot, forward, T, vol, True)

print(f"snapshot:   {timestamp}")
print(f"spot:       {spot:.4f}           expect 6.7037")
print(f"3M ATM vol: {vol}            expect 0.022")
print(f"CNY 3M:     {rd:.6f}         expect 0.014472")
print(f"USD 3M:     {rf:.6f}         expect 0.041782")
print(f"3M forward: {forward:.5f}        expect 6.65821")
print(f"call price: {call.price:.6f}       expect 0.029073")
print()

# Run over every snapshot to make sure nothing is missing
times = market.snapshot_times()
failures = 0
for t in times:
    try:
        s = market.get_spot(t)
        c, u = market.get_curves(t)
        f = s * u.discount(T) / c.discount(T)
        if f >= s:
            print(f"{t}: forward {f:.4f} not below spot {s:.4f}")
            failures += 1
    except ValueError as e:
        print(f"{t}: {e}")
        failures += 1

print(f"checked {len(times)} snapshots, {failures} problems")