import fxengine

from simulation.book import EXPIRY_TOL
from simulation.hedging import bucket_of, BUCKETS

STAGES = ["carry", "time", "spot", "rates", "vol"]


def option_value(position, snapshot):
    """Value of one option position (CNY) on a snapshot; intrinsic once it has expired."""
    T = position["expiry"] - snapshot.time()
    if T <= EXPIRY_TOL:
        spot = snapshot.spot()
        if position["is_call"]:
            payoff = max(spot - position["strike"], 0.0)
        else:
            payoff = max(position["strike"] - spot, 0.0)
        return position["notional"] * payoff
    result = snapshot.price(position["strike"], T, position["is_call"])
    return position["notional"] * result.price


def make_snapshot(time, spot, curves_from, surface_from):
    """A MarketSnapshot mixing pieces of two fixings' markets."""
    return fxengine.MarketSnapshot(time, spot, curves_from["cny"], curves_from["usd"],
                                   surface_from["surface"])


def decompose_step(positions, start, end, hedge_before, cash_before, hedge_after, cash_after):
    """
    positions      Book.position_terms() taken BEFORE the step (before accrue and settlement)
    start, end     {"time": years, "market": MarketLoader.get_market(...)} for fixings A and B
    hedge_before, cash_before    USD hedge and CNY cash before interest
    hedge_after,  cash_after     the same after Book.accrue, before settlement
    Returns a plain dict (JSON-ready).
    """
    a = start["market"]
    b = end["market"]
    t_a = start["time"]
    t_b = end["time"]
    spot_a = a["spot"]
    spot_b = b["spot"]

    # The chain of markets, one input moved at a time
    snaps = {
        "start": make_snapshot(t_a, spot_a, a, a),
        "time": make_snapshot(t_b, spot_a, a, a),
        "spot": make_snapshot(t_b, spot_b, a, a),
        "rates": make_snapshot(t_b, spot_b, b, a),
        "vol": make_snapshot(t_b, spot_b, b, b),
    }

    # Every option's value at every stage (carry doesn't touch options)
    values = {}
    for stage in snaps:
        row = []
        for position in positions:
            row.append(option_value(position, snaps[stage]))
        values[stage] = row

    # Options value at each stage
    options = {}
    for stage in values:
        options[stage] = 0.0
        for v in values[stage]:
            options[stage] += v

    # NAV at each stage
    nav = {}
    nav["start"] = options["start"] + hedge_before * spot_a + cash_before
    nav["carry"] = options["start"] + hedge_after * spot_a + cash_after
    nav["time"] = options["time"] + hedge_after * spot_a + cash_after
    nav["spot"] = options["spot"] + hedge_after * spot_b + cash_after
    nav["rates"] = options["rates"] + hedge_after * spot_b + cash_after
    nav["vol"] = options["vol"] + hedge_after * spot_b + cash_after

    previous = "start"
    parts = {}
    for stage in STAGES:
        parts[stage] = nav[stage] - nav[previous]
        previous = stage

    # ---- Greek splits ----
    dS = spot_b - spot_a
    option_delta = 0.0
    option_gamma = 0.0
    vega_pnl = 0.0
    for position in positions:
        T_b = position["expiry"] - t_b
        if T_b <= EXPIRY_TOL:
            continue      # expires in the step: no Greeks, its move stays in "other"
        r = snaps["time"].price(position["strike"], T_b, position["is_call"])
        option_delta += position["notional"] * r.delta
        option_gamma += position["notional"] * r.gamma
        # vega x change in vol at this option's own strike and maturity
        r_vol = snaps["rates"].price(position["strike"], T_b, position["is_call"])
        vol_before = snaps["rates"].vol(position["strike"], T_b)
        vol_after = snaps["vol"].vol(position["strike"], T_b)
        vega_pnl += position["notional"] * r_vol.vega * (vol_after - vol_before)

    delta_pnl = (option_delta + hedge_after) * dS
    gamma_pnl = 0.5 * option_gamma * dS * dS
    spot_split = {
        "delta": delta_pnl,
        "gamma": gamma_pnl,
        "other": parts["spot"] - delta_pnl - gamma_pnl,
    }
    vol_split = {
        "vega": vega_pnl,
        "other": parts["vol"] - vega_pnl,
    }

    # ---- By expiry bucket (options only; the USD hedge and cash have their own line) ----
    by_bucket = {}
    for label, edge in BUCKETS:
        by_bucket[label] = {"time": 0.0, "spot": 0.0, "rates": 0.0, "vol": 0.0, "positions": 0}
    by_bucket["expiring"] = {"time": 0.0, "spot": 0.0, "rates": 0.0, "vol": 0.0, "positions": 0}
    for i in range(len(positions)):
        position = positions[i]
        if position["expiry"] - t_b <= EXPIRY_TOL:
            label = "expiring"          # expires during this step
        else:
            label = bucket_of(position["expiry"] - t_a)
        by_bucket[label]["time"] += values["time"][i] - values["start"][i]
        by_bucket[label]["spot"] += values["spot"][i] - values["time"][i]
        by_bucket[label]["rates"] += values["rates"][i] - values["spot"][i]
        by_bucket[label]["vol"] += values["vol"][i] - values["rates"][i]
        by_bucket[label]["positions"] += 1

    hedge_and_cash = {
        "carry": parts["carry"],
        "spot": hedge_after * dS,
    }

    total_pnl = 0.0
    for stage in STAGES:
        total_pnl += parts[stage]

    return {
        "carry": parts["carry"],
        "time": parts["time"],
        "spot": parts["spot"],
        "rates": parts["rates"],
        "vol": parts["vol"],
        "total": total_pnl,
        "spot_split": spot_split,
        "vol_split": vol_split,
        "by_bucket": by_bucket,
        "hedge_and_cash": hedge_and_cash,
        "spot_start": spot_a,
        "spot_end": spot_b,
        "dS_pips": dS * 1e4,
        "nav_start": nav["start"],
        "nav_end": nav["vol"],
    }