"""
Reads the clean CSVs and turns them into what the C++ engine needs
for any snapshot: CNY/USD curves, spot, forwards, and vol quotes.
"""
import os
import pandas as pd
import fxengine

# Project root = the folder above sim/, so paths work from any working directory
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def years_between(start, end):
    # Calendar time in years (ACT/365), the sim's clock unit
    seconds = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds()
    return seconds / (365.0 * 24 * 3600)


class MarketLoader:
    def __init__(self, clean_dir=None):
        if clean_dir is None:
            clean_dir = os.path.join(PROJECT_ROOT, "data/clean")

        self.curves = pd.read_csv(os.path.join(clean_dir, "curves.csv"), parse_dates=["date"])
        self.spot = pd.read_csv(os.path.join(clean_dir, "spot.csv"), parse_dates=["timestamp"])
        self.vols = pd.read_csv(os.path.join(clean_dir, "vols.csv"), parse_dates=["timestamp"])

        # Spot lookup table: timestamp -> spot (also catches duplicates once, up front)
        self.spot_by_time = {}
        for i in range(len(self.spot)):
            t = pd.Timestamp(self.spot["timestamp"].iloc[i])
            if t in self.spot_by_time:
                raise ValueError(f"duplicate spot rows for {t}")
            # Yahoo stores prices as float32, so round off the noise (6.703700065 -> 6.7037)
            self.spot_by_time[t] = round(float(self.spot["spot"].iloc[i]), 4)

        # Curves are rebuilt only once per (date, currency)
        self.curve_cache = {}

    def get_spot(self, timestamp):
        timestamp = pd.Timestamp(timestamp)
        if timestamp not in self.spot_by_time:
            raise ValueError(f"no spot for {timestamp}")
        return self.spot_by_time[timestamp]

    def build_curve(self, date, currency):
        date = pd.Timestamp(date)
        key = (date, currency)
        if key in self.curve_cache:
            return self.curve_cache[key]

        rows = self.curves[(self.curves["date"] == date) & (self.curves["currency"] == currency)]
        rows = rows.sort_values("tenor_years")

        tenors = []
        rates = []
        for i in range(len(rows)):
            tenors.append(float(rows["tenor_years"].iloc[i]))
            rates.append(float(rows["rate"].iloc[i]))

        if len(tenors) == 0:
            raise ValueError(f"no {currency} curve for {date.date()}")

        curve = fxengine.InterpolatedCurve(tenors, rates)
        self.curve_cache[key] = curve
        return curve

    def get_curves(self, timestamp):
        # Curves are daily, so every snapshot on a day uses that day's curves
        timestamp = pd.Timestamp(timestamp)
        date = timestamp.normalize()
        cny = self.build_curve(date, "CNY")
        usd = self.build_curve(date, "USD")
        return cny, usd

    def get_forward(self, timestamp, T):
        # F = S * DF_usd(T) / DF_cny(T)
        if T < 0:
            raise ValueError(f"T must not be negative, got {T}")
        spot = self.get_spot(timestamp)
        cny, usd = self.get_curves(timestamp)
        return spot * usd.discount(T) / cny.discount(T)

    def get_forwards(self, timestamp):
        # Forward for every vol tenor at this snapshot, built from one set of curves
        timestamp = pd.Timestamp(timestamp)
        spot = self.get_spot(timestamp)
        cny, usd = self.get_curves(timestamp)

        tenors = self.vols[self.vols["timestamp"] == timestamp]
        tenors = tenors[["tenor", "tenor_years"]].drop_duplicates().sort_values("tenor_years")
        if len(tenors) == 0:
            raise ValueError(f"no vol tenors for {timestamp}")

        forwards = {}
        for i in range(len(tenors)):
            tenor = tenors["tenor"].iloc[i]
            T = float(tenors["tenor_years"].iloc[i])
            forwards[tenor] = spot * usd.discount(T) / cny.discount(T)
        return forwards

    def get_pillar_quotes(self, timestamp):
        """
        Everything the C++ strike function needs, per tenor, for one snapshot:
            {tenor: {"T", "forward", "rd", "rf", "vols": {pillar: vol}}}
        Reads data only - no strike maths here.
        """
        timestamp = pd.Timestamp(timestamp)
        spot = self.get_spot(timestamp)
        cny, usd = self.get_curves(timestamp)

        snapshot = self.vols[self.vols["timestamp"] == timestamp]
        if len(snapshot) == 0:
            raise ValueError(f"no vol quotes for {timestamp}")
        snapshot = snapshot.sort_values("tenor_years")

        quotes = {}
        for i in range(len(snapshot)):
            tenor = snapshot["tenor"].iloc[i]
            T = float(snapshot["tenor_years"].iloc[i])

            if tenor not in quotes:
                quotes[tenor] = {
                    "T": T,
                    "forward": spot * usd.discount(T) / cny.discount(T),
                    "rd": cny.zero_rate(T),     # CNY = domestic
                    "rf": usd.zero_rate(T),     # USD = foreign
                    "vols": {},
                }

            pillar = snapshot["pillar"].iloc[i]
            if pillar in quotes[tenor]["vols"]:
                raise ValueError(f"duplicate {tenor} {pillar} at {timestamp}")
            quotes[tenor]["vols"][pillar] = float(snapshot["vol"].iloc[i])

        for tenor in quotes:
            if len(quotes[tenor]["vols"]) != 5:
                raise ValueError(f"{tenor} at {timestamp} has {len(quotes[tenor]['vols'])} pillars, expected 5")
        return quotes

    def get_smile_vol(self, timestamp, model="hybrid"):
        """
        Builds the C++ SmileVol (vol surface) for one snapshot.
        model = "hybrid" -> VV inside 25D, SABR past 10D, blended between (the desk's surface)
        model = "vv"     -> vanna-volga only, through 25P / ATM / 25C (for comparison)
        model = "sabr"   -> SABR only, fitted to all 5 pillars inside C++ (QuantLib)
        Python only packs the quotes; strikes and the smile are done in C++.
        """
        timestamp = pd.Timestamp(timestamp)
        spot = self.get_spot(timestamp)
        cny, usd = self.get_curves(timestamp)
        pillar_quotes = self.get_pillar_quotes(timestamp)

        tenor_quotes = []
        for tenor in pillar_quotes:          # already ordered shortest -> longest
            q = pillar_quotes[tenor]
            tq = fxengine.TenorQuote()
            tq.T = q["T"]
            tq.vol_10p = q["vols"]["10P"]
            tq.vol_25p = q["vols"]["25P"]
            tq.vol_atm = q["vols"]["ATM"]
            tq.vol_25c = q["vols"]["25C"]
            tq.vol_10c = q["vols"]["10C"]
            tenor_quotes.append(tq)

        return fxengine.SmileVol(spot, cny, usd, tenor_quotes, model)

    def get_market(self, timestamp, model="hybrid"):
        """
        The pieces of the market at one fixing, as a dict:
            {"spot", "cny" (curve), "usd" (curve), "surface" (SmileVol)}
        The P&L decomposer mixes pieces from two fixings, so it needs them separately.
        """
        timestamp = pd.Timestamp(timestamp)
        cny, usd = self.get_curves(timestamp)
        return {
            "spot": self.get_spot(timestamp),
            "cny": cny,
            "usd": usd,
            "surface": self.get_smile_vol(timestamp, model),
        }

    def get_snapshot(self, timestamp, sim_start, model="hybrid"):
        """
        Builds the C++ MarketSnapshot for one fixing: spot, both curves, vol surface.
        Its time is years since sim_start - the same clock position expiries use.
        """
        timestamp = pd.Timestamp(timestamp)
        sim_start = pd.Timestamp(sim_start)
        if timestamp < sim_start:
            raise ValueError(f"{timestamp} is before the sim start {sim_start}")

        time = years_between(sim_start, timestamp)
        market = self.get_market(timestamp, model)
        return fxengine.MarketSnapshot(time, market["spot"], market["cny"], market["usd"],
                                       market["surface"])

    def get_vol_quote(self, timestamp, tenor, pillar):
        timestamp = pd.Timestamp(timestamp)
        rows = self.vols[(self.vols["timestamp"] == timestamp)
                         & (self.vols["tenor"] == tenor)
                         & (self.vols["pillar"] == pillar)]
        if len(rows) == 0:
            raise ValueError(f"no {tenor} {pillar} vol for {timestamp}")
        if len(rows) > 1:
            raise ValueError(f"{len(rows)} rows for {tenor} {pillar} at {timestamp}, expected 1")
        return float(rows["vol"].iloc[0])

    def snapshot_times(self):
        # The vol snapshots drive the sim; get_spot will complain if one has no spot
        times = sorted(self.vols["timestamp"].unique())
        result = []
        for t in times:
            result.append(pd.Timestamp(t))
        return result