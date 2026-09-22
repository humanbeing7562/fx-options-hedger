#include "pricer.hpp"

static double norm_cdf(double x) {
    return 0.5 * std::erfc(-x / std::sqrt(2.0));
}

static double norm_pdf(double x) {
    return std::exp(-0.5 * x * x) / std::sqrt(2.0 * M_PI);
}

BSPricer::BSPricer(double rate) : rate_(rate) {}

double BSPricer::d1(double spot, double strike, double T, double vol) const {
    return (std::log(spot / strike) + (rate_ + 0.5 * vol * vol) * T) 
           / (vol * std::sqrt(T));
}

double BSPricer::d2(double spot, double strike, double T, double vol) const {
    return d1(spot, strike, T, vol) - vol * std::sqrt(T);
}

PricingResult BSPricer::price(double spot, double strike, double T,
                               double vol, bool is_call) const {
    double d1_val = d1(spot, strike, T, vol);
    double d2_val = d2(spot, strike, T, vol);
    double df = std::exp(-rate_ * T);
    double sqrt_T = std::sqrt(T);

    PricingResult result;

    if (is_call) {
        result.price = spot * norm_cdf(d1_val) - strike * df * norm_cdf(d2_val);
        result.delta = norm_cdf(d1_val);
    } else {
        result.price = strike * df * norm_cdf(-d2_val) - spot * norm_cdf(-d1_val);
        result.delta = norm_cdf(d1_val) - 1.0;
    }

    result.gamma = norm_pdf(d1_val) / (spot * vol * sqrt_T);
    result.vega  = spot * norm_pdf(d1_val) * sqrt_T;
    result.rho   = is_call ?  strike * T * df * norm_cdf(d2_val)
                           : -strike * T * df * norm_cdf(-d2_val);
    result.theta = -(spot * norm_pdf(d1_val) * vol) / (2.0 * sqrt_T)
                   - (is_call ? 1.0 : -1.0) * rate_ * strike * df 
                     * norm_cdf(is_call ? d2_val : -d2_val);

    return result;
}

GKPricer::GKPricer(double domestic_rate, double foreign_rate) : domestic_rate_(domestic_rate), foreign_rate_(foreign_rate) {}

double GKPricer::d1(double spot, double strike, double T, double vol) const {
    return (std::log(spot / strike) + (domestic_rate_ - foreign_rate_ + 0.5 * vol * vol) * T) 
           / (vol * std::sqrt(T));
}

double GKPricer::d2(double spot, double strike, double T, double vol) const {
    return d1(spot, strike, T, vol) - vol * std::sqrt(T);
}

PricingResult GKPricer::price(double spot, double strike, double T,
                               double vol, bool is_call) const {
    double d1_val = d1(spot, strike, T, vol);
    double d2_val = d2(spot, strike, T, vol);
    double d_foreign = std::exp(-foreign_rate_ * T);
    double d_domestic = std::exp(-domestic_rate_ * T);
    double sqrt_T = std::sqrt(T);

    PricingResult result;

    if (is_call) {
        result.price = spot * norm_cdf(d1_val) * d_foreign - strike * d_domestic * norm_cdf(d2_val);
        result.delta = d_foreign * norm_cdf(d1_val);
    } else {
        result.price = strike * d_domestic * norm_cdf(-d2_val) - spot * d_foreign * norm_cdf(-d1_val);
        result.delta = d_foreign * (norm_cdf(d1_val) - 1.0);
    }

    result.gamma = d_foreign * norm_pdf(d1_val) / (spot * vol * sqrt_T);
    result.vega  = spot * d_foreign * norm_pdf(d1_val) * sqrt_T;
    result.rho   = is_call ?  strike * T * d_domestic * norm_cdf(d2_val)
                           : -strike * T * d_domestic * norm_cdf(-d2_val);
    result.theta = -(spot * d_foreign * norm_pdf(d1_val) * vol) / (2.0 * sqrt_T)
               + foreign_rate_ * spot * d_foreign * norm_cdf(is_call ? d1_val : -d1_val) * (is_call ? 1.0 : -1.0)
               - domestic_rate_ * strike * d_domestic * norm_cdf(is_call ? d2_val : -d2_val) * (is_call ? 1.0 : -1.0);

    return result;
}