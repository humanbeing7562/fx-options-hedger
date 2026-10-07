import sys
sys.path.insert(0, "build")

import pandas as pd
from simulation.market_loader import MarketLoader

market = MarketLoader("data/clean")
timestamp = pd.Timestamp("2026-09-30 16:00")
spot = market.get_spot(timestamp)

# Single forward
f_3m = market.get_forward(timestamp, 91 / 365)
print(f"3M forward: {f_3m:.5f}   expect 6.65821")
print()

# All tenors
forwards = market.get_forwards(timestamp)
print(f"spot {spot:.4f}")
print("tenor   forward    points (pips)")
for tenor in forwards:
    f = forwards[tenor]
    points = (f - spot) * 10000
    print(f"{tenor:>4}   {f:.5f}   {points:8.1f}")
print()

# Forward points should get more negative as the tenor gets longer
problems = 0
previous = spot
for tenor in forwards:
    if forwards[tenor] >= previous:
        print(f"{tenor}: forward not below the shorter tenor")
        problems += 1
    previous = forwards[tenor]

# Every snapshot should give 9 forwards, all below spot
for t in market.snapshot_times():
    fwd = market.get_forwards(t)
    s = market.get_spot(t)
    if len(fwd) != 9:
        print(f"{t}: {len(fwd)} tenors, expected 9")
        problems += 1
    for tenor in fwd:
        if fwd[tenor] >= s:
            print(f"{t} {tenor}: forward above spot")
            problems += 1

print(f"{problems} problems across {len(market.snapshot_times())} snapshots")