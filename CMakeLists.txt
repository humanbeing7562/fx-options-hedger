cmake_minimum_required(VERSION 3.15)
project(fx_options_hedger LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

# QuantLib (Ubuntu: sudo apt install libquantlib0-dev) - used for the SABR formula
find_package(PkgConfig REQUIRED)
pkg_check_modules(QUANTLIB REQUIRED IMPORTED_TARGET quantlib)

# Builds the Python module "fxengine" from these C++ files
pybind11_add_module(fxengine
    engine/bindings.cpp
    engine/src/yield_curve.cpp
    engine/src/pricer.cpp
    engine/src/smile_vol.cpp
    engine/src/market_snapshot.cpp
    engine/src/portfolio.cpp
)

target_include_directories(fxengine PRIVATE engine/include)
target_link_libraries(fxengine PRIVATE PkgConfig::QUANTLIB)