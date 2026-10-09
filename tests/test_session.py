"""
Checks for the Session: clock, orders, settlement, P&L bookkeeping, hedge policies,
netting and JSON output. Nothing here trusts the Session's own numbers where it can
be rebuilt independently.
"""
import sys
import json
import math
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

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

# ---- 3c. Client vs hedge orders: the tag decides whose side "side" is ----
tg = Session(market=market, policy="manual")
terms = {"is_call": True, "notional": 5e6, "strike": 6.80, "expiry": "2026-08-07 10:00"}
client_buy = tg.submit_order(dict(terms, side="buy"))                     # client buys -> we sell
hedge_buy = tg.submit_order(dict(terms, side="buy", tag="hedge"))         # we buy
check(client_buy["tag"] == "client" and client_buy["our_side"] == "sell", "default tag is client; client buy = our sell")
check(hedge_buy["tag"] == "hedge" and hedge_buy["our_side"] == "buy", "hedge order: side is our side")
notionals = {}
for pos in tg.book.portfolio.get_options():
    notionals[pos.id] = pos.notional
check(notionals[client_buy["position_id"]] == -5e6 and notionals[hedge_buy["position_id"]] == 5e6,
      f"client buy books -5m, hedge buy books +5m: {notionals}")
# Same terms, opposite sides: the options cancel exactly
g = tg.book.greeks(tg.snapshot)
check(abs(g["option_delta"]) < 1e-6 and abs(g["vega"]) < 1e-6 and abs(g["gamma"]) < 1e-6,
      "a hedge buy against a client buy of the same option leaves no option risk")
check(abs(tg.book.nav(tg.snapshot)) < 1e-6, "both at mid: NAV still 0")
sources = {}
for row in tg.state()["positions"]:
    sources[row["position_id"]] = row["source"]
check(sources[client_buy["position_id"]] == "client" and sources[hedge_buy["position_id"]] == "hedge",
      "state() shows who each position is with")
notes = []
for trade in tg.blotter():
    if trade["type"] == "option":
        notes.append(trade["note"])
check("client order #1: client buys" in notes and "hedge order #2: we buy" in notes, f"blotter notes: {notes}")
# A second identical hedge leg nets into the first; the client leg is never netted
hedge_buy_2 = tg.submit_order(dict(terms, side="buy", tag="hedge"))
left = {}
for pos in tg.book.portfolio.get_options():
    left[pos.id] = pos.notional
check(left == {client_buy["position_id"]: -5e6, hedge_buy["position_id"]: 10e6},
      f"identical hedge legs merge (client leg untouched): {left}")
check(abs(tg.book.nav(tg.snapshot)) < 1e-6, "netting leaves NAV unchanged")
# Pending hedge order books later, still as a hedge
pending_hedge = tg.submit_order({"time": "2026-07-07 12:30", "side": "sell", "tag": "hedge", "is_call": False,
                                 "notional": 2e6, "tenor_days": 30, "strike": "25D"})
check(pending_hedge["status"] == "pending" and pending_hedge["our_side"] == "sell", "pending hedge order waits")
tg.run(until="2026-07-07 14:00")
booked_hedge = tg.orders[-1]
check(booked_hedge["status"] == "booked" and booked_hedge["position_id"] in tg.hedge_ids,
      "a pending hedge order books as a hedge")
for pos in tg.book.portfolio.get_options():
    if pos.id == booked_hedge["position_id"]:
        check(pos.notional == -2e6, "hedge sell books -2m")
# Quotes: a client deals on our bid/ask; on a hedge we pay the market's ask and hit its bid
q_cb = tg.quote(dict(terms, side="buy", tag="client"))
q_cs = tg.quote(dict(terms, side="sell", tag="client"))
q_hb = tg.quote(dict(terms, side="buy", tag="hedge"))
q_hs = tg.quote(dict(terms, side="sell", tag="hedge"))
check(q_cb["our_price"] == q_cb["ask"] and q_cs["our_price"] == q_cs["bid"], "client buys at our ask, sells at our bid")
check(q_hb["our_price"] == q_hb["ask"] and q_hs["our_price"] == q_hs["bid"], "hedge: we buy at the ask, sell at the bid")
check(raises(tg.submit_order, dict(terms, side="buy", tag="prop")), "unknown tag refused")
print("3c. tag: client buy -> -5m, hedge buy -> +5m, same option cancels to zero risk; state/blotter show the tag; "
      "identical hedge legs net, client legs don't; quotes take the right side")

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

