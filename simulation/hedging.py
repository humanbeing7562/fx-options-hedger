"""
Smile hedging with the three market packages:

    ATM   delta-neutral straddle:  +1 ATM call, +1 ATM put           -> mostly vega
    RR    25-delta risk reversal:  +1 25D call, -1 25D put            -> mostly vanna (skew)
    BF    25-delta butterfly:      +1 25D strangle, -x ATM straddle   -> mostly volga (curvature)
          (x chosen so the butterfly has zero vega, as the market quotes it)

recommend_smile_hedge() finds how much of each package to trade so the book's
vega, vanna and volga are all zero at once. It only RECOMMENDS - the user decides
what to trade, and books it with Book.book_package().

Notional of a package = USD notional of each 1x leg. A leg with ratio -1 is sold.
"""
import numpy as np

PACKAGES = ["ATM", "RR", "BF"]


def _leg(snapshot, strike, is_call, ratio, T):
    r = snapshot.price(strike, T, is_call)
    return {
        "strike": strike,
        "is_call": is_call,
        "ratio": ratio,
        "vol": snapshot.vol(strike, T),
        "mid": r.price,
    }


def build_package(snapshot, kind, expiry):
    """
    Legs of one package expiring at `expiry` (sim clock, years), with strikes found
    on this snapshot's smile. Each leg: strike, is_call, ratio, vol, mid price.
    """
    T = expiry - snapshot.time()
    if T <= 0:
        raise ValueError("package expiry must be in the future")

    k_atm = snapshot.atm_strike(T)

    if kind == "ATM":
        return [
            _leg(snapshot, k_atm, True, 1.0, T),
            _leg(snapshot, k_atm, False, 1.0, T),
        ]

    k_25c = snapshot.strike_from_delta(0.25, T, True)
    k_25p = snapshot.strike_from_delta(-0.25, T, False)

    if kind == "RR":
        return [
            _leg(snapshot, k_25c, True, 1.0, T),
            _leg(snapshot, k_25p, False, -1.0, T),
        ]

    if kind == "BF":
        # Vega-neutral: sell enough ATM straddle to cancel the strangle's vega
        strangle_vega = snapshot.price(k_25c, T, True).vega + snapshot.price(k_25p, T, False).vega
        straddle_vega = snapshot.price(k_atm, T, True).vega + snapshot.price(k_atm, T, False).vega
        x = strangle_vega / straddle_vega
        return [
            _leg(snapshot, k_25c, True, 1.0, T),
            _leg(snapshot, k_25p, False, 1.0, T),
            _leg(snapshot, k_atm, True, -x, T),
            _leg(snapshot, k_atm, False, -x, T),
        ]

    raise ValueError(f"unknown package {kind!r} - use one of {PACKAGES}")


def package_greeks(snapshot, legs, expiry):
    """Greeks and mid price of 1 USD notional of a package."""
    T = expiry - snapshot.time()
    total = {"price": 0.0, "delta": 0.0, "gamma": 0.0, "vega": 0.0,
             "vanna": 0.0, "volga": 0.0, "theta": 0.0}
    for leg in legs:
        r = snapshot.price(leg["strike"], T, leg["is_call"])
        total["price"] += leg["ratio"] * r.price
        total["delta"] += leg["ratio"] * r.delta
        total["gamma"] += leg["ratio"] * r.gamma
        total["vega"] += leg["ratio"] * r.vega
        total["vanna"] += leg["ratio"] * r.vanna
        total["volga"] += leg["ratio"] * r.volga
        total["theta"] += leg["ratio"] * r.theta
    return total


# Expiry buckets, the same edges as the C++ Portfolio::greeks_by_expiry
# (label, upper edge in years of time left)
BUCKETS = [
    ("1W", 7.0 / 365),
    ("1M", 30.0 / 365),
    ("3M", 91.0 / 365),
    ("6M", 182.0 / 365),
    ("1Y", float("inf")),
]

# The key C++ greeks_by_expiry uses for each bucket
CPP_BUCKET_KEY = {"1W": 7.0 / 365, "1M": 30.0 / 365, "3M": 91.0 / 365, "6M": 182.0 / 365, "1Y": 1.0}

