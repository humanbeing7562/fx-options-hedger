#include "smile_vol.hpp"
#include "pricer.hpp"
#include <ql/termstructures/volatility/sabr.hpp>
#include <ql/math/interpolations/sabrinterpolation.hpp>
#include <ql/math/distributions/normaldistribution.hpp>
#include <algorithm>
#include <cmath>
#include <stdexcept>

// ======================================================================
// VannaVolgaSmile
// ======================================================================

VannaVolgaSmile::VannaVolgaSmile(double k_25p, double k_atm, double k_25c,
                                 double vol_25p, double vol_atm, double vol_25c,
                                 double k_min, double k_max, double T)
    : k1_(k_25p), k2_(k_atm), k3_(k_25c),
      v1_(vol_25p), v2_(vol_atm), v3_(vol_25c),
      k_min_(k_min), k_max_(k_max) {

    if (!(k1_ < k2_ && k2_ < k3_)) {
        throw std::invalid_argument("vanna-volga strikes must be ordered 25P < ATM < 25C");
    }
    if (v1_ <= 0 || v2_ <= 0 || v3_ <= 0) {
        throw std::invalid_argument("vanna-volga vols must be positive");
    }
    if (!(k_min_ <= k1_ && k3_ <= k_max_)) {
        throw std::invalid_argument("wing limits must lie outside the 25P and 25C strikes");
    }

    bool has_low = k_min_ > 0.0;
    bool has_high = k_max_ < std::numeric_limits<double>::infinity();
    if ((has_low || has_high) && T <= 0) {
        throw std::invalid_argument("T must be positive when wing limits are set");
    }

    // Slope of the VV smile in log-strike at each join (central difference)
    double h = 1e-6;
    if (has_low) {
        vol_low_ = raw_vol(k_min_);
        double up = raw_vol(k_min_ * std::exp(h));
        double down = raw_vol(k_min_ * std::exp(-h));
        slope_low_ = -(up - down) / (2.0 * h);           // outward = towards lower strikes
        length_low_ = WING_LENGTH_SD * vol_low_ * std::sqrt(T);
    }
    if (has_high) {
        vol_high_ = raw_vol(k_max_);
        double up = raw_vol(k_max_ * std::exp(h));
        double down = raw_vol(k_max_ * std::exp(-h));
        slope_high_ = (up - down) / (2.0 * h);           // outward = towards higher strikes
        length_high_ = WING_LENGTH_SD * vol_high_ * std::sqrt(T);
    }
}

double VannaVolgaSmile::vol(double strike) const {
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }

    double floor = 0.001;
    if (strike < k_min_) {
        double u = std::log(k_min_ / strike);
        double result = vol_low_ + slope_low_ * length_low_ * (1.0 - std::exp(-u / length_low_));
        if (result < floor) {
            result = floor;
        }
        return result;
    }
    if (strike > k_max_) {
        double u = std::log(strike / k_max_);
        double result = vol_high_ + slope_high_ * length_high_ * (1.0 - std::exp(-u / length_high_));
        if (result < floor) {
            result = floor;
        }
        return result;
    }
    return raw_vol(strike);
}

double VannaVolgaSmile::raw_vol(double strike) const {

    // First-order vanna-volga (Castagna-Mercurio).
    // Each weight is 1 at its own pillar and 0 at the other two,
    // so the curve passes exactly through all three dots.
    double ln_k_k1 = std::log(strike / k1_);
    double ln_k_k2 = std::log(strike / k2_);
    double ln_k3_k = std::log(k3_ / strike);
    double ln_k2_k = std::log(k2_ / strike);

    double ln_k2_k1 = std::log(k2_ / k1_);
    double ln_k3_k1 = std::log(k3_ / k1_);
    double ln_k3_k2 = std::log(k3_ / k2_);

    double w1 = (ln_k2_k * ln_k3_k) / (ln_k2_k1 * ln_k3_k1);
    double w2 = (ln_k_k1 * ln_k3_k) / (ln_k2_k1 * ln_k3_k2);
    double w3 = (ln_k_k1 * ln_k_k2) / (ln_k3_k1 * ln_k3_k2);

    double result = w1 * v1_ + w2 * v2_ + w3 * v3_;

    // Far in the wings a downward-curving smile could dip towards zero;
    // keep a small floor so pricing never sees a zero or negative vol.
    double floor = 0.001;
    if (result < floor) {
        result = floor;
    }
    return result;
}

// ======================================================================
// SabrSmile
// ======================================================================

