#include "vexpi/serial/packet_sender.hpp"
#include "vexpi/serial/serial_link.hpp"

#include <chrono>
#include <cstdlib>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

void check(bool ok) { if (!ok) throw std::runtime_error("serial recovery check failed"); }

int main()
{
    char dir[] = "/tmp/vexpi-serial-XXXXXX";
    check(::mkdtemp(dir) != nullptr);
    const std::string path = std::string(dir) + "/user-port";
    vexpi::PacketSender sender(path);
    check(!sender.send("O,1,2,3\n"));
    const int master = ::posix_openpt(O_RDWR | O_NOCTTY | O_NONBLOCK);
    check(master >= 0 && ::grantpt(master) == 0 && ::unlockpt(master) == 0);
    check(::symlink(::ptsname(master), path.c_str()) == 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(1100));
    check(sender.send("O,1,2,3\n"));
    char bytes[64];
    check(::read(master, bytes, sizeof(bytes)) == 8);

    vexpi::SerialLink stalled;
    check(stalled.open(path));
    const auto start = std::chrono::steady_clock::now();
    check(!stalled.write(std::string(1024 * 1024, 'x')));
    check(std::chrono::steady_clock::now() - start < std::chrono::seconds(1));
    ::close(master);
    check(!sender.send("O,1,2,3\n"));
    ::unlink(path.c_str());

    const int replacement = ::posix_openpt(O_RDWR | O_NOCTTY | O_NONBLOCK);
    check(replacement >= 0 && ::grantpt(replacement) == 0 && ::unlockpt(replacement) == 0);
    check(::symlink(::ptsname(replacement), path.c_str()) == 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(1100));
    check(sender.send("O,4,5,6\n"));
    check(::read(replacement, bytes, sizeof(bytes)) == 8);
    ::close(replacement);
    ::unlink(path.c_str());
    ::rmdir(dir);
}
