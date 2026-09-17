#pragma once

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <stdexcept>
#include <string>
#include <sys/ioctl.h>
#include <thread>
#include <unistd.h>

// Driver for the SparkFun Qwiic Optical Tracking Odometry Sensor (OTOS)
// over Linux i2c-dev. Register map and fixed-point encodings taken from
// SparkFun's sfDevOTOS reference implementation.
//
// All getters return SI units (meters, radians). Convert at the call site.
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

    explicit Otos(const std::string &bus = "/dev/i2c-1")
    {
        fd_ = ::open(bus.c_str(), O_RDWR);
        if (fd_ < 0)
            throw std::runtime_error("cannot open " + bus + ": " + std::strerror(errno));
    }

    ~Otos()
    {
        if (fd_ >= 0)
            ::close(fd_);
    }

    Otos(const Otos &) = delete;
    Otos &operator=(const Otos &) = delete;

    bool connected()
    {
        uint8_t id = 0;
        return readRegs(kRegProductId, &id, 1) && id == kProductId;
    }

    void version(uint8_t &hwMajor, uint8_t &hwMinor, uint8_t &fwMajor, uint8_t &fwMinor)
    {
        uint8_t v[2] = {0, 0};
        readRegs(kRegHwVersion, v, 2);
        hwMajor = v[0] >> 4;
        hwMinor = v[0] & 0x0F;
        fwMajor = v[1] >> 4;
        fwMinor = v[1] & 0x0F;
    }

    bool selfTest()
    {
        writeReg(kRegSelfTest, 0x01);
        for (int i = 0; i < 10; i++)
        {
            sleepMs(5);
            uint8_t v = 0;
            if (!readRegs(kRegSelfTest, &v, 1))
                return false;
            if (v & 0x02) // in progress
                continue;
            return (v & 0x04) != 0; // pass bit
        }
        return false;
    }

    // Robot MUST be completely stationary. 255 samples ~= 612 ms.
    bool calibrateImu(uint8_t numSamples = 255)
    {
        if (!writeReg(kRegImuCalib, numSamples))
            return false;
        sleepMs(3);
        for (int attempts = numSamples; attempts > 0; attempts--)
        {
            uint8_t remaining = 0;
            if (!readRegs(kRegImuCalib, &remaining, 1))
                return false;
            if (remaining == 0)
                return true;
            sleepMs(3);
        }
        return false;
    }

    bool resetTracking() { return writeReg(kRegReset, 0x01); }

    // Correction factor for distance drift; measure over a long straight push.
    bool setLinearScalar(float scalar)
    {
        if (scalar < kMinScalar || scalar > kMaxScalar)
            return false;
        return writeReg(kRegScalarLinear, encodeScalar(scalar));
    }

    // Correction factor for heading drift; measure over several full rotations.
    bool setAngularScalar(float scalar)
    {
        if (scalar < kMinScalar || scalar > kMaxScalar)
            return false;
        return writeReg(kRegScalarAngular, encodeScalar(scalar));
    }

    // Sensor mounting position relative to robot center, in meters/radians.
    bool setOffset(const Pose &p)
    {
        uint8_t raw[6];
        packPose(raw, p);
        return writeRegs(kRegOffXL, raw, 6);
    }

    Status status()
    {
        uint8_t v = 0;
        readRegs(kRegStatus, &v, 1);
        Status s;
        s.tiltWarning = v & 0x01;
        s.opticalWarning = v & 0x02;
        s.opticalFatal = v & 0x40;
        s.imuFatal = v & 0x80;
        return s;
    }

    Pose position() { return readPose(kRegPosXL, kInt16ToMeter, kInt16ToRad); }
    Pose velocity() { return readPose(kRegVelXL, kInt16ToMps, kInt16ToRps); }

  private:
    int fd_ = -1;

    static uint8_t encodeScalar(float scalar)
    {
        return static_cast<uint8_t>(static_cast<int8_t>((scalar - 1.0f) * 1000.0f + 0.5f));
    }

    static void packPose(uint8_t *raw, const Pose &p)
    {
        int16_t x = static_cast<int16_t>(p.x / kInt16ToMeter);
        int16_t y = static_cast<int16_t>(p.y / kInt16ToMeter);
        int16_t h = static_cast<int16_t>(p.h / kInt16ToRad);
        raw[0] = x & 0xFF;
        raw[1] = (x >> 8) & 0xFF;
        raw[2] = y & 0xFF;
        raw[3] = (y >> 8) & 0xFF;
        raw[4] = h & 0xFF;
        raw[5] = (h >> 8) & 0xFF;
    }

    Pose readPose(uint8_t reg, float xyScale, float hScale)
    {
        uint8_t raw[6] = {0};
        Pose p;
        if (!readRegs(reg, raw, 6))
            return p;
        p.x = static_cast<int16_t>((raw[1] << 8) | raw[0]) * xyScale;
        p.y = static_cast<int16_t>((raw[3] << 8) | raw[2]) * xyScale;
        p.h = static_cast<int16_t>((raw[5] << 8) | raw[4]) * hScale;
        return p;
    }

    // Repeated-start combined transaction; the OTOS auto-increments registers.
    bool readRegs(uint8_t reg, uint8_t *buf, size_t len)
    {
        i2c_msg msgs[2];
        msgs[0].addr = kAddress;
        msgs[0].flags = 0;
        msgs[0].len = 1;
        msgs[0].buf = &reg;
        msgs[1].addr = kAddress;
        msgs[1].flags = I2C_M_RD;
        msgs[1].len = static_cast<uint16_t>(len);
        msgs[1].buf = buf;

        i2c_rdwr_ioctl_data xfer{msgs, 2};
        return ::ioctl(fd_, I2C_RDWR, &xfer) >= 0;
    }

    bool writeRegs(uint8_t reg, const uint8_t *data, size_t len)
    {
        uint8_t out[16];
        if (len + 1 > sizeof(out))
            return false;
        out[0] = reg;
        std::memcpy(out + 1, data, len);

        i2c_msg msg;
        msg.addr = kAddress;
        msg.flags = 0;
        msg.len = static_cast<uint16_t>(len + 1);
        msg.buf = out;

        i2c_rdwr_ioctl_data xfer{&msg, 1};
        return ::ioctl(fd_, I2C_RDWR, &xfer) >= 0;
    }

    bool writeReg(uint8_t reg, uint8_t value) { return writeRegs(reg, &value, 1); }

    static void sleepMs(int ms)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(ms));
    }
};