SabrSmile::SabrSmile(double forward, double T,
                     double alpha, double beta, double rho, double nu)
    : forward_(forward), T_(T), alpha_(alpha), beta_(beta), rho_(rho), nu_(nu) {

    if (forward_ <= 0) {
        throw std::invalid_argument("SABR forward must be positive");
    }
    if (T_ <= 0) {
        throw std::invalid_argument("SABR T must be positive");
    }
    if (alpha_ <= 0) {
        throw std::invalid_argument("SABR alpha must be positive");
    }
    if (beta_ < 0 || beta_ > 1) {
        throw std::invalid_argument("SABR beta must be between 0 and 1");
    }
    if (rho_ <= -1 || rho_ >= 1) {
        throw std::invalid_argument("SABR rho must be strictly between -1 and 1");
    }
    if (nu_ < 0) {
        throw std::invalid_argument("SABR nu must not be negative");
    }
}

double SabrSmile::vol(double strike) const {
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }

    // QuantLib's Hagan (2002) lognormal SABR.
    // Careful: QuantLib takes nu BEFORE rho.
    double result = QuantLib::sabrVolatility(strike, forward_, T_,
                                             alpha_, beta_, nu_, rho_);

    double floor = 0.001;
    if (result < floor) {
        result = floor;
    }
    return result;
}

// ======================================================================
// HybridSmile
// ======================================================================

double hybrid_sabr_weight(double strike, double k_end_p, double k_25p, double k_25c, double k_end_c) {
    if (strike >= k_25p && strike <= k_25c) {
        return 0.0;
    }
    if (strike <= k_end_p || strike >= k_end_c) {
        return 1.0;
    }
    if (strike < k_25p) {
        return std::log(k_25p / strike) / std::log(k_25p / k_end_p);
    }
    return std::log(strike / k_25c) / std::log(k_end_c / k_25c);
}

// Slope of a smile in log-strike, by central difference
static double log_slope(const Smile& smile, double strike) {
    double h = 1e-6;
    double up = smile.vol(strike * std::exp(h));
    double down = smile.vol(strike * std::exp(-h));
    return (up - down) / (2.0 * h);
}

double HybridSmile::Bridge::vol(double x) const {
    // Cubic Hermite on t in [0, 1]; slopes are per unit log-strike, so scale by the length
    double length = x1 - x0;
    double t = (x - x0) / length;
    double t2 = t * t;
    double t3 = t2 * t;
    double h00 = 2.0 * t3 - 3.0 * t2 + 1.0;
    double h10 = t3 - 2.0 * t2 + t;
    double h01 = -2.0 * t3 + 3.0 * t2;
    double h11 = t3 - t2;
    return h00 * v0 + h10 * length * slope0 + h01 * v1 + h11 * length * slope1;
}

void HybridSmile::Bridge::derivatives(double x, double& v, double& dv, double& d2v) const {
    double length = x1 - x0;
    double t = (x - x0) / length;
    double t2 = t * t;
    double t3 = t2 * t;

    // Hermite basis and its derivatives in t
    double h00 = 2.0 * t3 - 3.0 * t2 + 1.0;
    double h10 = t3 - 2.0 * t2 + t;
    double h01 = -2.0 * t3 + 3.0 * t2;
    double h11 = t3 - t2;
    double h00_t = 6.0 * t2 - 6.0 * t;
    double h10_t = 3.0 * t2 - 4.0 * t + 1.0;
    double h01_t = -6.0 * t2 + 6.0 * t;
    double h11_t = 3.0 * t2 - 2.0 * t;
    double h00_tt = 12.0 * t - 6.0;
    double h10_tt = 6.0 * t - 4.0;
    double h01_tt = -12.0 * t + 6.0;
    double h11_tt = 6.0 * t - 2.0;

    double m0 = length * slope0;
    double m1 = length * slope1;
    v = h00 * v0 + h10 * m0 + h01 * v1 + h11 * m1;
    dv = (h00_t * v0 + h10_t * m0 + h01_t * v1 + h11_t * m1) / length;
    d2v = (h00_tt * v0 + h10_tt * m0 + h01_tt * v1 + h11_tt * m1) / (length * length);
}

