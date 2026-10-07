#pragma once

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
