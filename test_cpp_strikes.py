import sys
import math
sys.path.insert(0, "build")

import fxengine
from simulation.market_loader import MarketLoader

PILLAR_ORDER = ["10P", "25P", "ATM", "25C", "10C"]
PILLAR_DELTAS = {"10P": -0.10, "25P": -0.25, "25C": 0.25, "10C": 0.10}

# Saved from the Python prototype (spot delta, DNS ATM), 30 Sep 2026 16:00
REFERENCE = {
    "1M": {"10P": 6.64317, "25P": 6.66504, "ATM": 6.68963, "25C": 6.71661, "10C": 6.74388},
    "3M": {"10P": 6.55478, "25P": 6.60898, "ATM": 6.65861, "25C": 6.71093, "10C": 6.76974},
    "1Y": {"10P": 6.22312, "25P": 6.37293, "ATM": 6.50879, "25C": 6.65052, "10C": 6.81843},
}


def pillar_strikes(spot, quote):
    # Temporary glue: this loop moves inside the C++ SmileVol constructor later
    T = quote["T"]
    rf = quote["rf"]
    pricer = fxengine.GKPricer(quote["rd"], rf)

    strikes = {}
    for pillar in PILLAR_ORDER:
        flat = fxengine.FlatVol(quote["vols"][pillar])
        if pillar == "ATM":
            # DNS: d1 = 0, so the call's spot delta is 0.5 * e^(-rf*T)
            K = pricer.strike_from_delta(spot, T, flat, 0.5 * math.exp(-rf * T), True)
        else:
            delta = PILLAR_DELTAS[pillar]
            K = pricer.strike_from_delta(spot, T, flat, delta, delta > 0)
        strikes[pillar] = K
    return strikes


market = MarketLoader("data/clean")
problems = 0

# ---- 1. Reference values ----
t = "2026-09-30 16:00"
spot = market.get_spot(t)
quotes = market.get_pillar_quotes(t)

for tenor in REFERENCE:
    strikes = pillar_strikes(spot, quotes[tenor])
    print(f"{tenor}  forward {quotes[tenor]['forward']:.5f}")
    for pillar in PILLAR_ORDER:
        K = strikes[pillar]
        expected = REFERENCE[tenor][pillar]
        ok = abs(K - expected) < 1e-5
        if not ok:
            problems += 1
        print(f"  {pillar:>4}  {K:.5f}  expect {expected:.5f}  {'ok' if ok else 'MISMATCH'}")
print()

# ---- 2. Delta round trip, all tenors at 30 Sep 16:00 ----
worst = 0.0
for tenor in quotes:
    q = quotes[tenor]
    pricer = fxengine.GKPricer(q["rd"], q["rf"])
    strikes = pillar_strikes(spot, q)
    for pillar in PILLAR_ORDER:
        K = strikes[pillar]
        vol = q["vols"][pillar]
        if pillar == "ATM":
            call = pricer.price(spot, K, q["T"], vol, True)
            put = pricer.price(spot, K, q["T"], vol, False)
            error = abs(call.delta + put.delta)
        else:
            target = PILLAR_DELTAS[pillar]
            result = pricer.price(spot, K, q["T"], vol, target > 0)
            error = abs(result.delta - target)
        if error > worst:
            worst = error
if worst > 1e-8:
    problems += 1
print(f"delta round trip: max error {worst:.2e}")

# ---- 3. Strike ordering, every snapshot ----
order_problems = 0
times = market.snapshot_times()
for t in times:
    spot = market.get_spot(t)
    quotes = market.get_pillar_quotes(t)
    for tenor in quotes:
        strikes = pillar_strikes(spot, quotes[tenor])
        for i in range(1, len(PILLAR_ORDER)):
            if strikes[PILLAR_ORDER[i]] <= strikes[PILLAR_ORDER[i - 1]]:
                print(f"{t} {tenor}: {PILLAR_ORDER[i]} not above {PILLAR_ORDER[i - 1]}")
                order_problems += 1
                break
problems += order_problems
print(f"strike ordering: {order_problems} problems across {len(times)} snapshots")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")