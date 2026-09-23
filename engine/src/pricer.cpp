#include "pricer.hpp"

static double norm_inv(double p) {
    // Acklam's rational approximation
    // Accurate to ~1.15e-9 across full range
    
    const double a1 = -3.969683028665376e+01;
    const double a2 =  2.209460984245205e+02;
    const double a3 = -2.759285104469687e+02;
    const double a4 =  1.383577518672690e+02;
    const double a5 = -3.066479806614716e+01;
    const double a6 =  2.506628277459239e+00;

    const double b1 = -5.447609879822406e+01;
    const double b2 =  1.615858368580409e+02;
    const double b3 = -1.556989798598866e+02;
    const double b4 =  6.680131188771972e+01;
    const double b5 = -1.328068155288572e+01;

    const double c1 = -7.784894002430293e-03;
    const double c2 = -3.223964580411365e-01;
    const double c3 = -2.400758277161838e+00;
    const double c4 = -2.549732539343734e+00;
    const double c5 =  4.374664141464968e+00;
    const double c6 =  2.938163982698783e+00;

    const double d1 =  7.784695709041462e-03;
    const double d2 =  3.224671290700398e-01;
    const double d3 =  2.445134137142996e+00;
    const double d4 =  3.754408661907416e+00;

    const double p_low  = 0.02425;
    const double p_high = 1.0 - p_low;

    double q, r;

    if (p < p_low) {
        q = std::sqrt(-2.0 * std::log(p));
        return (((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q+c6) /
                ((((d1*q+d2)*q+d3)*q+d4)*q+1.0);
    } else if (p <= p_high) {
        q = p - 0.5;
        r = q * q;
        return (((((a1*r+a2)*r+a3)*r+a4)*r+a5)*r+a6)*q /
               (((((b1*r+b2)*r+b3)*r+b4)*r+b5)*r+1.0);
    } else {
        q = std::sqrt(-2.0 * std::log(1.0 - p));
        return -(((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q+c6) /
                 ((((d1*q+d2)*q+d3)*q+d4)*q+1.0);
    }
}

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

double GKPricer::strike_from_delta(double spot, double T, 
                         const VolProvider& vol_provider,
                         double target_delta, bool is_call,
                         int max_iter, double tol) const {

    // acklam's to search strike from ATM vol + spot price (1st iteration)
    double vol_atm = vol_provider.get_vol(spot, T);
    double d1 = norm_inv(target_delta * std::exp(foreign_rate_ * T));
    double K = spot * std::exp(-(d1 * vol_atm * std::sqrt(T) 
               - (domestic_rate_ - foreign_rate_ + 0.5 * vol_atm * vol_atm) * T)); 
    
    // newton-raphson on top
    for (int i = 0; i < max_iter; i++) {
        double vol = vol_provider.get_vol(K, T);  // vol for current guess
        PricingResult r = price(spot, K, T, vol, is_call);
        double error = r.delta - target_delta;
        
        if (std::abs(error) < tol) break;
        
        K = K - error / (-r.gamma);
    }
    return K;
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