# ---- 11. Inputs that used to slip through (found in review) ----
s = Session(market=market, policy="manual")
bad = 0
for field, value in [("notional", float("nan")), ("notional", float("inf")), ("price", float("nan")),
                     ("strike", float("nan")), ("tenor_days", float("inf")), ("tenor_days", -3)]:
    order = {"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7}
    order[field] = value
    if raises(s.submit_order, order):
        bad += 1
check(bad == 6, f"NaN / infinity / negative tenor must be refused ({bad} of 6)")
check(raises(Session, None, None, "threshold", {"delta_usd": float("nan")}, market), "a NaN limit is refused")
check(raises(s.submit_order, {"side": "buy", "is_call": True, "notional": 1e6,
                              "expiry": "2026-07-07 10:00:30"}), "expiry under a minute after the order is refused")
check(raises(s.submit_order, {"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7, "time": "not a time"}),
      "a non-date time is refused")
check(len(s.orders) == 0 and len(s.book.portfolio.get_options()) == 0, "refused orders leave no trace")
# times with a timezone are converted to Beijing time (the fixings' clock)
row = s.submit_order({"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7,
                      "time": "2026-07-07T02:00:00Z"})             # = 10:00 Beijing
check(row["status"] == "booked" and row["booked_at"] == "2026-07-07T10:00:00", "UTC time converted to Beijing time")
tz_session = Session(market=market, start="2026-07-08T02:00:00Z", end="2026-07-08 16:00")
check(tz_session.now() == pd.Timestamp("2026-07-08 10:00"), "a session start with a timezone works")
tz_session.run(until="2026-07-08T06:00:00Z")                        # = 14:00 Beijing
check(tz_session.now() == pd.Timestamp("2026-07-08 14:00"), "run(until=...) with a timezone works")
check(raises(tz_session.run, -1), "negative steps refused")
# a pending order that would expire seconds after its booking fixing is rejected cleanly, not half-booked
s = Session(market=market, policy="manual")
s.submit_order({"side": "buy", "is_call": True, "notional": 1e6, "time": "2026-07-07 10:30",
                "expiry": "2026-07-07 11:00:00.010"})
step = s.next()                                                     # 11:00: 10 ms before expiry
check(step["orders_booked"][0]["status"] == "rejected" and len(s.book.portfolio.get_options()) == 0,
      "an order 10 ms from expiry at its booking fixing is rejected, the step completes")
# a booking that fails takes the order back out, so order ids stay 1, 2, 3...
s = Session(market=market, policy="manual")
real_book_option = s.book.book_option


def failing_book_option(*args, **kwargs):
    raise ValueError("simulated booking failure")


s.book.book_option = failing_book_option
check(raises(s.submit_order, {"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7}), "booking failure raises")
s.book.book_option = real_book_option
row = s.submit_order({"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7})
check(row["order_id"] == 1 and len(s.orders) == 1, "a failed booking leaves no ghost order; the next order is #1")
print("11. NaN / infinity / bad times refused; timezones converted; a near-expiry pending order is rejected "
      "cleanly; a failed booking leaves no ghost order")

# ---- 12. Hedge plans can only confirm, never set, what is booked ----
import copy
s = Session(market=market, policy="manual")
s.submit_order({"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
s.submit_order({"side": "buy", "is_call": False, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
plan = s.recommend_hedge()
trades_before = len(s.book.trades)
# edited prices are ignored: the hedge books at this fixing's own mids, so NAV doesn't move
cheap = copy.deepcopy(plan)
for item in cheap["buckets"]:
    for kind in item.get("packages", {}):
        for leg in item["packages"][kind]["legs"]:
            leg["mid"] = 0.0
nav_before = s.book.nav(s.snapshot)
s2 = Session(market=market, policy="manual")        # same book, to test the edited plan on
s2.submit_order({"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
s2.submit_order({"side": "buy", "is_call": False, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
s2.hedge(cheap)
check(abs(s2.book.nav(s2.snapshot)) < 1e-4, f"zeroed mids must not create P&L, NAV {s2.book.nav(s2.snapshot):,.2f}")
# edited sizes, junk buckets, missing buckets, and a plan made before the book changed: nothing booked
edited = copy.deepcopy(plan)
for item in edited["buckets"]:
    for kind in item.get("packages", {}):
        item["packages"][kind]["notional"] *= 2
junk = copy.deepcopy(plan)
junk["buckets"].append({"bucket": "junk"})
no_buckets = {"time": plan["time"]}
refused = 0
for bad_plan in [edited, junk, no_buckets, {"buckets": []}, "not a plan"]:
    if raises(s.hedge, bad_plan):
        refused += 1
check(refused == 5 and len(s.book.trades) == trades_before, f"edited / malformed plans refused before booking ({refused} of 5)")
s.submit_order({"side": "sell", "is_call": True, "notional": 5e6, "tenor_days": 60})     # the book changes
check(raises(s.hedge, plan), "a plan made before the book changed is refused")
fresh = s.recommend_hedge()
s.hedge(fresh)
check(abs(s.book.net_delta(s.snapshot)) < 1e-3, "a current plan books normally")
print("12. hedge plans: edited prices are ignored (booked at mid, NAV 0); edited sizes, junk, missing parts and "
      "out-of-date plans are refused before anything is booked")

# ---- 13. Spot trades and the market screen ----
s = Session(policy="manual", market=market)
spot = s.snapshot.spot()
row = s.trade_spot({"side": "buy", "notional": 2e6, "price": spot + 0.0010})       # client buys at +10 pips
check(row["our_side"] == "sell" and abs(row["edge_cny"] - 2000.0) < 1e-6, "client spot buy: we sell, 10 pips x 2m = 2,000 edge")
check(abs(s.book.nav(s.snapshot) - 2000.0) < 1e-6 and abs(s.book.hedge_usd + 2e6) < 1e-6, "NAV = edge, short 2m USD")
row = s.trade_spot({"side": "sell", "notional": 2e6, "tag": "hedge"})               # we sell 2m more, at mid
check(row["our_side"] == "sell" and row["edge_cny"] == 0.0 and abs(s.book.hedge_usd + 4e6) < 1e-6, "hedge side is ours")
for bad in [{"side": "buy", "notional": -1}, {"side": "up", "notional": 1e6}, {"side": "buy", "notional": 1e6, "price": 0},
            {"side": "buy", "notional": 1e6, "tag": "friend"}, {"side": "buy", "notional": float("nan")}]:
    check(raises(s.trade_spot, bad), f"refused: {bad}")
check(abs(s.pnl_summary()["trading"] - 2000.0) < 1e-6, "spot edge counts as trading P&L")

s_eod = Session(policy="end_of_day", market=market)
s_eod.trade_spot({"side": "buy", "notional": 5e6})
check(abs(s_eod.book.net_delta(s_eod.snapshot)) < 1e-6, "end_of_day policy flattens delta after a client spot trade")
s_eod.trade_spot({"side": "buy", "notional": 5e6, "tag": "hedge"})
check(abs(s_eod.book.net_delta(s_eod.snapshot) - 5e6) < 1e-6, "a hedge trade is left as it is (no policy run)")

view = s.market_view()
snap = s.snapshot
for tenor in view["tenors"]:
    for pillar in tenor["pillars"]:
        T = tenor["days"] / 365.0
        # VV inside 25D goes through the quotes exactly; the 10D wings come from the SABR fit
        # (or the bridge to it), which is close to the 10D quotes but not forced through them
        gap = abs(snap.vol(pillar["strike"], T) - pillar["vol"])
        if pillar["pillar"] in ["25P", "ATM", "25C"]:
            check(gap < 1e-6, f"{tenor['tenor']} {pillar['pillar']}: surface returns the quoted vol")
        else:
            check(gap < 0.0015, f"{tenor['tenor']} {pillar['pillar']}: surface within 0.15 vol pt of the quote ({gap:.5f})")
    check(abs(tenor["forward"] - snap.forward(tenor["days"] / 365.0)) < 1e-6, f"{tenor['tenor']} forward matches the snapshot")
json.dumps(view, allow_nan=False)
print(f"13. spot trades: sides, edge, NAV and policy as expected; market screen: {len(view['tenors'])} tenors, "
      f"ATM / 25D vols exact, 10D within 0.15 vol pt (SABR wing), forwards agree")

# ---- 14. Orders agreed in vol ----
s = Session(policy="manual", market=market)
base = {"side": "buy", "is_call": True, "notional": 10e6, "strike": "25D", "tenor_days": 30}
q = s.quote(base)
order = dict(base); order["vol"] = q["vol"]
row = s.submit_order(order)
check(abs(row["booked_price"] - row["mid"]) < 1e-12 and abs(row["edge_cny"]) < 1e-4, "booked at the mid vol -> the mid premium, no edge")
check(row["booked_vol"] == q["vol"] and abs(row["mid_vol"] - q["vol"]) < 1e-15, "booked and mid vol recorded")
order = dict(base); order["vol"] = q["ask_vol"]
row = s.submit_order(order)
check(abs(row["booked_price"] - q["ask"]) < 1e-12, "client buys at the ask vol -> exactly the ask premium")
check(abs(row["edge_cny"] - 10e6 * (q["ask"] - q["mid"])) < 1e-4 and row["edge_cny"] > 0, "edge = notional x (ask - mid)")
q2 = s.quote(order)
check(abs(q2["agreed_price"] - q["ask"]) < 1e-12 and abs(q2["agreed_edge_vol"] - 0.001) < 1e-9, "quote prices the agreed vol: +0.10 vol pt to us")
hedge = dict(base); hedge["tag"] = "hedge"; hedge["vol"] = q["vol"] + 0.002
check(abs(s.quote(hedge)["agreed_edge_vol"] + 0.002) < 1e-9, "a hedge buying 0.2 pt over mid costs 0.2 pt")

# a waiting order agreed in vol is priced at its booking fixing (spot and rates of that fixing)
later = dict(base); later["vol"] = 0.025; later["time"] = s.times[2].isoformat()
s.submit_order(later)
s.next(); s.next()
booked = s.order_rows()[-1]
T = s.years(pd.Timestamp(booked["expiry"])) - s.snapshot.time()
pricer = fxengine.GKPricer(s.snapshot.domestic_rate(T), s.snapshot.foreign_rate(T))
expected = pricer.price(s.snapshot.spot(), booked["strike_value"], T, 0.025, True).price
check(booked["status"] == "booked" and abs(booked["booked_price"] - expected) < 1e-12,
      "a waiting order agreed in vol books at the booking fixing's spot and rates")

for bad in [0.0, -0.01, 5.0, float("nan"), "2%"]:
    order = dict(base); order["vol"] = bad
    check(raises(s.quote, order) and raises(s.submit_order, order), f"vol {bad!r} refused")
both = dict(base); both["vol"] = 0.02; both["price"] = 0.006
check(raises(s.submit_order, both), "price and vol together refused")
json.dumps(s.order_rows(), allow_nan=False)
print("14. orders agreed in vol: mid vol = mid premium, ask vol = ask premium, waiting orders priced at their "
      "booking fixing, bad vols and price + vol together refused")

# ---- 15. Sanity bands: no trade at a price nobody would deal ----
from simulation.session import implied_vol, MAX_VOL_OFF_MID, MAX_SPOT_OFF
s = Session(policy="manual", market=market)
base = {"side": "buy", "is_call": True, "notional": 10e6, "strike": "25D", "tenor_days": 30}
q = s.quote(base)
T = 30 / 365.0
pricer = fxengine.GKPricer(s.snapshot.domestic_rate(T), s.snapshot.foreign_rate(T))
check(abs(implied_vol(pricer, s.snapshot.spot(), q["strike"], T, q["ask"], True) - q["ask_vol"]) < 1e-12,
      "implied vol of the ask premium is the ask vol")
check(implied_vol(pricer, s.snapshot.spot(), q["strike"], T, 10.0, True) is None, "a premium above spot (no vol gives it) -> None")

row = s.submit_order(dict(base, price=q["ask"]))
check(abs(row["booked_vol"] - q["ask_vol"]) < 1e-12, "an order agreed in premium records its implied vol")
row = s.submit_order(dict(base, vol=q["our_vol"]))
check(abs(row["booked_price"] - q["our_price"]) < 1e-15, "booking at our_vol gives exactly our_price")
trades_before = len(s.book.trades)
orders_before = len(s.orders)
nav_before = s.book.nav(s.snapshot)
high = q["vol"] * (1 + MAX_VOL_OFF_MID)
for bad in [dict(base, price=0.0), dict(base, price=9.9999), dict(base, vol=0.0001),
            dict(base, vol=high * 1.001), dict(base, price=q["mid"] * 3)]:
    check(raises(s.submit_order, bad), f"refused: {bad}")
check(not raises(s.submit_order, dict(base, vol=high * 0.999)), "just inside the band books")
check(raises(s.quote, dict(base, vol=0.0001)), "the quote refuses an agreed vol outside the band too")
spot = s.snapshot.spot()
check(raises(s.trade_spot, {"side": "buy", "notional": 1e6, "price": 67.0}), "spot rate 10x spot refused")
check(raises(s.trade_spot, {"side": "buy", "notional": 1e6, "price": spot * (1 + MAX_SPOT_OFF) * 1.001}), "spot rate just over 2% refused")
check(not raises(s.trade_spot, {"side": "buy", "notional": 1e6, "price": spot * (1 + MAX_SPOT_OFF) * 0.999}), "just inside 2% books")
check(len(s.orders) == orders_before + 1 and s.order_count == len(s.orders), "refused orders leave no trace; ids stay 1, 2, 3")

# a waiting order whose agreed premium is out of the band at its fixing is rejected there, the step goes on
s.submit_order(dict(base, price=q["mid"] * 3, time=s.times[1].isoformat()))
step = s.next()
check(step["orders_booked"][0]["status"] == "rejected" and "outside" in step["orders_booked"][0]["reason"],
      "a waiting order outside the band is rejected at its fixing with the reason")
json.dumps(s.order_rows(), allow_nan=False)
print("15. sanity bands: premiums / vols outside mid vol +-50% and spot rates over 2% from spot refused "
      "(waiting orders rejected at their fixing); implied vol recorded; our_vol books exactly at our_price")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")