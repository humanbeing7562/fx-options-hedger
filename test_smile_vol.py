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

# ---- 4. Between tenors: total variance at the same forward-moneyness, checked by hand ----
one_m = tenors[tenor_names.index("1M")]
two_m = tenors[tenor_names.index("2M")]
i1 = tenor_names.index("1M")
i2 = tenor_names.index("2M")
K = 6.68
T = 47 / 365
# forward at 47 days: straight line in log-forward between the 1M and 2M forwards
fw = (T - one_m.T) / (two_m.T - one_m.T)
F_T = math.exp(math.log(one_m.forward) + fw * (math.log(two_m.forward) - math.log(one_m.forward)))
x = K / F_T
# each tenor's own smile at that moneyness, then the calendar floor (running max over tenors)
floor = 0.0
variances = []
for idx in range(len(tenors)):
    s = tenors[idx]
    v = smile.tenor_vol(idx, x * s.forward)
    floor = max(floor, v * v * s.T)
    variances.append(floor)
var = variances[i1] + fw * (variances[i2] - variances[i1])
by_hand = math.sqrt(var / T)
from_cpp = smile.get_vol(K, T)
ok = abs(by_hand - from_cpp) < 1e-14 and abs(smile.forward(T) - F_T) < 1e-12
if not ok:
    problems += 1
print(f"\n47-day vol at K={K}: C++ {from_cpp*100:.4f}%  by hand {by_hand*100:.4f}%  "
      f"(forward {F_T:.5f}, moneyness {x:.5f})  {'ok' if ok else 'MISMATCH'}")

# The raw tenor smiles still pass exactly through the quotes; the calendar floor only
# lifts points where the quotes themselves would let total variance fall with maturity.
moved = 0
largest_move = 0.0
for ts in market.snapshot_times():
    surface = market.get_smile_vol(ts)
    for idx in range(len(surface.tenors())):
        s = surface.tenors()[idx]
        for K_p, q in [(s.k_25p, s.vol_25p), (s.k_atm, s.vol_atm), (s.k_25c, s.vol_25c)]:
            if abs(surface.tenor_vol(idx, K_p) - q) > 1e-12:
                problems += 1
            move = abs(surface.get_vol(K_p, s.T) - q)
            if move > 1e-12:
                moved += 1
                largest_move = max(largest_move, move)
print(f"   quoted 25P/ATM/25C points lifted by the calendar floor: {moved} of "
      f"{len(market.snapshot_times()) * 27}, largest {largest_move*100:.3f} vol")

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

# ---- 7. Wings and arbitrage, every snapshot and tenor ----
# The default surface is the hybrid: VV inside 25D, SABR in the wings, a cubic bridge
# between. SABR wings keep rising far out, so the wing check is Lee's moment bound -
# total variance must grow slower than 2 x |log-moneyness| - rather than a fixed vol cap.
from statistics import NormalDist
_N = NormalDist().cdf


def undiscounted_call(F, K, T, vol):
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return F * _N(d1) - K * _N(d1 - sd)


join_jump = 0.0
join_kink = 0.0
highest_wing = 0.0
lee_worst = 0.0
butterfly_bad = 0
calendar_bad = 0
smiles_checked = 0
for ts in market.snapshot_times():
    surface = market.get_smile_vol(ts)
    tenors = surface.tenors()
    for s in tenors:
        F = s.forward
        T = s.T
        smiles_checked += 1
        # (a) smooth join: same vol and same slope just inside and just outside
        for k in [s.k_10p, s.k_10c]:
            e = 1e-7
            inside = surface.get_vol(k * math.exp(-e if k == s.k_10c else e), T)
            outside = surface.get_vol(k * math.exp(e if k == s.k_10c else -e), T)
            at = surface.get_vol(k, T)
            join_jump = max(join_jump, abs(outside - at), abs(inside - at))
            slope_in = abs(at - inside) / e
            slope_out = abs(outside - at) / e
            join_kink = max(join_kink, abs(slope_in - slope_out))
        # (b) bounded far out
        for mult in [0.5, 0.8, 1.25, 2.0]:
            v = surface.get_vol(F * mult, T)
            highest_wing = max(highest_wing, v)
            lee_worst = max(lee_worst, v * v * T / abs(math.log(mult)))
        # (c) butterfly: call prices convex in strike (fine grid near the joins + wide grid)
        points = []
        for k in [s.k_10p, s.k_10c]:
            step = 0.25 * surface.get_vol(k, T) * math.sqrt(T) / 100
            for n in range(-200, 201):
                points.append(k * math.exp(n * step))
        for n in range(601):
            points.append(F * (0.85 + 0.30 * n / 600))
        points.sort()
        prices = []
        for K in points:
            prices.append(undiscounted_call(F, K, T, surface.get_vol(K, T)))
        for n in range(1, len(points) - 1):
            a, b, c = points[n - 1], points[n], points[n + 1]
            if b - a < 1e-9 or c - b < 1e-9:
                continue
            second = 2 * ((prices[n + 1] - prices[n]) / (c - b) - (prices[n] - prices[n - 1]) / (b - a)) / (c - a)
            if second < -1e-6:
                butterfly_bad += 1
                break
    # (d) calendar: total variance at the same forward-moneyness grows with T
    for x in [0.95, 0.98, 0.99, 1.0, 1.01, 1.02, 1.05]:
        previous = 0.0
        for s in tenors:
            w = surface.get_vol(s.forward * x, s.T) ** 2 * s.T
            if w < previous - 1e-12:
                calendar_bad += 1
            previous = w

