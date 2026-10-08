#include "portfolio.hpp"

static double snap_to_bucket(double T) {
    if (T <= 7.0/365)   return 7.0/365;
    if (T <= 30.0/365)  return 30.0/365;
    if (T <= 90.0/365)  return 90.0/365;
    if (T <= 180.0/365) return 180.0/365;
    return 365.0/365;
}

int Portfolio::add_option(const Position& pos) {
    Position p = pos;
    p.id = next_id_++;
    options_.push_back(p);
    return p.id;
}

void Portfolio::remove_option(int position_id) {
    for (auto it = options_.begin(); it != options_.end(); ++it) {
        if (it->id == position_id) {
            options_.erase(it);
            return;
        }
    }
}

void Portfolio::add_spot_hedge(double amount, double rate) {
    spot_hedges_.push_back({amount, rate});
}

PricingResult Portfolio::aggregate_greeks(const MarketSnapshot& mkt) const {
    PricingResult total = {0, 0, 0, 0, 0, 0};

    for (const auto& pos : options_) {
        double T = pos.expiry - mkt.time();
        if (T <= 0) continue;

        // The snapshot picks the rates for this T and the smile vol for this strike
        PricingResult r = mkt.price(pos.strike, T, pos.is_call);

        total.price += r.price * pos.notional;
        total.delta += r.delta * pos.notional;
        total.gamma += r.gamma * pos.notional;
        total.theta += r.theta * pos.notional;
        total.vega  += r.vega  * pos.notional;
        total.rho   += r.rho   * pos.notional;
    }

    return total;
}

std::map<double, PricingResult> Portfolio::greeks_by_expiry(const MarketSnapshot& mkt) const {
    std::map<double, PricingResult> buckets;

    for (const auto& pos : options_) {
        double T = pos.expiry - mkt.time();
        if (T <= 0) continue;

        PricingResult r = mkt.price(pos.strike, T, pos.is_call);

        double bucket = snap_to_bucket(T);

        if (buckets.find(bucket) == buckets.end()) {
            buckets[bucket] = {0, 0, 0, 0, 0, 0};
        }

        buckets[bucket].price += r.price * pos.notional;
        buckets[bucket].delta += r.delta * pos.notional;
        buckets[bucket].gamma += r.gamma * pos.notional;
        buckets[bucket].theta += r.theta * pos.notional;
        buckets[bucket].vega  += r.vega  * pos.notional;
        buckets[bucket].rho   += r.rho   * pos.notional;
    }

    return buckets;
}

const std::vector<Position>& Portfolio::get_options() const {
    return options_;
}

double Portfolio::get_net_spot() const {
    double total = 0;
    for (const auto& sp : spot_hedges_) {
        total += sp.amount;
    }
    return total;
}