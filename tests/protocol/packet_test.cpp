#include "vexpi/units.hpp"
#include "vexpi/protocol/vex_packet.hpp"

#include <iostream>
#include <limits>
#include <string>

namespace
{
bool expect(const std::string &actual, const std::string &wanted)
{
    if (actual == wanted)
        return true;
    std::cerr << "Expected [" << wanted << "] but got [" << actual << "]\n";
    return false;
}
} // namespace

int main()
{
    vexpi::Otos::Pose pose;
    pose.x = vexpi::units::inchesToMeters(2.0f); // sensor X becomes robot Y
    pose.y = vexpi::units::inchesToMeters(1.0f); // sensor Y becomes robot X
    pose.h = vexpi::units::degreesToRadians(90.0f); // CCW becomes clockwise

    if (!expect(vexpi::packet::otosPose(pose), "O,1.000,2.000,-90.000\n") ||
        !expect(vexpi::packet::redTarget(24), "R,24\n") ||
        !expect(vexpi::packet::noTarget(), "N,0\n") ||
        !expect(vexpi::packet::distanceOffset(120, 0), "120,0\n"))
        return 1;

    pose.x = std::numeric_limits<float>::quiet_NaN();
    return vexpi::packet::otosPose(pose).empty() ? 0 : 1;
}
