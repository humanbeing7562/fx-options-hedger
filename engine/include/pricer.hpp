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
        
        PricingResult price(double spot, double strike, double T,
                            double vol, bool is_call) const override;

    private:
        double domestic_rate_;
        double foreign_rate_;
        
        double d1(double spot, double strike, double T, double vol) const;
        double d2(double spot, double strike, double T, double vol) const;
};