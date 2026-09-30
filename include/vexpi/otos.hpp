#pragma once

#include <cmath>
#include <cstdint>
#include <string>

namespace vexpi
{

// Driver for the SparkFun Qwiic Optical Tracking Odometry Sensor (OTOS)
// over Linux i2c-dev. Register map and fixed-point encodings taken from
// SparkFun's sfDevOTOS reference implementation.
//
// All getters return SI units (meters, radians). Convert at the call site --
// see units.hpp for the VEX-facing inch/degree helpers.
class Otos
{
  public:
    static constexpr uint8_t kAddress = 0x17;
    static constexpr uint8_t kProductId = 0x5F;

    static constexpr uint8_t kRegProductId = 0x00;
    static constexpr uint8_t kRegHwVersion = 0x01;
    static constexpr uint8_t kRegFwVersion = 0x02;
    static constexpr uint8_t kRegScalarLinear = 0x04;
    static constexpr uint8_t kRegScalarAngular = 0x05;
    static constexpr uint8_t kRegImuCalib = 0x06;
    static constexpr uint8_t kRegReset = 0x07;
    static constexpr uint8_t kRegSignalProcess = 0x0E;
    static constexpr uint8_t kRegSelfTest = 0x0F;
    static constexpr uint8_t kRegOffXL = 0x10;
    static constexpr uint8_t kRegStatus = 0x1F;
    static constexpr uint8_t kRegPosXL = 0x20;
    static constexpr uint8_t kRegVelXL = 0x26;

    // int16 fixed-point scales: +/-10 m, +/-pi rad, +/-5 m/s, +/-2000 deg/s
    static constexpr float kInt16ToMeter = 10.0f / 32768.0f;
    static constexpr float kInt16ToRad = float(M_PI) / 32768.0f;
    static constexpr float kInt16ToMps = 5.0f / 32768.0f;
    static constexpr float kInt16ToRps = (2000.0f * float(M_PI) / 180.0f) / 32768.0f;

    static constexpr float kMinScalar = 0.872f;
    static constexpr float kMaxScalar = 1.127f;

    static constexpr const char *kDefaultBus = "/dev/i2c-1";

    struct Pose
    {
        float x = 0.0f;
        float y = 0.0f;
        float h = 0.0f;
    };

    struct Status
    {
        bool tiltWarning = false;
        bool opticalWarning = false;
        bool opticalFatal = false;
        bool imuFatal = false;
        bool ok() const { return !opticalFatal && !imuFatal; }
    };

    // Throws std::runtime_error if the i2c bus device cannot be opened.
    explicit Otos(const std::string &bus = kDefaultBus);
    ~Otos();

    Otos(const Otos &) = delete;
    Otos &operator=(const Otos &) = delete;

    bool connected();
    void version(uint8_t &hwMajor, uint8_t &hwMinor, uint8_t &fwMajor, uint8_t &fwMinor);
    bool selfTest();

    // Robot MUST be completely stationary. 255 samples ~= 612 ms.
    bool calibrateImu(uint8_t numSamples = 255);

    bool resetTracking();

    // Correction factor for distance drift; measure over a long straight push.
    bool setLinearScalar(float scalar);

    // Correction factor for heading drift; measure over several full rotations.
    bool setAngularScalar(float scalar);

    // Sensor mounting position relative to robot center, in meters/radians.
    bool setOffset(const Pose &p);

    Status status();
    Pose position();
    Pose velocity();

  private:
    static uint8_t encodeScalar(float scalar);
    static void packPose(uint8_t *raw, const Pose &p);
    static void sleepMs(int ms);

    Pose readPose(uint8_t reg, float xyScale, float hScale);

    // Repeated-start combined transaction; the OTOS auto-increments registers.
    bool readRegs(uint8_t reg, uint8_t *buf, size_t len);
    bool writeRegs(uint8_t reg, const uint8_t *data, size_t len);
    bool writeReg(uint8_t reg, uint8_t value);

    int fd_ = -1;
};

} // namespace vexpi
