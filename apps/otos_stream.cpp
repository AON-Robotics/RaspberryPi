// Streams absolute OTOS pose to Override's odometry task on the V5 User Port.
// Usage: otos_stream [serial-device] [i2c-bus]
#include "vexpi/otos.hpp"
#include "vexpi/serial_link.hpp"
#include "vexpi/units.hpp"

#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <exception>
#include <thread>

namespace {
std::atomic<bool> running{true};
void onSignal(int) { running = false; }
}

int main(int argc, char** argv) {
    const char* port = argc > 1 ? argv[1] : vexpi::SerialLink::kDefaultDevice;
    const char* bus = argc > 2 ? argv[2] : vexpi::Otos::kDefaultBus;
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);
    try {
        vexpi::Otos otos(bus);
        if (!otos.connected()) {
            std::fprintf(stderr, "OTOS not found on %s\n", bus);
            return 1;
        }
        if (!otos.selfTest()) {
            std::fprintf(stderr, "OTOS self-test failed\n");
            return 1;
        }
        // Measure these values for the installed sensor before competition.
        vexpi::Otos::Pose offset{};
        if (!otos.setOffset(offset) || !otos.setLinearScalar(1.0f) ||
            !otos.setAngularScalar(1.0f)) {
            std::fprintf(stderr, "OTOS configuration failed\n");
            return 1;
        }
        std::fprintf(stderr, "Keep robot still: calibrating OTOS IMU\n");
        if (!otos.calibrateImu() || !otos.resetTracking()) {
            std::fprintf(stderr, "OTOS calibration/reset failed\n");
            return 1;
        }
        vexpi::SerialLink link;
        auto nextOpen = std::chrono::steady_clock::now();
        while (running) {
            if (!link.isOpen() && std::chrono::steady_clock::now() >= nextOpen) {
                if (!link.open(port)) {
                    std::fprintf(stderr, "%s\n", link.lastError().c_str());
                    nextOpen = std::chrono::steady_clock::now() + std::chrono::seconds(1);
                }
            }
            if (!otos.connected()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                continue;
            }
            const auto status = otos.status();
            if (!status.ok() || status.opticalWarning || status.tiltWarning) {
                // Withhold packets so the brain stops autonomous motion.
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                continue;
            }
            vexpi::Otos::Pose pose;
            if (!otos.readPosition(pose)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                continue;
            }
            // OTOS: X right, Y forward, heading CCW. Override: X forward,
            // Y right, heading CW (matching its previous gyro odometry).
            const double x = vexpi::units::metersToInches(pose.y);
            const double y = vexpi::units::metersToInches(pose.x);
            const double heading = -vexpi::units::radiansToDegrees(pose.h);
            if (link.isOpen() && std::isfinite(x) && std::isfinite(y) &&
                std::isfinite(heading)) {
                char packet[96];
                const int size = std::snprintf(packet, sizeof(packet),
                                               "O,%.3f,%.3f,%.3f\n", x, y, heading);
                if (size > 0 && size < static_cast<int>(sizeof(packet)) &&
                    !link.write(packet)) {
                    std::fprintf(stderr, "%s\n", link.lastError().c_str());
                    link.close();
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "OTOS stream error: %s\n", e.what());
        return 1;
    }
    return 0;
}
