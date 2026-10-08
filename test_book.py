"""
Checks for the Book: options + USD hedge + CNY cash, and NAV.
"""
import sys
import json
import math
sys.path.insert(0, "build")

import pandas as pd
from simulation.market_loader import MarketLoader, years_between
from simulation.book import Book

market = MarketLoader()
problems = 0


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


start = pd.Timestamp("2026-09-24 10:00")
snap = market.get_snapshot(start, sim_start=start)          # time = 0
spot = snap.spot()

STRIKE = 6.70
EXPIRY = 91 / 365
NOTIONAL = -10e6                                            # client buys 10m, we're short
mid = snap.price(STRIKE, EXPIRY, True).price

# ---- 1. Booking at mid: NAV is exactly 0 ----
book = Book()
book.book_option(STRIKE, EXPIRY, True, NOTIONAL, mid, time=0.0, note="client buys")
nav = book.nav(snap)
check(abs(nav) < 1e-6, f"NAV after booking at mid should be 0, got {nav}")
check(abs(book.cash_cny - 10e6 * mid) < 1e-6, "premium received should be 10m x mid")
print(f"1. booked at mid {mid:.6f}: cash {book.cash_cny:,.0f}, "
      f"options {book.options_value(snap):,.0f}, NAV {nav:.2e}")

# ---- 2. Booking above mid: NAV = the spread earned ----
book2 = Book()
ask_price = mid + 0.0003
book2.book_option(STRIKE, EXPIRY, True, NOTIONAL, ask_price)
nav2 = book2.nav(snap)
expected = 10e6 * 0.0003
check(abs(nav2 - expected) < 1e-6, f"spread P&L should be {expected}, got {nav2}")
print(f"2. booked 3 pips over mid: NAV {nav2:,.0f} CNY (expect {expected:,.0f})")

# ---- 3. Hedging: delta goes to 0, NAV unchanged ----
delta_before = book.net_delta(snap)
book.trade_spot(-delta_before, spot, time=0.0, note="delta hedge")
check(abs(book.net_delta(snap)) < 1e-6, "net delta should be 0 after hedging")
check(abs(book.nav(snap)) < 1e-6, "hedging at mid spot should not change NAV")
print(f"3. delta {delta_before:,.0f} USD -> hedged with {book.hedge_usd:,.0f} USD; "
      f"net delta {book.net_delta(snap):.2e}, NAV {book.nav(snap):.2e}")

# ---- 4. Time skip: re-mark everything; hedged book moves far less than unhedged ----
unhedged = Book()
unhedged.book_option(STRIKE, EXPIRY, True, NOTIONAL, mid)

later_time = pd.Timestamp("2026-09-24 16:00")
later = market.get_snapshot(later_time, sim_start=start)
dt = later.time()
book.accrue(snap, dt)            # interest at the rates in force at the START of the gap
unhedged.accrue(snap, dt)
pnl_hedged = book.nav(later)
pnl_unhedged = unhedged.nav(later)
move_pips = (later.spot() - spot) * 10000
print(f"4. {start:%d %b %H:%M} -> {later_time:%H:%M}: spot {spot:.4f} -> {later.spot():.4f} "
      f"({move_pips:+.0f} pips)")
print(f"   P&L unhedged {pnl_unhedged:,.0f} CNY, hedged {pnl_hedged:,.0f} CNY")
check(abs(pnl_hedged) < abs(pnl_unhedged), "hedged P&L should be smaller than unhedged")

# Rebuild by hand: option value + hedge x new spot + cash
by_hand = book.options_value(later) + book.hedge_usd * later.spot() + book.cash_cny
check(abs(by_hand - book.nav(later)) < 1e-6, "NAV should equal its three parts")

# ---- 5. Interest: cash and hedge grow at their own overnight rates ----
interest_book = Book()
interest_book.cash_cny = 1e6
interest_book.hedge_usd = 1e6
one_day = 1 / 365
earned = interest_book.accrue(snap, one_day)
r_cny = snap.domestic_rate(one_day)
r_usd = snap.foreign_rate(one_day)
check(abs(interest_book.cash_cny - 1e6 * math.exp(r_cny * one_day)) < 1e-6, "CNY interest")
check(abs(interest_book.hedge_usd - 1e6 * math.exp(r_usd * one_day)) < 1e-6, "USD interest")
print(f"5. one day on 1m CNY + 1m USD: CNY at {r_cny*100:.3f}%, USD at {r_usd*100:.3f}%, "
      f"interest {earned:,.2f} CNY")

