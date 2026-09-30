// Streams the distance to the nearest red target from an OAK-D Lite to the
// VEX V5 brain over USB serial.
//
//   Usage: red_tracker [serial-device]      (default /dev/ttyACM1 user port)
//
// Packets are "R,<inches>\n" while tracking and "N,0\n" when the target is
// lost; see README.md for the packet overview.

#include "vexpi/vision/oak_camera.hpp"
#include "vexpi/serial/packet_sender.hpp"
#include "vexpi/vision/red_target_tracker.hpp"
#include "vexpi/serial/serial_link.hpp"
#include "vexpi/units.hpp"
#include "vexpi/protocol/vex_packet.hpp"

#include <cmath>
#include <csignal>
#include <iomanip>
#include <iostream>
#include <string>

namespace
{
volatile std::sig_atomic_t g_running = 1;
void onSignal(int) { g_running = 0; }
} // namespace

int main(int argc, char **argv)
{
    const std::string serialPort = (argc > 1) ? argv[1] : vexpi::SerialLink::kDefaultDevice;

    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    // The camera can still be tuned without a brain attached.
    vexpi::PacketSender packets(serialPort);

    try
    {
        vexpi::OakCamera camera;
        vexpi::RedTargetTracker tracker;

        cv::Mat frame;
        cv::Mat depth;

        std::cout << "Tracking red targets. Ctrl-C to quit.\n";

        while (g_running)
        {
            if (!camera.nextFrames(frame, depth))
                continue;

            if (frame.size() != depth.size())
            {
                std::cerr << "RGB " << frame.cols << "x" << frame.rows << " and depth "
                          << depth.cols << "x" << depth.rows << " sizes differ; skipping frame\n";
                continue;
            }

            const vexpi::TrackResult result = tracker.update(frame, depth);

            const std::string payload =
                result.hasDistance()
                    ? vexpi::packet::redTarget(static_cast<int>(
                          std::round(vexpi::units::mmToInches(result.filteredDistanceMm))))
                    : vexpi::packet::noTarget();

            packets.send(payload);

            std::cout << std::fixed << std::setprecision(1) << std::left << std::setw(9)
                      << vexpi::toString(result.state);
            if (result.rawDistanceMm > 0)
                std::cout << " | RAW: " << vexpi::units::mmToInches(result.rawDistanceMm) << " in ("
                          << result.rawDistanceMm << " mm)";
            if (result.filteredDistanceMm > 0)
                std::cout << " | FILTERED: " << vexpi::units::mmToInches(result.filteredDistanceMm)
                          << " in (" << result.filteredDistanceMm << " mm)";
            std::cout << " | PACKET: " << payload << std::flush;
        }

        std::cout << "\nStopped.\n";
    }
    catch (const std::exception &e)
    {
        std::cerr << "OAK-D Lite error: " << e.what() << "\n";
        return 1;
    }

    return 0;
}
