#include "pricer.cpp"
#include "portfolio.cpp"
#include <iostream>
#include <iomanip>

int main() {
    GKPricer pricer(0.053, 0.038);
    FlatVol vol_provider(0.072);
    Portfolio portfolio;

    double spot = 1.0850;
    double current_time = 0.0;

    // Pos 1
    double strike1 = pricer.strike_from_delta(spot, 30.0/365, vol_provider, 0.25, true);
    int id1 = portfolio.add_option({
        0, strike1, 30.0/365, -100'000'000, true,
        pricer.price(spot, strike1, 30.0/365, 0.072, true).price
    });

    // Pos 2
    double strike2 = pricer.strike_from_delta(spot, 30.0/365, vol_provider, -0.10, false);
    int id2 = portfolio.add_option({
        0, strike2, 30.0/365, 50'000'000, false,
        pricer.price(spot, strike2, 30.0/365, 0.072, false).price
    });

    std::cout << "=== Aggregate Greeks ===\n";
    PricingResult agg = portfolio.aggregate_greeks(spot, current_time, pricer, vol_provider);
    std::cout << "Delta: " << std::fixed << std::setprecision(0) << agg.delta << " EUR\n";
    std::cout << "Gamma: " << agg.gamma << "\n";
    std::cout << "Vega:  " << agg.vega << "\n";
    std::cout << "Theta: " << agg.theta << "\n";

    double hedge_amount = -agg.delta;  // offset the options delta
    portfolio.add_spot_hedge(hedge_amount, spot);

    std::cout << "\n=== After Delta Hedge ===\n";
    std::cout << "Spot position: " << std::fixed << std::setprecision(0) 
              << portfolio.get_net_spot() << " EUR\n";
    double unhedged = agg.delta + portfolio.get_net_spot();
    std::cout << "Unhedged delta: " << unhedged << "\n";

    double new_spot = 1.0920;
    std::cout << "\n=== Spot moved to " << std::setprecision(4) << new_spot << " ===\n";
    PricingResult new_agg = portfolio.aggregate_greeks(new_spot, current_time, pricer, vol_provider);
    double new_unhedged = new_agg.delta + portfolio.get_net_spot();
    std::cout << "Options delta: " << std::setprecision(0) << new_agg.delta << " EUR\n";
    std::cout << "Spot position: " << portfolio.get_net_spot() << " EUR\n";
    std::cout << "Unhedged delta: " << new_unhedged << " EUR\n";
    std::cout << "→ Need to hedge: " << -new_unhedged << " EUR\n";


    std::cout << "\n=== Greeks By Expiry ===\n";
    auto buckets = portfolio.greeks_by_expiry(new_spot, current_time, pricer, vol_provider);
    for (const auto& [tenor, greeks] : buckets) {
        std::cout << "Bucket " << std::setprecision(4) << tenor * 365 << " days:\n";
        std::cout << "  Delta: " << std::setprecision(0) << greeks.delta << "\n";
        std::cout << "  Gamma: " << greeks.gamma << "\n";
        std::cout << "  Vega:  " << greeks.vega << "\n";
    }

    return 0;
}