"""
Fuzz test for the bucketed smile hedge: random books on random dates.
Checks the HEDGED BOOK itself (through the C++ engine), not the plan's prediction.
"""
import sys
import random
import time
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

from simulation.market_loader import MarketLoader
from simulation.book import Book
from simulation.hedging import recommend_bucketed_hedge, bucket_of, CPP_BUCKET_KEY

random.seed(7)
market = MarketLoader()
times = market.snapshot_times()
start = times[0]

N_BOOKS = 300
failures = []
worst = {"smile_rel": 0.0, "delta": 0.0, "nav": 0.0, "premium": 0.0, "size_ratio": 0.0,
         "gamma_4x4_rel": 0.0, "gamma_3x3_pnl": 0.0, "condition_3x3": 0.0, "condition_4x4": 0.0}
gamma_increased = []
used_4x4 = 0
used_3x3 = 0
skipped = 0
started = time.time()

for n in range(N_BOOKS):
    t = random.choice(times[:-1])
    snap = market.get_snapshot(t, sim_start=start)
    now = snap.time()

    book = Book()
    gross = 0.0
    for k in range(random.randint(2, 8)):
        days = random.uniform(2, 365)
        expiry = now + days / 365
        strike = snap.forward(days / 365) * random.uniform(0.97, 1.03)
        is_call = random.random() < 0.5
        notional = random.choice([-1, 1]) * random.uniform(1e6, 20e6)
        price = snap.price(strike, days / 365, is_call).price
        book.book_option(strike, expiry, is_call, notional, price, time=now)
        gross += abs(notional)

    try:
        plan = recommend_bucketed_hedge(book, snap)
    except Exception as error:
        failures.append((n, "plan raised: " + str(error)))
        continue

    before_buckets = book.portfolio.greeks_by_expiry(snap)
    nav_before = book.nav(snap)
    cash_before = book.cash_cny

    for item in plan["buckets"]:
        if "skipped" in item:
            skipped += 1
            continue
        if item["gamma_expiry"] is None:
            used_3x3 += 1
            worst["condition_3x3"] = max(worst["condition_3x3"], item["condition"])
        else:
            used_4x4 += 1
            worst["condition_4x4"] = max(worst["condition_4x4"], item["condition"])
        for kind in item["packages"]:
            pkg = item["packages"][kind]
            worst["size_ratio"] = max(worst["size_ratio"], abs(pkg["notional"]) / gross)
            book.book_package(kind, pkg["legs"], pkg["notional"], pkg["expiry"], time=now)

    premium_moved = book.cash_cny - cash_before
    worst["premium"] = max(worst["premium"], abs(premium_moved - plan["premium_at_mid_cny"]))
    book.trade_spot(plan["spot_trade_usd"], snap.spot(), time=now)

    # ---- check the hedged book through C++ ----
    worst["delta"] = max(worst["delta"], abs(book.net_delta(snap)))
    worst["nav"] = max(worst["nav"], abs(book.nav(snap) - nav_before))

    after_buckets = book.portfolio.greeks_by_expiry(snap)
    for item in plan["buckets"]:
        if "skipped" in item:
            continue
        key = CPP_BUCKET_KEY[item["bucket"]]
        before = before_buckets[key]
        after = after_buckets[key]
        for name in ["vega", "vanna", "volga"]:
            scale = max(abs(getattr(before, name)), 1.0)
            worst["smile_rel"] = max(worst["smile_rel"], abs(getattr(after, name)) / scale)
        if item["gamma_expiry"] is not None:
            worst["gamma_4x4_rel"] = max(worst["gamma_4x4_rel"],
                                         abs(after.gamma) / max(abs(before.gamma), 1.0))
        else:
            pnl = abs(0.5 * after.gamma * 1e-4)
            worst["gamma_3x3_pnl"] = max(worst["gamma_3x3_pnl"], pnl)
            before_pnl = abs(0.5 * before.gamma * 1e-4)
            if pnl > before_pnl:
                gamma_increased.append((item["bucket"], before_pnl, pnl))

    # every hedge leg must sit in the same bucket as the risk it hedges
    for item in plan["buckets"]:
        if "skipped" in item:
            continue
        for kind in item["packages"]:
            leg_bucket = bucket_of(item["packages"][kind]["expiry"] - now)
            if leg_bucket != item["bucket"]:
                failures.append((n, f"{kind} for {item['bucket']} landed in {leg_bucket}"))

elapsed = time.time() - started
print(f"{N_BOOKS} random books in {elapsed:.1f}s; buckets hedged 3x3: {used_3x3}, 4x4: {used_4x4}, "
      f"skipped (<1 day): {skipped}")
print(f"worst vega/vanna/volga left in a bucket (relative to before): {worst['smile_rel']:.1e}")
print(f"worst gamma left in a 4x4 bucket (relative to before):        {worst['gamma_4x4_rel']:.1e}")
print(f"worst gamma left in a 3x3 bucket, P&L per 100-pip move:       {worst['gamma_3x3_pnl']:,.0f} CNY")
print(f"worst delta left after the spot trade:                         {worst['delta']:.1e} USD")
print(f"worst NAV change from hedging at mid:                          {worst['nav']:.1e} CNY")
print(f"worst premium reported vs cash actually moved:                 {worst['premium']:.1e} CNY")
print(f"largest single package vs the book's gross notional:           {worst['size_ratio']:.2f}x")
print(f"worst condition number, 3x3 / 4x4:                             {worst['condition_3x3']:.1e} / {worst['condition_4x4']:.1e}")
print(f"3x3 buckets where the hedge left MORE gamma than before: {len(gamma_increased)}")
gamma_increased.sort(key=lambda row: row[2] - row[1], reverse=True)
for bucket, before_pnl, after_pnl in gamma_increased[:5]:
    print(f"     {bucket}: {before_pnl:,.0f} -> {after_pnl:,.0f} CNY per 100 pips")
print(f"failures: {len(failures)}")
for f in failures[:10]:
    print("  ", f)

passed = (len(failures) == 0
          and worst["smile_rel"] < 1e-9
          and worst["gamma_4x4_rel"] < 1e-9
          and worst["gamma_3x3_pnl"] < 1000
          and worst["delta"] < 1e-3
          and worst["nav"] < 1e-3
          and worst["premium"] < 1e-3
          and worst["size_ratio"] < 2.0)
print()
print("ALL CHECKS PASSED" if passed else "PROBLEMS")