if join_jump > 1e-6 or join_kink > 1e-3 or lee_worst >= 2.0 or butterfly_bad > 0 or calendar_bad > 0:
    problems += 1
print(f"\n7. {smiles_checked} smiles: join jump {join_jump:.1e}, slope change at join {join_kink:.1e}; "
      f"highest vol out to F x 0.5 / 2.0 {highest_wing*100:.2f}%, "
      f"wing total variance / |log-moneyness| at most {lee_worst:.4f} (Lee bound: < 2)")
print(f"   butterfly arbitrage in {butterfly_bad} smiles; calendar arbitrage in {calendar_bad} checks")

# ---- 8. The hybrid picks VV or SABR by strike ----
# VV inside 25D (exact at 25P / ATM / 25C), SABR past the bridge end (10D unless that
# would make negative density), a cubic bridge between them.
hybrid_bad = 0
miss_10d = []
ends = {}
for ts in market.snapshot_times()[::10]:
    hybrid = market.get_smile_vol(ts)
    vv_only = market.get_smile_vol(ts, model="vv")
    sabr_only = market.get_smile_vol(ts, model="sabr")
    if hybrid.model() != "hybrid":
        hybrid_bad += 1
    for i in range(len(hybrid.tenors())):
        s = hybrid.tenors()[i]
        # inside 25D: identical to VV, weight 0
        for K in [s.k_25p, 0.5 * (s.k_25p + s.k_atm), s.k_atm, 0.5 * (s.k_atm + s.k_25c), s.k_25c]:
            if hybrid.tenor_vol(i, K) != vv_only.tenor_vol(i, K) or hybrid.sabr_weight(i, K) != 0.0:
                hybrid_bad += 1
        # past the bridge ends: identical to SABR, weight 1
        for K in [s.k_put_end * 0.999, s.k_put_end * 0.99, s.k_call_end * 1.001, s.k_call_end * 1.01]:
            if hybrid.tenor_vol(i, K) != sabr_only.tenor_vol(i, K) or hybrid.sabr_weight(i, K) != 1.0:
                hybrid_bad += 1
        # bridge ends at or beyond 10D, and passed the density check
        if s.k_put_end > s.k_10p * (1 + 1e-12) or s.k_call_end < s.k_10c * (1 - 1e-12) or not s.arbitrage_free:
            hybrid_bad += 1
        for d in [s.put_end_delta, s.call_end_delta]:
            ends[d] = ends.get(d, 0) + 1
        miss_10d.append(abs(hybrid.tenor_vol(i, s.k_10p) - s.vol_10p))
        miss_10d.append(abs(hybrid.tenor_vol(i, s.k_10c) - s.vol_10c))
miss_10d.sort()
median_miss = miss_10d[len(miss_10d) // 2]
if hybrid_bad > 0 or median_miss > 0.0005:
    problems += 1
ends_text = []
for d in sorted(ends, reverse=True):
    ends_text.append(f"{d*100:.0f}D: {ends[d]}")
print(f"\n8. hybrid: VV inside 25D, SABR past the bridge, mismatches {hybrid_bad}; "
      f"10D quotes missed by {median_miss*100:.3f} vol (median)")
print(f"   bridge ends: {', '.join(ends_text)}")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")