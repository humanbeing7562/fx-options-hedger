import pandas as pd
import yfinance as yf
 
VOLS_FILE = "clean/vols.csv"
OUT_FILE = "clean/spot.csv"
 
 
def download_spot():
    ticker = yf.Ticker("CNY=X")
    bars = ticker.history(start="2026-07-01", end="2026-10-01", interval="1h")
    if len(bars) == 0:
        raise RuntimeError("Yahoo returned no data - check the ticker or date range")
 
    # Yahoo gives UTC (or exchange time), convert so it matches the vol timestamps
    bars.index = bars.index.tz_convert("Asia/Shanghai").tz_localize(None)
    return bars
 
 
def get_snapshot_times():
    vols = pd.read_csv(VOLS_FILE, parse_dates=["timestamp"])
    times = vols["timestamp"].unique()
    return sorted(pd.to_datetime(times))
 
 
def match_spot(bars, snapshot_times):
    rows = []
    missing = []
 
    for t in snapshot_times:
        if t in bars.index:
            # The bar starting at the fixing time opens at (roughly) the fixing price
            spot = float(bars.loc[t, "Open"])
            rows.append({"timestamp": t, "spot": spot, "source": "exact"})
            continue
 
        # Fall back to the latest close earlier on the same day
        same_day_start = t.normalize()
        earlier = bars[(bars.index < t) & (bars.index >= same_day_start)]
        if len(earlier) > 0:
            spot = float(earlier["Close"].iloc[-1])
            rows.append({"timestamp": t, "spot": spot, "source": "filled"})
        else:
            missing.append(t)
 
    return rows, missing
 
 
if __name__ == "__main__":
    bars = download_spot()
    snapshot_times = get_snapshot_times()
    rows, missing = match_spot(bars, snapshot_times)
 
    spot = pd.DataFrame(rows)
    spot.to_csv(OUT_FILE, index=False)
 
    exact = 0
    filled = 0
    for i in range(len(spot)):
        if spot["source"].iloc[i] == "exact":
            exact += 1
        else:
            filled += 1
 
    print(f"snapshots: {len(snapshot_times)}")
    print(f"exact:     {exact}")
    print(f"filled:    {filled}")
    print(f"missing:   {len(missing)}")
    print(f"spot range: {spot['spot'].min():.4f} - {spot['spot'].max():.4f}")
 
    if len(missing) > 0:
        print("\nNo spot at all for:")
        for t in missing:
            print("  ", t)