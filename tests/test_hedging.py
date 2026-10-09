"""
Checks for vanna / volga and smile hedging with ATM, RR and BF packages.
"""
import sys
import json
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
import fxengine
from simulation.market_loader import MarketLoader
from simulation.book import Book
from simulation.hedging import (build_package, package_greeks, recommend_smile_hedge,
                         recommend_bucketed_hedge, bucket_of, CPP_BUCKET_KEY)

market = MarketLoader()
problems = 0


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


start = pd.Timestamp("2026-09-24 10:00")
snap = market.get_snapshot(start, sim_start=start)
spot = snap.spot()

# ---- 1. Closed-form vanna / volga match bump-and-reprice ----
pricer = fxengine.GKPricer(0.0145, 0.0418)
worst_vanna = 0.0
worst_volga = 0.0
for strike in [6.55, 6.65, 6.72, 6.80]:
    for is_call in [True, False]:
        for T in [7 / 365, 91 / 365, 1.0]:
            vol = 0.025
            base = pricer.price(spot, strike, T, vol, is_call)
            dv = 1e-5
            dS = 1e-4
            up_vol = pricer.price(spot, strike, T, vol + dv, is_call)
            down_vol = pricer.price(spot, strike, T, vol - dv, is_call)
            up_spot = pricer.price(spot + dS, strike, T, vol, is_call)
            down_spot = pricer.price(spot - dS, strike, T, vol, is_call)
            vanna_from_delta = (up_vol.delta - down_vol.delta) / (2 * dv)       # d delta / d vol
            vanna_from_vega = (up_spot.vega - down_spot.vega) / (2 * dS)        # d vega / d spot
            volga_bumped = (up_vol.vega - down_vol.vega) / (2 * dv)             # d vega / d vol
            scale_vanna = max(abs(base.vanna), 1e-3)
            scale_volga = max(abs(base.volga), 1e-3)
            worst_vanna = max(worst_vanna, abs(base.vanna - vanna_from_delta) / scale_vanna,
                              abs(base.vanna - vanna_from_vega) / scale_vanna)
            worst_volga = max(worst_volga, abs(base.volga - volga_bumped) / scale_volga)
check(worst_vanna < 1e-4 and worst_volga < 1e-4, "closed-form vanna/volga should match bumps")
print(f"1. vanna/volga vs bump-and-reprice: worst relative error vanna {worst_vanna:.1e}, "
      f"volga {worst_volga:.1e}")

# ---- 2. Strikes from delta on the smile reproduce the market pillars ----
smile = market.get_smile_vol(start)
names = list(market.get_pillar_quotes(start).keys())
worst = 0.0
for i in range(len(names)):
    s = smile.tenors()[i]
    worst = max(worst,
                abs(snap.strike_from_delta(0.25, s.T, True) - s.k_25c),
                abs(snap.strike_from_delta(-0.25, s.T, False) - s.k_25p),
                abs(snap.atm_strike(s.T) - s.k_atm))
check(worst < 1e-8, "25D / ATM strikes at quoted tenors should equal the pillar strikes")
print(f"2. 25D call / 25D put / ATM strikes on the smile vs pillar strikes, all 9 tenors: "
      f"max diff {worst:.1e}")

# ---- 3. Package shapes: each isolates the risk it's meant to ----
expiry = 91 / 365
print("3. 1 USD of each 3M package:")
print("      package   price       delta      vega       vanna      volga")
shapes = {}
for kind in ["ATM", "RR", "BF"]:
    legs = build_package(snap, kind, expiry)
    g = package_greeks(snap, legs, expiry)
    shapes[kind] = g
    print(f"      {kind:<6} {g['price']:>9.5f} {g['delta']:>10.5f} {g['vega']:>9.4f} "
          f"{g['vanna']:>10.4f} {g['volga']:>10.4f}")
