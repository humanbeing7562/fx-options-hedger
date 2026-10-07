#include "yield_curve.hpp"
#include <iostream>
#include <cmath>

// 30 Sep 2026 curves from clean/curves.csv
int main() {
    std::vector<double> cny_tenors = {1.0/365, 7.0/365, 14.0/365, 30.0/365,
                                      91.0/365, 182.0/365, 273.0/365, 1.0};
    std::vector<double> cny_rates  = {0.013809, 0.013899, 0.014170, 0.014399,
                                      0.014472, 0.014648, 0.014822, 0.014894};

    std::vector<double> usd_tenors = {1.0/365, 30.0/365, 45.0/365, 61.0/365,
                                      91.0/365, 122.0/365, 182.0/365, 1.0};
    std::vector<double> usd_rates  = {0.039540, 0.040134, 0.041195, 0.041456,
                                      0.041782, 0.042595, 0.042839, 0.044892};

    InterpolatedCurve cny(cny_tenors, cny_rates);
    InterpolatedCurve usd(usd_tenors, usd_rates);

    // 1. On a quoted tenor, should return the quote exactly
    std::cout << "CNY 3M (quoted):        " << cny.zero_rate(91.0/365) << "  expect 0.014472\n";

    // 2. Between tenors: 2M (61d) sits between CNY 1M (30d) and 3M (91d)
    std::cout << "CNY 2M (interpolated):  " << cny.zero_rate(61.0/365) << "  expect ~0.014455\n";

    // 3. Outside the range: flat at the ends
    std::cout << "USD 2Y (beyond 1Y):     " << usd.zero_rate(2.0) << "  expect 0.044892\n";

    // 4. Forward: F = S * DF_usd / DF_cny, should sit below spot
    double spot = 6.7037;   // 30 Sep 16:00
    double T = 91.0 / 365;
    double fwd = spot * usd.discount(T) / cny.discount(T);
    std::cout << "3M forward:             " << fwd << "  (spot " << spot << ", should be lower)\n";
    std::cout << "3M forward points:      " << (fwd - spot) * 10000 << " pips\n";

    // 5. Bad input should throw
    try {
        InterpolatedCurve bad({0.5, 0.25}, {0.01, 0.02});
        std::cout << "FAIL: unsorted tenors accepted\n";
    } catch (const std::invalid_argument& e) {
        std::cout << "Unsorted tenors rejected: " << e.what() << "\n";
    }
    return 0;
}