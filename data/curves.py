import math
import warnings
import pandas as pd

warnings.filterwarnings("ignore")   # openpyxl "no default style" noise

RAW_DIR = "raw"
OUT_DIR = "clean"

START = pd.Timestamp("2026-07-01")
END = pd.Timestamp("2026-09-30")

# Days per tenor, used for both the day-count conversion and tenor_years
TENOR_DAYS = {
    "O/N": 1, "1W": 7, "2W": 14, "3W": 21,
    "1M": 30, "1.5M": 45, "2M": 61, "3M": 91, "4M": 122,
    "6M": 182, "9M": 273, "1Y": 365,
}

VOL_TENORS = ["1W", "2W", "3W", "1M", "2M", "3M", "6M", "9M", "1Y"]

PILLAR_NAMES = {
    "10D PUT": "10P",
    "25D PUT": "25P",
    "ATM": "ATM",
    "25D CALL": "25C",
    "10D CALL": "10C",
}


def to_continuous(rate_pct, days, basis):
    rate = rate_pct / 100.0
    year_frac = days / 365.0
    return math.log(1 + rate * days / basis) / year_frac


def in_window(date):
    return START <= date <= END


def load_shibor():
    raw = pd.read_excel(f"{RAW_DIR}/SHIBOR_rates.xlsx")
    tenors = ["O/N", "1W", "2W", "1M", "3M", "6M", "9M", "1Y"]
    rows = []

    for i in range(len(raw)):
        date = pd.to_datetime(raw["Date"].iloc[i], format="%d %b %Y", errors="coerce")
        if pd.isna(date) or not in_window(date):
            continue   # skips the footer rows too

        for tenor in tenors:
            days = TENOR_DAYS[tenor]
            rate = to_continuous(float(raw[tenor].iloc[i]), days, 360)
            rows.append({
                "date": date, "currency": "CNY", "tenor": tenor,
                "tenor_years": days / 365.0, "rate": rate,
            })
    return rows


def load_sofr():
    raw = pd.read_excel(f"{RAW_DIR}/SOFR_rates.xlsx")
    sofr_by_date = {}

    for i in range(len(raw)):
        if raw["Rate Type"].iloc[i] != "SOFR":
            continue
        date = pd.to_datetime(raw["Effective Date"].iloc[i], format="%m/%d/%Y")
        sofr_by_date[date] = to_continuous(float(raw["Rate (%)"].iloc[i]), 1, 360)
    return sofr_by_date


def load_usd():
    raw = pd.read_csv(f"{RAW_DIR}/daily-treasury-rates.csv")
    columns = {"1 Mo": "1M", "1.5 Month": "1.5M", "2 Mo": "2M", "3 Mo": "3M",
               "4 Mo": "4M", "6 Mo": "6M", "1 Yr": "1Y"}
    sofr_by_date = load_sofr()
    sofr_dates = sorted(sofr_by_date.keys())
    rows = []

    for i in range(len(raw)):
        date = pd.to_datetime(raw["Date"].iloc[i], format="%m/%d/%Y")
        if not in_window(date):
            continue

        # O/N point from SOFR, using the latest fixing on or before this date
        last_sofr = None
        for d in sofr_dates:
            if d <= date:
                last_sofr = sofr_by_date[d]
        if last_sofr is not None:
            rows.append({
                "date": date, "currency": "USD", "tenor": "O/N",
                "tenor_years": 1 / 365.0, "rate": last_sofr,
            })

        for col in columns:
            tenor = columns[col]
            days = TENOR_DAYS[tenor]
            quote = float(raw[col].iloc[i])
            if tenor == "1Y":
                rate = 2 * math.log(1 + quote / 200.0)    # semiannual basis
            else:
                rate = to_continuous(quote, days, 365)    # simple ACT/365
            rows.append({
                "date": date, "currency": "USD", "tenor": tenor,
                "tenor_years": days / 365.0, "rate": rate,
            })
    return rows