bool HybridSmile::density_ok(const Bridge& bridge) const {
    // In log-moneyness y = ln(K / F) with total variance w = vol^2 * T:
    //   g(y) = (1 - y w' / (2w))^2 - (w'^2 / 4)(1/w + 1/4) + w'' / 2
    // The density is >= 0 exactly where g >= 0 (Gatheral, "The Volatility Surface", ch. 2).
    // vol'' of the cubic is a straight line in t, so its most extreme values are at the two
    // ends - both ends are always in the sample.
    double floor = 0.001;
    int n = 400;
    for (int j = 0; j <= n; j++) {
        double x = bridge.x0 + (bridge.x1 - bridge.x0) * j / n;
        double v, dv, d2v;
        bridge.derivatives(x, v, dv, d2v);
        if (v < floor) {
            return false;            // the floor would put a kink in the smile
        }
        double y = x - std::log(forward_);
        double w = v * v * T_;
        double w1 = 2.0 * v * dv * T_;
        double w2 = 2.0 * (dv * dv + v * d2v) * T_;
        double a = 1.0 - y * w1 / (2.0 * w);
        double g = a * a - 0.25 * w1 * w1 * (1.0 / w + 0.25) + 0.5 * w2;
        if (g < 0.0) {
            return false;
        }
    }
    return true;
}

double HybridSmile::side_vol(const Bridge& bridge, bool is_call, double strike) const {
    bool inside;
    bool past_end;
    if (is_call) {
        inside = strike <= k_25c_;
        past_end = strike >= bridge.k_end;
    } else {
        inside = strike >= k_25p_;
        past_end = strike <= bridge.k_end;
    }
    if (inside) {
        return vv_->vol(strike);
    }
    if (past_end) {
        return sabr_->vol(strike);
    }
    double result = bridge.vol(std::log(strike));
    double floor = 0.001;
    if (result < floor) {
        result = floor;
    }
    return result;
}

HybridSmile::Bridge HybridSmile::build_bridge(bool is_call, double k_25, double k_10) const {
    // Candidate end points: the quoted 10D strike first, then further out
    std::vector<double> deltas = {0.10, 0.08, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01};

    // Strikes for the deeper deltas: Black forward delta at the SABR vol of the 10D strike
    // (only used to space the candidates out - the 10D one is the quoted strike itself)
    double sd = sabr_->vol(k_10) * std::sqrt(T_);
    QuantLib::InverseCumulativeNormal inverse_normal;

    Bridge bridge;
    for (size_t i = 0; i < deltas.size(); i++) {
        double k_end = k_10;
        if (i > 0) {
            double z = inverse_normal(deltas[i]);
            if (is_call) {
                k_end = forward_ * std::exp(-z * sd + 0.5 * sd * sd);
            } else {
                k_end = forward_ * std::exp(z * sd + 0.5 * sd * sd);
            }
        }

        bridge.x0 = std::log(k_25);
        bridge.x1 = std::log(k_end);
        bridge.v0 = vv_->vol(k_25);
        bridge.v1 = sabr_->vol(k_end);
        bridge.slope0 = log_slope(*vv_, k_25);
        bridge.slope1 = log_slope(*sabr_, k_end);
        bridge.k_end = k_end;
        bridge.end_delta = deltas[i];

        bool convex = density_ok(bridge);
        bridge.convex = convex;
        if (convex) {
            return bridge;
        }
    }
    return bridge;    // the deepest candidate, flagged as not convex
}

HybridSmile::HybridSmile(std::shared_ptr<const Smile> vv, std::shared_ptr<const Smile> sabr,
                         double k_10p, double k_25p, double k_25c, double k_10c,
                         double forward, double T)
    : vv_(vv), sabr_(sabr), k_25p_(k_25p), k_25c_(k_25c), forward_(forward), T_(T) {

    if (!vv_ || !sabr_) {
        throw std::invalid_argument("hybrid smile needs both a VV and a SABR smile");
    }
    if (!(k_10p < k_25p && k_25p < k_25c && k_25c < k_10c)) {
        throw std::invalid_argument("hybrid strikes must be ordered 10P < 25P < 25C < 10C");
    }
    if (forward_ <= 0 || T_ <= 0) {
        throw std::invalid_argument("hybrid smile needs a positive forward and T");
    }

    put_side_ = build_bridge(false, k_25p_, k_10p);
    call_side_ = build_bridge(true, k_25c_, k_10c);
}

double HybridSmile::vol(double strike) const {
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }
    if (strike < k_25p_) {
        return side_vol(put_side_, false, strike);
    }
    return side_vol(call_side_, true, strike);
}

// ======================================================================
// calibrate_sabr
// ======================================================================

