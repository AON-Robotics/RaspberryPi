// Raspberry Pi entry point: configure and start sensor workers here.
// Usage: vexpi [--debug] [serial-device] [i2c-bus]

#include "robot_config.hpp"

#include "vexpi/otos/otos_stream.hpp"
#include "vexpi/serial/packet_sender.hpp"
#include "vexpi/serial/serial_link.hpp"

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <exception>
#include <thread>

namespace
{
volatile std::sig_atomic_t stopRequested = 0;
void onSignal(int) { stopRequested = 1; }

void printUsage(const char *program, FILE *out)
{
    std::fprintf(out, "Usage: %s [--debug] [serial-device] [i2c-bus]\n", program);
    std::fprintf(out, "  --debug  Print each OTOS packet and whether the serial write succeeded\n");
}
} // namespace

int main(int argc, char **argv)
{
    const char *port = vexpi::SerialLink::kDefaultDevice;
    const char *bus = vexpi::Otos::kDefaultBus;
    bool debugPackets = false;
    int devices = 0;
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--debug") == 0)
            debugPackets = true;
        else if (std::strcmp(argv[i], "--help") == 0 || std::strcmp(argv[i], "-h") == 0)
        {
            printUsage(argv[0], stdout);
            return 0;
        }
        else if (argv[i][0] == '-' || devices >= 2)
        {
            printUsage(argv[0], stderr);
            return 2;
        }
        else if (devices++ == 0)
            port = argv[i];
        else
            bus = argv[i];
    }

    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    try
    {
        vexpi::PacketSender packets(port); // Share this with future sensor workers.
        vexpi::OtosConfig otosConfig = vexpi::robot::otosConfig();
        otosConfig.debugPackets = debugPackets;
        vexpi::OtosStream otos(bus, packets, otosConfig);
        if (!otos.calibrate())
            return 1;
        if (!otos.start())
            return 1;

        while (!stopRequested && !otos.failed())
            std::this_thread::sleep_for(std::chrono::milliseconds(100));

        otos.stop();
        return otos.failed() ? 1 : 0;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "Startup error: %s\n", e.what());
        return 1;
    }
}
