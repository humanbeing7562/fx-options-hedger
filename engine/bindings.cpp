#include <pybind11/pybind11.h>
#include <pybind11/stl.h>     // lets Python lists turn into std::vector automatically
#include "yield_curve.hpp"

namespace py = pybind11;

PYBIND11_MODULE(fxengine, m) {
    m.doc() = "C++ pricing engine for the FX options hedger";

    // Base class: Python can call these on any curve
    py::class_<YieldCurve>(m, "YieldCurve")
        .def("zero_rate", &YieldCurve::zero_rate, py::arg("T"))
        .def("discount", &YieldCurve::discount, py::arg("T"));

    py::class_<FlatCurve, YieldCurve>(m, "FlatCurve")
        .def(py::init<double>(), py::arg("rate"));

    py::class_<InterpolatedCurve, YieldCurve>(m, "InterpolatedCurve")
        .def(py::init<const std::vector<double>&, const std::vector<double>&>(),
             py::arg("tenors"), py::arg("rates"));
}