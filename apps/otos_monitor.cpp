// Live read-out from the SparkFun Qwiic OTOS over I2C.
//
//   Usage: otos_monitor [i2c-bus]           (default /dev/i2c-1)
//
// Calibrates the IMU, resets tracking, then prints pose at 50 Hz. Use it to
// sanity-check wiring and to measure the scalars in tuneOtos() below.

#include "vexpi/otos.hpp"
#include "vexpi/units.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <thread>

namespace
{

std::atomic<bool> g_running{true};

void onSignal(int) { g_running = false; }

// Everything here is robot-specific. Measure on YOUR robot and edit.
void tuneOtos(vexpi::Otos &otos)
{
    // Where the sensor sits relative to robot centre, in inches and degrees.
    vexpi::Otos::Pose offset;
    offset.x = vexpi::units::inchesToMeters(0.0f);
    offset.y = vexpi::units::inchesToMeters(0.0f);
    offset.h = vexpi::units::degreesToRadians(0.0f);
    otos.setOffset(offset);

    // Drift correction; 1.0 means none. Push a measured distance / spin a
    // measured number of turns, then set these to actual / reported.
    otos.setLinearScalar(1.0f);
    otos.setAngularScalar(1.0f);
}

} // namespace

int main(int argc, char **argv)
{
    const char *bus = (argc > 1) ? argv[1] : vexpi::Otos::kDefaultBus;

    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    try
    {
        vexpi::Otos otos(bus);

        if (!otos.connected())
        {
            std::fprintf(stderr,
                         "OTOS not found at 0x%02X on %s.\n"
                         "Check the Qwiic cable, then run: i2cdetect -y 1\n",
                         vexpi::Otos::kAddress, bus);
            return 1;
        }

        uint8_t hwMaj, hwMin, fwMaj, fwMin;
        otos.version(hwMaj, hwMin, fwMaj, fwMin);
        std::printf("OTOS connected  hw v%u.%u  fw v%u.%u\n", hwMaj, hwMin, fwMaj, fwMin);

        std::printf("Self test... %s\n", otos.selfTest() ? "PASS" : "FAIL");

        tuneOtos(otos);

        std::printf("Calibrating IMU - keep the robot PERFECTLY STILL...\n");
        std::printf("Calibration %s\n", otos.calibrateImu() ? "done" : "FAILED");

        otos.resetTracking();
        std::printf("\nPush the robot around. Ctrl-C to quit.\n\n");

        while (g_running)
        {
            const vexpi::Otos::Pose p = otos.position();
            const vexpi::Otos::Status s = otos.status();

            std::printf("\rX %8.2f in   Y %8.2f in   H %8.2f deg  %s%s%s%s   ",
                        vexpi::units::metersToInches(p.x), vexpi::units::metersToInches(p.y),
                        vexpi::units::radiansToDegrees(p.h),
                        s.tiltWarning ? "[TILT] " : "",
                        s.opticalWarning ? "[OPTICAL] " : "",
                        s.opticalFatal ? "[PAA FAULT] " : "",
                        s.imuFatal ? "[IMU FAULT] " : "");
            std::fflush(stdout);

            std::this_thread::sleep_for(std::chrono::milliseconds(20)); // 50 Hz
        }

        std::printf("\nStopped.\n");
        return 0;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}
