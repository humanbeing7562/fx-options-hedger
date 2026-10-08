#pragma once
#include <cmath>
#include "vol_provider.hpp"

struct PricingResult {
    double price = 0.0;
    double delta = 0.0;
    double gamma = 0.0;
    double theta = 0.0;
    double vega = 0.0;
    double rho = 0.0;
    double vanna = 0.0;     // d(delta)/d(vol) = d(vega)/d(spot)
    double volga = 0.0;     // d(vega)/d(vol)
};

class Pricer {
public:
    virtual ~Pricer() = default;
    virtual PricingResult price(double spot, double strike, double T,
                                double vol, bool is_call) const = 0;
};

class BSPricer : public Pricer {
public:
    BSPricer(double rate);
    PricingResult price(double spot, double strike, double T,
                        double vol, bool is_call) const override;
private:
    double d1(double spot, double strike, double T, double vol) const;
    double d2(double spot, double strike, double T, double vol) const;
    double rate_;
};

class GKPricer : public Pricer {
public:
    GKPricer(double domestic_rate, double foreign_rate);
    PricingResult price(double spot, double strike, double T,
                        double vol, bool is_call) const override;
    double strike_from_delta(double spot, double T,
                             const VolProvider& vol_provider,
                             double target_delta, bool is_call,
                             int max_iter = 50, double tol = 1e-10) const;
private:
    double d1(double spot, double strike, double T, double vol) const;
    double d2(double spot, double strike, double T, double vol) const;
    double domestic_rate_;
    double foreign_rate_;
};