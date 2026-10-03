#include "vexpi/serial/serial_link.hpp"

#include <cerrno>
#include <chrono>
#include <poll.h>
#include <cstring>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <utility>

namespace vexpi
{

SerialLink::~SerialLink() { close(); }

SerialLink::SerialLink(SerialLink &&other) noexcept
    : fd_(other.fd_), device_(std::move(other.device_)), lastError_(std::move(other.lastError_))
{
    other.fd_ = -1;
}

SerialLink &SerialLink::operator=(SerialLink &&other) noexcept
{
    if (this != &other)
    {
        close();
        fd_ = other.fd_;
        device_ = std::move(other.device_);
        lastError_ = std::move(other.lastError_);
        other.fd_ = -1;
    }
    return *this;
}

bool SerialLink::open(const std::string &device)
{
    close();
    device_ = device;

    fd_ = ::open(device.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd_ < 0)
    {
        lastError_ = "could not open " + device + ": " + std::strerror(errno);
        return false;
    }

    if (!configure())
    {
        lastError_ = "could not configure " + device + ": " + std::strerror(errno);
        close();
        return false;
    }

    lastError_.clear();
    return true;
}

void SerialLink::close()
{
    if (fd_ >= 0)
    {
        ::close(fd_);
        fd_ = -1;
    }
}

// Raw 115200 8N1: no parity, no flow control, no canonical mode, no output
// post-processing. Anything else would rewrite the bytes in transit.
bool SerialLink::configure()
{
    termios tty{};
    if (::tcgetattr(fd_, &tty) != 0)
        return false;

    ::cfsetospeed(&tty, B115200);
    ::cfsetispeed(&tty, B115200);

    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
    tty.c_iflag &= ~IGNBRK;
    tty.c_lflag = 0;
    tty.c_oflag = 0;

    tty.c_cc[VMIN] = 0;
    tty.c_cc[VTIME] = 1;

    tty.c_iflag &= ~(IXON | IXOFF | IXANY);
    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~(PARENB | PARODD);
    tty.c_cflag &= ~CSTOPB;
    tty.c_cflag &= ~CRTSCTS;

    return ::tcsetattr(fd_, TCSANOW, &tty) == 0;
}

bool SerialLink::write(const std::string &payload)
{
    if (fd_ < 0)
        return false;

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
    size_t sent = 0;
    while (sent < payload.size())
    {
        if (std::chrono::steady_clock::now() >= deadline)
        {
            lastError_ = "serial write timed out";
            return false;
        }
        const ssize_t written = ::write(fd_, payload.data() + sent, payload.size() - sent);
        if (written < 0 && errno == EINTR)
            continue;
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
        {
            pollfd pending{fd_, POLLOUT, 0};
            const int ready = ::poll(&pending, 1, 10);
            if (ready < 0 && errno != EINTR)
            {
                lastError_ = std::string("serial poll failed: ") + std::strerror(errno);
                return false;
            }
            if (ready > 0 && (pending.revents & (POLLERR | POLLHUP | POLLNVAL)))
            {
                lastError_ = "serial device disconnected";
                return false;
            }
            continue;
        }
        if (written <= 0)
        {
            lastError_ = std::string("serial write failed: ") +
                         (written < 0 ? std::strerror(errno) : "zero-byte write");
            return false;
        }
        sent += static_cast<size_t>(written);
    }
    return true;
}

} // namespace vexpi
