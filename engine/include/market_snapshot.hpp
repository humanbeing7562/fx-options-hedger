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

private:
    double time_;
    double spot_;
    const YieldCurve* domestic_;
    const YieldCurve* foreign_;
    const VolProvider* vol_;
};