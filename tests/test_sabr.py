"""
Checks for SABR. The formula and the fitting are QuantLib's (C++);
these tests check that we use them correctly and that the fits are good.
"""
import sys
import math
import time
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import fxengine
from simulation.market_loader import MarketLoader

PILLAR_ORDER = ["10P", "25P", "ATM", "25C", "10C"]
TENOR_NAMES = ["1W", "2W", "3W", "1M", "2M", "3M", "6M", "9M", "1Y"]
BAND = 0.0005        # half a 0.1-vol tick, so mid-only quotes can still be "inside" bid/ask

problems = 0


def hagan_reference(F, K, T, alpha, beta, rho, nu):
    # Independent pure-Python Hagan (2002) lognormal SABR, only for checking
    omb = 1.0 - beta
    log_fk = math.log(F / K)
    fk = (F * K) ** (omb / 2.0)
    denom = fk * (1 + omb ** 2 / 24 * log_fk ** 2 + omb ** 4 / 1920 * log_fk ** 4)
    z = nu / alpha * fk * log_fk
    if abs(z) < 1e-4:
        zx = 1.0 - 0.5 * rho * z + (2 - 3 * rho * rho) * z * z / 12
    else:
        x = math.log((math.sqrt(1 - 2 * rho * z + z * z) + z - rho) / (1 - rho))
        zx = z / x
    corr = 1 + (omb ** 2 / 24 * alpha ** 2 / fk ** 2 + 0.25 * rho * beta * nu * alpha / fk
                + (2 - 3 * rho ** 2) / 24 * nu ** 2) * T
    return alpha / denom * zx * corr


try:
    import QuantLib as ql
except ImportError:
    ql = None

# ---- 1. We call QuantLib's SABR formula correctly (argument order, units) ----
cases = [
    (6.65, 0.25, 0.022, 1.0, 0.3, 0.8),
    (6.50, 1.00, 0.031, 1.0, 0.25, 0.5),
    (6.69, 0.08, 0.019, 1.0, -0.2, 2.0),
    (6.65, 0.25, 0.050, 0.5, -0.4, 0.6),
]
worst = 0.0
for F, T, a, b, r, n in cases:
    smile = fxengine.SabrSmile(F, T, a, b, r, n)
    for i in range(41):
        K = F * (0.85 + 0.30 * i / 40)
        worst = max(worst, abs(smile.vol(K) - hagan_reference(F, K, T, a, b, r, n)))
if worst > 1e-10:
    problems += 1
print(f"1. SABR formula vs independent Hagan: max diff {worst:.1e}")

# ---- 2. Bad inputs are rejected ----
rejected = 0
bad_inputs = [(6.65, 0.25, 0.0, 1.0, 0.3, 0.8),     # alpha = 0
              (6.65, 0.25, 0.02, 1.0, 1.0, 0.8),    # rho = 1
              (6.65, 0.25, 0.02, 1.5, 0.3, 0.8),    # beta > 1
              (6.65, 0.25, 0.02, 1.0, 0.3, -0.1)]   # nu < 0
for args in bad_inputs:
    try:
        fxengine.SabrSmile(*args)
    except ValueError:
        rejected += 1
market = MarketLoader()
try:
    market.get_smile_vol("2026-09-30 16:00", model="sarb")
except ValueError:
    rejected += 1
if rejected != len(bad_inputs) + 1:
    problems += 1
print(f"2. bad inputs rejected: {rejected} / {len(bad_inputs) + 1} (incl. a typo'd model name)")

# ---- 3. Our fitting wiring matches QuantLib-Python's own fitter ----
t_check = "2026-09-30 16:00"
if ql is not None:
    surface = market.get_smile_vol(t_check, model="sabr")
    worst_param = 0.0
    for s in surface.tenors():
        strikes = [s.k_10p, s.k_25p, s.k_atm, s.k_25c, s.k_10c]
        vols = [s.vol_10p, s.vol_25p, s.vol_atm, s.vol_25c, s.vol_10c]
        start_rho = 0.0
        if s.vol_25c > s.vol_25p:
            start_rho = 0.2
        elif s.vol_25c < s.vol_25p:
            start_rho = -0.2
        theirs = ql.SABRInterpolation(strikes, vols, s.T, s.forward,
                                      s.vol_atm, 1.0, 1.0, start_rho,   # alpha, beta, nu, rho
                                      False, True, False, False, True)
        theirs(s.k_atm, True)                                           # triggers the fit
        diffs = [s.sabr.alpha - theirs.alpha(), s.sabr.rho - theirs.rho(), s.sabr.nu - theirs.nu()]
        for d in diffs:
            worst_param = max(worst_param, abs(d))
    if worst_param > 1e-8:
        problems += 1
    print(f"3. fit inside SmileVol vs QuantLib-Python fitter: max parameter diff {worst_param:.1e}")
