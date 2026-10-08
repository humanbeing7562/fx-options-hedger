"""
Checks for the Session: clock, orders, settlement, P&L bookkeeping, hedge policies,
netting and JSON output. Nothing here trusts the Session's own numbers where it can
be rebuilt independently.
"""
import sys
import json
import math
sys.path.insert(0, "build")

import pandas as pd
import fxengine
from simulation.market_loader import MarketLoader
from simulation.book import Book
from simulation.hedging import bucket_of, CPP_BUCKET_KEY
from simulation.session import Session

market = MarketLoader()          # shared, so each Session doesn't reload the CSVs
problems = 0


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


def raises(function, *args):
    try:
        function(*args)
    except ValueError:
        return True
    return False


def as_json(data):
    return json.dumps(data, allow_nan=False)


def independent_nav(session):
    """Options repriced with GKPricer + smile vol, plus hedge and cash."""
    snap = session.snapshot
    total = 0.0
    for pos in session.book.portfolio.get_options():
        T = pos.expiry - snap.time()
        pricer = fxengine.GKPricer(snap.domestic_rate(T), snap.foreign_rate(T))
        r = pricer.price(snap.spot(), pos.strike, T, snap.vol(pos.strike, T), pos.is_call)
        total += pos.notional * r.price
    return total + session.book.hedge_usd * snap.spot() + session.book.cash_cny


# ---- 1. Clock ----
s = Session(market=market, policy="manual")
check(s.now() == pd.Timestamp("2026-07-07 10:00"), "first fixing should be 7 Jul 10:00")
seen = [s.now()]
s.run(steps=10)
for record in s.history():
    if record["type"] == "step":
        seen.append(pd.Timestamp(record["time"]))
ok = True
for i in range(1, len(seen)):
    if not seen[i] > seen[i - 1]:
        ok = False
check(ok and len(seen) == 11, "clock should move forward one fixing per next()")
hours = set()
for t in seen:
    hours.add(t.hour)
check(hours <= {10, 11, 14, 15, 16}, f"only fixing hours expected, got {sorted(hours)}")
s.run(until="2026-07-15 11:00")
check(s.now() == pd.Timestamp("2026-07-15 11:00"), f"run(until) stopped at {s.now()}")
s.run()
check(s.done() and s.now() == s.times[-1], "run() should end on the last fixing")
check(raises(s.next), "next() past the end should refuse")
print(f"1. clock: {len(s.times)} fixings, {s.times[0]:%d %b %H:%M} -> {s.times[-1]:%d %b %H:%M}, "
      f"only moves forward, stops at the end")

# ---- 2. Orders: validation ----
s = Session(market=market, policy="manual")
good = {"side": "buy", "is_call": True, "notional": 5e6, "tenor_days": 7}
bad_orders = [
    dict(good, side="long"),
    dict(good, notional=-5e6),
    dict(good, is_call="yes"),
    dict(good, time="2026-07-06 10:00"),                  # before the clock
    dict(good, time="2026-12-01 10:00"),                  # after the data
    {"side": "buy", "is_call": True, "notional": 5e6},    # no expiry
    dict(good, tenor_days=None, expiry="2026-07-07 09:00"),
    dict(good, strike="ABC"),
    dict(good, strike=-1.0),
    dict(good, price=-0.01),
]
all_refused = True
for order in bad_orders:
    if not raises(s.submit_order, order):
        all_refused = False
        print("   accepted a bad order:", order)
check(all_refused, "bad orders should be refused")
check(len(s.book.portfolio.get_options()) == 0 and len(s.orders) == 0, "a refused order must leave no trace")
print(f"2. {len(bad_orders)} malformed orders refused, book untouched")

# ---- 3. Orders: booked now vs at the next fixing; strikes; price ----
s = Session(market=market, policy="manual")
snap = s.snapshot
now_row = s.submit_order({"side": "buy", "is_call": True, "notional": 10e6,
                          "tenor_days": 30, "strike": "ATM"})