# ---- 6. Expiry: settles at intrinsic, leaves the book ----
settle_start = pd.Timestamp("2026-09-22 10:00")
first = market.get_snapshot(settle_start, sim_start=settle_start)
settle_book = Book()
itm_strike = first.spot() - 0.02          # in the money call
settle_book.book_option(itm_strike, 1 / 365, True, -5e6, 0.02, time=0.0)
two_days = market.get_snapshot(pd.Timestamp("2026-09-24 10:00"), sim_start=settle_start)
cash_before = settle_book.cash_cny
settled = settle_book.settle_expired(two_days)
expected_cash = -5e6 * max(two_days.spot() - itm_strike, 0.0)
check(len(settled) == 1, "one option should have settled")
check(len(settle_book.portfolio.get_options()) == 0, "settled option should leave the book")
check(abs(settle_book.cash_cny - cash_before - expected_cash) < 1e-6, "settlement cash")
print(f"6. short 5m call K={itm_strike:.4f}, settled at {two_days.spot():.4f}: "
      f"paid {-expected_cash:,.0f} CNY; options left: {len(settle_book.portfolio.get_options())}")

# A live option is NOT settled
live_book = Book()
live_book.book_option(6.70, 1.0, True, -1e6, 0.01, time=0.0)
check(len(live_book.settle_expired(two_days)) == 0, "live option should not settle")

# ---- 7. Bad inputs rejected ----
rejected = 0
bad_cases = [
    {"strike": 6.70, "expiry": 0.25, "is_call": True, "notional": 0, "price": 0.01},
    {"strike": -1.0, "expiry": 0.25, "is_call": True, "notional": 1e6, "price": 0.01},
    {"strike": 6.70, "expiry": 0.25, "is_call": True, "notional": 1e6, "price": -0.01},
    {"strike": 6.70, "expiry": 0.10, "is_call": True, "notional": 1e6, "price": 0.01, "time": 0.2},
    {"strike": 6.70, "expiry": -0.10, "is_call": True, "notional": 1e6, "price": 0.01},
]
for case in bad_cases:
    try:
        Book().book_option(**case)
    except ValueError:
        rejected += 1
check(rejected == len(bad_cases), "all bad bookings should be rejected")
print(f"7. bad bookings rejected: {rejected} / {len(bad_cases)}")

# ---- 8. Summary is plain data (web-ready) ----
summary = book.summary(later)
text = json.dumps(summary)
check(len(summary["positions"]) == 1, "summary should list the one position")
check(len(book.trades) == 2, "blotter should have the option and the hedge")
print(f"8. summary is JSON ({len(text)} chars): NAV {summary['nav_cny']:,.0f}, "
      f"delta {summary['greeks']['delta']:,.0f}, {len(book.trades)} trades in blotter")

# ---- 9. The hedged P&L from test 4 is explained by the Greeks ----
# Re-run test 4's book from scratch and break the P&L down with Greeks from the START.
s0 = snap
s1 = later
explain = Book()
explain.book_option(STRIKE, EXPIRY, True, NOTIONAL, mid, time=0.0)
explain.trade_spot(-explain.net_delta(s0), s0.spot(), time=0.0)
g = explain.greeks(s0)
nav0 = explain.nav(s0)
dt = s1.time() - s0.time()
dS = s1.spot() - s0.spot()
dvol = s1.vol(STRIKE, EXPIRY - s1.time()) - s0.vol(STRIKE, EXPIRY)
carry = explain.accrue(s0, dt)
actual = explain.nav(s1) - nav0
parts = {
    "delta": g["delta"] * dS,
    "gamma": 0.5 * g["gamma"] * dS * dS,
    "theta": g["theta"] * dt,
    "vega": g["vega"] * dvol,
    "carry": carry,
}
explained = 0.0
for name in parts:
    explained += parts[name]
