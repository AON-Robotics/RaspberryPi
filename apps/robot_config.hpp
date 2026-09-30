#pragma once

#include "vexpi/otos/otos_stream.hpp"

// Robot-specific OTOS measurements belong here, outside the sensor driver.
// Edit these values after measuring the installed sensor on your robot.
namespace vexpi::robot
{

inline OtosConfig otosConfig()
{
    OtosConfig config;
    config.offsetXInches = 0.0f;
    config.offsetYInches = 0.0f;
    config.offsetDegrees = 0.0f;
    config.linearScalar = 1.0f;
    config.angularScalar = 1.0f;
    return config;
}

} // namespace vexpi::robot
