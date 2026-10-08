#include "portfolio.hpp"
#include <stdexcept>
#include <string>

// Bucket edges match the tenor day counts used everywhere else
// (1W = 7, 1M = 30, 3M = 91, 6M = 182 days), so a 3M option lands in the 3M bucket.
// EDGE_TOL absorbs floating-point rounding (expiry - time can come out as 7.000000000000002 days).
static const double EDGE_TOL = 1e-9;

static double snap_to_bucket(double T) {
    if (T <= 7.0/365 + EDGE_TOL)   return 7.0/365;
    if (T <= 30.0/365 + EDGE_TOL)  return 30.0/365;
    if (T <= 91.0/365 + EDGE_TOL)  return 91.0/365;
    if (T <= 182.0/365 + EDGE_TOL) return 182.0/365;
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

void Portfolio::set_notional(int position_id, double notional) {
    for (auto it = options_.begin(); it != options_.end(); ++it) {
        if (it->id == position_id) {
            if (notional == 0.0) {
                options_.erase(it);
            } else {
                it->notional = notional;
            }
            return;
        }
    }
    throw std::invalid_argument("no position with id " + std::to_string(position_id));
}

void Portfolio::add_spot_hedge(double amount, double rate) {
    spot_hedges_.push_back({amount, rate});
}

PricingResult Portfolio::aggregate_greeks(const MarketSnapshot& mkt) const {
    PricingResult total;

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
        total.vanna += r.vanna * pos.notional;
        total.volga += r.volga * pos.notional;
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
            buckets[bucket] = PricingResult();
        }

        buckets[bucket].price += r.price * pos.notional;
        buckets[bucket].delta += r.delta * pos.notional;
        buckets[bucket].gamma += r.gamma * pos.notional;
        buckets[bucket].theta += r.theta * pos.notional;
        buckets[bucket].vega  += r.vega  * pos.notional;
        buckets[bucket].rho   += r.rho   * pos.notional;
        buckets[bucket].vanna += r.vanna * pos.notional;
        buckets[bucket].volga += r.volga * pos.notional;
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