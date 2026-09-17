#include "otos.hpp"

#include <atomic>
#include <csignal>
#include <cstdio>

namespace
{
constexpr float kMeterToInch = 39.37f;
constexpr float kRadToDeg = 180.0f / float(M_PI);

std::atomic<bool> g_running{true};

void onSignal(int) { g_running = false; }
} // namespace

int main(int argc, char **argv)
{
    const char *bus = (argc > 1) ? argv[1] : "/dev/i2c-1";

    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    try
    {
        Otos otos(bus);

        if (!otos.connected())
        {
            std::fprintf(stderr,
                         "OTOS not found at 0x%02X on %s.\n"
                         "Check the Qwiic cable, then run: i2cdetect -y 1\n",
                         Otos::kAddress, bus);
            return 1;
        }

        uint8_t hwMaj, hwMin, fwMaj, fwMin;
        otos.version(hwMaj, hwMin, fwMaj, fwMin);
        std::printf("OTOS connected  hw v%u.%u  fw v%u.%u\n", hwMaj, hwMin, fwMaj, fwMin);

        std::printf("Self test... %s\n", otos.selfTest() ? "PASS" : "FAIL");

        // Mounting offset of the sensor relative to robot center.
        // Measure on your actual robot and fill these in (inches / degrees).
        Otos::Pose offset;
        offset.x = 0.0f / kMeterToInch;
        offset.y = 0.0f / kMeterToInch;
        offset.h = 0.0f / kRadToDeg;
        otos.setOffset(offset);

        // Tune these after measuring drift; 1.0 means no correction.
        otos.setLinearScalar(1.0f);
        otos.setAngularScalar(1.0f);

        std::printf("Calibrating IMU - keep the robot PERFECTLY STILL...\n");
        std::printf("Calibration %s\n", otos.calibrateImu() ? "done" : "FAILED");

        otos.resetTracking();
        std::printf("\nPush the robot around. Ctrl-C to quit.\n\n");

        while (g_running)
        {
            const Otos::Pose p = otos.position();
            const Otos::Status s = otos.status();

            std::printf("\rX %8.2f in   Y %8.2f in   H %8.2f deg  %s%s%s%s   ",
                        p.x * kMeterToInch, p.y * kMeterToInch, p.h * kRadToDeg,
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
