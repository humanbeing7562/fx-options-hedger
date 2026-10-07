"""
Checks for the C++ SmileVol (vanna-volga + total-variance blending).
"""
import sys
import math
sys.path.insert(0, "build")

import fxengine
from simulation.market_loader import MarketLoader

market = MarketLoader()
problems = 0

t = "2026-09-30 16:00"
smile = market.get_smile_vol(t)
tenors = smile.tenors()
tenor_names = list(market.get_pillar_quotes(t).keys())

# ---- 1. Strikes built inside C++ match the saved reference values ----
REFERENCE_3M = {"k_10p": 6.55478, "k_25p": 6.60898, "k_atm": 6.65861, "k_25c": 6.71093, "k_10c": 6.76974}
s3m = tenors[tenor_names.index("3M")]
print("3M strikes from SmileVol:")
for name in REFERENCE_3M:
    K = getattr(s3m, name)
    ok = abs(K - REFERENCE_3M[name]) < 1e-5
    if not ok:
        problems += 1
    print(f"  {name}: {K:.5f}  expect {REFERENCE_3M[name]:.5f}  {'ok' if ok else 'MISMATCH'}")
print()

# ---- 2. At each tenor's own T, the smile passes exactly through 25P / ATM / 25C ----
worst = 0.0
for s in tenors:
    pairs = [(s.k_25p, s.vol_25p), (s.k_atm, s.vol_atm), (s.k_25c, s.vol_25c)]
    for K, quoted in pairs:
        error = abs(smile.get_vol(K, s.T) - quoted)
        if error > worst:
            worst = error
if worst > 1e-12:
    problems += 1
print(f"passes through 25P/ATM/25C: max error {worst:.2e}")

# ---- 3. 10D dots: VV doesn't use them, so show how far off it lands ----
print("\n10D pillars vs vanna-volga (not fitted, for information):")
for name in ["1M", "3M", "1Y"]:
    s = tenors[tenor_names.index(name)]
    vv_10p = smile.get_vol(s.k_10p, s.T)
    vv_10c = smile.get_vol(s.k_10c, s.T)
    print(f"  {name}: 10P quote {s.vol_10p*100:.3f}% vs VV {vv_10p*100:.3f}%   "
          f"10C quote {s.vol_10c*100:.3f}% vs VV {vv_10c*100:.3f}%")

# ---- 4. Between tenors: total-variance blend, checked by hand ----
one_m = tenors[tenor_names.index("1M")]
two_m = tenors[tenor_names.index("2M")]
K = 6.68
T = 47 / 365
v1 = smile.get_vol(K, one_m.T)
v2 = smile.get_vol(K, two_m.T)
weight = (T - one_m.T) / (two_m.T - one_m.T)
var = v1 * v1 * one_m.T + weight * (v2 * v2 * two_m.T - v1 * v1 * one_m.T)
by_hand = math.sqrt(var / T)
from_cpp = smile.get_vol(K, T)
ok = abs(by_hand - from_cpp) < 1e-14
if not ok:
    problems += 1
print(f"\n47-day vol at K={K}: C++ {from_cpp*100:.4f}%  by hand {by_hand*100:.4f}%  "
      f"(1M {v1*100:.4f}%, 2M {v2*100:.4f}%)  {'ok' if ok else 'MISMATCH'}")

# ---- 5. Every snapshot builds, and vols on a strike grid are sensible ----
bad = 0
times = market.snapshot_times()
for ts in times:
    surface = market.get_smile_vol(ts)
    for s in surface.tenors():
        low = s.k_10p * 0.99
        high = s.k_10c * 1.01
        for i in range(21):
            K = low + (high - low) * i / 20
            v = surface.get_vol(K, s.T)
            if v <= 0.001 or v > 0.20:
                bad += 1
if bad > 0:
    problems += 1
print(f"\nall {len(times)} snapshots built; out-of-range vols on strike grid: {bad}")

# ---- 6. Pricing a non-pillar option with the smile ----
quotes = market.get_pillar_quotes(t)
q = quotes["3M"]
pricer = fxengine.GKPricer(q["rd"], q["rf"])
spot = market.get_spot(t)
vol = smile.get_vol(6.70, q["T"])
result = pricer.price(spot, 6.70, q["T"], vol, True)
print(f"\n3M call at 6.70: smile vol {vol*100:.4f}%  price {result.price:.6f}  delta {result.delta:.4f}")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")