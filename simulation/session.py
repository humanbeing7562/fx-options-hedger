"""
Session: one run of the desk through the fixings, one step at a time.

    session = Session(policy="end_of_day")
    session.submit_order({...})        # a client order (booked now or at a later fixing)
    session.next()                      # move to the next fixing
    session.state()                     # the whole book as plain data

What next() does, in order:
    1. move the clock to the next fixing and build its snapshot
    2. accrue interest over the gap (rates from the START of the gap)
    3. settle options that expired during the gap (intrinsic at this fixing)
    4. mark the book: market P&L = NAV now - NAV after the last action
    5. book client orders that are due (time <= this fixing)
    6. hedge, if the policy says so
    7. net identical hedge legs
    8. record the step in the history and return it

Hedge policies:
    "end_of_day"  delta hedged with spot at every fixing; smile hedge (ATM / RR / BF
                  per bucket) at the last fixing of each day
    "threshold"   delta hedged when |delta| > limit; smile hedge when any bucket's
                  |vega| per vol point > limit
    "manual"      nothing automatic - use recommend_hedge(), hedge(), hedge_delta()

Rules: no prints, no input, no globals. Everything returned is a plain dict of
str / float / int / bool / None / list, so it can go straight to JSON.
The clock only moves forward. Dates live here; C++ only sees years since the start.
"""
import math
import pandas as pd
import fxengine

from simulation.market_loader import MarketLoader, years_between
from simulation.book import Book, EXPIRY_TOL
from simulation.hedging import recommend_bucketed_hedge, bucket_of, BUCKETS
from simulation.pnl import decompose_step, STAGES

POLICIES = ["end_of_day", "threshold", "manual"]
TAGS = ["client", "hedge"]
# Quoted vol tenors by their day count
TENOR_LABELS = {7: "1W", 14: "2W", 21: "3W", 30: "1M", 61: "2M", 91: "3M", 182: "6M", 273: "9M", 365: "1Y"}
DEFAULT_LIMITS = {
    "delta_usd": 1e6,                # hedge delta when |net delta| is above this (USD)
    "vega_cny_per_volpt": 50_000,    # smile-hedge when a bucket's |vega| per 1 vol point is above this
}
# Default bid/ask: mid vol -/+ this much (0.0010 = 0.1 vol point each side, 0.2 wide)
DEFAULT_HALF_SPREAD_VOL = 0.0010
# Hedge packages smaller than this (USD) aren't worth trading
MIN_HEDGE_NOTIONAL = 1.0
SECONDS_PER_YEAR = 365.0 * 24 * 3600
# The fixings are Beijing time with no timezone attached; a time sent WITH a timezone
# (e.g. "2026-07-07T02:00:00Z") is converted to Beijing time first
CLOCK_TZ = "Asia/Shanghai"
# An option must live at least this long after it is ordered
MIN_LIFE = pd.Timedelta(minutes=1)
# A hedge plan sent back must match the one recommended now to this relative precision
PLAN_TOL = 1e-9
# An agreed vol must be above 0 and below this (5.0 = 500%, far beyond any USD/CNY quote)
MAX_VOL = 5.0
MIN_VOL = 1e-4
# Sanity bands, so a typo can't book a trade nobody would do (the game's P&L would be meaningless):
#   an option's agreed vol, or the vol implied by its agreed premium, within 50% of the mid vol
#   either way (mid 2.13% -> 1.07% to 3.20%; a real bid/ask is about 0.1 vol point either side)
#   a spot rate within 2% of spot (about 1,350 pips; USD/CNY moves ~0.3% on a big day)
MAX_VOL_OFF_MID = 0.5
MAX_SPOT_OFF = 0.02


def clock_time(value, what):
    """A timestamp on the session's clock (Beijing time, no timezone). Refuses non-dates."""
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        raise ValueError(f"{what}: {value!r} is not a date/time") from None
    if pd.isna(ts):
        raise ValueError(f"{what}: {value!r} is not a date/time")
    if ts.tzinfo is not None:
        ts = ts.tz_convert(CLOCK_TZ).tz_localize(None)
    return ts


