"""
End-to-end scenario: trade like a desk through Jul-Sep 2026, one fixing at a time.

- 10 client trades at different timestamps (one placed between fixings, one expiring
  on a Saturday, several expiring inside the window, some outliving it)
- delta re-hedged with spot at every fixing
- bucketed smile hedge (ATM / RR / BF, + gamma straddle where needed) after every
  client trade and at every 16:00 close
- interest accrued every step, expiries settled at the first fixing on/after expiry
- an identical UNHEDGED book runs alongside for comparison

Independent checks at every step (nothing here trusts the Book's own numbers):
  A. NAV rebuilt by repricing every live option directly with GKPricer + the smile
  B. a shadow cash / USD account rebuilt from the trades and our own interest maths
  C. settlements: right fixing, intrinsic payoff, never early, options gone afterwards
  D. Greeks after each hedge: delta 0; bucket vega/vanna/volga 0 after a smile hedge
  E. P&L explained from the previous fixing's Greeks; clock steps positive and ordered
"""
import sys
import math
import time
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
import fxengine
from simulation.market_loader import MarketLoader, years_between
from simulation.book import Book
from simulation.hedging import recommend_bucketed_hedge, bucket_of, CPP_BUCKET_KEY

market = MarketLoader()
FIXINGS = market.snapshot_times()
START = FIXINGS[0]
problems = []


def check(ok, message):
    if not ok:
        problems.append(message)


def clock(ts):
    return years_between(START, ts)


# ------------------------------------------------------------------
# Client orders: (order time, client side, call/put, strike rule, notional USD, expiry)
#   strike rule: ("fwd", x) = x * forward for that expiry at booking
#   expiry: a pd.Timedelta after the order time
# ------------------------------------------------------------------
ORDERS = [
    ("2026-07-07 10:00", "buy",  True,  ("fwd", 1.000), 10e6, pd.Timedelta(days=30)),   # 1M ATM call
    ("2026-07-07 11:00", "sell", False, ("fwd", 0.995),  5e6, pd.Timedelta(days=7)),    # 1W put, client sells
    ("2026-07-08 14:00", "buy",  True,  ("fwd", 1.010), 20e6, pd.Timedelta(days=91)),   # 3M call, outlives window
    ("2026-07-10 15:00", "buy",  False, ("fwd", 0.990),  8e6, pd.Timedelta(days=14)),   # 2W put, Friday expiry
    ("2026-07-13 10:00", "buy",  True,  ("fwd", 1.002),  6e6, pd.Timedelta(days=5)),    # expires Saturday 18 Jul
    ("2026-07-15 12:30", "buy",  True,  ("fwd", 1.000),  5e6, pd.Timedelta(days=7)),    # placed BETWEEN fixings
    ("2026-08-03 16:00", "sell", True,  ("fwd", 1.005), 12e6, pd.Timedelta(days=30)),   # 1M call, client sells
    ("2026-08-25 10:00", "buy",  True,  ("fwd", 1.000), 15e6, pd.Timedelta(days=14)),   # 2W straddle: call leg
    ("2026-08-25 10:00", "buy",  False, ("fwd", 1.000), 15e6, pd.Timedelta(days=14)),   #              put leg
    ("2026-09-01 14:00", "buy",  False, ("fwd", 0.985), 10e6, pd.Timedelta(days=182)),  # 6M put, outlives window
    ("2026-09-21 11:00", "buy",  True,  ("fwd", 1.003),  5e6, pd.Timedelta(days=7)),    # 1W call
]

orders = []
for order_time, side, is_call, rule, size, tenor in ORDERS:
    order_ts = pd.Timestamp(order_time)
    orders.append({
        "order_ts": order_ts,
        "expiry_ts": order_ts + tenor,
        "side": side,
        "is_call": is_call,
        "rule": rule,
        "size": size,
        "booked": False,
    })

hedged = Book()
unhedged = Book()

# Shadow accounts, built only from what WE decide to trade and our own interest maths
shadow_cash = {"hedged": 0.0, "unhedged": 0.0}
shadow_usd = {"hedged": 0.0, "unhedged": 0.0}

# What we expect to settle: position id -> details
expected = {"hedged": {}, "unhedged": {}}

rows = []
previous_snap = None
previous_ts = None
previous_greeks = None
previous_nav = None
settle_log = []
smile_hedges = 0
spot_hedges = 0
started = time.time()


