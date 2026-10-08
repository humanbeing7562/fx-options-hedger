"""
Book: everything we own at one moment, and the actions that change it.

    portfolio   C++ Portfolio: the options (from OUR side: notional < 0 = we sold)
    hedge_usd   USD held as a hedge (+ bought, - sold)
    cash_cny    every CNY in and out (premiums, spot payments, settlements, interest)

NAV = options value (at mid) + hedge_usd * spot + cash_cny

The Book doesn't move the clock or decide prices - the Session does that.
Times (expiry, snapshot.time()) are years since the sim started.
"""
import math
import fxengine

OVERNIGHT = 1.0 / 365.0

# An option within this much (years, ~0.03 s) of expiry counts as expired, so
# floating-point rounding can't leave it alive at its own expiry fixing.
EXPIRY_TOL = 1e-9

# Netting: legs count as identical if strike and expiry agree this closely, and a
# merged notional this small (USD) counts as zero
NET_TOL = 1e-12
NET_ZERO = 1e-6


class Book:
    def __init__(self):
        self.portfolio = fxengine.Portfolio()
        self.hedge_usd = 0.0
        self.cash_cny = 0.0
        self.trades = []          # every trade, in order (the blotter)
        self.package_count = 0

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def book_option(self, strike, expiry, is_call, notional, price, time=None, note=""):
        """
        notional: USD, from our side (-10e6 = we sold 10m, +10e6 = we bought 10m)
        price:    agreed premium in CNY per 1 USD of notional
        time:     when it was booked (sim clock), only used for the trade log
        """
        if notional == 0:
            raise ValueError("notional must not be zero")
        if strike <= 0:
            raise ValueError("strike must be positive")
        if price < 0:
            raise ValueError("price must not be negative")
        now = 0.0
        if time is not None:
            now = time
        if expiry <= now + EXPIRY_TOL:
            raise ValueError("option would already be expired")

        position = fxengine.Position()
        position.strike = strike
        position.expiry = expiry
        position.is_call = is_call
        position.notional = notional
        position_id = self.portfolio.add_option(position)

        # We sold (notional < 0) -> we receive the premium; we bought -> we pay it
        premium = -notional * price
        self.cash_cny += premium

        self.trades.append({
            "type": "option",
            "time": time,
            "position_id": position_id,
            "strike": strike,
            "expiry": expiry,
            "is_call": is_call,
            "notional": notional,
            "price": price,
            "cash_cny": premium,
            "note": note,
        })
        return position_id

    def book_package(self, kind, legs, notional, expiry, prices=None, time=None, note=""):
        """
        Books a package (ATM straddle, RR, BF...) as one trade: each leg becomes an
        option with notional * leg ratio.
        legs:     from hedging.build_package (strike, is_call, ratio, mid)
        notional: USD per 1x leg, from OUR side (+ = we buy the package)
        prices:   agreed price per leg; None = trade every leg at its mid
        Returns the position ids of the legs.
        """
        if notional == 0:
            raise ValueError("notional must not be zero")
        if prices is not None and len(prices) != len(legs):
            raise ValueError("need one price per leg")

        self.package_count += 1
        package_id = self.package_count
        label = f"{kind} package #{package_id}"
        if note:
            label = label + " - " + note

        position_ids = []
        total_cash = 0.0
        for i in range(len(legs)):
            leg = legs[i]
            if prices is None:
                price = leg["mid"]
            else:
                price = prices[i]
            leg_notional = notional * leg["ratio"]
            cash_before = self.cash_cny
            position_id = self.book_option(leg["strike"], expiry, leg["is_call"],
                                           leg_notional, price, time=time, note=label)
            total_cash += self.cash_cny - cash_before
            position_ids.append(position_id)

        self.trades.append({
            "type": "package",
            "time": time,
            "package_id": package_id,
            "kind": kind,
            "notional": notional,
            "expiry": expiry,
            "position_ids": position_ids,
            "cash_cny": 0.0,              # the legs above already moved the cash
            "package_cash_cny": total_cash,
            "note": note,
        })
        return position_ids

    def trade_spot(self, usd_amount, spot, time=None, note=""):
        """usd_amount > 0 buys USD, < 0 sells USD, at the given spot rate."""
        if usd_amount == 0:
            return
        if spot <= 0:
            raise ValueError("spot must be positive")

        self.hedge_usd += usd_amount
        cash_change = -usd_amount * spot
        self.cash_cny += cash_change

        self.trades.append({
            "type": "spot",
            "time": time,
            "usd_amount": usd_amount,
            "spot": spot,
            "cash_cny": cash_change,
            "note": note,
        })

    def net_positions(self, position_ids, time=None):
        """
        Merges options among `position_ids` that have the same strike, expiry and
        call/put into one position (the oldest id keeps the summed notional).
        A merge that sums to zero removes the position. No cash moves: two options
        with identical terms are the same risk, so NAV and Greeks don't change.
        Returns {merged id: surviving id or None if it netted to zero}.
        """
        wanted = set(position_ids)
        groups = []          # each: {"keep": id, "strike", "expiry", "is_call", "total", "ids": [...]}
        for pos in self.portfolio.get_options():
            if pos.id not in wanted:
                continue
            found = None
            for group in groups:
                if (group["is_call"] == pos.is_call
                        and abs(group["strike"] - pos.strike) <= NET_TOL
                        and abs(group["expiry"] - pos.expiry) <= NET_TOL):
                    found = group
                    break
            if found is None:
                groups.append({"keep": pos.id, "strike": pos.strike, "expiry": pos.expiry,
                               "is_call": pos.is_call, "total": pos.notional, "ids": [pos.id]})
            else:
                found["total"] += pos.notional
                found["ids"].append(pos.id)

        merged = {}
        for group in groups:
            if len(group["ids"]) < 2:
                continue
            total = group["total"]
            if abs(total) < NET_ZERO:
                total = 0.0
            keep = group["keep"]
            self.portfolio.set_notional(keep, total)
            for position_id in group["ids"][1:]:
                self.portfolio.set_notional(position_id, 0.0)
            survivor = keep if total != 0.0 else None
            for position_id in group["ids"]:
                if position_id != survivor:
                    merged[position_id] = survivor
            self.trades.append({
                "type": "net",
                "time": time,
                "position_ids": group["ids"],
                "kept_id": survivor,
                "notional": total,
                "cash_cny": 0.0,
                "note": "netted identical legs",
            })
        return merged

    def accrue(self, snapshot, dt):
        """
        Interest for dt years: CNY cash at the CNY overnight rate, the USD hedge at
        the USD overnight rate.
        Pass the snapshot from the START of the interval - interest over the gap is
        earned at the rate that was in force when the gap began.
        Returns the interest in CNY (USD interest valued at this snapshot's spot).
        """
        if dt < 0:
            raise ValueError("dt must not be negative")

        r_cny = snapshot.domestic_rate(OVERNIGHT)
        r_usd = snapshot.foreign_rate(OVERNIGHT)

        old_cash = self.cash_cny
        old_hedge = self.hedge_usd
        self.cash_cny = self.cash_cny * math.exp(r_cny * dt)
        self.hedge_usd = self.hedge_usd * math.exp(r_usd * dt)

        cny_interest = self.cash_cny - old_cash
        usd_interest = self.hedge_usd - old_hedge
        return cny_interest + usd_interest * snapshot.spot()

    def settle_expired(self, snapshot):
        """
        Options whose expiry is at or before this snapshot pay their intrinsic value
        at this snapshot's spot (cash settled) and leave the book.
        Returns a list of what was settled.
        """
        spot = snapshot.spot()
        now = snapshot.time()

        expired = []
        for pos in self.portfolio.get_options():
            if pos.expiry <= now + EXPIRY_TOL:
                expired.append((pos.id, pos.strike, pos.is_call, pos.notional))

        settled = []
        for position_id, strike, is_call, notional in expired:
            if is_call:
                payoff = max(spot - strike, 0.0)
            else:
                payoff = max(strike - spot, 0.0)
            cash = notional * payoff
            self.cash_cny += cash
            self.portfolio.remove_option(position_id)

            settled.append({
                "position_id": position_id,
                "strike": strike,
                "is_call": is_call,
                "notional": notional,
                "spot": spot,
                "payoff_per_usd": payoff,
                "cash_cny": cash,
            })
            self.trades.append({
                "type": "settlement",
                "time": now,
                "position_id": position_id,
                "spot": spot,
                "cash_cny": cash,
                "note": "expired",
            })
        return settled

    # ------------------------------------------------------------------
    # Questions
    # ------------------------------------------------------------------

    def check_settled(self, snapshot):
        """
        The C++ portfolio skips options with no time left, so an expired option that
        hasn't been settled would silently vanish from NAV. Refuse instead.
        """
        now = snapshot.time()
        for pos in self.portfolio.get_options():
            if pos.expiry <= now + EXPIRY_TOL:
                raise ValueError(f"position {pos.id} has expired but is not settled - "
                                 f"call settle_expired first")

    def options_value(self, snapshot):
        self.check_settled(snapshot)
        return self.portfolio.aggregate_greeks(snapshot).price

    def nav(self, snapshot):
        return self.options_value(snapshot) + self.hedge_usd * snapshot.spot() + self.cash_cny

    def greeks(self, snapshot):
        """Book Greeks. The USD hedge only adds delta (1 per USD held)."""
        self.check_settled(snapshot)
        g = self.portfolio.aggregate_greeks(snapshot)
        return {
            "delta": g.delta + self.hedge_usd,
            "option_delta": g.delta,
            "gamma": g.gamma,
            "vega": g.vega,
            "vanna": g.vanna,
            "volga": g.volga,
            "theta": g.theta,
            "rho": g.rho,
        }

    def net_delta(self, snapshot):
        """USD delta left after the hedge - trade -net_delta to flatten it."""
        self.check_settled(snapshot)
        return self.portfolio.aggregate_greeks(snapshot).delta + self.hedge_usd

    def positions(self, snapshot):
        """Each live option with its current mid value and Greeks."""
        self.check_settled(snapshot)
        now = snapshot.time()
        result = []
        for pos in self.portfolio.get_options():
            T = pos.expiry - now
            r = snapshot.price(pos.strike, T, pos.is_call)
            result.append({
                "position_id": pos.id,
                "strike": pos.strike,
                "expiry": pos.expiry,
                "T": T,
                "is_call": pos.is_call,
                "notional": pos.notional,
                "vol": snapshot.vol(pos.strike, T),
                "price": r.price,
                "value": r.price * pos.notional,
                "delta": r.delta * pos.notional,
                "gamma": r.gamma * pos.notional,
                "vega": r.vega * pos.notional,
                "vanna": r.vanna * pos.notional,
                "volga": r.volga * pos.notional,
                "theta": r.theta * pos.notional,
            })
        return result

    def summary(self, snapshot):
        """Plain dict of the whole book - ready to send to a web page as JSON."""
        options_value = self.options_value(snapshot)
        hedge_value = self.hedge_usd * snapshot.spot()
        return {
            "time": snapshot.time(),
            "spot": snapshot.spot(),
            "cash_cny": self.cash_cny,
            "hedge_usd": self.hedge_usd,
            "hedge_value_cny": hedge_value,
            "options_value_cny": options_value,
            "nav_cny": options_value + hedge_value + self.cash_cny,
            "greeks": self.greeks(snapshot),
            "positions": self.positions(snapshot),
        }