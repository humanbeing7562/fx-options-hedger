#pragma once
#include <vector>
#include <memory>
#include <string>
#include <limits>
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
// Wings beyond [k_min, k_max] (the 10P and 10C strikes when built by SmileVol):
//   First-order VV is quadratic in log-strike and explodes far out (~49% vol for a 1W
//   strike 5% out, against 1.6% ATM). Holding the vol flat instead puts a kink in the
//   option-price curve at the join, which means negative probability (butterfly
//   arbitrage). So past each join the vol keeps the smile's slope and levels off:
//       vol(u) = vol_join + slope * L * (1 - exp(-u / L)),   u = log-distance past the join
//   with L = WING_LENGTH_SD standard deviations (vol_join * sqrt(T)). Smooth at the join,
//   bounded far out, and arbitrage-free on every smile in the Jul-Sep data (tested).
class VannaVolgaSmile : public Smile {
public:
    static constexpr double WING_LENGTH_SD = 3.0;

    VannaVolgaSmile(double k_25p, double k_atm, double k_25c,
                    double vol_25p, double vol_atm, double vol_25c,
                    double k_min = 0.0, double k_max = std::numeric_limits<double>::infinity(),
                    double T = 0.0);
    double vol(double strike) const override;

private:
    double raw_vol(double strike) const;      // the VV formula, no wing control
    double k1_, k2_, k3_;      // 25P, ATM, 25C strikes
    double v1_, v2_, v3_;      // 25P, ATM, 25C vols
    double k_min_, k_max_;     // wings start beyond these strikes

    // Wing shape, worked out once in the constructor
    double vol_low_ = 0.0, slope_low_ = 0.0, length_low_ = 1.0;     // put side
    double vol_high_ = 0.0, slope_high_ = 0.0, length_high_ = 1.0;  // call side
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

class SmileVol : public VolProvider {
public:
    SmileVol(double spot,
             const YieldCurve& domestic,     // CNY
             const YieldCurve& foreign,      // USD
             const std::vector<TenorQuote>& quotes,
             const std::string& model = "vv");

    double get_vol(double strike, double T) const override;

    // One tenor's own smile (VV or SABR), WITHOUT the calendar floor - for tests and plots
    double tenor_vol(size_t index, double strike) const;

    // Forward for any maturity: log-linear between the tenor forwards (spot at T = 0)
    double forward(double T) const;

    // Read-only access for tests and plots
    const std::vector<TenorSmile>& tenors() const { return smiles_; }
    const std::string& model() const { return model_; }

private:
    // Total variance of every tenor at moneyness x, after the calendar floor
    std::vector<double> floored_variances(double moneyness) const;

    std::vector<TenorSmile> smiles_;     // sorted by T, shortest first
    std::string model_;
    double spot_;
};