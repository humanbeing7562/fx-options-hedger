#pragma once
#include <vector>
#include <cmath>

// Base class: anything that can give a continuous zero rate for maturity T (years)
class YieldCurve {
public:
    virtual ~YieldCurve() = default;
    virtual double zero_rate(double T) const = 0;

    double discount(double T) const {
        return std::exp(-zero_rate(T) * T);
    }
};

// Same rate for every maturity
class FlatCurve : public YieldCurve {
public:
    FlatCurve(double rate) : rate_(rate) {}
    double zero_rate(double T) const override { return rate_; }

private:
    double rate_;
};

// Rates at quoted tenors, log linear in between, flat beyond the ends
class InterpolatedCurve : public YieldCurve {
public:
    InterpolatedCurve(const std::vector<double>& tenors,
                      const std::vector<double>& rates);
    double zero_rate(double T) const override;

private:
    std::vector<double> tenors_;   // years, sorted ascending
    std::vector<double> rates_;    // continuous, decimal
};