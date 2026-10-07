#pragma once
#include <vector>
#include <memory>
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

// ---------- The surface ----------

// Market quotes for one tenor, as Python passes them in.
// T is years from "now" (the snapshot), so no dates are needed here.
struct TenorQuote {
    double T;
    double vol_10p;
    double vol_25p;
    double vol_atm;
    double vol_25c;
    double vol_10c;
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

    // The smile model for this tenor (vanna-volga for now, SABR later)
    std::shared_ptr<const Smile> smile;
};

// The vol surface at one snapshot.
//   - constructor: deltas -> strikes for every tenor, builds one Smile per tenor
//   - get_vol(K, T): that tenor's Smile, blended in total variance between tenors
class SmileVol : public VolProvider {
public:
    SmileVol(double spot,
             const YieldCurve& domestic,     // CNY
             const YieldCurve& foreign,      // USD
             const std::vector<TenorQuote>& quotes);

    double get_vol(double strike, double T) const override;

    // Read-only access for tests and plots
    const std::vector<TenorSmile>& tenors() const { return smiles_; }

private:
    std::vector<TenorSmile> smiles_;     // sorted by T, shortest first
};