check(abs(shapes["ATM"]["delta"]) < 1e-8, "a DNS straddle has zero delta")
check(abs(shapes["BF"]["vega"]) < 1e-10, "the butterfly is vega-neutral")
check(abs(shapes["RR"]["vanna"]) > abs(shapes["RR"]["volga"]) * 0.1, "RR should carry vanna")
check(abs(shapes["BF"]["volga"]) > 0, "BF should carry volga")

# ---- 4. Hedge a sold off-pillar option: vega, vanna, volga, delta all go to zero ----
book = Book()
strike = 6.70
mid = snap.price(strike, expiry, True).price
book.book_option(strike, expiry, True, -10e6, mid, time=0.0, note="client buys")
before = book.greeks(snap)
nav_before = book.nav(snap)

plan = recommend_smile_hedge(book, snap, expiry)
for kind in ["ATM", "RR", "BF"]:
    item = plan["packages"][kind]
    book.book_package(kind, item["legs"], item["notional"], expiry, time=0.0, note="smile hedge")
book.trade_spot(plan["spot_trade_usd"], spot, time=0.0, note="delta hedge")
after = book.greeks(snap)

print("4. short 10m 3M 6.70 call, before -> after hedge:")
for name in ["delta", "vega", "vanna", "volga", "gamma"]:
    print(f"      {name:<6} {before[name]:>16,.2f} -> {after[name]:>16,.6f}")
print(f"   packages: ATM {plan['packages']['ATM']['notional']:,.0f}, "
      f"RR {plan['packages']['RR']['notional']:,.0f}, BF {plan['packages']['BF']['notional']:,.0f} USD; "
      f"spot {plan['spot_trade_usd']:,.0f} USD")
for name in ["delta", "vega", "vanna", "volga"]:
    check(abs(after[name]) < 1e-3 * max(1.0, abs(before[name])), f"{name} should be hedged to ~0")
check(abs(book.nav(snap) - nav_before) < 1e-6, "hedging at mid should not change NAV")
check(len(book.portfolio.get_options()) == 9, "1 option + 2 ATM legs + 2 RR legs + 4 BF legs")

# ---- 5. Does it help? Same book: unhedged / delta-only / delta + smile, held over time ----
times = market.snapshot_times()
starts = []
for i in range(0, len(times) - 25, 15):
    starts.append(times[i])

abs_pnl = {"unhedged": 0.0, "delta only": 0.0, "delta + smile": 0.0}
count = 0
for t0 in starts:
    t1 = times[times.index(t0) + 25]                  # hold for 25 fixings (~5 trading days)
    s0 = market.get_snapshot(t0, sim_start=t0)
    s1 = market.get_snapshot(t1, sim_start=t0)
    strike0 = round(s0.forward(expiry) + 0.03, 4)     # an OTM call, off the pillars
    mid0 = s0.price(strike0, expiry, True).price

    books = {}
    for name in abs_pnl:
        b = Book()
        b.book_option(strike0, expiry, True, -10e6, mid0, time=0.0)
        if name == "delta + smile":
            p = recommend_smile_hedge(b, s0, expiry)
            for kind in ["ATM", "RR", "BF"]:
                item = p["packages"][kind]
                b.book_package(kind, item["legs"], item["notional"], expiry, time=0.0)
        if name != "unhedged":
            b.trade_spot(-b.net_delta(s0), s0.spot(), time=0.0)
        books[name] = b

    for name in books:
        b = books[name]
        nav0 = b.nav(s0)
        b.accrue(s0, s1.time() - s0.time())
        abs_pnl[name] += abs(b.nav(s1) - nav0)
    count += 1

print(f"5. average |P&L| over {count} five-day holds, no re-hedging (short 10m 3M OTM call):")
for name in abs_pnl:
    print(f"      {name:<14} {abs_pnl[name] / count:>10,.0f} CNY")
check(abs_pnl["delta only"] < abs_pnl["unhedged"], "delta hedging should reduce P&L swings")
check(abs_pnl["delta + smile"] < abs_pnl["delta only"], "smile hedging should reduce them further")

# ---- 6. Plan is plain data (web-ready) ----
text = json.dumps(plan)
print(f"6. hedge plan is JSON ({len(text)} chars); premium at mid {plan['premium_at_mid_cny']:,.0f} CNY")