SabrFit calibrate_sabr(const std::vector<double>& strikes,
                       const std::vector<double>& vols,
                       double forward, double T, double beta,
                       double start_alpha, double start_rho, double start_nu) {

    if (strikes.size() != vols.size() || strikes.size() < 3) {
        throw std::invalid_argument("need at least 3 strikes and the same number of vols");
    }
    if (forward <= 0 || T <= 0) {
        throw std::invalid_argument("forward and T must be positive");
    }

    // QuantLib keeps pointers into these, so they must stay alive while we use it
    std::vector<double> k = strikes;
    std::vector<double> v = vols;

    QuantLib::SABRInterpolation fit(k.begin(), k.end(), v.begin(),
                                    T, forward,
                                    start_alpha, beta, start_nu, start_rho,
                                    false,      // alpha is fitted
                                    true,       // beta is fixed
                                    false,      // nu is fitted
                                    false,      // rho is fitted
                                    true);      // vega-weighted errors
    fit.update();       // runs the optimisation

    SabrFit result;
    result.alpha = fit.alpha();
    result.beta = fit.beta();
    result.rho = fit.rho();
    result.nu = fit.nu();
    result.rms_error = fit.rmsError();
    result.max_error = fit.maxError();

    QuantLib::EndCriteria::Type how_it_ended = fit.endCriteria();
    result.converged = (how_it_ended != QuantLib::EndCriteria::MaxIterations
                        && how_it_ended != QuantLib::EndCriteria::None);
    return result;
}

// ======================================================================
// SmileVol
// ======================================================================

SmileVol::SmileVol(double spot,
                   const YieldCurve& domestic,
                   const YieldCurve& foreign,
                   const std::vector<TenorQuote>& quotes,
                   const std::string& model)
    : model_(model), spot_(spot) {

    if (model_ != "hybrid" && model_ != "vv" && model_ != "sabr") {
        throw std::invalid_argument("model must be \"hybrid\", \"vv\" or \"sabr\", got \"" + model_ + "\"");
    }
    if (quotes.size() == 0) {
        throw std::invalid_argument("SmileVol needs at least one tenor");
    }
    if (spot <= 0) {
        throw std::invalid_argument("spot must be positive");
    }

    for (size_t i = 0; i < quotes.size(); i++) {
        const TenorQuote& q = quotes[i];

        if (q.T <= 0) {
            throw std::invalid_argument("tenor T must be positive");
        }
        if (i > 0 && q.T <= quotes[i - 1].T) {
            throw std::invalid_argument("tenors must be sorted by T and unique");
        }

        double rd = domestic.zero_rate(q.T);
        double rf = foreign.zero_rate(q.T);

        TenorSmile s;
        s.T = q.T;
        s.forward = spot * foreign.discount(q.T) / domestic.discount(q.T);

        // Delta -> strike for each pillar, using that pillar's own quoted vol.
        // With a flat vol the first (closed-form) step is already exact.
        GKPricer pricer(rd, rf);

        s.k_10p = pricer.strike_from_delta(spot, q.T, FlatVol(q.vol_10p), -0.10, false);
        s.k_25p = pricer.strike_from_delta(spot, q.T, FlatVol(q.vol_25p), -0.25, false);
        s.k_25c = pricer.strike_from_delta(spot, q.T, FlatVol(q.vol_25c), 0.25, true);
        s.k_10c = pricer.strike_from_delta(spot, q.T, FlatVol(q.vol_10c), 0.10, true);

        // ATM = delta-neutral straddle: d1 = 0, so the call's spot delta is 0.5 * e^(-rf*T)
        double atm_delta = 0.5 * std::exp(-rf * q.T);
        s.k_atm = pricer.strike_from_delta(spot, q.T, FlatVol(q.vol_atm), atm_delta, true);

        s.vol_10p = q.vol_10p;
        s.vol_25p = q.vol_25p;
        s.vol_atm = q.vol_atm;
        s.vol_25c = q.vol_25c;
        s.vol_10c = q.vol_10c;

        // Smile model for this tenor
        std::shared_ptr<const Smile> vv_smile;
        std::shared_ptr<const Smile> sabr_smile;

        if (model_ == "vv" || model_ == "hybrid") {
            vv_smile = std::make_shared<VannaVolgaSmile>(s.k_25p, s.k_atm, s.k_25c,
                                                         s.vol_25p, s.vol_atm, s.vol_25c,
                                                         s.k_10p, s.k_10c, s.T);
        }
        if (model_ == "sabr" || model_ == "hybrid") {
            std::vector<double> strikes = {s.k_10p, s.k_25p, s.k_atm, s.k_25c, s.k_10c};
            std::vector<double> vols = {s.vol_10p, s.vol_25p, s.vol_atm, s.vol_25c, s.vol_10c};

            // Starting guess: alpha ~ ATM vol (beta = 1), rho from the risk reversal's sign
            double start_alpha = s.vol_atm;
            double start_rho = 0.0;
            if (s.vol_25c > s.vol_25p) {
                start_rho = 0.2;
            } else if (s.vol_25c < s.vol_25p) {
                start_rho = -0.2;
            }
            double start_nu = 1.0;

            s.sabr = calibrate_sabr(strikes, vols, s.forward, s.T, 1.0,
                                    start_alpha, start_rho, start_nu);
            sabr_smile = std::make_shared<SabrSmile>(s.forward, s.T, s.sabr.alpha,
                                                     s.sabr.beta, s.sabr.rho, s.sabr.nu);
        }

        if (model_ == "vv") {
            s.smile = vv_smile;
        } else if (model_ == "sabr") {
            s.smile = sabr_smile;
        } else {
            std::shared_ptr<HybridSmile> hybrid = std::make_shared<HybridSmile>(
                vv_smile, sabr_smile, s.k_10p, s.k_25p, s.k_25c, s.k_10c, s.forward, s.T);
            s.put_end_delta = hybrid->put_end_delta();
            s.call_end_delta = hybrid->call_end_delta();
            s.k_put_end = hybrid->put_end_strike();
            s.k_call_end = hybrid->call_end_strike();
            s.arbitrage_free = hybrid->arbitrage_free();
            s.smile = hybrid;
        }
        smiles_.push_back(s);
    }
}