def independent_option_value(book, snap):
    """Reprice every live option with GKPricer, rates from the curves, vol from the smile."""
    total = 0.0
    for pos in book.portfolio.get_options():
        T = pos.expiry - snap.time()
        pricer = fxengine.GKPricer(snap.domestic_rate(T), snap.foreign_rate(T))
        vol = snap.vol(pos.strike, T)
        total += pos.notional * pricer.price(snap.spot(), pos.strike, T, vol, pos.is_call).price
    return total


def book_and_shadow(name, book, strike, expiry, is_call, notional, price, now, note):
    position_id = book.book_option(strike, expiry, is_call, notional, price, time=now, note=note)
    shadow_cash[name] += -notional * price
    expected[name][position_id] = {"strike": strike, "expiry": expiry, "is_call": is_call,
                                   "notional": notional, "note": note}
    return position_id


for ts in FIXINGS:
    snap = market.get_snapshot(ts, sim_start=START)
    now = snap.time()
    spot = snap.spot()

    # ---- E. clock ----
    if previous_ts is not None:
        check(ts > previous_ts, f"{ts}: clock went backwards")
        dt = now - previous_snap.time()
        check(dt > 0, f"{ts}: non-positive time step")
    else:
        dt = 0.0

    # ---- 1. accrue interest over the gap (rates from the START of the gap) ----
    carry = 0.0
    if previous_snap is not None:
        r_cny = previous_snap.domestic_rate(1 / 365)
        r_usd = previous_snap.foreign_rate(1 / 365)
        for name, book in [("hedged", hedged), ("unhedged", unhedged)]:
            interest = book.accrue(previous_snap, dt)
            if name == "hedged":
                carry = interest
            shadow_cash[name] = shadow_cash[name] * math.exp(r_cny * dt)
            shadow_usd[name] = shadow_usd[name] * math.exp(r_usd * dt)

    # ---- 2. settle expiries ----
    for name, book in [("hedged", hedged), ("unhedged", unhedged)]:
        due_ids = []
        for position_id in expected[name]:
            if expected[name][position_id]["expiry"] <= now + 1e-9:
                due_ids.append(position_id)
        settled = book.settle_expired(snap)
        settled_ids = []
        for row in settled:
            settled_ids.append(row["position_id"])
            info = expected[name][row["position_id"]]
            if info["is_call"]:
                payoff = max(spot - info["strike"], 0.0)
            else:
                payoff = max(info["strike"] - spot, 0.0)
            shadow_cash[name] += info["notional"] * payoff
            check(abs(row["cash_cny"] - info["notional"] * payoff) < 1e-6,
                  f"{ts} {name}: settlement cash wrong for position {row['position_id']}")
            # settled at the FIRST fixing on/after expiry: the previous fixing was before expiry
            check(previous_snap is not None and previous_snap.time() < info["expiry"] - 1e-9,
                  f"{ts} {name}: position {row['position_id']} settled late")
            if name == "hedged" and not info["note"].startswith("hedge"):
                settle_log.append((ts, info, spot, info["notional"] * payoff))
        check(sorted(settled_ids) == sorted(due_ids), f"{ts} {name}: settled {settled_ids}, expected {due_ids}")
        for position_id in settled_ids:
            del expected[name][position_id]
        for pos in book.portfolio.get_options():
            check(pos.expiry > now + 1e-9, f"{ts} {name}: expired option {pos.id} still on the book")

    # ---- P&L explain for the step (before any trading at this fixing) ----
    # Greeks from the previous fixing; vol change measured per option at its own strike.
    # Options that expired during the step are left out of the vega terms (their last
    # step is all intrinsic), and their P&L lands in "unexplained".
    if previous_snap is not None:
        nav_now = hedged.nav(snap)
        actual = nav_now - previous_nav
        dS = spot - previous_snap.spot()
        g = previous_greeks
        vol_terms = 0.0
        for pos in previous_positions:
            T_prev = pos["expiry"] - previous_snap.time()
            T_now = pos["expiry"] - now
            if T_now <= 1e-9:
                continue
            dvol = snap.vol(pos["strike"], T_now) - previous_snap.vol(pos["strike"], T_prev)
            vol_terms += (pos["vega"] * dvol + pos["vanna"] * dS * dvol
                          + 0.5 * pos["volga"] * dvol * dvol)
        explained = (g["delta"] * dS + 0.5 * g["gamma"] * dS * dS + g["theta"] * dt
                     + vol_terms + carry)
        rows.append({"ts": ts, "spot": spot, "dS_pips": dS * 1e4, "dt_hours": dt * 365 * 24,
                     "pnl": actual, "explained": explained, "vol_terms": vol_terms, "carry": carry})

    # ---- 3. book client orders that are due (placed at or before this fixing) ----
    traded_now = False
    for order in orders:
        if order["booked"] or order["order_ts"] > ts:
            continue
        expiry = clock(order["expiry_ts"])
        T = expiry - now
        strike = round(snap.forward(T) * order["rule"][1], 4)
        mid = snap.price(strike, T, order["is_call"]).price
        our_notional = -order["size"] if order["side"] == "buy" else order["size"]
        note = f"client {order['side']}s, ordered {order['order_ts']:%d %b %H:%M}"
        for name, book in [("hedged", hedged), ("unhedged", unhedged)]:
            book_and_shadow(name, book, strike, expiry, order["is_call"], our_notional, mid, now, note)
        order["booked"] = True
        order["booked_ts"] = ts
        traded_now = True

    # ---- 4. hedge (hedged book only) ----
    if traded_now or ts.hour == 16:
        plan = recommend_bucketed_hedge(hedged, snap)
        for item in plan["buckets"]:
            if "skipped" in item:
                continue
            for kind in item["packages"]:
                pkg = item["packages"][kind]
                for leg in pkg["legs"]:
                    leg_notional = pkg["notional"] * leg["ratio"]
                    if abs(leg_notional) < 1.0:
                        continue
                    book_and_shadow("hedged", hedged, leg["strike"], pkg["expiry"], leg["is_call"],
                                    leg_notional, leg["mid"], now, f"hedge {kind} {item['bucket']}")
        smile_hedges += 1
        # D. every hedged bucket is flat in vega / vanna / volga
        after = hedged.portfolio.greeks_by_expiry(snap)
        for item in plan["buckets"]:
            if "skipped" in item:
                continue
            a = after[CPP_BUCKET_KEY[item["bucket"]]]
            for greek in ["vega", "vanna", "volga"]:
                check(abs(getattr(a, greek)) < 1e-6 * max(1.0, abs(item["before"][greek])) + 1e-3,
                      f"{ts}: bucket {item['bucket']} {greek} not flat after smile hedge")

    trade = -hedged.net_delta(snap)
    if abs(trade) > 0:
        hedged.trade_spot(trade, spot, time=now, note="delta hedge")
        shadow_usd["hedged"] += trade
        shadow_cash["hedged"] += -trade * spot
        spot_hedges += 1
    check(abs(hedged.net_delta(snap)) < 1e-3, f"{ts}: delta not flat after spot hedge")

    # ---- A + B. independent NAV and accounts ----
    for name, book in [("hedged", hedged), ("unhedged", unhedged)]:
        check(abs(book.cash_cny - shadow_cash[name]) < 1e-4 * max(1.0, abs(shadow_cash[name])) + 1e-3,
              f"{ts} {name}: cash {book.cash_cny} vs shadow {shadow_cash[name]}")
        check(abs(book.hedge_usd - shadow_usd[name]) < 1e-6 * max(1.0, abs(shadow_usd[name])) + 1e-6,
              f"{ts} {name}: hedge {book.hedge_usd} vs shadow {shadow_usd[name]}")
        independent_nav = independent_option_value(book, snap) + shadow_usd[name] * spot + shadow_cash[name]
        check(abs(book.nav(snap) - independent_nav) < 1e-3,
              f"{ts} {name}: NAV {book.nav(snap)} vs independent {independent_nav}")
        check(len(book.portfolio.get_options()) == len(expected[name]),
              f"{ts} {name}: book has {len(book.portfolio.get_options())} options, expected {len(expected[name])}")

    previous_greeks = hedged.greeks(snap)
    previous_positions = hedged.positions(snap)
    previous_nav = hedged.nav(snap)
    previous_snap = snap
    previous_ts = ts

