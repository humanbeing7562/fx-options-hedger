"""
Checks for the P&L decomposer (sim/pnl.py) and its use in the Session.

  1. Full desk run: every step's stages add up to the market P&L exactly, start at the
     previous NAV and end at the NAV after settlement; buckets + hedge/cash add up to each
     stage; carry is the Book's interest; rates is exactly 0 within a day; the summary
     adds up to the NAV
  2. One option between two real fixings: every stage rebuilt by hand with GKPricer
  3. Cash and USD hedge only: carry is the interest, spot is hedge x dS, nothing else moves
  4. An option that expires inside the step ends at its settlement cash
  5. A pure spot move: time / rates / vol are exactly 0, and "other" shrinks like dS^3
  6. A pure vol move: vega explains it, the rest is small
  7. Everything is plain JSON
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
from simulation.market_loader import MarketLoader, years_between
from simulation.pnl import decompose_step, option_value, STAGES
from simulation.session import Session

market = MarketLoader()
problems = 0


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


def shifted(m, spot=None, surface_from=None, curves_from=None):
    """A copy of a market dict with some pieces swapped."""
    result = dict(m)
    if spot is not None:
        result["spot"] = spot
    if surface_from is not None:
        result["surface"] = surface_from["surface"]
    if curves_from is not None:
        result["cny"] = curves_from["cny"]
        result["usd"] = curves_from["usd"]
    return result


# ---- 1. Full desk run ----
ORDERS = [
    ("2026-07-07 10:00", "buy",  True,  10e6, 30,  "ATM"),
    ("2026-07-07 11:00", "sell", False,  5e6, 7,   "25D"),
    ("2026-07-08 14:00", "buy",  True,  20e6, 91,  "25D"),
    ("2026-07-13 10:00", "buy",  True,   6e6, 5,   "ATM"),      # expires on a Saturday
    ("2026-07-15 12:30", "buy",  True,   5e6, 7,   "ATMF"),
    ("2026-08-03 16:00", "sell", True,  12e6, 30,  "25D"),
    ("2026-09-01 14:00", "buy",  False, 10e6, 182, "10D"),
]
s = Session(market=market, policy="end_of_day")
for t, side, is_call, notional, days, strike in ORDERS:
    s.submit_order({"time": t, "side": side, "is_call": is_call, "notional": notional,
                    "tenor_days": days, "strike": strike})
worst_sum = 0.0
worst_start = 0.0
worst_end = 0.0
expiring_steps = 0
worst_bucket = 0.0
worst_carry = 0.0
intraday_steps = 0
intraday_rates = 0
while not s.done():
    step = s.next()
    e = step["pnl_explain"]
    stage_sum = 0.0
    for stage in STAGES:
        stage_sum += e[stage]
    worst_sum = max(worst_sum, abs(stage_sum - step["market_pnl_cny"]), abs(e["total"] - step["market_pnl_cny"]))
    nav_after_settle = step["nav_cny"] - step["trade_pnl_cny"]
    worst_end = max(worst_end, abs(e["nav_end"] - nav_after_settle))
    if e["by_bucket"]["expiring"]["positions"] > 0:
        expiring_steps += 1
    # buckets (options) + the hedge/cash line add up to every stage
    for stage in ["time", "spot", "rates", "vol"]:
        bucket_total = 0.0
        for label in e["by_bucket"]:
            bucket_total += e["by_bucket"][label][stage]
        if stage == "spot":
            bucket_total += e["hedge_and_cash"]["spot"]
        worst_bucket = max(worst_bucket, abs(bucket_total - e[stage]))
    # carry is exactly the interest the Book booked
    worst_carry = max(worst_carry, abs(e["carry"] - step["carry_cny"]))
    # curves are daily: within a day the rates stage must be exactly 0
    if pd.Timestamp(step["time"]).normalize() == pd.Timestamp(step["from"]).normalize():
        intraday_steps += 1
        if e["rates"] != 0.0:
            intraday_rates += 1
# nav_start must equal the NAV after whatever happened before the step
records = s.history()
for i in range(1, len(records)):
    if records[i]["type"] == "step":
        worst_start = max(worst_start, abs(records[i]["pnl_explain"]["nav_start"] - records[i - 1]["nav_cny"]))
summary = s.pnl_summary()
check(worst_sum < 1e-6, f"stages should add up to the market P&L, worst gap {worst_sum:.2e}")
check(worst_start < 1e-6, f"the explain should start at the previous NAV, worst gap {worst_start:.2e}")
check(worst_end < 1e-6, f"the explain should end at the NAV after settlement, worst gap {worst_end:.2e}")
check(abs(summary["total"] - summary["nav_cny"]) < 1e-4, "summary should add up to the NAV")
check(expiring_steps > 0, "the run should include steps where options expire")
check(worst_bucket < 1e-6, f"buckets + hedge/cash should add up to each stage, worst gap {worst_bucket:.2e}")
check(worst_carry < 1e-6, f"carry should equal the interest the Book booked, worst gap {worst_carry:.2e}")
check(intraday_steps > 0 and intraday_rates == 0, f"{intraday_rates} of {intraday_steps} same-day steps had rates P&L")
print(f"1. full run, {summary['steps']} steps: stages = market P&L (worst gap {worst_sum:.1e}), "
      f"start/end NAVs match ({worst_start:.1e} / {worst_end:.1e}); {expiring_steps} steps with expiries")
print(f"   buckets + hedge/cash = stages ({worst_bucket:.1e}); carry = Book interest ({worst_carry:.1e}); "
      f"rates exactly 0 on all {intraday_steps} same-day steps")
print(f"   since the start (CNY): carry {summary['carry']:,.0f}, time {summary['time']:,.0f}, "
      f"spot {summary['spot']:,.0f}, rates {summary['rates']:,.0f}, vol {summary['vol']:,.0f}, "
      f"trading {summary['trading']:,.0f} = NAV {summary['nav_cny']:,.0f}")

# ---- 2. One option between two real fixings, rebuilt by hand ----
t_a = pd.Timestamp("2026-09-29 16:00")
t_b = pd.Timestamp("2026-09-30 10:00")
start_ts = pd.Timestamp("2026-09-01 10:00")
ma = market.get_market(t_a)
mb = market.get_market(t_b)
time_a = years_between(start_ts, t_a)
time_b = years_between(start_ts, t_b)
position = {"position_id": 1, "strike": 6.70, "expiry": time_a + 91 / 365, "is_call": True, "notional": -10e6}


def by_hand(time, spot, curves, surface):
    T = position["expiry"] - time
    rd = curves["cny"].zero_rate(T)
    rf = curves["usd"].zero_rate(T)
    vol = surface["surface"].get_vol(position["strike"], T)
    return position["notional"] * fxengine.GKPricer(rd, rf).price(spot, position["strike"], T, vol, True).price


e = decompose_step([position], {"time": time_a, "market": ma}, {"time": time_b, "market": mb}, 0.0, 0.0, 0.0, 0.0)
v0 = by_hand(time_a, ma["spot"], ma, ma)
v1 = by_hand(time_b, ma["spot"], ma, ma)
v2 = by_hand(time_b, mb["spot"], ma, ma)
v3 = by_hand(time_b, mb["spot"], mb, ma)
v4 = by_hand(time_b, mb["spot"], mb, mb)
expected = {"carry": 0.0, "time": v1 - v0, "spot": v2 - v1, "rates": v3 - v2, "vol": v4 - v3}
worst = 0.0
for stage in STAGES:
    worst = max(worst, abs(e[stage] - expected[stage]))
check(worst < 1e-8, f"stages should match the hand repricing, worst gap {worst:.2e}")
print(f"2. short 10m 3M 6.70 call, {t_a:%d %b %H:%M} -> {t_b:%d %b %H:%M}: time {e['time']:,.0f}, "
      f"spot {e['spot']:,.0f} ({e['dS_pips']:+.0f} pips), rates {e['rates']:,.0f}, vol {e['vol']:,.0f}; "
      f"hand repricing matches to {worst:.0e}")

# ---- 3. Cash and USD hedge only ----
hedge = 1e6
cash = -6.7e6
dt = time_b - time_a
r_cny = fxengine.MarketSnapshot(time_a, ma["spot"], ma["cny"], ma["usd"], ma["surface"]).domestic_rate(1 / 365)
r_usd = fxengine.MarketSnapshot(time_a, ma["spot"], ma["cny"], ma["usd"], ma["surface"]).foreign_rate(1 / 365)
hedge_after = hedge * math.exp(r_usd * dt)
cash_after = cash * math.exp(r_cny * dt)
e = decompose_step([], {"time": time_a, "market": ma}, {"time": time_b, "market": mb}, hedge, cash, hedge_after, cash_after)
carry_expected = (cash_after - cash) + (hedge_after - hedge) * ma["spot"]
check(abs(e["carry"] - carry_expected) < 1e-9, "carry should be the interest earned")
check(abs(e["spot"] - hedge_after * (mb["spot"] - ma["spot"])) < 1e-9, "spot should be the hedge times dS")
check(e["time"] == 0.0 and e["rates"] == 0.0 and e["vol"] == 0.0, "no options: time, rates, vol are 0")
check(abs(e["spot_split"]["other"]) < 1e-9 and abs(e["spot_split"]["gamma"]) < 1e-12, "spot is all delta for cash USD")
print(f"3. 1m USD + -6.7m CNY overnight: carry {e['carry']:,.2f} CNY, spot {e['spot']:,.2f} = 1m x dS; nothing else")

# ---- 4. An option expiring inside the step ends at its settlement cash ----
# Long 5m call, struck 10 big figures below spot so it finishes in the money
expiring = {"position_id": 2, "strike": 6.60, "expiry": time_a + 2 / 24 / 365, "is_call": True, "notional": 5e6}
e = decompose_step([expiring], {"time": time_a, "market": ma}, {"time": time_b, "market": mb}, 0.0, 0.0, 0.0, 0.0)
settle = 5e6 * max(mb["spot"] - 6.60, 0.0)
value_a = option_value(expiring, fxengine.MarketSnapshot(time_a, ma["spot"], ma["cny"], ma["usd"], ma["surface"]))
check(settle > 0, "this option should finish in the money")
check(abs(e["nav_end"] - settle) < 1e-9, "the final stage should equal the settlement cash")
check(abs(e["nav_start"] - value_a) < 1e-9, "the first stage should be its value before the step")
check(abs(e["time"] - (5e6 * (ma["spot"] - 6.60) - value_a)) < 1e-9, "time: from its value to intrinsic at spot A")
check(abs(e["spot"] - 5e6 * (mb["spot"] - ma["spot"])) < 1e-9, "spot: intrinsic moves one for one with spot")
check(e["rates"] == 0.0 and e["vol"] == 0.0, "an expired option has no rates or vol P&L")
check(e["by_bucket"]["expiring"]["positions"] == 1, "it is reported under 'expiring'")
print(f"4. long 5m 6.60 call expiring 2h after {t_a:%H:%M}: value {value_a:,.0f} -> settles {settle:,.0f} "
      f"(time {e['time']:,.0f}, spot {e['spot']:,.0f})")

# ---- 5. A pure spot move ----
book = [{"position_id": 1, "strike": 6.70, "expiry": time_a + 30 / 365, "is_call": True, "notional": -10e6},
        {"position_id": 2, "strike": 6.65, "expiry": time_a + 91 / 365, "is_call": False, "notional": 8e6}]
others = []
for move in [0.0005, 0.001, 0.002]:
    end_market = shifted(ma, spot=ma["spot"] * (1 + move))
    e = decompose_step(book, {"time": time_a, "market": ma}, {"time": time_a, "market": end_market},
                       0.0, 0.0, 0.0, 0.0)
    check(e["time"] == 0.0 and e["rates"] == 0.0 and e["vol"] == 0.0, "only spot moved")
    others.append(e["spot_split"]["other"])
    if move == 0.001:
        print(f"5. spot +0.1% ({e['dS_pips']:.0f} pips) on a 2-option book: spot {e['spot']:,.1f} = delta "
              f"{e['spot_split']['delta']:,.1f} + gamma {e['spot_split']['gamma']:,.1f} + other {e['spot_split']['other']:,.2f}")
ratio_1 = others[1] / others[0]
ratio_2 = others[2] / others[1]
check(6.0 < ratio_1 < 10.0 and 6.0 < ratio_2 < 10.0,
      f"'other' should grow like dS^3 (x8 per doubling): {ratio_1:.2f}, {ratio_2:.2f}")
print(f"   doubling the move multiplies 'other' by {ratio_1:.2f} and {ratio_2:.2f} (third order: 8)")

# ---- 6. A pure vol move: next fixing's surface, everything else unchanged ----
t_next = pd.Timestamp("2026-09-29 15:00")
m_next = market.get_market(t_next)
m_from = market.get_market(pd.Timestamp("2026-09-29 14:00"))
end_market = shifted(m_from, surface_from=m_next)
e = decompose_step(book, {"time": time_a, "market": m_from}, {"time": time_a, "market": end_market},
                   0.0, 0.0, 0.0, 0.0)
check(e["time"] == 0.0 and e["spot"] == 0.0 and e["rates"] == 0.0, "only the surface moved")
check(abs(e["vol_split"]["other"]) < 0.05 * abs(e["vol"]) + 1.0,
      f"vega should explain most of a one-hour vol move: vol {e['vol']:.1f}, other {e['vol_split']['other']:.1f}")
print(f"6. one hour of vol change (14:00 -> 15:00 surface): vol {e['vol']:,.1f} = vega {e['vol_split']['vega']:,.1f} "
      f"+ other {e['vol_split']['other']:,.2f}")

# ---- 7. Plain JSON ----
try:
    json.dumps(s.history(), allow_nan=False)
    json.dumps(s.pnl_summary(), allow_nan=False)
    json_ok = True
except (TypeError, ValueError) as error:
    json_ok = False
    print("   ", error)
check(json_ok, "history and summary must be plain JSON")
print("7. step explains and the summary are plain JSON")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")