# Below this much time left, a 25D/ATM package is too close to expiry to hedge with
MIN_HEDGE_T = 1.0 / 365
# If the system is this badly conditioned, the packages can't separate the risks and the
# solver would answer with huge offsetting trades. Healthy cases on the Jul-Sep data stay
# below ~2e3 (fuzz_hedging.py); a near-duplicate instrument showed ~7e7.
MAX_CONDITION = 1e5
# The gamma straddle is used only if it expires at least this long before the packages
MIN_GAMMA_GAP = 3.0 / 365

SMILE_GREEKS = ["vega", "vanna", "volga"]


# Same rounding tolerance as the C++ snap_to_bucket
EDGE_TOL = 1e-9


def bucket_of(T):
    for label, edge in BUCKETS:
        if T <= edge + EDGE_TOL:
            return label
    return BUCKETS[-1][0]


def solve_packages(snapshot, expiry, target, gamma_expiry=None):
    """
    Notionals of ATM, RR, BF expiring at `expiry` that cancel the vega, vanna and
    volga in `target`. Returns (packages dict, condition number).

        [ vega_ATM   vega_RR   vega_BF  ] [n_ATM]     [ vega  ]
        [ vanna_ATM  vanna_RR  vanna_BF ] [n_RR ] = - [ vanna ]
        [ volga_ATM  volga_RR  volga_BF ] [n_BF ]     [ volga ]

    With gamma_expiry set, a 4th instrument - an ATM straddle at gamma_expiry ("GAMMA")
    - and a 4th row (gamma) are added, so gamma is cancelled too. Needed when the
    target mixes expiries: then gamma is no longer just vega rescaled.
    """
    kinds = list(PACKAGES)
    rows = list(SMILE_GREEKS)

    packages = {}
    for kind in PACKAGES:
        legs = build_package(snapshot, kind, expiry)
        packages[kind] = {"legs": legs, "expiry": expiry,
                          "greeks": package_greeks(snapshot, legs, expiry)}

    if gamma_expiry is not None:
        legs = build_package(snapshot, "ATM", gamma_expiry)
        packages["GAMMA"] = {"legs": legs, "expiry": gamma_expiry,
                             "greeks": package_greeks(snapshot, legs, gamma_expiry)}
        kinds.append("GAMMA")
        rows = ["gamma"] + rows

    size = len(kinds)
    matrix = np.zeros((size, size))
    rhs = np.zeros(size)
    for i in range(size):
        for j in range(size):
            matrix[i, j] = packages[kinds[j]]["greeks"][rows[i]]
        rhs[i] = -target[rows[i]]

    condition = float(np.linalg.cond(matrix))
    if condition > MAX_CONDITION:
        raise ValueError(f"packages can't separate {', '.join(rows)} here (condition {condition:.1e})")

    notionals = np.linalg.solve(matrix, rhs)
    for j in range(size):
        packages[kinds[j]]["notional"] = float(notionals[j])
    return packages, condition


def _package_summary(packages):
    """Turn solved packages into plain data, plus the delta / premium / Greeks they add."""
    added = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "vanna": 0.0, "volga": 0.0, "price": 0.0}
    result = {}
    for kind in packages:
        n = packages[kind]["notional"]
        g = packages[kind]["greeks"]
        for name in added:
            added[name] += n * g[name]
        result[kind] = {
            "notional": n,
            "expiry": packages[kind]["expiry"],
            "mid_price": g["price"],
            "legs": packages[kind]["legs"],
        }
    return result, added


def recommend_smile_hedge(book, snapshot, expiry):
    """
    Whole-book hedge: ATM, RR and BF all expiring at `expiry`, so the TOTAL vega,
    vanna and volga become zero. Fine for a book at one expiry; for a book spread
    over several expiries use recommend_bucketed_hedge.
    """
    book_greeks = book.greeks(snapshot)
    packages, condition = solve_packages(snapshot, expiry, book_greeks)
    summary, added = _package_summary(packages)
    return {
        "expiry": expiry,
        "packages": summary,
        "spot_trade_usd": -(book_greeks["delta"] + added["delta"]),
        "premium_at_mid_cny": -added["price"],       # cash we'd receive (+) or pay (-)
        "condition": condition,
    }