# ---- 7. Bad inputs ----
rejected = 0
for args in [(0.25, -0.1, True), (-0.25, 0.25, True), (0.25, 0.25, False), (1.2, 0.25, True)]:
    try:
        snap.strike_from_delta(*args)
    except ValueError:
        rejected += 1
try:
    build_package(snap, "STRADDLE", expiry)
except ValueError:
    rejected += 1
try:
    build_package(snap, "ATM", -0.1)
except ValueError:
    rejected += 1
check(rejected == 6, "bad inputs should be rejected")
print(f"7. bad inputs rejected: {rejected} / 6")

# ---- 8. Python buckets agree with the C++ greeks_by_expiry ----
def multi_expiry_book(s, t_offset=0.0):
    """A desk-like book spread over several expiries (expiries relative to s.time())."""
    b = Book()
    trades = [
        (7 / 365, 6.73, True, -10e6),      # short 1W call
        (14 / 365, 6.70, False, -5e6),     # short 2W put
        (30 / 365, 6.75, True, 8e6),       # long 1M call
        (75 / 365, 6.65, False, -6e6),     # short 75-day put
        (91 / 365, 6.75, True, -20e6),     # short 3M call
        (182 / 365, 6.60, False, 7e6),     # long 6M put
        (365 / 365, 6.85, True, -15e6),    # short 1Y call
    ]
    for days, strike, is_call, notional in trades:
        expiry = s.time() + days
        price = s.price(strike, days, is_call).price
        b.book_option(strike, expiry, is_call, notional, price, time=s.time())
    return b


multi = multi_expiry_book(snap)
cpp_buckets = multi.portfolio.greeks_by_expiry(snap)
py_buckets = {}
for row in multi.positions(snap):
    label = bucket_of(row["T"])
    if label not in py_buckets:
        py_buckets[label] = {"gamma": 0.0, "vega": 0.0, "vanna": 0.0, "volga": 0.0}
    for name in py_buckets[label]:
        py_buckets[label][name] += row[name]
worst = 0.0
for label in py_buckets:
    key = CPP_BUCKET_KEY[label]
    cpp = cpp_buckets[key]
    for name in ["gamma", "vega", "vanna", "volga"]:
        worst = max(worst, abs(py_buckets[label][name] - getattr(cpp, name)))
check(len(py_buckets) == len(cpp_buckets) == 5 and worst < 1e-6, "Python and C++ buckets should agree")
labels = []
for label in ["1W", "1M", "3M", "6M", "1Y"]:
    if label in py_buckets:
        labels.append(label)
print(f"8. buckets {labels}: Python vs C++ max diff {worst:.1e}; "
      f"3M option in bucket {bucket_of(91 / 365)}, 6M option in {bucket_of(182 / 365)}")
check(bucket_of(91 / 365) == "3M" and bucket_of(182 / 365) == "6M", "tenors land in their own bucket")

# Rounding at a non-zero sim time must not push an exact tenor into the next bucket
later_snap = market.get_snapshot(pd.Timestamp("2026-08-12 14:00"), sim_start=times[0])
wrong = []
for days, label in [(7, "1W"), (30, "1M"), (91, "3M"), (182, "6M"), (365, "1Y")]:
    T = (later_snap.time() + days / 365) - later_snap.time()
    if bucket_of(T) != label:
        wrong.append(days)
late_book = multi_expiry_book(later_snap)
late_cpp = late_book.portfolio.greeks_by_expiry(later_snap)
check(len(wrong) == 0 and len(late_cpp) == 5, "exact tenors at a non-zero time land in their own bucket")
print(f"   at a non-zero sim time: tenors in the wrong bucket {wrong}; C++ buckets {len(late_cpp)}")

# ---- 9. Bucketed hedge on the multi-expiry book ----
whole = recommend_smile_hedge(multi, snap, 91 / 365)
bucketed = recommend_bucketed_hedge(multi, snap)