T = 30 / 365
check(now_row["status"] == "booked" and now_row["booked_at"] == "2026-07-07T10:00:00",
      "an order at the current time books straight away")
check(abs(now_row["strike_value"] - snap.atm_strike(T)) < 1e-12, "ATM strike should be the snapshot's DNS ATM")
check(abs(now_row["mid"] - snap.price(now_row["strike_value"], T, True).price) < 1e-12, "mid price wrong")
check(abs(now_row["edge_cny"]) < 1e-9, "booking at mid has no edge")
check(s.book.portfolio.get_options()[0].notional == -10e6, "client buys -> we are short")

later = s.submit_order({"time": "2026-07-07 12:30", "side": "sell", "is_call": False,
                        "notional": 5e6, "tenor_days": 7, "strike": "25D", "price": 0.0030})
check(later["status"] == "pending", "a 12:30 order waits")
s.next()                                                   # 11:00
check(len(s.pending) == 1, "still waiting at 11:00")
step = s.next()                                            # 14:00
check(s.now() == pd.Timestamp("2026-07-07 14:00") and len(step["orders_booked"]) == 1,
      "the 12:30 order books at 14:00")
row = step["orders_booked"][0]
T_put = (pd.Timestamp("2026-07-14 12:30") - s.now()).total_seconds() / (365 * 24 * 3600)
put_delta = s.snapshot.price(row["strike_value"], T_put, False).delta
check(abs(put_delta + 0.25) < 1e-8, f"25D put strike should have delta -0.25, got {put_delta}")
check(row["expiry"] == "2026-07-14T12:30:00", "tenor counts from the ORDER time, not the booking time")
expected_edge = 5e6 * (row["mid"] - 0.0030)               # we buy at 0.0030
check(abs(row["edge_cny"] - expected_edge) < 1e-6, "edge on a user price")
check(abs(step["trade_pnl_cny"] - expected_edge) < 1e-6,
      f"step trade P&L {step['trade_pnl_cny']} should be the edge {expected_edge}")
print(f"3. ATM order booked at once (K={now_row['strike_value']:.4f}); 12:30 order booked at 14:00, "
      f"25D put K={row['strike_value']:.4f}, bought at 0.0030 vs mid {row['mid']:.5f} -> edge {row['edge_cny']:,.0f} CNY")

