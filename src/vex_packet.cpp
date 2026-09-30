#include "vexpi/vex_packet.hpp"

namespace vexpi::packet
{

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
