import sys
import math
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)
import fxengine

# 30 Sep 2026 curves (from clean/curves.csv)
cny = fxengine.InterpolatedCurve(
    [1/365, 7/365, 14/365, 30/365, 91/365, 182/365, 273/365, 1.0],
    [0.013809, 0.013899, 0.014170, 0.014399, 0.014472, 0.014648, 0.014822, 0.014894],
)
usd = fxengine.InterpolatedCurve(
    [1/365, 30/365, 45/365, 61/365, 91/365, 122/365, 182/365, 1.0],
    [0.039540, 0.040134, 0.041195, 0.041456, 0.041782, 0.042595, 0.042839, 0.044892],
)

spot = 6.7037       # 30 Sep 16:00
T = 91 / 365        # 3M
vol = 0.022         # 3M ATM vol on 30 Sep

# USD/CNY: CNY is domestic, USD is foreign
rd = cny.zero_rate(T)
rf = usd.zero_rate(T)
pricer = fxengine.GKPricer(rd, rf)

forward = spot * usd.discount(T) / cny.discount(T)
print(f"rates: CNY {rd:.6f}, USD {rf:.6f}")
print(f"3M forward: {forward:.5f}")
print()

# Price a call and a put struck at the forward
call = pricer.price(spot, forward, T, vol, True)
put = pricer.price(spot, forward, T, vol, False)
print(f"call: price {call.price:.6f}  delta {call.delta:+.4f}  vega {call.vega:.4f}")
print(f"put:  price {put.price:.6f}  delta {put.delta:+.4f}  vega {put.vega:.4f}")
print()

# Check 1: struck at the forward, call and put cost the same
print(f"check 1 - call minus put at the forward: {call.price - put.price:.2e}  (expect ~0)")

# Check 2: put-call parity at another strike
K = 6.60
call = pricer.price(spot, K, T, vol, True)
put = pricer.price(spot, K, T, vol, False)
lhs = call.price - put.price
rhs = spot * math.exp(-rf * T) - K * math.exp(-rd * T)
print(f"check 2 - parity at K={K}: C-P {lhs:.6f}  vs  S*DFf - K*DFd {rhs:.6f}")

# Check 3: the pricer's discounting matches the curve's discounting
print(f"check 3 - CNY discount: curve {cny.discount(T):.8f}  vs  exp(-rd*T) {math.exp(-rd * T):.8f}")