elapsed = time.time() - started

# ------------------------------------------------------------------
# Report
# ------------------------------------------------------------------
print(f"stepped through {len(FIXINGS)} fixings ({FIXINGS[0]:%d %b %H:%M} -> {FIXINGS[-1]:%d %b %H:%M}) in {elapsed:.1f}s")
print(f"client orders booked: {sum(1 for o in orders if o['booked'])} / {len(orders)}; "
      f"smile hedges: {smile_hedges}; spot hedges: {spot_hedges}; "
      f"options on the hedged book at the end: {len(hedged.portfolio.get_options())}")

print("\nclient orders - when booked:")
for o in orders:
    print(f"  ordered {o['order_ts']:%a %d %b %H:%M} -> booked {o['booked_ts']:%a %d %b %H:%M}; "
          f"expiry {o['expiry_ts']:%a %d %b %H:%M}")
late = [o for o in orders if o["order_ts"] != o["booked_ts"]]
check(len(late) == 1 and late[0]["order_ts"] == pd.Timestamp("2026-07-15 12:30")
      and late[0]["booked_ts"] == pd.Timestamp("2026-07-15 14:00"),
      "only the 12:30 order should wait - until the 14:00 fixing")

print("\nclient-option settlements (hedged book):")
for ts, info, spot, cash in settle_log:
    expiry_ts = START + pd.Timedelta(seconds=info["expiry"] * 365 * 24 * 3600)
    kind = "call" if info["is_call"] else "put"
    print(f"  expiry {expiry_ts:%a %d %b %H:%M} -> settled {ts:%a %d %b %H:%M}: "
          f"{'we sold' if info['notional'] < 0 else 'we bought'} {abs(info['notional'])/1e6:.0f}m {kind} "
          f"K={info['strike']:.4f}, spot {spot:.4f}, cash {cash:+,.0f} CNY")
