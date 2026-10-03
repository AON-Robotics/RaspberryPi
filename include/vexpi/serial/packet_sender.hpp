#pragma once

#include "vexpi/serial/serial_link.hpp"

#include <chrono>
#include <mutex>
#include <string>

namespace vexpi
{

// Shared serial output for sensor workers. Each packet is written under one
// lock, and a missing V5 User Port is retried once per second.
class PacketSender
{
  public:
    explicit PacketSender(std::string device);

    bool send(const std::string &packet);

  private:
    const std::string device_;
    SerialLink link_;
    std::mutex mutex_;
    std::chrono::steady_clock::time_point nextOpen_{};
};

} // namespace vexpi
