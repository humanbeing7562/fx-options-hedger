#pragma once

#include "pricer.hpp"
#include <vector>
#include <map>

struct Position{
    int id;
    double strike;
    double expiry;
    double notional;
    bool is_call;
    double entry_price;
};

struct SpotPosition{
    double amount;
    double entry_rate;
};

class Portfolio {
    public:
        int add_option(const Position& pos);
        void remove_option(int position_id);
        void add_spot_hedge(double amount, double rate);
        
        PricingResult aggregate_greeks(double spot, double current_time,
                                    const Pricer& pricer,
                                    const VolProvider& vol_provider) const;
        
        std::map<double, PricingResult> greeks_by_expiry(
            double spot, double current_time,
            const Pricer& pricer,
            const VolProvider& vol_provider) const;
        
        const std::vector<Position>& get_options() const;
        double get_net_spot() const;

    private:
        std::vector<Position> options_;
        std::vector<SpotPosition> spot_hedges_;
        int next_id_ = 1;
};