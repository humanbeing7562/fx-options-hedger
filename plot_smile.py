import sys
import math
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import matplotlib
matplotlib.use("Agg")            # save to file, no window needed
import matplotlib.pyplot as plt
import fxengine
from simulation.market_loader import MarketLoader

PILLAR_ORDER = ["10P", "25P", "ATM", "25C", "10C"]
PILLAR_DELTAS = {"10P": -0.10, "25P": -0.25, "25C": 0.25, "10C": 0.10}


def pillar_strikes(spot, quote):
    # Temporary glue: this loop moves inside the C++ SmileVol constructor later
    T = quote["T"]
    rf = quote["rf"]
    pricer = fxengine.GKPricer(quote["rd"], rf)

    strikes = {}
    for pillar in PILLAR_ORDER:
        flat = fxengine.FlatVol(quote["vols"][pillar])
        if pillar == "ATM":
            K = pricer.strike_from_delta(spot, T, flat, 0.5 * math.exp(-rf * T), True)
        else:
            delta = PILLAR_DELTAS[pillar]
            K = pricer.strike_from_delta(spot, T, flat, delta, delta > 0)
        strikes[pillar] = K
    return strikes


market = MarketLoader("data/clean")
timestamp = "2026-09-30 16:00"
spot = market.get_spot(timestamp)
quotes = market.get_pillar_quotes(timestamp)

fig, ax = plt.subplots(figsize=(9, 5))
for tenor in ["1M", "3M"]:
    q = quotes[tenor]
    strikes = pillar_strikes(spot, q)

    xs = []
    ys = []
    for pillar in PILLAR_ORDER:
        xs.append(strikes[pillar])
        ys.append(q["vols"][pillar] * 100)

    ax.plot(xs, ys, marker="o", label=f"{tenor} (F = {q['forward']:.4f})")
    for i in range(len(PILLAR_ORDER)):
        ax.annotate(PILLAR_ORDER[i], (xs[i], ys[i]), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=8)
    ax.axvline(q["forward"], linestyle=":", linewidth=1, color="grey")

ax.set_xlabel("Strike (CNY per USD)")
ax.set_ylabel("Implied vol (%)")
ax.set_title(f"USD/CNY smile pillars, {timestamp}")
ax.legend()
ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig("smile_2026-09-30_1600.png", dpi=130)
print("saved smile_2026-09-30_1600.png")