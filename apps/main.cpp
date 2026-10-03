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

    // Make redirected stdout useful immediately in the systemd journal.
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::fprintf(stderr, "Starting VEX coprocessor: serial=%s, I2C=%s\n", port, bus);
    vexpi::PacketSender packets(port);
    vexpi::OtosConfig otosConfig = vexpi::robot::otosConfig();
    otosConfig.debugPackets = debugPackets;
    while (!stopRequested)
    {
        try
        {
            // Reopen the bus on every attempt, including when it appeared late.
            vexpi::OtosStream otos(bus, packets, otosConfig);
            if (otos.calibrate() && !stopRequested && otos.start())
            {
                std::fprintf(stderr, "OTOS stream started\n");
                while (!stopRequested && !otos.failed())
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                otos.stop();
            }
        }
        catch (const std::exception &e)
        {
            std::fprintf(stderr, "OTOS startup/worker error: %s\n", e.what());
        }
        if (!stopRequested)
        {
            std::fprintf(stderr, "Retrying OTOS initialization in 2 seconds\n");
            for (int i = 0; i < 20 && !stopRequested; ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }
    std::fprintf(stderr, "VEX coprocessor stopped\n");
    return 0;
}