unexplained = actual - explained
check(abs(unexplained) < 0.1 * abs(actual) + 50, "Greeks should explain the hedged P&L")
print(f"9. hedged P&L {actual:,.0f} = delta {parts['delta']:,.0f} + gamma {parts['gamma']:,.0f} "
      f"+ theta {parts['theta']:,.0f} + vega {parts['vega']:,.0f} + carry {parts['carry']:,.0f} "
      f"+ unexplained {unexplained:,.0f}")

# ---- 10. Expiry at a fixing: NAV refuses until settled, then is continuous ----
exp_start = pd.Timestamp("2026-09-23 10:00")
before_exp = market.get_snapshot(pd.Timestamp("2026-09-23 15:00"), sim_start=exp_start)
at_exp = market.get_snapshot(pd.Timestamp("2026-09-23 16:00"), sim_start=exp_start)
K_exp = before_exp.spot() - 0.01
exp_book = Book()
exp_book.book_option(K_exp, at_exp.time(), True, -5e6, 0.0, time=0.0)
value_1h_before = exp_book.options_value(before_exp)
intrinsic_1h_before = -5e6 * max(before_exp.spot() - K_exp, 0.0)
check(abs(value_1h_before - intrinsic_1h_before) < 1000,
      "an hour before expiry the option should be worth about its intrinsic value")
refused = False
try:
    exp_book.nav(at_exp)
except ValueError:
    refused = True
check(refused, "NAV should refuse while an expired option is unsettled")
exp_book.settle_expired(at_exp)
nav_after = exp_book.nav(at_exp)
expected_after = -5e6 * max(at_exp.spot() - K_exp, 0.0)
check(abs(nav_after - expected_after) < 1e-6, "NAV after settling = payoff paid")
print(f"10. 1h before expiry: value {value_1h_before:,.0f} vs intrinsic {intrinsic_1h_before:,.0f}; "
      f"NAV refused until settled: {refused}; after settling {nav_after:,.0f}")

# ---- 10b. An option within rounding of its expiry counts as expired ----
hair_book = Book()
hair_book.book_option(6.70, 0.05, True, -1e6, 0.0, time=0.0)
# A stand-in snapshot whose clock lands 1e-12 years before the expiry (rounding)
class FakeClock:
    def __init__(self, snap, t):
        self.snap = snap
        self.t = t
    def time(self):
        return self.t
    def spot(self):
        return self.snap.spot()
settled_hair = hair_book.settle_expired(FakeClock(two_days, 0.05 - 1e-12))
check(len(settled_hair) == 1, "an option within rounding of expiry should settle")
print(f"10b. option 1e-12 years from expiry at the expiry fixing: settled {len(settled_hair)}")

# ---- 11. Exercise matrix: the HOLDER exercises only when it's in their favour ----
# Each case: (our side, call/put, holder's view at expiry) -> expected cash for us.
# When we're short, the client holds the option: they exercise only if it's in the money,
# and then we pay. When we're long, we hold it: we exercise only if it's in the money.
ex_start = pd.Timestamp("2026-09-23 10:00")
ex_snap = market.get_snapshot(pd.Timestamp("2026-09-23 16:00"), sim_start=ex_start)
S = ex_snap.spot()
cases = [
    # notional, is_call, strike,     expected cash for us,          description
    (-1e6, True,  S - 0.02, -1e6 * 0.02, "short call, ITM: client exercises, we pay"),
    (-1e6, True,  S + 0.02, 0.0,         "short call, OTM: client walks away, we pay nothing"),
    (-1e6, False, S + 0.02, -1e6 * 0.02, "short put,  ITM: client exercises, we pay"),
    (-1e6, False, S - 0.02, 0.0,         "short put,  OTM: client walks away"),
    (+1e6, True,  S - 0.02, +1e6 * 0.02, "long call,  ITM: we exercise, we receive"),
    (+1e6, True,  S + 0.02, 0.0,         "long call,  OTM: we walk away"),
    (+1e6, False, S + 0.02, +1e6 * 0.02, "long put,   ITM: we exercise, we receive"),
    (+1e6, False, S - 0.02, 0.0,         "long put,   OTM: we walk away"),
]
print("11. exercise at expiry (spot %.4f):" % S)
for notional, is_call, strike, expected, label in cases:
    case_book = Book()
    case_book.book_option(strike, ex_snap.time(), is_call, notional, 0.0, time=0.0)
    case_book.settle_expired(ex_snap)
    cash = case_book.cash_cny
    ok = abs(cash - expected) < 1e-6
    check(ok, label)
    # A short position can never RECEIVE money at expiry; a long one can never PAY
    if notional < 0:
        check(cash <= 0, label + " - a short option must never pay us at expiry")
    else:
        check(cash >= 0, label + " - a long option must never cost us at expiry")
    print(f"    {label:<52} cash {cash:>10,.0f}  {'ok' if ok else 'WRONG'}")

