#include "yield_curve.hpp"
#include <stdexcept>

InterpolatedCurve::InterpolatedCurve(const std::vector<double>& tenors,
                                     const std::vector<double>& rates)
    : tenors_(tenors), rates_(rates) {

    if (tenors_.size() == 0) {
        throw std::invalid_argument("curve needs at least one point");
    }
    if (tenors_.size() != rates_.size()) {
        throw std::invalid_argument("tenors and rates must be the same length");
    }
    for (size_t i = 1; i < tenors_.size(); i++) {
        if (tenors_[i] <= tenors_[i - 1]) {
            throw std::invalid_argument("tenors must be sorted and unique");
        }
    }
}

double InterpolatedCurve::zero_rate(double T) const {
    size_t last = tenors_.size() - 1;

    // Before the first tenor or after the last: hold the end rate flat
    if (T <= tenors_[0]) {
        return rates_[0];
    }
    if (T >= tenors_[last]) {
        return rates_[last];
    }

    // Find the two tenors either side of T.
    // Interpolate r*T (= -log of the discount factor) in a straight line,
    // which means the forward rate is flat between the two tenors.
    for (size_t i = 0; i < last; i++) {
        double t_left = tenors_[i];
        double t_right = tenors_[i + 1];

        if (T <= t_right) {
            double rt_left = rates_[i] * t_left;
            double rt_right = rates_[i + 1] * t_right;
            double weight = (T - t_left) / (t_right - t_left);
            double rt = rt_left + weight * (rt_right - rt_left);
            return rt / T;
        }
    }

    return rates_[last];   // not reached, keeps the compiler happy
}