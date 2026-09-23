#include "pricer.cpp"
#include <iostream>
#include <format>

int main(){
    GKPricer pricer(0.04, 0.025);  // domestic rate, foreign rate
    FlatVol vol_provider(0.072);
    
    
    double T = 30.0 / 365;
    double strike = pricer.strike_from_delta(1.0850, T, vol_provider, 0.25, true);
    double vol = vol_provider.get_vol(strike, T);
    
    std::cout << strike << std::endl;  
    std::cout << vol << std::endl; 

    PricingResult result = pricer.price(
        1.0850,    
        strike,    
        T,  
        vol,     
        true       
    );

    std::cout << result.price << std::endl;  
    std::cout << result.delta << std::endl; 
    std::cout << result.gamma << std::endl; 
    std::cout << result.theta << std::endl;
    std::cout << result.vega << std::endl;
    std::cout << result.rho << std::endl;

    double notional = 10'000'000; 
    double premium = result.price * notional;
    double delta_exposure = result.delta * notional;

    std::cout << premium << std::endl;
    std::cout << std::format("{}\n", delta_exposure);
}