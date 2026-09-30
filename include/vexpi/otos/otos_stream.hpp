#pragma once

#include "vexpi/otos/otos.hpp"
#include "vexpi/serial/packet_sender.hpp"

#include <atomic>
#include <string>
#include <thread>

namespace vexpi
{

struct OtosConfig
{
    float offsetXInches = 0.0f;
    float offsetYInches = 0.0f;
    float offsetDegrees = 0.0f;
    float linearScalar = 1.0f;
    float angularScalar = 1.0f;
    bool debugPackets = false;
};

// Owns OTOS calibration and its background packet stream. Other sensor
// workers can share the same PacketSender.
class OtosStream
{
  public:
    OtosStream(const std::string &bus, PacketSender &packets, OtosConfig config = {});
    ~OtosStream();

    OtosStream(const OtosStream &) = delete;
    OtosStream &operator=(const OtosStream &) = delete;

    bool calibrate();
    bool start();
    void stop();
    bool failed() const { return failed_.load(); }

  private:
    void run();
    bool sendPositionIfHealthy();

    Otos sensor_;
    PacketSender &packets_;
    OtosConfig config_;
    std::thread worker_;
    std::atomic<bool> running_{false};
    std::atomic<bool> failed_{false};
    bool calibrated_ = false;
};

} // namespace vexpi
