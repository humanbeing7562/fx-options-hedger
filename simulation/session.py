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
import pandas as pd
import fxengine

from simulation.market_loader import MarketLoader, years_between
from simulation.book import Book
from simulation.hedging import recommend_bucketed_hedge, bucket_of, BUCKETS

POLICIES = ["end_of_day", "threshold", "manual"]
DEFAULT_LIMITS = {
    "delta_usd": 1e6,                # hedge delta when |net delta| is above this (USD)
    "vega_cny_per_volpt": 50_000,    # smile-hedge when a bucket's |vega| per 1 vol point is above this
}
# Default bid/ask: mid vol -/+ this much (0.0010 = 0.1 vol point each side, 0.2 wide)
DEFAULT_HALF_SPREAD_VOL = 0.0010
# Hedge packages smaller than this (USD) aren't worth trading
MIN_HEDGE_NOTIONAL = 1.0
SECONDS_PER_YEAR = 365.0 * 24 * 3600


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
                self.limits[name] = float(limits[name])

        # The fixings this session walks through
        self.times = []
        for t in market.snapshot_times():
            if start is not None and t < pd.Timestamp(start):
                continue
            if end is not None and t > pd.Timestamp(end):
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
        # The vol surface picks its own model by strike (VV inside 25D, SABR in the wings)
        return self.market.get_snapshot(self.times[index], sim_start=self.start)

    def _is_end_of_day(self):
        if self.done():
            return True
        return self.times[self.index + 1].normalize() != self.now().normalize()

    def next(self):
        """Move to the next fixing and run the whole step. Returns the step record."""
        if self.done():
            raise ValueError("no more fixings - the session has reached its end")

        previous_snapshot = self.snapshot
        previous_time = self.now()

        # 1. clock
        self.index += 1
        self.snapshot = self._build_snapshot(self.index)
        dt = self.snapshot.time() - previous_snapshot.time()

        # 2. interest over the gap, at the rates in force when the gap began
        carry = self.book.accrue(previous_snapshot, dt)

        # 3. expiries
        settled = self.book.settle_expired(self.snapshot)
        for row in settled:
            self.hedge_ids.discard(row["position_id"])

        # 4. mark
        nav_marked = self.book.nav(self.snapshot)
        market_pnl = nav_marked - self.last_nav
        self.last_nav = nav_marked

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
        records = []
        count = 0
        while not self.done():
            if steps is not None and count >= steps:
                break
            if until is not None and self.times[self.index + 1] > pd.Timestamp(until):
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
            side        "buy" or "sell" - what the CLIENT does
            is_call     True / False
            notional    USD, positive
            strike      a number, "ATM" (delta-neutral straddle), "ATMF" (the forward to
                        expiry) or "25D" style (n-delta call or put)
            expiry      a timestamp, or tenor_days instead (counted from the order time)
            time        when the client asks (default: now); booked at the first fixing >= time
            price       optional agreed premium (CNY per USD); default = mid at booking
        """
        side = order.get("side")
        if side not in ["buy", "sell"]:
            raise ValueError("side must be 'buy' or 'sell' (what the client does)")
        if "is_call" not in order or not isinstance(order["is_call"], bool):
            raise ValueError("is_call must be true or false")
        notional = float(order.get("notional", 0))
        if notional <= 0:
            raise ValueError("notional must be positive (USD)")

        if order.get("time") is None:
            order_time = self.now()
        else:
            order_time = pd.Timestamp(order["time"])
        if order_time < self.now():
            raise ValueError(f"order time {order_time} is before the clock ({self.now()}) - "
                             f"the clock only moves forward")
        if order_time > self.times[-1]:
            raise ValueError(f"order time {order_time} is after the last fixing")

        if order.get("expiry") is not None:
            expiry_time = pd.Timestamp(order["expiry"])
        elif order.get("tenor_days") is not None:
            expiry_time = order_time + pd.Timedelta(days=float(order["tenor_days"]))
        else:
            raise ValueError("give expiry or tenor_days")
        if expiry_time <= order_time:
            raise ValueError("expiry must be after the order time")

        strike = order.get("strike", "ATM")
        if isinstance(strike, str):
            text = strike.strip().upper()
            if text != "ATM" and text != "ATMF":
                if not text.endswith("D"):
                    raise ValueError("strike must be a number, 'ATM', 'ATMF' or like '25D'")
                delta = float(text[:-1])
                if delta <= 0 or delta >= 100:
                    raise ValueError("delta strike must be between 0D and 100D")
            strike = text
        else:
            strike = float(strike)
            if strike <= 0:
                raise ValueError("strike must be positive")

        price = order.get("price")
        if price is not None:
            price = float(price)
            if price < 0:
                raise ValueError("price must not be negative")

        return {
            "side": side,
            "is_call": order["is_call"],
            "notional": notional,
            "strike": strike,
            "time": order_time,
            "expiry": expiry_time,
            "price": price,
        }

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

        if o["side"] == "buy":
            our_price = ask
        else:
            our_price = bid
        return {
            "time": self.now().isoformat(),
            "side": o["side"],
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
            "premium_cny_at_our_price": our_price * o["notional"],
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
            row = self._book_order(o)
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
        if T <= 0:
            o["status"] = "rejected"
            o["reason"] = "expired before it could be booked"
            return self._order_row(o)

        strike = self._resolve_strike(o["strike"], T, o["is_call"])
        mid = self.snapshot.price(strike, T, o["is_call"]).price
        price = o["price"]
        if price is None:
            price = mid

        # Our side is the opposite of the client's
        if o["side"] == "buy":
            our_notional = -o["notional"]
        else:
            our_notional = o["notional"]

        note = f"client order #{o['order_id']}: client {o['side']}s"
        position_id = self.book.book_option(strike, expiry, o["is_call"], our_notional, price,
                                            time=now, note=note)
        o["status"] = "booked"
        o["booked_at"] = self.now()
        o["position_id"] = position_id
        o["strike_value"] = strike
        o["mid"] = mid
        o["booked_price"] = price
        # Day-one P&L: what we gain versus mid (positive when we sold above mid or bought below)
        o["edge_cny"] = -our_notional * (price - mid)
        return self._order_row(o)

    def _order_row(self, o):
        row = {
            "order_id": o["order_id"],
            "status": o["status"],
            "side": o["side"],
            "is_call": o["is_call"],
            "notional": o["notional"],
            "strike": o["strike"],
            "time": o["time"].isoformat(),
            "expiry": o["expiry"].isoformat(),
            "price": o["price"],
        }
        if o["status"] == "booked":
            row["booked_at"] = o["booked_at"].isoformat()
            row["position_id"] = o["position_id"]
            row["strike_value"] = o["strike_value"]
            row["mid"] = o["mid"]
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
        Books a smile hedge plan from recommend_hedge() (made at THIS fixing) - or a
        fresh one if none is given - then flattens delta with spot and nets the legs.
        Returns what was traded.
        """
        if plan is None:
            plan = self.recommend_hedge()
        if plan.get("time") != self.now().isoformat():
            raise ValueError("this plan was made at another fixing - ask for a new one")
        nav_before = self.book.nav(self.snapshot)
        result = self._hedge_smile(plan)
        result["spot"] = self._hedge_delta()
        result["netted"] = self._net()
        nav_after = self.book.nav(self.snapshot)
        record = {"type": "hedge", "hedges": result, "trade_pnl_cny": nav_after - nav_before,
                  "nav_cny": nav_after}
        self._record(record)
        self.last_nav = nav_after
        return result

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