# ---- 3b. ATM forward strike ----
atmf = Session(market=market, policy="manual")
T = 91 / 365
call_row = atmf.submit_order({"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 91, "strike": "ATMF"})
put_row = atmf.submit_order({"side": "buy", "is_call": False, "notional": 1e6, "tenor_days": 91, "strike": "atmf"})
F = atmf.snapshot.forward(T)
check(call_row["strike_value"] == F and put_row["strike_value"] == F, "ATMF strike should be the forward to expiry")
check(abs(call_row["mid"] - put_row["mid"]) < 1e-12, "at the forward, call and put prices must match (put-call parity)")
dns = atmf.snapshot.atm_strike(T)
check(dns > F, "the DNS ATM strike sits above the forward (K = F e^(vol^2 T / 2))")
# A pending ATMF order takes the forward at the fixing where it BOOKS
forward_at_submit = atmf.snapshot.forward((pd.Timestamp("2026-07-07 12:30") + pd.Timedelta(days=30) - atmf.now()).total_seconds() / (365 * 24 * 3600))
atmf.submit_order({"time": "2026-07-07 12:30", "side": "sell", "is_call": True, "notional": 1e6,
                "tenor_days": 30, "strike": "ATMF"})
atmf.run(until="2026-07-07 14:00")
booked = atmf.orders[-1]
T_book = atmf.years(booked["expiry"]) - atmf.snapshot.time()
check(booked["strike_value"] == atmf.snapshot.forward(T_book), "pending ATMF uses the forward at its booking fixing")
check(booked["strike_value"] != forward_at_submit, "and not the forward from when it was submitted")
q = atmf.quote({"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 91, "strike": "ATMF"})
check(q["strike"] == q["forward"], "quote at ATMF prices at the forward")
check(raises(atmf.submit_order, {"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 91, "strike": "ATMX"}),
      "unknown strike words are refused")
print(f"3b. ATMF 3M: strike = forward {F:.5f} (DNS ATM {dns:.5f}, {(dns - F) * 1e4:.1f} pips higher); "
      f"call = put = {call_row['mid']:.6f}; pending ATMF uses the booking fixing's forward")

# ---- 4. Cancel + quote ----
pending = s.submit_order({"time": "2026-07-08 10:00", "side": "buy", "is_call": True,
                          "notional": 1e6, "tenor_days": 14})
s.cancel_order(pending["order_id"])
check(raises(s.cancel_order, pending["order_id"]), "cancelling twice should refuse")
s.run(until="2026-07-08 10:00")
check(len(s.book.portfolio.get_options()) == 2, "a cancelled order must never book")
q = s.quote({"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
check(q["bid"] < q["mid"] < q["ask"] and q["our_price"] == q["ask"], "client buys at our ask")
q2 = s.quote({"side": "sell", "is_call": True, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
check(q2["our_price"] == q2["bid"], "client sells at our bid")
check(len(s.book.portfolio.get_options()) == 2, "quote must not book anything")
print(f"4. cancel works; quote 3M 25D call: bid {q['bid']:.5f} / mid {q['mid']:.5f} / ask {q['ask']:.5f} "
      f"(vol {q['vol']*100:.2f} +/- 0.10)")

# ---- 5. Netting keeps NAV and Greeks ----
snap0 = market.get_snapshot("2026-08-03 10:00", sim_start="2026-07-07 10:00")
book = Book()
K = snap0.atm_strike(30 / 365)
expiry = snap0.time() + 30 / 365
ids = []
ids.append(book.book_option(K, expiry, True, 4e6, 0.01, time=snap0.time()))
ids.append(book.book_option(K, expiry, True, -1.5e6, 0.01, time=snap0.time()))
ids.append(book.book_option(K, expiry, False, 2e6, 0.01, time=snap0.time()))
ids.append(book.book_option(K, expiry, False, -2e6, 0.01, time=snap0.time()))
ids.append(book.book_option(K, expiry, True, 1e6, 0.01, time=snap0.time()))
client = book.book_option(K, expiry, True, -3e6, 0.01, time=snap0.time())     # not in the list
nav0 = book.nav(snap0)
g0 = book.greeks(snap0)
cash0 = book.cash_cny
merged = book.net_positions(ids, time=snap0.time())
g1 = book.greeks(snap0)
left = {}
for pos in book.portfolio.get_options():
    left[pos.id] = pos.notional
check(abs(book.nav(snap0) - nav0) < 1e-6 and book.cash_cny == cash0, "netting must not change NAV or cash")
same = True
for name in ["delta", "gamma", "vega", "vanna", "volga", "theta"]:
    if abs(g1[name] - g0[name]) > 1e-9 * max(1.0, abs(g0[name])):
        same = False
check(same, "netting must not change Greeks")
check(left == {ids[0]: 3.5e6, client: -3e6}, f"calls should merge to 3.5m, puts vanish, client untouched: {left}")
print(f"5. netting: 5 legs -> 1 (calls 4m-1.5m+1m = 3.5m, puts +2m-2m gone), client leg untouched, "
      f"NAV and Greeks unchanged")

# ---- 6. Full run, end_of_day policy, independent checks at every fixing ----
ORDERS = [
    ("2026-07-07 10:00", "buy",  True,  10e6, 30,  "ATM"),
    ("2026-07-07 11:00", "sell", False,  5e6, 7,   "25D"),
    ("2026-07-08 14:00", "buy",  True,  20e6, 91,  "25D"),
    ("2026-07-10 15:00", "buy",  False,  8e6, 14,  "25D"),
    ("2026-07-13 10:00", "buy",  True,   6e6, 5,   "ATM"),     # expires Saturday 18 Jul
    ("2026-07-15 12:30", "buy",  True,   5e6, 7,   "ATM"),     # between fixings
    ("2026-08-03 16:00", "sell", True,  12e6, 30,  "25D"),
    ("2026-08-25 10:00", "buy",  True,  15e6, 14,  "ATM"),
    ("2026-08-25 10:00", "buy",  False, 15e6, 14,  "ATM"),
    ("2026-09-01 14:00", "buy",  False, 10e6, 182, "10D"),
    ("2026-09-21 11:00", "buy",  True,   5e6, 7,   "ATM"),
]
s = Session(market=market, policy="end_of_day")
for t, side, is_call, notional, days, strike in ORDERS:
    s.submit_order({"time": t, "side": side, "is_call": is_call, "notional": notional,
                    "tenor_days": days, "strike": strike})

nav_errors = 0
delta_errors = 0
smile_errors = 0
smile_hedges = 0
smile_off_close = 0
max_nav_gap = 0.0
while not s.done():
    step = s.next()
    gap = abs(step["nav_cny"] - independent_nav(s))
    max_nav_gap = max(max_nav_gap, gap)
    if gap > 1e-3:
        nav_errors += 1
    if abs(s.book.net_delta(s.snapshot)) > 1e-3:
        delta_errors += 1
    if len(step["hedges"]["packages"]) > 0:
        smile_hedges += 1
        if not s._is_end_of_day():
            smile_off_close += 1
        after = s.book.portfolio.greeks_by_expiry(s.snapshot)
        hedged_buckets = set()
        for pkg in step["hedges"]["packages"]:
            hedged_buckets.add(pkg["bucket"])
        # Flat up to solver precision (scales with the bucket's gross Greek), plus whatever
        # a skipped package leaves: packages under MIN_HEDGE_NOTIONAL (1 USD) aren't traded,
        # and 1 USD of a package carries well under 1 CNY of vega / vanna / volga
        gross = {}
        for row in s.book.positions(s.snapshot):
            label = bucket_of(row["T"])
            if label not in gross:
                gross[label] = {"vega": 0.0, "vanna": 0.0, "volga": 0.0}
            for greek in gross[label]:
                gross[label][greek] += abs(row[greek])
        for label in hedged_buckets:
            a = after[CPP_BUCKET_KEY[label]]
            for greek in ["vega", "vanna", "volga"]:
                if abs(getattr(a, greek)) > 1e-8 * gross[label][greek] + 1.0:
                    smile_errors += 1
    for o in step["orders_booked"]:
        check(abs(o["edge_cny"]) < 1e-9, "orders without a price book at mid")

check(nav_errors == 0, f"NAV differs from the independent repricing at {nav_errors} fixings")
check(delta_errors == 0, f"delta not flat at {delta_errors} fixings")
check(smile_errors == 0, f"{smile_errors} hedged bucket Greeks not flat after a smile hedge")
check(smile_off_close == 0, "end_of_day should smile-hedge only at the last fixing of a day")
days = set()
for t in s.times:
    days.add(t.normalize())
check(smile_hedges == len(days), f"{smile_hedges} smile hedges for {len(days)} trading days")

# Orders: every one booked, only the 12:30 one late
booked_late = []
for o in s.orders:
    check(o["status"] == "booked", f"order {o['order_id']} is {o['status']}")
    if o["booked_at"] != o["time"]:
        booked_late.append((o["time"], o["booked_at"]))
check(booked_late == [(pd.Timestamp("2026-07-15 12:30"), pd.Timestamp("2026-07-15 14:00"))],
      f"only the 12:30 order should wait until 14:00: {booked_late}")

# Settlements of client options: at the first fixing on/after expiry, intrinsic
client_ids = {}
for o in s.orders:
    client_ids[o["position_id"]] = o
settlements = 0
for record in s.history():
    if record["type"] != "step":
        continue
    for row in record["settled"]:
        if row["position_id"] not in client_ids:
            continue
        o = client_ids[row["position_id"]]
        settled_at = pd.Timestamp(row["time"])
        first = None
        for t in s.times:
            if t >= o["expiry"]:
                first = t
                break
        check(settled_at == first, f"order {o['order_id']} expiring {o['expiry']} settled {settled_at}, expected {first}")
        if o["is_call"]:
            payoff = max(row["spot"] - o["strike_value"], 0.0)
        else:
            payoff = max(o["strike_value"] - row["spot"], 0.0)
        check(abs(row["cash_cny"] - row["notional"] * payoff) < 1e-6, "settlement must be intrinsic")
        settlements += 1
expiring = 0
for o in s.orders:
    if o["expiry"] <= s.times[-1]:
        expiring += 1
check(settlements == expiring, f"{settlements} client settlements, expected {expiring}")
saturday = None
for o in s.orders:
    if o["expiry"].dayofweek == 5:
        saturday = o
for record in s.history():
    for row in record.get("settled", []):
        if row["position_id"] == saturday["position_id"]:
            check(record["time"] == "2026-07-20T10:00:00", "Saturday expiry should settle Monday 10:00")

# P&L bookkeeping: every NAV change is market P&L or trading P&L, and they add up
total = 0.0
previous_nav = 0.0
chain_ok = True
for record in s.history():
    if record["type"] == "start":
        continue
    change = record.get("market_pnl_cny", 0.0) + record.get("trade_pnl_cny", 0.0)
    if abs(record["nav_cny"] - previous_nav - change) > 1e-4:
        chain_ok = False
    total += change
    previous_nav = record["nav_cny"]
final_nav = s.book.nav(s.snapshot)
check(chain_ok, "each record's NAV change should be its market + trade P&L")
check(abs(total - final_nav) < 1e-3, f"P&L adds to {total}, NAV is {final_nav}")

state = s.state()
hedge_positions = 0
for row in state["positions"]:
    if row["source"] == "hedge":
        hedge_positions += 1
legs_booked = 0
for trade in s.book.trades:
    if trade["type"] == "option" and "hedge" in trade["note"]:
        legs_booked += 1
print(f"6. full run, end_of_day: {len(s.times)} fixings, 11 orders, {smile_hedges} smile hedges "
      f"(one per day, at the close), delta flat every fixing")
print(f"   NAV vs independent repricing: worst gap {max_nav_gap:.1e} CNY; {settlements} client settlements "
      f"at the right fixing, intrinsic; Saturday expiry -> Mon 10:00")
print(f"   hedge legs booked {legs_booked} -> {hedge_positions} live after netting and expiries; "
      f"final NAV {final_nav:,.0f} CNY = sum of step P&L")

# ---- 7. JSON ----
try:
    as_json(s.state())
    as_json(s.history())
    as_json(s.blotter())
    as_json(s.recommend_hedge())
    json_ok = True
except (TypeError, ValueError) as error:
    json_ok = False
    print("   ", error)
check(json_ok, "state / history / blotter / plan must be plain JSON")
print(f"7. state, history ({len(s.history())} records), blotter ({len(s.blotter())} trades) and plans are plain JSON")

# ---- 8. manual policy ----
s = Session(market=market, policy="manual")
s.submit_order({"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
s.submit_order({"side": "buy", "is_call": False, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
s.run(until="2026-07-07 16:00")
spot_trades = 0
for t in s.book.trades:
    if t["type"] in ["spot", "package"]:
        spot_trades += 1
check(spot_trades == 0 and s.book.hedge_usd == 0, "manual policy must never trade by itself")
plan = s.recommend_hedge()
check(len(s.book.trades) == 2, "recommend_hedge must not trade")
s.next()
check(raises(s.hedge, plan), "a plan from an earlier fixing must be refused")
result = s.hedge()
after = s.book.portfolio.greeks_by_expiry(s.snapshot)
flat = True
for pkg in result["packages"]:
    a = after[CPP_BUCKET_KEY[pkg["bucket"]]]
    for greek in ["vega", "vanna", "volga"]:
        if abs(getattr(a, greek)) > 1e-3:
            flat = False
check(flat and abs(s.book.net_delta(s.snapshot)) < 1e-3, "hedge() should flatten buckets and delta")
check(abs(s.history()[-1]["trade_pnl_cny"]) < 1e-6, "hedging at mid costs nothing at mid")
print(f"8. manual: no trades by itself; stale plan refused; hedge() booked {len(result['packages'])} packages "
      f"+ spot, buckets and delta flat")

# ---- 9. threshold policy ----
s = Session(market=market, policy="threshold", limits={"delta_usd": 2e6, "vega_cny_per_volpt": 20_000})
s.submit_order({"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 30})
check(s.book.hedge_usd == 0, "a 1m ATM call (~0.5m delta) is inside a 2m delta limit - no hedge")
s.submit_order({"side": "buy", "is_call": True, "notional": 20e6, "tenor_days": 91})
check(abs(s.book.net_delta(s.snapshot)) < 1e-3, "breaching the limit should hedge")
breaches = 0
while s.now() < pd.Timestamp("2026-08-14 16:00"):
    step = s.next()
    if abs(s.book.net_delta(s.snapshot)) > 2e6 + 1e-6:
        breaches += 1
    for label, g in s.state()["greeks_by_bucket"].items():
        if abs(g["vega"]) / 100 > 20_000 + 1e-6:
            breaches += 1
check(breaches == 0, f"limits left breached after {breaches} steps")
reasons = {}
for record in s.history():
    if "hedges" in record and record["hedges"].get("reason"):
        reasons[record["hedges"]["reason"]] = reasons.get(record["hedges"]["reason"], 0) + 1
check(raises(Session, None, None, "threshold", {"gamma": 1}, market), "unknown limit refused")

# Delta limit on its own: vega limit out of reach, so only spot moves can trigger
s = Session(market=market, policy="threshold", limits={"delta_usd": 1e5, "vega_cny_per_volpt": 1e12})
s.submit_order({"side": "buy", "is_call": True, "notional": 20e6, "tenor_days": 30})
delta_hedges = 0
worst = 0.0
while s.now() < pd.Timestamp("2026-07-31 16:00"):
    step = s.next()
    if step["hedges"]["reason"] == "delta limit":
        delta_hedges += 1
        check(abs(s.book.net_delta(s.snapshot)) < 1e-3, "a delta-limit hedge should flatten delta")
    check(len(step["hedges"]["packages"]) == 0, "vega limit out of reach - no smile hedges")
    worst = max(worst, abs(s.book.net_delta(s.snapshot)))
check(delta_hedges > 0 and worst <= 1e5 + 1e-6, f"delta hedges {delta_hedges}, worst delta left {worst:,.0f}")
reasons["delta limit (separate run)"] = delta_hedges
print(f"9. threshold: small trade left alone, limits never left breached; hedges by reason {reasons}")

# ---- 10. Late start, and the surface picks VV / SABR by itself ----
s = Session(market=market, start="2026-09-24 10:00")
s.submit_order({"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30, "strike": "25D"})
s.run()
check(abs(s.book.nav(s.snapshot) - independent_nav(s)) < 1e-3, "late-start session NAV")
check(s.snapshot.time() > 0 and s.times[0] == pd.Timestamp("2026-09-24 10:00"), "clock starts at the session start")
check(raises(Session, None, None, "hedge_everything"), "unknown policy refused")
check(raises(Session, "2026-12-01", None), "no fixings in range refused")
print(f"10. session from 24 Sep: {len(s.times)} fixings, NAV {s.book.nav(s.snapshot):,.0f} CNY; bad policy / empty range refused")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")