def load_vols():
    rows = []

    for month in ["jul26", "aug26", "sep26"]:
        raw = pd.read_excel(f"{RAW_DIR}/FX_Option_Implied_Volatility_Curve_Historical_Data_{month}.xlsx")

        for i in range(len(raw)):
            if raw["Currency Pair"].iloc[i] != "USD.CNY":
                continue   # footer rows
            vol_type = raw["Volatility Type"].iloc[i]
            tenor = raw["Tenor"].iloc[i]
            if vol_type not in PILLAR_NAMES or tenor not in VOL_TENORS:
                continue   # drops RR/BF (derivable), 1D, 18M+

            stamp = raw["Date"].iloc[i] + " " + raw["Time"].iloc[i]
            timestamp = pd.to_datetime(stamp, format="%d %b %Y %H:%M")
            bid = float(raw["Volatility Bid(%)"].iloc[i]) / 100.0
            ask = float(raw["Volatility Ask(%)"].iloc[i]) / 100.0
            if bid > ask:
                bid, ask = ask, bid    # CFETS put quotes sometimes come through crossed
            rows.append({
                "timestamp": timestamp,
                "tenor": tenor,
                "tenor_years": TENOR_DAYS[tenor] / 365.0,
                "pillar": PILLAR_NAMES[vol_type],
                "vol": float(raw["Volatility(%)"].iloc[i]) / 100.0,
                "bid": bid,
                "ask": ask,
                "one_sided": bid == ask,   # CFETS often publishes mid only
            })
    return rows


def fill_missing_curves(curve_rows, vol_dates):
    # Every day that has vols must have both curves.
    # If a currency is missing that day (e.g. US holiday), copy its latest earlier curve.
    for currency in ["CNY", "USD"]:
        rows_by_date = {}
        for row in curve_rows:
            if row["currency"] == currency:
                if row["date"] not in rows_by_date:
                    rows_by_date[row["date"]] = []
                rows_by_date[row["date"]].append(row)
        available = sorted(rows_by_date.keys())

        for vol_date in vol_dates:
            if vol_date in rows_by_date:
                continue
            previous = None
            for d in available:
                if d < vol_date:
                    previous = d
            if previous is None:
                print(f"WARNING: no {currency} curve on or before {vol_date.date()}")
                continue
            for row in rows_by_date[previous]:
                copied = dict(row)
                copied["date"] = vol_date
                curve_rows.append(copied)
            print(f"filled {currency} curve for {vol_date.date()} using {previous.date()}")
    return curve_rows


def check_outputs(curves, vols):
    problems = []

    # Each snapshot should have 9 tenors x 5 pillars
    counts = vols.groupby("timestamp").size()
    for t in counts.index:
        if counts[t] != 45:
            problems.append(f"snapshot {t} has {counts[t]} vol rows, expected 45")

    # Vols in a sensible range, bid <= ask
    if vols["vol"].min() <= 0 or vols["vol"].max() > 0.20:
        problems.append("vol outside 0-20%")
    if (vols["bid"] > vols["ask"]).sum() > 0:
        problems.append("crossed bid/ask still present")

    # Every vol day has a full CNY (8) and USD (8) curve
    vol_dates = pd.to_datetime(vols["timestamp"]).dt.normalize().unique()
    for d in vol_dates:
        for currency in ["CNY", "USD"]:
            n = len(curves[(curves["date"] == d) & (curves["currency"] == currency)])
            if n != 8:
                problems.append(f"{currency} curve on {pd.Timestamp(d).date()} has {n} points, expected 8")

    # Rates in a sensible range
    if curves["rate"].min() < 0 or curves["rate"].max() > 0.10:
        problems.append("rate outside 0-10%")

    # No duplicates
    if curves.duplicated(["date", "currency", "tenor"]).sum() > 0:
        problems.append("duplicate curve rows")
    if vols.duplicated(["timestamp", "tenor", "pillar"]).sum() > 0:
        problems.append("duplicate vol rows")

    if len(problems) == 0:
        print("CHECKS PASSED")
    else:
        print("CHECKS FAILED:")
        for p in problems:
            print("  ", p)


if __name__ == "__main__":
    vols = pd.DataFrame(load_vols()).sort_values(["timestamp", "tenor_years", "pillar"])
    vol_dates = sorted(pd.to_datetime(vols["timestamp"]).dt.normalize().unique())
    vol_dates = [pd.Timestamp(d) for d in vol_dates]

    curve_rows = load_shibor() + load_usd()
    curve_rows = fill_missing_curves(curve_rows, vol_dates)
    curves = pd.DataFrame(curve_rows).sort_values(["date", "currency", "tenor_years"])

    curves.to_csv(f"{OUT_DIR}/curves.csv", index=False)
    vols.to_csv(f"{OUT_DIR}/vols.csv", index=False)

    print(f"curves: {len(curves)} rows, {curves['date'].nunique()} dates")
    print(f"vols:   {len(vols)} rows, {vols['timestamp'].nunique()} snapshots")
    check_outputs(curves, vols)