def finite_number(value, what):
    """A plain float; refuses text, NaN and infinity (they would poison every later sum)."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number")
    return number


def implied_vol(pricer, spot, strike, T, price, is_call):
    """
    The vol at which the GK premium equals `price`, by bisection between MIN_VOL and MAX_VOL
    (the premium rises with vol, so halving the interval 100 times pins it down).
    None if the price is below the premium at MIN_VOL or above the one at MAX_VOL.
    """
    low = MIN_VOL
    high = MAX_VOL
    if price < pricer.price(spot, strike, T, low, is_call).price:
        return None
    if price > pricer.price(spot, strike, T, high, is_call).price:
        return None
    for i in range(100):
        middle = 0.5 * (low + high)
        if pricer.price(spot, strike, T, middle, is_call).price < price:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def close_enough(a, b):
    if isinstance(a, bool) or not isinstance(a, (int, float)):
        return False
    return abs(a - b) <= PLAN_TOL * max(1.0, abs(b))


class Session:
    def __init__(self, start=None, end=None, policy="end_of_day",
                 limits=None, market=None):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r} - use one of {POLICIES}")

        if market is None:
            market = MarketLoader()
        self.market = market
        self.policy = policy
        self.limits = dict(DEFAULT_LIMITS)
        if limits is not None:
            for name in limits:
                if name not in self.limits:
                    raise ValueError(f"unknown limit {name!r}")
                self.limits[name] = finite_number(limits[name], f"limit {name}")

        # The fixings this session walks through
        start_ts = None
        end_ts = None
        if start is not None:
            start_ts = clock_time(start, "start")
        if end is not None:
            end_ts = clock_time(end, "end")
        self.times = []
        for t in market.snapshot_times():
            if start_ts is not None and t < start_ts:
                continue
            if end_ts is not None and t > end_ts:
                continue
            self.times.append(t)
        if len(self.times) == 0:
            raise ValueError("no fixings between start and end")
        self.start = self.times[0]

        self.book = Book()
        self.index = 0
        self.snapshot = self._build_snapshot(0)

        self.pending = []          # client orders waiting for their fixing
        self.orders = []           # every order ever submitted (pending and booked)
        self.order_count = 0
        self.hedge_ids = set()     # position ids that came from hedges (only these get netted)
        self.history_log = []
        self.last_nav = 0.0        # NAV after the most recent step or action

        self._record({"type": "start", "nav_cny": 0.0})

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def now(self):
        return self.times[self.index]

    def done(self):
        return self.index >= len(self.times) - 1

    def years(self, timestamp):
        """Sim-clock position (years since the start) of a timestamp."""
        return years_between(self.start, timestamp)

    def timestamp(self, years):
        """Timestamp of a sim-clock position, rounded to the second."""
        ts = self.start + pd.Timedelta(seconds=years * SECONDS_PER_YEAR)
        return ts.round("s")

    def _build_snapshot(self, index):
        # The vol surface picks its own model by strike (VV inside 25D, SABR in the wings).
        # The pieces are kept too: the P&L decomposer mixes this fixing's and the next one's.
        self.market_now = self.market.get_market(self.times[index])
        m = self.market_now
        return fxengine.MarketSnapshot(self.years(self.times[index]), m["spot"], m["cny"], m["usd"],
                                       m["surface"])

    def _is_end_of_day(self):
        if self.done():
            return True
        return self.times[self.index + 1].normalize() != self.now().normalize()

    def next(self):
        """Move to the next fixing and run the whole step. Returns the step record."""
        if self.done():
            raise ValueError("no more fixings - the session has reached its end")

        previous_snapshot = self.snapshot
        previous_market = self.market_now
        previous_time = self.now()
        # The book as it stands before anything in this step changes it (for the P&L explain)
        positions_before = self.book.position_terms()
        hedge_before = self.book.hedge_usd
        cash_before = self.book.cash_cny

        # 1. clock
        self.index += 1
        self.snapshot = self._build_snapshot(self.index)
        dt = self.snapshot.time() - previous_snapshot.time()

        # 2. interest over the gap, at the rates in force when the gap began
        carry = self.book.accrue(previous_snapshot, dt)
        hedge_after = self.book.hedge_usd
        cash_after = self.book.cash_cny

        # 3. expiries
        settled = self.book.settle_expired(self.snapshot)
        for row in settled:
            self.hedge_ids.discard(row["position_id"])

        # 4. mark, and explain the move by full revaluation (stages add up to market_pnl)
        nav_marked = self.book.nav(self.snapshot)
        market_pnl = nav_marked - self.last_nav
        self.last_nav = nav_marked
        explain = decompose_step(positions_before,
                                 {"time": previous_snapshot.time(), "market": previous_market},
                                 {"time": self.snapshot.time(), "market": self.market_now},
                                 hedge_before, cash_before, hedge_after, cash_after)

        # 5. client orders that are due
        booked = []
        still_pending = []
        for order in self.pending:
            if order["time"] <= self.now():
                booked.append(self._book_order(order))
            else:
                still_pending.append(order)
        self.pending = still_pending

        # 6 + 7. hedge by policy, then net
        hedges = self._apply_policy()

        record = {
            "type": "step",
            "from": previous_time.isoformat(),
            "dt_hours": dt * 365 * 24,
            "carry_cny": carry,
            "market_pnl_cny": market_pnl,
            "pnl_explain": explain,
            "settled": self._settled_rows(settled),
            "orders_booked": booked,
            "hedges": hedges,
            "nav_cny": self.book.nav(self.snapshot),
        }
        # Trading at this fixing: client edge versus mid (hedges trade at mid, so ~0)
        record["trade_pnl_cny"] = record["nav_cny"] - nav_marked
        self.last_nav = record["nav_cny"]
        return self._record(record)

    def run(self, steps=None, until=None):
        """Call next() `steps` times, or until the clock reaches `until`. Returns the step records."""
        until_ts = None
        if until is not None:
            until_ts = clock_time(until, "until")
        if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 0):
            raise ValueError("steps must be a whole number, 0 or more")
        records = []
        count = 0
        while not self.done():
            if steps is not None and count >= steps:
                break
            if until_ts is not None and self.times[self.index + 1] > until_ts:
                break
            records.append(self.next())
            count += 1
        return records

    # ------------------------------------------------------------------
    # Client orders
    # ------------------------------------------------------------------

    def _parse_order(self, order):
        """
        Checks a client order and fills in its times.
            tag         "client" (default) or "hedge" - who the trade is with
            side        "buy" or "sell". For a client order: what the CLIENT does
                        (client buys -> we sell). For a hedge order: what WE do.
            is_call     True / False
            notional    USD, positive
            strike      a number, "ATM" (delta-neutral straddle), "ATMF" (the forward to
                        expiry) or "25D" style (n-delta call or put)
            expiry      a timestamp, or tenor_days instead (counted from the order time)
            time        when the client asks (default: now); booked at the first fixing >= time
            price       optional agreed premium (CNY per USD)
            vol         optional agreed vol (0.0215 = 2.15%) instead of a price: the premium is
                        worked out at booking, at that vol and the booking fixing's spot and rates
            With neither price nor vol, the order books at mid.
        """
        tag, side, our_side = self._sides(order)
        if "is_call" not in order or not isinstance(order["is_call"], bool):
            raise ValueError("is_call must be true or false")
        notional = finite_number(order.get("notional", 0), "notional")
        if notional <= 0:
            raise ValueError("notional must be positive (USD)")

        if order.get("time") is None:
            order_time = self.now()
        else:
            order_time = clock_time(order["time"], "time")
        if order_time < self.now():
            raise ValueError(f"order time {order_time} is before the clock ({self.now()}) - "
                             f"the clock only moves forward")
        if order_time > self.times[-1]:
            raise ValueError(f"order time {order_time} is after the last fixing")

        if order.get("expiry") is not None:
            expiry_time = clock_time(order["expiry"], "expiry")
        elif order.get("tenor_days") is not None:
            tenor_days = finite_number(order["tenor_days"], "tenor_days")
            if tenor_days <= 0:
                raise ValueError("tenor_days must be positive")
            expiry_time = order_time + pd.Timedelta(days=tenor_days)
        else:
            raise ValueError("give expiry or tenor_days")
        if expiry_time < order_time + MIN_LIFE:
            raise ValueError("expiry must be at least a minute after the order time")

        strike = order.get("strike", "ATM")
        if isinstance(strike, str):
            text = strike.strip().upper()
            if text != "ATM" and text != "ATMF":
                if not text.endswith("D"):
                    raise ValueError("strike must be a number, 'ATM', 'ATMF' or like '25D'")
                delta = finite_number(text[:-1], "delta strike")
                if delta <= 0 or delta >= 100:
                    raise ValueError("delta strike must be between 0D and 100D")
            strike = text
        else:
            strike = finite_number(strike, "strike")
            if strike <= 0:
                raise ValueError("strike must be positive")

        price = order.get("price")
        if price is not None:
            price = finite_number(price, "price")
            if price < 0:
                raise ValueError("price must not be negative")

        vol = order.get("vol")
        if vol is not None:
            if price is not None:
                raise ValueError("give an agreed price or an agreed vol, not both")
            vol = finite_number(vol, "vol")
            if vol <= 0 or vol >= MAX_VOL:
                raise ValueError("vol must be above 0 and below 5.0 (as a fraction: 0.0215 = 2.15%)")

        return {
            "tag": tag,
            "side": side,
            "our_side": our_side,
            "is_call": order["is_call"],
            "notional": notional,
            "strike": strike,
            "time": order_time,
            "expiry": expiry_time,
            "price": price,
            "vol": vol,
        }

    def _sides(self, order):
        """
        (tag, side, our_side) of an order or spot trade. For a client trade `side` is what
        the CLIENT does (client buys -> we sell); for a hedge it is what WE do.
        """
        tag = order.get("tag", "client")
        if tag not in TAGS:
            raise ValueError(f"tag must be one of {TAGS}")
        side = order.get("side")
        if side not in ["buy", "sell"]:
            raise ValueError("side must be 'buy' or 'sell' (client trade: the client's side; "
                             "hedge trade: our side)")
        # Our side of the trade, so nothing downstream has to know the convention
        if tag == "client":
            if side == "buy":
                our_side = "sell"
            else:
                our_side = "buy"
        else:
            our_side = side
        return tag, side, our_side

    def _resolve_strike(self, strike, T, is_call):
        if not isinstance(strike, str):
            return strike
        if strike == "ATM":
            return self.snapshot.atm_strike(T)
        if strike == "ATMF":
            # The forward to this expiry, at the fixing where the order books
            return self.snapshot.forward(T)
        delta = float(strike[:-1]) / 100.0
        if not is_call:
            delta = -delta
        return self.snapshot.strike_from_delta(delta, T, is_call)

    def quote(self, order, half_spread_vol=DEFAULT_HALF_SPREAD_VOL):
        """
        Prices an order on the CURRENT snapshot (nothing is booked).
        bid / ask = premium at mid vol -/+ half_spread_vol. The client buys at our ask
        and sells at our bid; `our_price` is the one that applies to this order.
        """
        o = self._parse_order(order)
        now = self.snapshot.time()
        expiry = self.years(o["expiry"])
        T = expiry - now
        if T <= 0:
            raise ValueError("this order would already have expired")
        strike = self._resolve_strike(o["strike"], T, o["is_call"])

        vol = self.snapshot.vol(strike, T)
        pricer = fxengine.GKPricer(self.snapshot.domestic_rate(T), self.snapshot.foreign_rate(T))
        spot = self.snapshot.spot()
        mid = pricer.price(spot, strike, T, vol, o["is_call"])
        bid_vol = max(vol - half_spread_vol, 1e-4)
        ask_vol = vol + half_spread_vol
        bid = pricer.price(spot, strike, T, bid_vol, o["is_call"]).price
        ask = pricer.price(spot, strike, T, ask_vol, o["is_call"]).price

        # An agreed vol: the premium it gives at this fixing, and how far it is from mid
        agreed_price = None
        agreed_edge_vol = None
        if o["vol"] is not None:
            self._check_vol_band(o["vol"], vol)
            agreed_price = pricer.price(spot, strike, T, o["vol"], o["is_call"]).price
            if o["our_side"] == "sell":
                agreed_edge_vol = o["vol"] - vol
            else:
                agreed_edge_vol = vol - o["vol"]

        # bid / ask around mid: a client buys from us at the ask and sells to us at the bid;
        # on a hedge we deal with the market, so we buy at the ask and sell at the bid.
        # Either way: our buy is at the ask only for a hedge, our sell at the ask only for a client.
        if o["tag"] == "client":
            if o["our_side"] == "sell":
                our_price = ask
                our_vol = ask_vol
            else:
                our_price = bid
                our_vol = bid_vol
        else:
            if o["our_side"] == "buy":
                our_price = ask
                our_vol = ask_vol
            else:
                our_price = bid
                our_vol = bid_vol
        return {
            "time": self.now().isoformat(),
            "tag": o["tag"],
            "side": o["side"],
            "our_side": o["our_side"],
            "is_call": o["is_call"],
            "notional": o["notional"],
            "strike": strike,
            "expiry": o["expiry"].isoformat(),
            "days_to_expiry": T * 365,
            "spot": spot,
            "forward": self.snapshot.forward(T),
            "vol": vol,
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "mid": mid.price,
            "bid": bid,
            "ask": ask,
            "our_price": our_price,
            "our_vol": our_vol,                        # the vol behind our_price (send it as "vol" to book at it)
            "premium_cny_at_our_price": our_price * o["notional"],
            "agreed_vol": o["vol"],
            "agreed_price": agreed_price,              # premium at the agreed vol (None if no vol given)
            "agreed_edge_vol": agreed_edge_vol,        # vol points in our favour (+ = we gain)
            "greeks_per_usd": {"delta": mid.delta, "gamma": mid.gamma, "vega": mid.vega,
                               "vanna": mid.vanna, "volga": mid.volga, "theta": mid.theta},
        }

    def submit_order(self, order):
        """
        Takes a client order. If its time is now, it is booked straight away (and the
        policy hedges it); if later, it waits and is booked at the first fixing at or
        after its time. Returns the order record.
        """
        o = self._parse_order(order)
        self.order_count += 1
        o["order_id"] = self.order_count
        o["status"] = "pending"
        self.orders.append(o)

        if o["time"] <= self.now():
            nav_before = self.book.nav(self.snapshot)
            try:
                row = self._book_order(o)
                if o["status"] == "rejected":
                    raise ValueError(o["reason"])
            except Exception:
                # Nothing was booked: take the order back out, so ids stay 1, 2, 3, ...
                self.orders.remove(o)
                self.order_count -= 1
                raise
            hedges = self._apply_policy()
            nav_after = self.book.nav(self.snapshot)
            self._record({
                "type": "order",
                "orders_booked": [row],
                "hedges": hedges,
                "trade_pnl_cny": nav_after - nav_before,
                "nav_cny": nav_after,
            })
            self.last_nav = nav_after
            return row

        self.pending.append(o)
        return self._order_row(o)

    def trade_spot(self, order):
        """
        A spot USD/CNY trade at this fixing, booked straight away.
            tag       "client" (default) or "hedge"
            side      client trade: what the CLIENT does; hedge trade: what WE do
            notional  USD, positive
            price     optional, CNY per USD; default = this fixing's spot (mid)
        Our USD position and CNY cash change; edge = our USD x (spot - price), so buying
        below spot or selling above it shows as trading P&L. After a client trade the
        hedge policy runs (manual: nothing; end_of_day: delta is flattened).
        """
        tag, side, our_side = self._sides(order)
        notional = finite_number(order.get("notional", 0), "notional")
        if notional <= 0:
            raise ValueError("notional must be positive (USD)")
        spot = self.snapshot.spot()
        price = order.get("price")
        if price is None:
            price = spot
        else:
            price = finite_number(price, "price")
            if price <= 0:
                raise ValueError("price must be positive")
            if abs(price / spot - 1.0) > MAX_SPOT_OFF:
                raise ValueError(f"rate {price:.4f} is more than {MAX_SPOT_OFF:.0%} away from spot "
                                 f"{spot:.4f} - check for a typo")
        if our_side == "buy":
            our_usd = notional
        else:
            our_usd = -notional

        nav_before = self.book.nav(self.snapshot)
        if tag == "client":
            note = f"client spot: client {side}s"
        else:
            note = f"hedge spot: we {our_side}"
        self.book.trade_spot(our_usd, price, time=self.snapshot.time(), note=note)
        row = {
            "type": "spot",
            "tag": tag,
            "side": side,
            "our_side": our_side,
            "notional": notional,
            "usd_amount": our_usd,
            "spot": spot,
            "price": price,
            "cash_cny": -our_usd * price,
            "edge_cny": our_usd * (spot - price),
        }
        hedges = None
        if tag == "client":
            hedges = self._apply_policy()
        nav_after = self.book.nav(self.snapshot)
        self._record({"type": "spot", "trade": row, "hedges": hedges,
                      "trade_pnl_cny": nav_after - nav_before, "nav_cny": nav_after})
        self.last_nav = nav_after
        row["time"] = self.now().isoformat()
        return row

    def _check_vol_band(self, vol, mid_vol, what=None):
        """ValueError unless `vol` is within MAX_VOL_OFF_MID (as a fraction) of the mid vol.
        `what` names it in the message (default: the vol itself)."""
        if what is None:
            what = f"vol {vol:.3%}"
        low = mid_vol * (1.0 - MAX_VOL_OFF_MID)
        high = mid_vol * (1.0 + MAX_VOL_OFF_MID)
        if vol < low or vol > high:
            raise ValueError(f"{what} is outside {low:.3%} to {high:.3%} (mid {mid_vol:.3%} "
                             f"\u00b1 {MAX_VOL_OFF_MID:.0%}) - check for a typo")

    def order_rows(self):
        """Every order submitted to this session (pending, booked, cancelled, rejected), oldest first."""
        rows = []
        for o in self.orders:
            rows.append(self._order_row(o))
        return rows

    def cancel_order(self, order_id):
        for o in self.pending:
            if o["order_id"] == order_id:
                self.pending.remove(o)
                o["status"] = "cancelled"
                return self._order_row(o)
        raise ValueError(f"no pending order {order_id}")

    def _book_order(self, o):
        """Books one client order on the current snapshot. Returns its record."""
        now = self.snapshot.time()
        expiry = self.years(o["expiry"])
        T = expiry - now
        if T <= EXPIRY_TOL:          # the Book treats anything this close to expiry as expired
            o["status"] = "rejected"
            o["reason"] = "expired before it could be booked"
            return self._order_row(o)

        strike = self._resolve_strike(o["strike"], T, o["is_call"])
        mid_vol = self.snapshot.vol(strike, T)
        mid = self.snapshot.price(strike, T, o["is_call"]).price
        # The same pricer and rates as mid, so the mid vol gives exactly the mid premium
        pricer = fxengine.GKPricer(self.snapshot.domestic_rate(T), self.snapshot.foreign_rate(T))
        spot = self.snapshot.spot()
        price = o["price"]
        if o["vol"] is not None:
            # Agreed in vol: the premium at that vol, this fixing's spot and this expiry's rates
            booked_vol = o["vol"]
            price = pricer.price(spot, strike, T, booked_vol, o["is_call"]).price
        elif price is not None:
            # Agreed premium: the vol it implies (None if no vol gives that premium)
            booked_vol = implied_vol(pricer, spot, strike, T, price, o["is_call"])
        else:
            price = mid
            booked_vol = mid_vol

        problem = None
        if booked_vol is None:
            problem = (f"premium {price * 1e4:.1f} pips is outside what any vol gives for this option "
                       f"(mid {mid * 1e4:.1f} pips)")
        else:
            what = None
            if o["vol"] is None and o["price"] is not None:
                what = f"premium {price * 1e4:.1f} pips (implied vol {booked_vol:.3%})"
            try:
                self._check_vol_band(booked_vol, mid_vol, what)
            except ValueError as error:
                problem = str(error)
        if problem is not None:
            o["status"] = "rejected"
            o["reason"] = problem
            return self._order_row(o)

        if o["our_side"] == "buy":
            our_notional = o["notional"]
        else:
            our_notional = -o["notional"]

        if o["tag"] == "client":
            note = f"client order #{o['order_id']}: client {o['side']}s"
        else:
            note = f"hedge order #{o['order_id']}: we {o['our_side']}"
        position_id = self.book.book_option(strike, expiry, o["is_call"], our_notional, price,
                                            time=now, note=note)
        if o["tag"] == "hedge":
            self.hedge_ids.add(position_id)      # shown as a hedge, and netted with other hedge legs
        o["status"] = "booked"
        o["booked_at"] = self.now()
        o["position_id"] = position_id
        o["strike_value"] = strike
        o["mid"] = mid
        o["mid_vol"] = mid_vol
        o["booked_vol"] = booked_vol           # agreed vol, the vol implied by an agreed premium, or mid
        o["booked_price"] = price
        # Day-one P&L: what we gain versus mid (positive when we sold above mid or bought below)
        o["edge_cny"] = -our_notional * (price - mid)
        return self._order_row(o)

    def _order_row(self, o):
        row = {
            "order_id": o["order_id"],
            "status": o["status"],
            "tag": o["tag"],
            "side": o["side"],
            "our_side": o["our_side"],
            "is_call": o["is_call"],
            "notional": o["notional"],
            "strike": o["strike"],
            "time": o["time"].isoformat(),
            "expiry": o["expiry"].isoformat(),
            "price": o["price"],
            "vol": o["vol"],
        }
        if o["status"] == "booked":
            row["booked_at"] = o["booked_at"].isoformat()
            row["position_id"] = o["position_id"]
            row["strike_value"] = o["strike_value"]
            row["mid"] = o["mid"]
            row["mid_vol"] = o["mid_vol"]
            row["booked_vol"] = o["booked_vol"]
            row["booked_price"] = o["booked_price"]
            row["edge_cny"] = o["edge_cny"]
        if "reason" in o:
            row["reason"] = o["reason"]
        return row

    # ------------------------------------------------------------------
    # Hedging
    # ------------------------------------------------------------------

    def recommend_hedge(self):
        """The bucketed smile hedge for the book right now (nothing is traded)."""
        plan = recommend_bucketed_hedge(self.book, self.snapshot)
        plan["time"] = self.now().isoformat()
        for item in plan["buckets"]:
            item["hedge_expiry_ts"] = self.timestamp(item["hedge_expiry"]).isoformat()
            if item.get("gamma_expiry") is not None:
                item["gamma_expiry_ts"] = self.timestamp(item["gamma_expiry"]).isoformat()
        return plan

    def hedge(self, plan=None):
        """
        Books the recommended smile hedge, then flattens delta with spot and nets the legs.
        Returns what was traded.

        The hedge is always worked out again here, on this fixing's market, and that fresh
        plan is what gets booked - prices included. A plan passed in (the one a page showed
        from recommend_hedge) is only a confirmation: it must have been made at this fixing
        and must still match the fresh plan, or nothing is booked. So a plan that was edited,
        or made before the book changed, can't book anything.
        """
        fresh = self.recommend_hedge()
        if plan is not None:
            self._check_plan(plan, fresh)
        nav_before = self.book.nav(self.snapshot)
        result = self._hedge_smile(fresh)
        result["spot"] = self._hedge_delta()
        result["netted"] = self._net()
        nav_after = self.book.nav(self.snapshot)
        record = {"type": "hedge", "hedges": result, "trade_pnl_cny": nav_after - nav_before,
                  "nav_cny": nav_after}
        self._record(record)
        self.last_nav = nav_after
        return result

    def _check_plan(self, plan, fresh):
        """Raises ValueError unless `plan` is the same hedge as `fresh` (same fixing, buckets,
        packages, notionals and expiries). Checks everything before anything is booked."""
        changed = "the book or the plan has changed since this plan was made - ask for a new one"
        if not isinstance(plan, dict) or "time" not in plan:
            raise ValueError("a hedge plan must be one from recommend_hedge")
        if plan["time"] != self.now().isoformat():
            raise ValueError("this plan was made at another fixing - ask for a new one")
        buckets = plan.get("buckets")
        if not isinstance(buckets, list) or len(buckets) != len(fresh["buckets"]):
            raise ValueError(changed)
        for i in range(len(buckets)):
            theirs = buckets[i]
            mine = fresh["buckets"][i]
            if not isinstance(theirs, dict) or theirs.get("bucket") != mine["bucket"]:
                raise ValueError(changed)
            if ("skipped" in theirs) != ("skipped" in mine):
                raise ValueError(changed)
            if "packages" not in mine:
                continue
            packages = theirs.get("packages")
            if not isinstance(packages, dict) or set(packages.keys()) != set(mine["packages"].keys()):
                raise ValueError(changed)
            for kind in mine["packages"]:
                p_theirs = packages[kind]
                p_mine = mine["packages"][kind]
                if not isinstance(p_theirs, dict):
                    raise ValueError(changed)
                if not close_enough(p_theirs.get("notional"), p_mine["notional"]):
                    raise ValueError(changed)
                if not close_enough(p_theirs.get("expiry"), p_mine["expiry"]):
                    raise ValueError(changed)

    def hedge_delta(self):
        """Flattens delta with a spot trade. Returns the trade (None if already flat)."""
        nav_before = self.book.nav(self.snapshot)
        trade = self._hedge_delta()
        nav_after = self.book.nav(self.snapshot)
        self._record({"type": "hedge", "hedges": {"spot": trade},
                      "trade_pnl_cny": nav_after - nav_before, "nav_cny": nav_after})
        self.last_nav = nav_after
        return trade

    def _hedge_smile(self, plan):
        now = self.snapshot.time()
        booked = []
        skipped = []
        for item in plan["buckets"]:
            if "skipped" in item:
                skipped.append({"bucket": item["bucket"], "reason": item["skipped"]})
                continue
            for kind in item["packages"]:
                pkg = item["packages"][kind]
                if abs(pkg["notional"]) < MIN_HEDGE_NOTIONAL:
                    continue
                ids = self.book.book_package(kind, pkg["legs"], pkg["notional"], pkg["expiry"],
                                             time=now, note=f"hedge {item['bucket']}")
                for position_id in ids:
                    self.hedge_ids.add(position_id)
                legs = []
                for leg in pkg["legs"]:
                    legs.append({"strike": leg["strike"], "is_call": leg["is_call"],
                                 "notional": pkg["notional"] * leg["ratio"], "vol": leg["vol"],
                                 "mid": leg["mid"]})
                booked.append({
                    "bucket": item["bucket"],
                    "kind": kind,
                    "notional": pkg["notional"],
                    "expiry": self.timestamp(pkg["expiry"]).isoformat(),
                    "days_to_expiry": (pkg["expiry"] - now) * 365,
                    "premium_cny": -pkg["notional"] * pkg["mid_price"],
                    "position_ids": ids,
                    "legs": legs,
                })
        return {"packages": booked, "skipped": skipped}

    def _hedge_delta(self):
        trade = -self.book.net_delta(self.snapshot)
        if trade == 0:
            return None
        spot = self.snapshot.spot()
        self.book.trade_spot(trade, spot, time=self.snapshot.time(), note="delta hedge")
        return {"usd_amount": trade, "spot": spot, "cash_cny": -trade * spot}

    def _net(self):
        merged = self.book.net_positions(sorted(self.hedge_ids), time=self.snapshot.time())
        for position_id in merged:
            if merged[position_id] != position_id:
                self.hedge_ids.discard(position_id)
        return len(merged)

    def _smile_limit_breached(self):
        limit = self.limits["vega_cny_per_volpt"]
        by_bucket = {}
        for row in self.book.positions(self.snapshot):
            label = bucket_of(row["T"])
            by_bucket[label] = by_bucket.get(label, 0.0) + row["vega"]
        for label in by_bucket:
            if abs(by_bucket[label]) / 100.0 > limit:
                return True
        return False

    def _apply_policy(self):
        """Hedges according to the policy. Returns what was traded."""
        result = {"packages": [], "skipped": [], "spot": None, "netted": 0, "reason": None}
        if self.policy == "manual":
            # No trading by itself, but identical hedge legs (e.g. from hedge orders) still merge;
            # netting never changes NAV or Greeks
            result["netted"] = self._net()
            return result

        smile = False
        if self.policy == "end_of_day" and self._is_end_of_day():
            smile = True
            result["reason"] = "end of day"
        if self.policy == "threshold" and self._smile_limit_breached():
            smile = True
            result["reason"] = "vega limit"

        if smile and len(self.book.portfolio.get_options()) > 0:
            smile_result = self._hedge_smile(self.recommend_hedge())
            result["packages"] = smile_result["packages"]
            result["skipped"] = smile_result["skipped"]

        if self.policy == "end_of_day":
            result["spot"] = self._hedge_delta()
        else:
            if smile or abs(self.book.net_delta(self.snapshot)) > self.limits["delta_usd"]:
                result["spot"] = self._hedge_delta()
                if result["reason"] is None:
                    result["reason"] = "delta limit"

        result["netted"] = self._net()
        return result

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _settled_rows(self, settled):
        rows = []
        for row in settled:
            out = dict(row)
            out["time"] = self.now().isoformat()
            rows.append(out)
        return rows

    def _greeks_by_bucket(self):
        result = {}
        for label, edge in BUCKETS:
            result[label] = {"gamma": 0.0, "vega": 0.0, "vanna": 0.0, "volga": 0.0,
                             "delta": 0.0, "positions": 0}
        for row in self.book.positions(self.snapshot):
            label = bucket_of(row["T"])
            for name in ["gamma", "vega", "vanna", "volga", "delta"]:
                result[label][name] += row[name]
            result[label]["positions"] += 1
        return result

    def _record(self, record):
        record["time"] = self.now().isoformat()
        record["index"] = self.index
        record["spot"] = self.snapshot.spot()
        self.history_log.append(record)
        return record

    def market_view(self):
        """
        The market screen at this fixing: spot, overnight rates, and per quoted tenor the
        forward, forward points, ATM / 25D risk reversal / 25D butterfly, and every pillar's
        strike, quoted vol, premium and Greeks (priced at the quoted vol with that tenor's rates).
        The data has vols only; every price and Greek here is the engine's.
        """
        snap = self.snapshot
        spot = snap.spot()
        tenors = []
        for s in self.market_now["surface"].tenors():
            T = s.T
            days = int(round(T * 365))
            rd = snap.domestic_rate(T)
            rf = snap.foreign_rate(T)
            pricer = fxengine.GKPricer(rd, rf)
            pillars = []
            quotes = [("10P", s.k_10p, s.vol_10p, False), ("25P", s.k_25p, s.vol_25p, False),
                      ("ATM", s.k_atm, s.vol_atm, True), ("25C", s.k_25c, s.vol_25c, True),
                      ("10C", s.k_10c, s.vol_10c, True)]
            for name, strike, vol, is_call in quotes:
                r = pricer.price(spot, strike, T, vol, is_call)
                pillars.append({
                    "pillar": name,
                    "is_call": is_call,
                    "strike": strike,
                    "vol": vol,
                    "price": r.price,
                    "delta": r.delta,
                    "gamma": r.gamma,
                    "vega": r.vega,
                    "theta": r.theta,
                })
            tenors.append({
                "tenor": TENOR_LABELS.get(days, f"{days}D"),
                "days": days,
                "expiry": self.timestamp(snap.time() + T).isoformat(),
                "forward": s.forward,
                "forward_points_pips": (s.forward - spot) * 1e4,
                "rate_cny": rd,
                "rate_usd": rf,
                "atm_vol": s.vol_atm,
                "rr25": s.vol_25c - s.vol_25p,
                "bf25": 0.5 * (s.vol_25c + s.vol_25p) - s.vol_atm,
                "pillars": pillars,
            })
        return {
            "time": self.now().isoformat(),
            "spot": spot,
            "rate_cny_on": snap.domestic_rate(1.0 / 365),
            "rate_usd_on": snap.foreign_rate(1.0 / 365),
            "tenors": tenors,
        }

    def state(self):
        """Everything about the book right now, as plain data."""
        summary = self.book.summary(self.snapshot)
        positions = []
        for row in summary["positions"]:
            row["expiry_ts"] = self.timestamp(row["expiry"]).isoformat()
            row["days_to_expiry"] = row["T"] * 365
            row["bucket"] = bucket_of(row["T"])
            row["source"] = "hedge" if row["position_id"] in self.hedge_ids else "client"
            positions.append(row)
        summary["positions"] = positions

        pending = []
        for o in self.pending:
            pending.append(self._order_row(o))

        summary["time"] = self.now().isoformat()
        summary["index"] = self.index
        summary["fixings"] = len(self.times)
        summary["done"] = self.done()
        summary["next_time"] = None if self.done() else self.times[self.index + 1].isoformat()
        summary["policy"] = self.policy
        summary["limits"] = dict(self.limits)
        summary["greeks_by_bucket"] = self._greeks_by_bucket()
        summary["pending_orders"] = pending
        summary["pnl_since_start_cny"] = summary["nav_cny"]    # NAV started at 0
        return summary

    def history(self):
        return list(self.history_log)

    def pnl_summary(self):
        """
        P&L since the start, by source: carry / time / spot / rates / vol from every step's
        explain, plus trading (client edge and anything booked away from mid).
        These add up to the NAV.
        """
        totals = {}
        for stage in STAGES:
            totals[stage] = 0.0
        totals["trading"] = 0.0
        steps = 0
        for record in self.history_log:
            if record["type"] == "step":
                for stage in STAGES:
                    totals[stage] += record["pnl_explain"][stage]
                steps += 1
            if "trade_pnl_cny" in record:
                totals["trading"] += record["trade_pnl_cny"]
        total = 0.0
        for name in totals:
            total += totals[name]
        totals["total"] = total
        totals["nav_cny"] = self.book.nav(self.snapshot)
        totals["steps"] = steps
        return totals

    def blotter(self):
        """Every trade the book has made, with timestamps instead of sim-clock years."""
        rows = []
        for trade in self.book.trades:
            row = dict(trade)
            if row.get("time") is not None:
                row["time"] = self.timestamp(row["time"]).isoformat()
            if row.get("expiry") is not None:
                row["expiry"] = self.timestamp(row["expiry"]).isoformat()
            rows.append(row)
        return rows