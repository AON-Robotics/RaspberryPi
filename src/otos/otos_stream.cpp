#include "vexpi/otos/otos_stream.hpp"
#include "vexpi/units.hpp"
#include "vexpi/protocol/vex_packet.hpp"

#include <chrono>
#include <cstdio>
#include <exception>
#include <thread>
#include <utility>

namespace vexpi
{

OtosStream::OtosStream(const std::string &bus, PacketSender &packets, OtosConfig config)
    : sensor_(bus), packets_(packets), config_(std::move(config)) {}

OtosStream::~OtosStream() { stop(); }

bool OtosStream::calibrate()
{
    if (worker_.joinable())
        return false;
    calibrated_ = false;

    if (!sensor_.connected())
    {
        std::fprintf(stderr, "OTOS not found\n");
        return false;
    }
    if (!sensor_.selfTest())
    {
        std::fprintf(stderr, "OTOS self-test failed\n");
        return false;
    }

    Otos::Pose offset;
    offset.x = units::inchesToMeters(config_.offsetXInches);
    offset.y = units::inchesToMeters(config_.offsetYInches);
    offset.h = units::degreesToRadians(config_.offsetDegrees);
    if (!sensor_.setOffset(offset) || !sensor_.setLinearScalar(config_.linearScalar) ||
        !sensor_.setAngularScalar(config_.angularScalar))
    {
        std::fprintf(stderr, "OTOS configuration failed\n");
        return false;
    }

    std::fprintf(stderr, "Keep robot still: calibrating OTOS IMU\n");
    if (!sensor_.calibrateImu() || !sensor_.resetTracking())
    {
        std::fprintf(stderr, "OTOS calibration/reset failed\n");
        return false;
    }
    calibrated_ = true;
    return true;
}

bool OtosStream::start()
{
    if (!calibrated_ || worker_.joinable())
        return false;

    failed_ = false;
    running_ = true;
    worker_ = std::thread([this]
    {
        try
        {
            run();
        }
        catch (const std::exception &e)
        {
            std::fprintf(stderr, "OTOS worker error: %s\n", e.what());
            failed_ = true;
        }
        catch (...)
        {
            std::fprintf(stderr, "OTOS worker error: unknown exception\n");
            failed_ = true;
        }
        running_ = false;
    });
    return true;
}

void OtosStream::stop()
{
    running_ = false;
    if (worker_.joinable())
        worker_.join();
}

void OtosStream::run()
{
    auto nextTick = std::chrono::steady_clock::now();
    bool sensorHealthy = true;
    while (running_.load())
    {
        const bool healthy = sendPositionIfHealthy();
        if (healthy != sensorHealthy)
        {
            std::fprintf(stderr, "%s\n", healthy ? "OTOS readings recovered"
                                                 : "OTOS reading unavailable or warning active");
            sensorHealthy = healthy;
        }
        nextTick += std::chrono::milliseconds(20);
        const auto now = std::chrono::steady_clock::now();
        if (nextTick < now)
            nextTick = now;
        std::this_thread::sleep_until(nextTick);
    }
}

bool OtosStream::sendPositionIfHealthy()
{
    Otos::Status status;
    Otos::Pose pose;
    if (!sensor_.readStatus(status) || !status.ok() || status.opticalWarning ||
        status.tiltWarning || !sensor_.readPosition(pose))
        return false; // The brain must treat its last pose as stale when packets stop.

    const std::string packet = packet::otosPose(pose);
    if (packet.empty())
        return false;

    const bool sent = packets_.send(packet);
    if (config_.debugPackets)
    {
        std::fputs(sent ? "SENT " : "UNSENT ", stdout);
        std::fputs(packet.c_str(), stdout);
        std::fflush(stdout);
    }
    return true;
}

} // namespace vexpi
