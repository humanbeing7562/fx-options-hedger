#pragma once
#include <vector>
#include <map>
#include "pricer.hpp"
#include "market_snapshot.hpp"

// One option in the book.
// expiry is on the sim clock (years since the sim started), like MarketSnapshot::time.
// notional > 0 = long, notional < 0 = short (in USD).
struct Position {
    int id = 0;
    double strike = 0.0;
    double expiry = 0.0;
    bool is_call = true;
    double notional = 0.0;
};

struct SpotHedge {
    double amount;    // USD bought (+) or sold (-)
    double rate;      // spot rate it was traded at
};

class Portfolio {
public:
    int add_option(const Position& pos);
    void remove_option(int position_id);
    // Change a position's notional (used to net legs together); 0 removes it.
    // Throws if the id isn't in the book.
    void set_notional(int position_id, double notional);
    void add_spot_hedge(double amount, double rate);

    // Totals across all live options (expired ones are skipped)
    PricingResult aggregate_greeks(const MarketSnapshot& mkt) const;

    // Same, grouped into expiry buckets: <=1W, <=1M, <=3M, <=6M, longer (keys 7/365 ... 1.0)
    std::map<double, PricingResult> greeks_by_expiry(const MarketSnapshot& mkt) const;

    const std::vector<Position>& get_options() const;
    double get_net_spot() const;

private:
    std::vector<Position> options_;
    std::vector<SpotHedge> spot_hedges_;
    int next_id_ = 1;
};