#include <pybind11/pybind11.h>
#include <pybind11/stl.h>     // lets Python lists turn into std::vector automatically
#include "yield_curve.hpp"
#include "pricer.hpp"
#include "vol_provider.hpp"
#include "smile_vol.hpp"

namespace py = pybind11;

PYBIND11_MODULE(fxengine, m) {
    m.doc() = "C++ pricing engine for the FX options hedger";

    // ---------- Yield curves ----------
    py::class_<YieldCurve>(m, "YieldCurve")
        .def("zero_rate", &YieldCurve::zero_rate, py::arg("T"))
        .def("discount", &YieldCurve::discount, py::arg("T"));

    py::class_<FlatCurve, YieldCurve>(m, "FlatCurve")
        .def(py::init<double>(), py::arg("rate"));

    py::class_<InterpolatedCurve, YieldCurve>(m, "InterpolatedCurve")
        .def(py::init<const std::vector<double>&, const std::vector<double>&>(),
             py::arg("tenors"), py::arg("rates"));

    // ---------- Pricing ----------
    // Read-only result: Python sees res.price, res.delta, ...
    py::class_<PricingResult>(m, "PricingResult")
        .def_readonly("price", &PricingResult::price)
        .def_readonly("delta", &PricingResult::delta)
        .def_readonly("gamma", &PricingResult::gamma)
        .def_readonly("vega", &PricingResult::vega)
        .def_readonly("theta", &PricingResult::theta)
        .def_readonly("rho", &PricingResult::rho);

    // ---------- Vol ----------
    py::class_<VolProvider>(m, "VolProvider")
        .def("get_vol", &VolProvider::get_vol, py::arg("strike"), py::arg("T"));

    py::class_<FlatVol, VolProvider>(m, "FlatVol")
        .def(py::init<double>(), py::arg("vol"));

    py::class_<GKPricer>(m, "GKPricer")
        .def(py::init<double, double>(),
             py::arg("domestic_rate"), py::arg("foreign_rate"))
        .def("price", &GKPricer::price,
             py::arg("spot"), py::arg("strike"), py::arg("T"),
             py::arg("vol"), py::arg("is_call"))
        .def("strike_from_delta", &GKPricer::strike_from_delta,
             py::arg("spot"), py::arg("T"), py::arg("vol_provider"),
             py::arg("target_delta"), py::arg("is_call"),
             py::arg("max_iter") = 50, py::arg("tol") = 1e-10);

    // ---------- Smile ----------
    py::class_<Smile, std::shared_ptr<Smile>>(m, "Smile")
        .def("vol", &Smile::vol, py::arg("strike"));

    py::class_<VannaVolgaSmile, Smile, std::shared_ptr<VannaVolgaSmile>>(m, "VannaVolgaSmile")
        .def(py::init<double, double, double, double, double, double>(),
             py::arg("k_25p"), py::arg("k_atm"), py::arg("k_25c"),
             py::arg("vol_25p"), py::arg("vol_atm"), py::arg("vol_25c"));

    py::class_<SabrSmile, Smile, std::shared_ptr<SabrSmile>>(m, "SabrSmile")
        .def(py::init<double, double, double, double, double, double>(),
             py::arg("forward"), py::arg("T"), py::arg("alpha"),
             py::arg("beta"), py::arg("rho"), py::arg("nu"));

    py::class_<SabrFit>(m, "SabrFit")
        .def_readonly("alpha", &SabrFit::alpha)
        .def_readonly("beta", &SabrFit::beta)
        .def_readonly("rho", &SabrFit::rho)
        .def_readonly("nu", &SabrFit::nu)
        .def_readonly("rms_error", &SabrFit::rms_error)
        .def_readonly("max_error", &SabrFit::max_error)
        .def_readonly("converged", &SabrFit::converged);

    m.def("calibrate_sabr", &calibrate_sabr,
          py::arg("strikes"), py::arg("vols"), py::arg("forward"), py::arg("T"),
          py::arg("beta"), py::arg("start_alpha"), py::arg("start_rho"), py::arg("start_nu"));

    py::class_<TenorQuote>(m, "TenorQuote")
        .def(py::init<>())
        .def_readwrite("T", &TenorQuote::T)
        .def_readwrite("vol_10p", &TenorQuote::vol_10p)
        .def_readwrite("vol_25p", &TenorQuote::vol_25p)
        .def_readwrite("vol_atm", &TenorQuote::vol_atm)
        .def_readwrite("vol_25c", &TenorQuote::vol_25c)
        .def_readwrite("vol_10c", &TenorQuote::vol_10c);

    // Read-only view of a built tenor (the smile pointer itself isn't exposed)
    py::class_<TenorSmile>(m, "TenorSmile")
        .def_readonly("T", &TenorSmile::T)
        .def_readonly("forward", &TenorSmile::forward)
        .def_readonly("k_10p", &TenorSmile::k_10p)
        .def_readonly("k_25p", &TenorSmile::k_25p)
        .def_readonly("k_atm", &TenorSmile::k_atm)
        .def_readonly("k_25c", &TenorSmile::k_25c)
        .def_readonly("k_10c", &TenorSmile::k_10c)
        .def_readonly("vol_10p", &TenorSmile::vol_10p)
        .def_readonly("vol_25p", &TenorSmile::vol_25p)
        .def_readonly("vol_atm", &TenorSmile::vol_atm)
        .def_readonly("vol_25c", &TenorSmile::vol_25c)
        .def_readonly("vol_10c", &TenorSmile::vol_10c)
        .def_readonly("sabr", &TenorSmile::sabr);

    py::class_<SmileVol, VolProvider>(m, "SmileVol")
        .def(py::init<double, const YieldCurve&, const YieldCurve&,
                      const std::vector<TenorQuote>&, const std::string&>(),
             py::arg("spot"), py::arg("domestic"), py::arg("foreign"),
             py::arg("quotes"), py::arg("model") = "vv")
        .def("tenors", &SmileVol::tenors, py::return_value_policy::reference_internal)
        .def("model", &SmileVol::model);
}