print("9. multi-expiry book (7 options, 1W to 1Y), per bucket before -> after the bucketed hedge:")
print("      bucket  hedge exp   gamma before        after     vega before   after   vanna after  volga after")
for item in bucketed["buckets"]:
    b = item["before"]
    a = item["after"]
    print(f"      {item['bucket']:<6} {item['hedge_days_left']:>6.1f}d  {b['gamma']:>14,.0f} {a['gamma']:>12,.0f}"
          f"  {b['vega']:>12,.0f} {a['vega']:>7.2f} {a['vanna']:>12.2f} {a['volga']:>12.2f}")
    check("skipped" not in item, f"bucket {item['bucket']} should be hedged")
    for name in ["vega", "vanna", "volga"]:
        check(abs(a[name]) < 1e-3 * max(1.0, abs(b[name])), f"{item['bucket']} {name} should be ~0")
    # Gamma: exact when the 4x4 is used. For a one-expiry bucket the 3x3 leaves a small
    # residual, because gamma = vega / (S^2 sigma T) and each strike has its own smile vol.
    # Judge that residual by its P&L for a 100-pip move.
    if item["gamma_expiry"] is not None:
        check(abs(a["gamma"]) < 1e-3 * abs(b["gamma"]), f"{item['bucket']} gamma should be ~0 (4x4)")
    else:
        pnl_100_pips = 0.5 * a["gamma"] * 0.01 * 0.01
        check(abs(pnl_100_pips) < 500, f"{item['bucket']} leftover gamma should be P&L-negligible")
    if item["gamma_expiry"] is not None:
        print(f"             ({item['bucket']} mixes expiries -> 4x4 with a "
              f"{item['gamma_days_left']:.0f}d straddle for gamma)")
    else:
        print(f"             ({item['bucket']} one expiry -> 3x3; leftover gamma = "
              f"{0.5 * a['gamma'] * 1e-4:,.0f} CNY per 100-pip move, from the smile)")

# Book the bucketed hedge for real and check the book agrees with the prediction
hedged = multi_expiry_book(snap)
nav_before = hedged.nav(snap)
for item in bucketed["buckets"]:
    for kind in item["packages"]:
        pkg = item["packages"][kind]
        hedged.book_package(kind, pkg["legs"], pkg["notional"], pkg["expiry"], time=0.0,
                            note=f"{item['bucket']} smile hedge")
hedged.trade_spot(bucketed["spot_trade_usd"], spot, time=0.0, note="delta hedge")
after_book = hedged.greeks(snap)
check(abs(hedged.nav(snap) - nav_before) < 1e-6, "booking hedges at mid keeps NAV")
check(abs(after_book["delta"]) < 1e-3, "delta should be flat after the spot trade")

# Same book, whole-book hedge with 3M packages only
whole_book = multi_expiry_book(snap)
for kind in ["ATM", "RR", "BF"]:
    pkg = whole["packages"][kind]
    whole_book.book_package(kind, pkg["legs"], pkg["notional"], 91 / 365, time=0.0)
whole_book.trade_spot(whole["spot_trade_usd"], spot, time=0.0)
whole_greeks = whole_book.greeks(snap)
before_total = multi.greeks(snap)

print("   totals:              gamma            vega        vanna        volga")
for name, g in [("before", before_total), ("whole-book (3M)", whole_greeks), ("bucketed", after_book)]:
    print(f"      {name:<16} {g['gamma']:>14,.0f} {g['vega']:>14,.2f} {g['vanna']:>12,.2f} {g['volga']:>12,.2f}")
check(abs(after_book["gamma"]) < 0.02 * abs(before_total["gamma"]), "bucketed hedge should remove ~all gamma")
check(abs(whole_greeks["gamma"]) > abs(after_book["gamma"]), "whole-book 3M hedge leaves more gamma")

# Bucket view after the whole-book hedge: totals zero, buckets not
whole_buckets = whole_book.portfolio.greeks_by_expiry(snap)
worst_bucket_vega = 0.0
for key in whole_buckets:
    worst_bucket_vega = max(worst_bucket_vega, abs(whole_buckets[key].vega))
