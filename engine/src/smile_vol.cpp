#include "smile_vol.hpp"
#include "pricer.hpp"
#include <cmath>
#include <stdexcept>

// ======================================================================
// VannaVolgaSmile
// ======================================================================

VannaVolgaSmile::VannaVolgaSmile(double k_25p, double k_atm, double k_25c,
                                 double vol_25p, double vol_atm, double vol_25c)
    : k1_(k_25p), k2_(k_atm), k3_(k_25c),
      v1_(vol_25p), v2_(vol_atm), v3_(vol_25c) {

    if (!(k1_ < k2_ && k2_ < k3_)) {
        throw std::invalid_argument("vanna-volga strikes must be ordered 25P < ATM < 25C");
    }
    if (v1_ <= 0 || v2_ <= 0 || v3_ <= 0) {
        throw std::invalid_argument("vanna-volga vols must be positive");
    }
}

double VannaVolgaSmile::vol(double strike) const {
    if (strike <= 0) {
        throw std::invalid_argument("strike must be positive");
    }

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

    double floor = 0.001;
    if (result < floor) {
        result = floor;
    }
    return result;
}

// ======================================================================
// SmileVol
// ======================================================================

SmileVol::SmileVol(double spot,
                   const YieldCurve& domestic,
                   const YieldCurve& foreign,
                   const std::vector<TenorQuote>& quotes) {

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

        // Smile model for this tenor: vanna-volga through 25P / ATM / 25C
        s.smile = std::make_shared<VannaVolgaSmile>(s.k_25p, s.k_atm, s.k_25c,
                                                    s.vol_25p, s.vol_atm, s.vol_25c);
        smiles_.push_back(s);
    }
}

double SmileVol::get_vol(double strike, double T) const {
    size_t last = smiles_.size() - 1;

    // Shorter than the first tenor or longer than the last: use that tenor's smile
    if (T <= smiles_[0].T) {
        return smiles_[0].smile->vol(strike);
    }
    if (T >= smiles_[last].T) {
        return smiles_[last].smile->vol(strike);
    }

    // Find the two tenors either side of T and blend in total variance (vol^2 * T)
    for (size_t i = 0; i < last; i++) {
        const TenorSmile& left = smiles_[i];
        const TenorSmile& right = smiles_[i + 1];

        if (T <= right.T) {
            double vol_left = left.smile->vol(strike);
            double vol_right = right.smile->vol(strike);

            double var_left = vol_left * vol_left * left.T;
            double var_right = vol_right * vol_right * right.T;

            double weight = (T - left.T) / (right.T - left.T);
            double var = var_left + weight * (var_right - var_left);
            return std::sqrt(var / T);
        }
    }

    return smiles_[last].smile->vol(strike);   // not reached
}