#pragma once

#include <string>

// Builders for the newline-delimited ASCII packets sent to the V5 brain.
// See docs/serial-protocol.md for the wire format and the matching
// brain-side parser sketch.
namespace vexpi::packet
{

// "R,<inches>\n" -- a red target is being tracked at <inches>.
std::string redTarget(int distanceInches);

// "N,0\n" -- no usable target this frame.
std::string noTarget();

// "<distance>,<offset>\n" -- the original untagged format, kept for the
// depth-center demo and any brain program still parsing two bare numbers.
std::string distanceOffset(int distance, int offset);

} // namespace vexpi::packet
