#pragma once

// The sensors speak SI (meters, radians); VEX auton routines are written in
// inches and degrees. Convert at the boundary, using these, so the factors
// live in exactly one place.
namespace vexpi::units
{

inline constexpr float kPi = 3.14159265358979323846f;
inline constexpr float kMeterToInch = 39.37f;
inline constexpr float kRadToDeg = 180.0f / kPi;

inline constexpr float metersToInches(float m) { return m * kMeterToInch; }
inline constexpr float inchesToMeters(float in) { return in / kMeterToInch; }

inline constexpr float radiansToDegrees(float rad) { return rad * kRadToDeg; }
inline constexpr float degreesToRadians(float deg) { return deg / kRadToDeg; }

inline constexpr float mmToInches(float mm) { return mm / 25.4f; }

} // namespace vexpi::units