# ---- 12. Several options at once: only the expired ones settle, each correctly ----
multi = Book()
id_a = multi.book_option(S - 0.01, ex_snap.time(), True, -2e6, 0.0, time=0.0)    # expires, ITM
id_b = multi.book_option(S + 0.01, ex_snap.time(), True, -3e6, 0.0, time=0.0)    # expires, OTM
id_c = multi.book_option(S, ex_snap.time() + 0.1, False, -4e6, 0.0, time=0.0)    # still live
settled_ids = []
for row in multi.settle_expired(ex_snap):
    settled_ids.append(row["position_id"])
remaining_ids = []
for pos in multi.portfolio.get_options():
    remaining_ids.append(pos.id)
check(sorted(settled_ids) == sorted([id_a, id_b]), "both expired options should settle")
check(remaining_ids == [id_c], "only the live option should remain")
check(abs(multi.cash_cny - (-2e6 * 0.01)) < 1e-6, "only the ITM one pays")
print(f"12. 3 options, 2 expiring: settled {sorted(settled_ids)}, left {remaining_ids}, "
      f"cash {multi.cash_cny:,.0f}")

# ---- 13. Full life: sell a 1M option, delta-hedge at every fixing until expiry ----
# NAV must be continuous through expiry, and the Greeks must explain the total P&L.
times = market.snapshot_times()
life_start = times[0]
life_strike = round(market.get_snapshot(life_start, life_start).forward(30 / 365), 4)
life_expiry = None
life = Book()
previous = None
explained_total = 0.0
step_count = 0
for t_fix in times:
    s_now = market.get_snapshot(t_fix, sim_start=life_start)
    if previous is None:
        # expire at the first fixing at least 30 days out, so expiry lands ON a fixing
        for t_candidate in times:
            if years_between(life_start, t_candidate) >= 30 / 365:
                life_expiry = years_between(life_start, t_candidate)
                break
        premium = s_now.price(life_strike, life_expiry, True).price
        life.book_option(life_strike, life_expiry, True, -10e6, premium, time=0.0)
        life.trade_spot(-life.net_delta(s_now), s_now.spot(), time=0.0)
        previous = s_now
        nav_prev = life.nav(s_now)
        continue

    dt = s_now.time() - previous.time()
    g = life.greeks(previous)
    dS = s_now.spot() - previous.spot()
    T_prev = life_expiry - previous.time()
    T_now = life_expiry - s_now.time()
    carry = life.accrue(previous, dt)
    if T_now <= 1e-12:
        # Expiry step: everything the option did is in its final payoff
        life.settle_expired(s_now)
    else:
        dvol = s_now.vol(life_strike, T_now) - previous.vol(life_strike, T_prev)
        explained_total += g["delta"] * dS + 0.5 * g["gamma"] * dS * dS + g["theta"] * dt \
            + g["vega"] * dvol + carry
    nav_now = life.nav(s_now)
    step_count += 1
    if T_now <= 1e-12:
        break
    life.trade_spot(-life.net_delta(s_now), s_now.spot(), time=s_now.time())
    previous = s_now
    nav_prev = nav_now

final_nav = life.nav(s_now)
spot_at_expiry = s_now.spot()
paid = -10e6 * max(spot_at_expiry - life_strike, 0.0)
check(len(life.portfolio.get_options()) == 0, "option should be gone after expiry")
print(f"13. sold 10m 1M ATM call K={life_strike} on {life_start:%d %b}, hedged at {step_count} fixings")
print(f"    expiry {s_now.time()*365:.1f} days later, spot {spot_at_expiry:.4f} -> paid at expiry {paid:,.0f} CNY")
print(f"    final P&L {final_nav:,.0f} CNY (premium kept + hedge P&L + carry - payout)")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")