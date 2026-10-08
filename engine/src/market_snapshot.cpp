#include "market_snapshot.hpp"
#include <stdexcept>

MarketSnapshot::MarketSnapshot(double time, double spot,
                               const YieldCurve& domestic,
                               const YieldCurve& foreign,
                               const VolProvider& vol)
    : time_(time), spot_(spot), domestic_(&domestic), foreign_(&foreign), vol_(&vol) {

    if (spot_ <= 0) {
        throw std::invalid_argument("spot must be positive");
    }
}

double MarketSnapshot::domestic_rate(double T) const {
    return domestic_->zero_rate(T);
}

double MarketSnapshot::foreign_rate(double T) const {
    return foreign_->zero_rate(T);
}

double MarketSnapshot::forward(double T) const {
    if (T < 0) {
        throw std::invalid_argument("T must not be negative");
    }
    return spot_ * foreign_->discount(T) / domestic_->discount(T);
}

double MarketSnapshot::vol(double strike, double T) const {
    return vol_->get_vol(strike, T);
}

PricingResult MarketSnapshot::price(double strike, double T, bool is_call) const {
    if (T <= 0) {
        throw std::invalid_argument("T must be positive to price (option has expired)");
    }
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }

    // Each option gets the rates for its own maturity
    GKPricer pricer(domestic_rate(T), foreign_rate(T));
    return pricer.price(spot_, strike, T, vol(strike, T), is_call);
}