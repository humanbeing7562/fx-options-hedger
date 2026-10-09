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

// The surface the desk uses: VV where the market is liquid, SABR in the deep wings.
//   between 25P and 25C     -> vanna-volga (exact at 25P / ATM / 25C)
//   past the bridge end     -> SABR fitted to all 5 pillars
//   from 25D to bridge end  -> a cubic in log-strike that starts at VV's vol AND slope at
//                              25D and ends at SABR's vol AND slope at the end (Hermite),
//                              so there is no jump or kink at either join
// The bridge ends at 10D whenever that is arbitrage-free (93% of smile sides in the
// Jul-Sep data). VV reaches 25D with a steep slope; when the market's 10D is much flatter,
// bending into SABR by 10D makes negative density, so the end moves out one step at a time
// (8D, 6D, 5D, 4D, 3D, 2D, 1D) until the density along the bridge is >= 0 everywhere.
// The switch is by STRIKE: the 25D and 10D strikes are where those deltas sit, and
// switching on the option's own delta would be circular (delta needs the vol).
//
// 0 inside 25D, 1 past the bridge end, in between = how far along the bridge (log-strike)
double hybrid_sabr_weight(double strike, double k_end_p, double k_25p, double k_25c, double k_end_c);

class HybridSmile : public Smile {
public:
    static constexpr double FIRST_END_DELTA = 0.10;

    HybridSmile(std::shared_ptr<const Smile> vv, std::shared_ptr<const Smile> sabr,
                double k_10p, double k_25p, double k_25c, double k_10c,
                double forward, double T);
    double vol(double strike) const override;

    // Where each bridge ended (strike and delta) and whether it passed the convexity check
    double put_end_strike() const { return put_side_.k_end; }
    double call_end_strike() const { return call_side_.k_end; }
    double put_end_delta() const { return put_side_.end_delta; }
    double call_end_delta() const { return call_side_.end_delta; }
    bool arbitrage_free() const { return put_side_.convex && call_side_.convex; }

private:
    // One join: cubic from (x0, v0, slope0) at 25D to (x1, v1, slope1) at the end, x = log-strike
    struct Bridge {
        double x0 = 0, x1 = 0, v0 = 0, v1 = 0, slope0 = 0, slope1 = 0;
        double k_end = 0, end_delta = 0;
        bool convex = false;
        double vol(double x) const;
        // vol and its first two derivatives in log-strike, in closed form
        void derivatives(double x, double& v, double& dv, double& d2v) const;
    };

    // Butterfly check on a bridge: the risk-neutral density, written with Gatheral's
    // g(y) in total variance, must be >= 0 everywhere along it (both ends included)
    bool density_ok(const Bridge& bridge) const;

    Bridge build_bridge(bool is_call, double k_25, double k_10) const;
    // Vol on one side of the smile (ATM outwards) with the given bridge
    double side_vol(const Bridge& bridge, bool is_call, double strike) const;

    std::shared_ptr<const Smile> vv_;
    std::shared_ptr<const Smile> sabr_;
    double k_25p_, k_25c_;
    double forward_, T_;
    Bridge put_side_;
    Bridge call_side_;
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

    // SABR fit for this tenor (filled in when model = "sabr" or "hybrid")
    SabrFit sabr;

    // Hybrid only: where the VV -> SABR bridges ended (0.10 = at 10D as quoted)
    double put_end_delta = 0.0;
    double call_end_delta = 0.0;
    double k_put_end = 0.0;
    double k_call_end = 0.0;
    bool arbitrage_free = true;

    // The smile model for this tenor
    std::shared_ptr<const Smile> smile;
};

// The vol surface at one snapshot.
//   - constructor: deltas -> strikes for every tenor, builds one Smile per tenor
//     model = "hybrid" -> HybridSmile: VV inside 25D, SABR past 10D, blended between (default)
//     model = "vv"     -> VannaVolgaSmile only (kept for comparison)
//     model = "sabr"   -> SABR only, fitted here to all 5 pillars (QuantLib), beta fixed at 1
//   - get_vol(K, T): that tenor's Smile, blended in total variance between tenors
class SmileVol : public VolProvider {
public:
    SmileVol(double spot,
             const YieldCurve& domestic,     // CNY
             const YieldCurve& foreign,      // USD
             const std::vector<TenorQuote>& quotes,
             const std::string& model = "hybrid");

    double get_vol(double strike, double T) const override;

    // One tenor's own smile (VV or SABR), WITHOUT the calendar floor - for tests and plots
    double tenor_vol(size_t index, double strike) const;

    // Which part of the hybrid a strike falls in, for one tenor:
    // 0 = VV (inside 25D), 1 = SABR (past the bridge end), in between = how far along the bridge
    double sabr_weight(size_t index, double strike) const;

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