double SmileVol::tenor_vol(size_t index, double strike) const {
    if (index >= smiles_.size()) {
        throw std::out_of_range("tenor index out of range");
    }
    return smiles_[index].smile->vol(strike);
}

double SmileVol::sabr_weight(size_t index, double strike) const {
    if (index >= smiles_.size()) {
        throw std::out_of_range("tenor index out of range");
    }
    if (model_ == "vv") {
        return 0.0;
    }
    if (model_ == "sabr") {
        return 1.0;
    }
    const TenorSmile& s = smiles_[index];
    return hybrid_sabr_weight(strike, s.k_put_end, s.k_25p, s.k_25c, s.k_call_end);
}

double SmileVol::forward(double T) const {
    if (T < 0) {
        throw std::invalid_argument("T must not be negative");
    }
    size_t last = smiles_.size() - 1;

    // Before the first tenor: between spot (T = 0) and the first forward
    if (T <= smiles_[0].T) {
        double a = std::log(spot_);
        double b = std::log(smiles_[0].forward);
        return std::exp(a + (b - a) * T / smiles_[0].T);
    }
    if (T >= smiles_[last].T) {
        if (last == 0) {
            return smiles_[0].forward;
        }
        // Continue the last segment's slope
        const TenorSmile& a = smiles_[last - 1];
        const TenorSmile& b = smiles_[last];
        double slope = (std::log(b.forward) - std::log(a.forward)) / (b.T - a.T);
        return std::exp(std::log(b.forward) + slope * (T - b.T));
    }
    for (size_t i = 0; i < last; i++) {
        const TenorSmile& a = smiles_[i];
        const TenorSmile& b = smiles_[i + 1];
        if (T <= b.T) {
            double weight = (T - a.T) / (b.T - a.T);
            return std::exp(std::log(a.forward) + weight * (std::log(b.forward) - std::log(a.forward)));
        }
    }
    return smiles_[last].forward;   // not reached
}

std::vector<double> SmileVol::floored_variances(double moneyness) const {
    std::vector<double> result;
    double highest = 0.0;
    for (size_t i = 0; i < smiles_.size(); i++) {
        const TenorSmile& s = smiles_[i];
        double vol = s.smile->vol(moneyness * s.forward);
        double variance = vol * vol * s.T;
        if (variance > highest) {
            highest = variance;
        }
        result.push_back(highest);
    }
    return result;
}

double SmileVol::get_vol(double strike, double T) const {
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }
    if (T <= 0) {
        throw std::invalid_argument("T must be positive");
    }

    size_t last = smiles_.size() - 1;
    double moneyness = strike / forward(T);
    std::vector<double> variances = floored_variances(moneyness);

    // Shorter than the first tenor or longer than the last: that tenor's vol at this moneyness
    if (T <= smiles_[0].T) {
        return std::sqrt(variances[0] / smiles_[0].T);
    }
    if (T >= smiles_[last].T) {
        return std::sqrt(variances[last] / smiles_[last].T);
    }

    // Between two tenors: straight line in total variance
    for (size_t i = 0; i < last; i++) {
        const TenorSmile& left = smiles_[i];
        const TenorSmile& right = smiles_[i + 1];
        if (T <= right.T) {
            double weight = (T - left.T) / (right.T - left.T);
            double variance = variances[i] + weight * (variances[i + 1] - variances[i]);
            return std::sqrt(variance / T);
        }
    }
    return std::sqrt(variances[last] / smiles_[last].T);   // not reached
}