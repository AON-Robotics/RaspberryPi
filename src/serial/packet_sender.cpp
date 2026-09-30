#include "vexpi/serial/packet_sender.hpp"

#include <chrono>
#include <cstdio>
#include <utility>

namespace vexpi
{

PacketSender::PacketSender(std::string device) : device_(std::move(device)) {}

bool PacketSender::send(const std::string &packet)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (!link_.isOpen())
    {
        const auto now = std::chrono::steady_clock::now();
        if (now < nextOpen_)
            return false;
        if (!link_.open(device_))
        {
            std::fprintf(stderr, "%s\n", link_.lastError().c_str());
            nextOpen_ = now + std::chrono::seconds(1);
            return false;
        }
        std::fprintf(stderr, "V5 User Port connected on %s\n", device_.c_str());
    }

    if (link_.write(packet))
        return true;

    std::fprintf(stderr, "%s\n", link_.lastError().c_str());
    link_.close();
    nextOpen_ = std::chrono::steady_clock::now() + std::chrono::seconds(1);
    return false;
}

} // namespace vexpi