def recommend_bucketed_hedge(book, snapshot):
    """
    Desk-style hedge, one expiry bucket at a time (1W, 1M, 3M, 6M, 1Y).

    For each bucket: ATM, RR and BF expiring at that bucket's vega-weighted expiry
    cancel the bucket's own vega, vanna and volga.
      - All positions at (nearly) one expiry: gamma is vega rescaled, so the 3x3
        flattens gamma too.
      - Mixed expiries, with the shortest at least 3 days before the hedge expiry:
        gamma and vega come apart, so a 4th instrument - an ATM straddle at the
        bucket's SHORTEST expiry - and a gamma row are added (4x4), and gamma is
        cancelled as well.
    One spot trade at the end clears the delta of everything.

    Buckets too close to expiry (< 1 day) are skipped and reported - their gamma can
    only be managed by delta re-hedging until they expire.
    """
    now = snapshot.time()

    # Group the book's positions by bucket
    groups = {}
    for row in book.positions(snapshot):
        label = bucket_of(row["T"])
        if label not in groups:
            groups[label] = {"rows": [], "gamma": 0.0, "vega": 0.0, "vanna": 0.0, "volga": 0.0,
                             "delta": 0.0, "vega_weight": 0.0, "expiry_x_weight": 0.0,
                             "shortest": row["expiry"], "longest": row["expiry"]}
        g = groups[label]
        g["rows"].append(row)
        for name in ["gamma", "vega", "vanna", "volga", "delta"]:
            g[name] += row[name]
        weight = abs(row["vega"])
        g["vega_weight"] += weight
        g["expiry_x_weight"] += weight * row["expiry"]
        if row["expiry"] < g["shortest"]:
            g["shortest"] = row["expiry"]
        if row["expiry"] > g["longest"]:
            g["longest"] = row["expiry"]

    buckets = []
    total_added_delta = 0.0
    total_premium = 0.0
    for label, edge in BUCKETS:
        if label not in groups:
            continue
        g = groups[label]
        before = {"gamma": g["gamma"], "vega": g["vega"], "vanna": g["vanna"], "volga": g["volga"]}

        if g["vega_weight"] > 0:
            hedge_expiry = g["expiry_x_weight"] / g["vega_weight"]
        else:
            hedge_expiry = g["rows"][0]["expiry"]

        item = {
            "bucket": label,
            "positions": len(g["rows"]),
            "hedge_expiry": hedge_expiry,                     # sim clock (years)
            "hedge_days_left": (hedge_expiry - now) * 365,    # for display
            "before": before,
        }

        if hedge_expiry - now < MIN_HEDGE_T:
            item["skipped"] = "less than a day to expiry - manage by delta re-hedging"
            item["after"] = before
            buckets.append(item)
            continue

        # The gamma straddle must sit at a clearly different expiry from the packages;
        # two instruments at (nearly) the same expiry make the 4x4 almost singular and
        # the solver answers with huge offsetting trades.
        gamma_expiry = None
        if hedge_expiry - g["shortest"] >= MIN_GAMMA_GAP and g["shortest"] - now >= MIN_HEDGE_T:
            gamma_expiry = g["shortest"]
        item["gamma_expiry"] = gamma_expiry
        if gamma_expiry is not None:
            item["gamma_days_left"] = (gamma_expiry - now) * 365

        try:
            packages, condition = solve_packages(snapshot, hedge_expiry, before, gamma_expiry)
        except ValueError as error:
            item["skipped"] = str(error)
            item["after"] = before
            buckets.append(item)
            continue

        summary, added = _package_summary(packages)
        item["packages"] = summary
        item["condition"] = condition
        item["after"] = {
            "gamma": before["gamma"] + added["gamma"],
            "vega": before["vega"] + added["vega"],
            "vanna": before["vanna"] + added["vanna"],
            "volga": before["volga"] + added["volga"],
        }
        total_added_delta += added["delta"]
        total_premium += added["price"]
        buckets.append(item)

    book_delta = book.net_delta(snapshot)
    return {
        "buckets": buckets,
        "spot_trade_usd": -(book_delta + total_added_delta),
        "premium_at_mid_cny": -total_premium,
    }