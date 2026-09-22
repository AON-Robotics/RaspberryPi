#pragma once

#include <string>

namespace vexpi
{

// RAII wrapper around the USB serial link to the VEX V5 brain.
//
// A directly connected brain normally exposes two CDC-ACM devices. ttyACM0 is
// usually its communications/programming port and ttyACM1 its user/console
// port. User-program stdin is on the latter. Verify the interface name when
// other serial devices are present.
class SerialLink
{
  public:
    static constexpr const char *kDefaultDevice = "/dev/ttyACM1";

    SerialLink() = default;
    ~SerialLink();

    // Move-only: the file descriptor has a single owner.
    SerialLink(const SerialLink &) = delete;
    SerialLink &operator=(const SerialLink &) = delete;
    SerialLink(SerialLink &&other) noexcept;
    SerialLink &operator=(SerialLink &&other) noexcept;

    // Opens and configures the port. Returns false and sets lastError() on
    // failure; a failed open leaves the object closed but usable.
    bool open(const std::string &device = kDefaultDevice);
    void close();

    bool isOpen() const { return fd_ >= 0; }
    const std::string &device() const { return device_; }
    const std::string &lastError() const { return lastError_; }

    // Writes the payload verbatim. Returns false on a write error or if the
    // port is closed, so callers can keep running headless without a brain.
    bool write(const std::string &payload);

  private:
    bool configure();

    int fd_ = -1;
    std::string device_;
    std::string lastError_;
};

} // namespace vexpi
