#include <pybind11/pybind11.h>
#include <pybind11/stl.h>     // lets Python lists turn into std::vector automatically
#include "yield_curve.hpp"
#include "pricer.hpp"
#include "vol_provider.hpp"

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
}