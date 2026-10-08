#pragma once
#include "yield_curve.hpp"
#include "vol_provider.hpp"
#include "pricer.hpp"

class MarketSnapshot {
public:
    MarketSnapshot(double time, double spot,
                   const YieldCurve& domestic,     // CNY
                   const YieldCurve& foreign,      // USD
                   const VolProvider& vol);

    double time() const { return time_; }
    double spot() const { return spot_; }

    // Market data at maturity T (years from now)
    double domestic_rate(double T) const;
    double foreign_rate(double T) const;
    double forward(double T) const;
    double vol(double strike, double T) const;

    // Garman-Kohlhagen with the rates for this T and the smile vol for this strike
    PricingResult price(double strike, double T, bool is_call) const;

    // Strike with this spot delta at maturity T, searched on this snapshot's smile
    // (e.g. 0.25 call, -0.25 put). Works for any T, not just the quoted tenors.
    double strike_from_delta(double delta, double T, bool is_call) const;

    // ATM (delta-neutral straddle) strike at maturity T, on this snapshot's smile
    double atm_strike(double T) const;

private:
    double time_;
    double spot_;
    const YieldCurve* domestic_;
    const YieldCurve* foreign_;
    const VolProvider* vol_;
};