else:
    print("3. (QuantLib-Python not installed, skipped)")

# ---- 4. Every snapshot: SABR surface builds, converges, and fits well ----
bid_ask = {}
for i in range(len(market.vols)):
    key = (market.vols["timestamp"].iloc[i], market.vols["tenor"].iloc[i], market.vols["pillar"].iloc[i])
    bid_ask[key] = (float(market.vols["bid"].iloc[i]), float(market.vols["ask"].iloc[i]))

stats = {}
for name in TENOR_NAMES:
    stats[name] = {"rms": [], "worst_core": 0.0, "worst_any": 0.0, "outside": 0}

not_converged = 0
bad_grid = 0
times = market.snapshot_times()
started = time.time()
for t in times:
    surface = market.get_smile_vol(t, model="sabr")
    tenors = surface.tenors()
    for j in range(len(tenors)):
        s = tenors[j]
        name = TENOR_NAMES[j]
        if not s.sabr.converged:
            not_converged += 1

        strikes = [s.k_10p, s.k_25p, s.k_atm, s.k_25c, s.k_10c]
        quoted = [s.vol_10p, s.vol_25p, s.vol_atm, s.vol_25c, s.vol_10c]
        sum_sq = 0.0
        outside_here = 0
        for i in range(5):
            fitted = surface.get_vol(strikes[i], s.T)
            error = fitted - quoted[i]
            sum_sq += error * error
            stats[name]["worst_any"] = max(stats[name]["worst_any"], abs(error))
            if PILLAR_ORDER[i] in ["25P", "ATM", "25C"]:
                stats[name]["worst_core"] = max(stats[name]["worst_core"], abs(error))
            bid, ask = bid_ask[(t, name, PILLAR_ORDER[i])]
            if fitted < bid - BAND or fitted > ask + BAND:
                outside_here += 1
        stats[name]["rms"].append((sum_sq / 5) ** 0.5)
        if outside_here > 0:
            stats[name]["outside"] += 1

        low = s.k_10p * 0.99
        high = s.k_10c * 1.01
        for i in range(21):
            v = surface.get_vol(low + (high - low) * i / 20, s.T)
            if v <= 0.001 or v > 0.20:
                bad_grid += 1
elapsed = time.time() - started

if not_converged > 0 or bad_grid > 0:
    problems += 1
print(f"4. built {len(times)} SABR surfaces in {elapsed:.1f}s; "
      f"not converged: {not_converged}; out-of-range vols on grid: {bad_grid}")
print("   tenor   median rms   worst 25D/ATM   worst any   smiles with a pillar outside bid/ask")
for name in TENOR_NAMES:
    rms_sorted = sorted(stats[name]["rms"])
    median = rms_sorted[len(rms_sorted) // 2]
    print(f"   {name:>4}    {median*100:8.3f}    {stats[name]['worst_core']*100:10.3f}"
          f"    {stats[name]['worst_any']*100:8.3f}    {stats[name]['outside']:5d} / {len(times)}")

# ---- 5. Quotes vs VV vs SABR, 30 Sep 16:00 ----
vv = market.get_smile_vol(t_check, model="vv")
sabr = market.get_smile_vol(t_check, model="sabr")
print("\n5. 30 Sep 16:00 - quote / VV / SABR (vol %):")
for name in ["1M", "3M", "1Y"]:
    s = vv.tenors()[TENOR_NAMES.index(name)]
    print(f"   {name}")
    pillars = [("10P", s.k_10p, s.vol_10p), ("25P", s.k_25p, s.vol_25p), ("ATM", s.k_atm, s.vol_atm),
               ("25C", s.k_25c, s.vol_25c), ("10C", s.k_10c, s.vol_10c)]
    for label, K, q in pillars:
        print(f"     {label:>4}  {q*100:.3f}   {vv.get_vol(K, s.T)*100:.3f}   {sabr.get_vol(K, s.T)*100:.3f}")

# ---- 6. Same option, both models ----
quotes = market.get_pillar_quotes(t_check)
spot = market.get_spot(t_check)
print("\n6. pricing (30 Sep 16:00):")
for label, tenor, K in [("3M call 6.70", "3M", 6.70), ("1Y 10D call", "1Y", vv.tenors()[8].k_10c)]:
    q = quotes[tenor]
    pricer = fxengine.GKPricer(q["rd"], q["rf"])
    for model_name, surface in [("VV", vv), ("SABR", sabr)]:
        vol = surface.get_vol(K, q["T"])
        r = pricer.price(spot, K, q["T"], vol, True)
        print(f"   {label:<14} {model_name:<5} vol {vol*100:.3f}%  price {r.price:.6f}")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")