client_expiries = 0
for o in orders:
    if o["expiry_ts"] <= FIXINGS[-1]:
        client_expiries += 1
check(len(settle_log) == client_expiries, f"{len(settle_log)} client settlements, expected {client_expiries}")

# Saturday expiry settles Monday 10:00; 12:30 expiry settles 14:00
for ts, info, spot, cash in settle_log:
    expiry_ts = START + pd.Timedelta(seconds=info["expiry"] * 365 * 24 * 3600)
    if expiry_ts.dayofweek == 5:
        check(ts == pd.Timestamp("2026-07-20 10:00"), "Saturday expiry should settle Monday 10:00")
    if expiry_ts.hour == 12:
        check(ts == pd.Timestamp("2026-07-22 14:00"), "12:30 expiry should settle at 14:00 the same day")

table = pd.DataFrame(rows)
table["unexplained"] = table["pnl"] - table["explained"]
gaps = table["dt_hours"].round(2).value_counts().sort_index()
print("\ntime steps between fixings (hours: count):",
      ", ".join(f"{h:g}h: {c}" for h, c in gaps.items()))
print(f"hedged book step P&L: mean |P&L| {table['pnl'].abs().mean():,.0f} CNY, "
      f"worst {table['pnl'].abs().max():,.0f} CNY at {table.loc[table['pnl'].abs().idxmax(), 'ts']:%d %b %H:%M}")
print(f"  explained by delta, gamma, theta, vega/vanna/volga (per strike) and carry from the previous fixing:")
print(f"  mean |unexplained| {table['unexplained'].abs().mean():,.0f} CNY "
      f"({table['unexplained'].abs().sum() / table['pnl'].abs().sum() * 100:.0f}% of total |P&L|)")
worst_rows = table.reindex(table["unexplained"].abs().sort_values(ascending=False).index).head(4)
for _, r in worst_rows.iterrows():
    print(f"    {r['ts']:%a %d %b %H:%M}: gap {r['dt_hours']:.0f}h, spot {r['dS_pips']:+.0f} pips, "
          f"P&L {r['pnl']:,.0f}, explained {r['explained']:,.0f}")

last = previous_snap
print(f"\nfinal NAV ({FIXINGS[-1]:%d %b %H:%M}): hedged {hedged.nav(last):,.0f} CNY, unhedged {unhedged.nav(last):,.0f} CNY")
hedged_steps = table["pnl"].abs().mean()

print()
if problems:
    print(f"{len(problems)} PROBLEMS")
    for p in problems[:15]:
        print("  ", p)
else:
    print("ALL CHECKS PASSED")