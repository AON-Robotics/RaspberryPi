#include <iostream>
#include <fcntl.h>
#include <unistd.h>
#include <string>
#include <thread>
#include <chrono>

int main() {
    // Open serial port to Vex Brain
    int vex_port = open("/dev/ttyACM0", O_RDWR | O_NOCTTY);

    if (vex_port < 0) {
        std::cerr << "Error: Could not open, Check cable" << std::endl;
        return 1;
    }

    std::cout << "Connected Transmitting" << std::endl;
    int test_Distance = 150;
    int test_offset = -20;

    while(true) {
        //Format string: "distance,offset\n"
        std::string payload = std::to_string(test_Distance) + "," + std::to_string(test_offset) + "\n";

        //Send byte over USB
        write(vex_port, payload.c_str(), payload.length());
        std::cout << "Sent: " <<payload;

        //Send data every 200ms
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    close(vex_port);
    return 0;
}