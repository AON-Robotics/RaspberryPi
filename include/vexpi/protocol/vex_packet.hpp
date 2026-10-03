#pragma once

#include "vexpi/otos/otos.hpp"

#include <string>

// Builders for the newline-delimited ASCII packets sent to the V5 brain.
// See README.md for the wire format used by the Pi applications.
namespace vexpi::packet
{

// Convert an OTOS pose to Override's X-forward, Y-right, clockwise-heading
// convention and format "O,<x inches>,<y inches>,<heading degrees>\n".
// Returns an empty string for a non-finite pose or formatting failure.
std::string otosPose(const Otos::Pose &pose);

// "R,<inches>\n" -- a red target is being tracked at <inches>.
std::string redTarget(int distanceInches);

// "N,0\n" -- no usable target this frame.
std::string noTarget();

// "<distance>,<offset>\n" -- the original untagged format, kept for the
// depth-center demo and any brain program still parsing two bare numbers.
std::string distanceOffset(int distance, int offset);

} // namespace vexpi::packet
