#pragma once
#include <vector>
#include <memory>
#include <string>
#include "vol_provider.hpp"
#include "yield_curve.hpp"

// ---------- One tenor's smile ----------

// Any single-tenor smile: "what's the vol at strike K?"
class Smile {
public:
    virtual ~Smile() = default;
    virtual double vol(double strike) const = 0;
};

// Vanna-volga through the 25P / ATM / 25C dots
class VannaVolgaSmile : public Smile {
public:
    VannaVolgaSmile(double k_25p, double k_atm, double k_25c,
                    double vol_25p, double vol_atm, double vol_25c);
    double vol(double strike) const override;

private:
    double k1_, k2_, k3_;      // 25P, ATM, 25C strikes
    double v1_, v2_, v3_;      // 25P, ATM, 25C vols
};

// SABR (Hagan 2002 lognormal approximation).
// alpha, rho, nu are calibrated in Python; beta is fixed (1.0 for FX here).
class SabrSmile : public Smile {
public:
    SabrSmile(double forward, double T,
              double alpha, double beta, double rho, double nu);
    double vol(double strike) const override;

private:
    double forward_;
    double T_;
    double alpha_;
    double beta_;
    double rho_;
    double nu_;
};

// Result of fitting SABR to one tenor's quotes
struct SabrFit {
    double alpha = 0.0;
    double beta = 1.0;
    double rho = 0.0;
    double nu = 0.0;
    double rms_error = 0.0;      // as reported by QuantLib (vega-weighted fit)
    double max_error = 0.0;
    bool converged = false;
};

// Fits alpha, rho, nu (beta fixed) to the given strikes and vols using
// QuantLib's SABRInterpolation (vega-weighted, Levenberg-Marquardt).
// start_* are the initial guesses, e.g. the previous snapshot's answer.
SabrFit calibrate_sabr(const std::vector<double>& strikes,
                       const std::vector<double>& vols,
                       double forward, double T, double beta,
                       double start_alpha, double start_rho, double start_nu);

// ---------- The surface ----------

// Market quotes for one tenor, as Python passes them in.
// T is years from "now" (the snapshot), so no dates are needed here.
struct TenorQuote {
    double T = 0.0;
    double vol_10p = 0.0;
    double vol_25p = 0.0;
    double vol_atm = 0.0;
    double vol_25c = 0.0;
    double vol_10c = 0.0;
};

// One tenor after construction
struct TenorSmile {
    double T;
    double forward;

    // Pillar strikes, worked out from the deltas in the constructor
    double k_10p;
    double k_25p;
    double k_atm;
    double k_25c;
    double k_10c;

    // Pillar vols, as quoted
    double vol_10p;
    double vol_25p;
    double vol_atm;
    double vol_25c;
    double vol_10c;

    // SABR fit for this tenor (only filled in when model = "sabr")
    SabrFit sabr;

    // The smile model for this tenor
    std::shared_ptr<const Smile> smile;
};

// The vol surface at one snapshot.
//   - constructor: deltas -> strikes for every tenor, builds one Smile per tenor
//     model = "vv"   -> VannaVolgaSmile from 25P / ATM / 25C
//     model = "sabr" -> SABR fitted here to all 5 pillars (QuantLib), beta fixed at 1
//   - get_vol(K, T): that tenor's Smile, blended in total variance between tenors
class SmileVol : public VolProvider {
public:
    SmileVol(double spot,
             const YieldCurve& domestic,     // CNY
             const YieldCurve& foreign,      // USD
             const std::vector<TenorQuote>& quotes,
             const std::string& model = "vv");

    double get_vol(double strike, double T) const override;

    // Read-only access for tests and plots
    const std::vector<TenorSmile>& tenors() const { return smiles_; }
    const std::string& model() const { return model_; }

private:
    std::vector<TenorSmile> smiles_;     // sorted by T, shortest first
    std::string model_;
};