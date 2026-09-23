#pragma once
#include <cmath>

struct PricingResult {
    double price;
    double delta;
    double gamma;
    double theta;
    double vega;
    double rho;
};

class VolProvider {
public:
    virtual ~VolProvider() = default;
    virtual double get_vol(double strike, double T) const = 0;
};

class FlatVol : public VolProvider {
    double vol_;
public:
    FlatVol(double vol) : vol_(vol) {}
    void set_vol(double vol) { vol_ = vol; }
    double get_vol(double strike, double T) const override { return vol_; }
};

class SmileVol : public VolProvider {
    // TBC
};

class Pricer {
    public:
        virtual ~Pricer() = default;
        
        virtual PricingResult price(
            double spot,
            double strike,
            double time_to_expiry,
            double vol,
            bool is_call
        ) const = 0;
};


class BSPricer: public Pricer {
    public:
        BSPricer(double rate);
        PricingResult price(double spot, double strike, double T,
                            double vol, bool is_call) const override;

    private:
        double rate_;
        double d1(double spot, double strike, double T, double vol) const;
        double d2(double spot, double strike, double T, double vol) const;
};


class GKPricer: public Pricer {
    public:
        GKPricer(double domestic_rate, double foreign_rate);
        void set_rates(double domestic_rate, double foreign_rate);
        double strike_from_delta(double spot, double T,
                             const VolProvider& vol_provider,
                             double target_delta, bool is_call,
                             int max_iter = 100,
                             double tol = 1e-10) const;
        PricingResult price(double spot, double strike, double T,
                            double vol, bool is_call) const override;
        

    private:
        double domestic_rate_;
        double foreign_rate_;
        double vol_;
        
        double d1(double spot, double strike, double T, double vol) const;
        double d2(double spot, double strike, double T, double vol) const;
};