print(f"   whole-book hedge: total vega {whole_greeks['vega']:,.2f}, but largest single-bucket vega "
      f"{worst_bucket_vega:,.0f}")

# ---- 10. Does bucketing help over time? Average |P&L| over 1-day holds, no re-hedging ----
results = {"unhedged": 0.0, "delta only": 0.0, "whole-book 3M": 0.0, "bucketed": 0.0}
count = 0
for i in range(0, len(times) - 5, 10):
    t0 = times[i]
    t1 = times[i + 5]                                       # 5 fixings = 1 trading day
    s0 = market.get_snapshot(t0, sim_start=t0)
    s1 = market.get_snapshot(t1, sim_start=t0)
    for name in results:
        b = multi_expiry_book(s0)
        if name == "whole-book 3M":
            plan = recommend_smile_hedge(b, s0, 91 / 365)
            for kind in ["ATM", "RR", "BF"]:
                pkg = plan["packages"][kind]
                b.book_package(kind, pkg["legs"], pkg["notional"], 91 / 365, time=0.0)
        if name == "bucketed":
            plan = recommend_bucketed_hedge(b, s0)
            for item in plan["buckets"]:
                for kind in item["packages"]:
                    pkg = item["packages"][kind]
                    b.book_package(kind, pkg["legs"], pkg["notional"], pkg["expiry"], time=0.0)
        if name != "unhedged":
            b.trade_spot(-b.net_delta(s0), s0.spot(), time=0.0)
        nav0 = b.nav(s0)
        b.accrue(s0, s1.time() - s0.time())
        results[name] += abs(b.nav(s1) - nav0)
    count += 1
print(f"10. average |P&L| over {count} one-day holds, multi-expiry book, no re-hedging:")
for name in results:
    print(f"      {name:<14} {results[name] / count:>10,.0f} CNY")
check(results["bucketed"] < results["delta only"], "bucketed should beat delta-only")
check(results["bucketed"] < results["whole-book 3M"], "bucketed should beat a single-expiry hedge")

# ---- 11. A bucket under a day from expiry is skipped, not forced ----
near = Book()
near.book_option(6.72, snap.time() + 0.5 / 365, True, -10e6, 0.001, time=snap.time())
near.book_option(6.75, snap.time() + 91 / 365, True, -10e6, 0.01, time=snap.time())
near_plan = recommend_bucketed_hedge(near, snap)
skipped = []
hedged_labels = []
for item in near_plan["buckets"]:
    if "skipped" in item:
        skipped.append(item["bucket"])
    else:
        hedged_labels.append(item["bucket"])
check(skipped == ["1W"] and hedged_labels == ["3M"], "only the near-expiry bucket should be skipped")
print(f"11. option 12h from expiry: buckets skipped {skipped}, hedged {hedged_labels}")

# ---- 11b. A far-out option barely moves the hedge expiry: no near-duplicate gamma straddle ----
# Found by fuzz_hedging.py: a 13.4-day near-ATM option plus an 18-day option 3% out. The far
# option has almost no vega, so the packages sit at ~13.4 days - the same expiry as the
# shortest position. Using a gamma straddle there made the 4x4 almost singular and the
# solver suggested +189m / -190m offsetting straddles on a 19m book.
dup_snap = market.get_snapshot(pd.Timestamp("2026-08-28 10:00"), sim_start=times[0])
now_dup = dup_snap.time()
dup = Book()
for days, moneyness, notional in [(13.4, 0.9997, 2.9e6), (18.0, 1.03, -16.5e6)]:
    K = dup_snap.forward(days / 365) * moneyness
    dup.book_option(K, now_dup + days / 365, True, notional,
                    dup_snap.price(K, days / 365, True).price, time=now_dup)
dup_plan = recommend_bucketed_hedge(dup, dup_snap)
dup_item = dup_plan["buckets"][0]
biggest = 0.0
for kind in dup_item["packages"]:
    biggest = max(biggest, abs(dup_item["packages"][kind]["notional"]))
