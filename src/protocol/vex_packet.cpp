#include "vexpi/protocol/vex_packet.hpp"
#include "vexpi/units.hpp"

#include <cmath>
#include <cstdio>

namespace vexpi::packet
{

std::string otosPose(const Otos::Pose &pose)
{
    // OTOS: X right, Y forward, heading CCW. Override: X forward,
    // Y right, heading CW.
    const double x = units::metersToInches(pose.y);
    const double y = units::metersToInches(pose.x);
    const double heading = -units::radiansToDegrees(pose.h);
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(heading))
        return {};

    char packet[96];
    const int size = std::snprintf(packet, sizeof(packet), "O,%.3f,%.3f,%.3f\n",
                                   x, y, heading);
    if (size <= 0 || size >= static_cast<int>(sizeof(packet)))
        return {};
    return std::string(packet, static_cast<size_t>(size));
}

std::string redTarget(int distanceInches)
{
    return "R," + std::to_string(distanceInches) + "\n";
}

std::string noTarget() { return "N,0\n"; }

std::string distanceOffset(int distance, int offset)
{
    return std::to_string(distance) + "," + std::to_string(offset) + "\n";
}

} // namespace vexpi::packet
