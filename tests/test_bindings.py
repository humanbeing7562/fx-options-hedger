import sys
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)
import fxengine

cny = fxengine.InterpolatedCurve(
    [1/365, 7/365, 14/365, 30/365, 91/365, 182/365, 273/365, 1.0],
    [0.013809, 0.013899, 0.014170, 0.014399, 0.014472, 0.014648, 0.014822, 0.014894],
)
print("CNY 3M:", cny.zero_rate(91/365), " expect 0.014472")
print("CNY 2M:", cny.zero_rate(61/365), " expect ~0.0144543")

try:
    fxengine.InterpolatedCurve([0.5, 0.25], [0.01, 0.02])
    print("FAIL: unsorted tenors accepted")
except ValueError as e:
    print("Unsorted tenors rejected:", e)