check(dup_item["gamma_expiry"] is None, "no gamma straddle at (almost) the packages' own expiry")
check(biggest < 19.4e6, "hedge should not be bigger than the book")
print(f"11b. 13.4d near-ATM + 18d far-out option: gamma row used: {dup_item['gamma_expiry'] is not None}; "
      f"largest package {biggest/1e6:.1f}m USD on a 19.4m book")

# ---- 11c. Full revaluation under shocks (not Greeks): the hedge protects the book ----
stress_hedged = multi_expiry_book(snap)
stress_plan = recommend_bucketed_hedge(stress_hedged, snap)
for item in stress_plan["buckets"]:
    for kind in item["packages"]:
        pkg = item["packages"][kind]
        stress_hedged.book_package(kind, pkg["legs"], pkg["notional"], pkg["expiry"], time=0.0)
stress_hedged.trade_spot(stress_plan["spot_trade_usd"], spot, time=0.0)
stress_unhedged = multi_expiry_book(snap)
cny_s, usd_s = market.get_curves(start)
base_surface = market.get_smile_vol(start)


def fixed_strike_vol_pnl(b, dv):
    # every option's own vol + dv at its fixed strike, repriced in full
    total = 0.0
    for row in b.positions(snap):
        T = row["T"]
        pr = fxengine.GKPricer(snap.domestic_rate(T), snap.foreign_rate(T))
        v = snap.vol(row["strike"], T)
        new_price = pr.price(spot, row["strike"], T, v + dv, row["is_call"]).price
        old_price = pr.price(spot, row["strike"], T, v, row["is_call"]).price
        total += row["notional"] * (new_price - old_price)
    return total


print("11c. full revaluation, hedged vs unhedged (CNY):")
# Measured against the book's real spot exposure - the larger of the two unhedged moves.
# (One direction alone can be small just because the positions offset there.)
# What's left after hedging is third-order, plus a little gamma: a single-expiry bucket
# gets only the 3x3, and gamma = vega / (S^2 sigma T) holds per strike, so with a smile
# about 0.5% of that bucket's gamma survives.
spot_moves = {}
for label, spot_mult in [("spot +0.5%", 1.005), ("spot -0.5%", 0.995)]:
    shocked = fxengine.MarketSnapshot(snap.time(), spot * spot_mult, cny_s, usd_s, base_surface)
    h = stress_hedged.nav(shocked) - stress_hedged.nav(snap)
    u = stress_unhedged.nav(shocked) - stress_unhedged.nav(snap)
    spot_moves[label] = (u, h)
exposure = 0.0
for label in spot_moves:
    exposure = max(exposure, abs(spot_moves[label][0]))
for label in spot_moves:
    u, h = spot_moves[label]
    check(abs(h) < 0.01 * exposure, f"{label}: hedge should remove ~all of the P&L")
    print(f"      {label:<26} unhedged {u:>12,.0f}   hedged {h:>10,.0f}   ({abs(h) / exposure * 100:.2f}% of exposure)")
for label, dv in [("vols +0.20 at fixed strikes", 0.002), ("vols -0.20 at fixed strikes", -0.002)]:
    h = fixed_strike_vol_pnl(stress_hedged, dv)
    u = fixed_strike_vol_pnl(stress_unhedged, dv)
    check(abs(h) < 0.01 * abs(u), f"{label}: hedge should remove ~all of the P&L")
    print(f"      {label:<26} unhedged {u:>12,.0f}   hedged {h:>10,.0f}")
print("      (shifting every market QUOTE instead also reshapes the smile, because the quotes")
print("       are pinned to deltas, not strikes - fixed-strike vega doesn't capture that)")

# ---- 12. The bucketed plan is plain data ----
check(len(json.dumps(bucketed)) > 0, "bucketed plan should be JSON")
print(f"12. bucketed plan is JSON; spot trade {bucketed['spot_trade_usd']:,.0f} USD, "
      f"premium at mid {bucketed['premium_at_mid_cny']:,.0f} CNY")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")