// Reports the distance to whatever sits at the centre of the frame, with a
// live preview window. No colour detection -- this is the simple depth demo
// to fall back on when you want to check the camera itself.
//
//   Usage: depth_center_demo [serial-device]    (default /dev/ttyACM1 user port)
//
// Packets use the untagged "<centimetres>,<offset>\n" format.

#include "vexpi/oak_camera.hpp"
#include "vexpi/serial_link.hpp"
#include "vexpi/vex_packet.hpp"

#include <algorithm>
#include <iostream>
#include <string>
#include <vector>

#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>

namespace
{

constexpr int kRoiSize = 10;

// Median of the valid depth pixels in a small patch at the frame centre.
// Zero means the camera returned nothing usable there.
int centerDistanceCm(const cv::Mat &depth)
{
    const cv::Rect roi(depth.cols / 2 - kRoiSize / 2, depth.rows / 2 - kRoiSize / 2, kRoiSize,
                       kRoiSize);
    const cv::Mat patch = depth(roi);

    std::vector<uint16_t> valid;
    for (int y = 0; y < patch.rows; y++)
        for (int x = 0; x < patch.cols; x++)
        {
            const uint16_t d = patch.at<uint16_t>(y, x);
            if (d > 0) // 0 = invalid depth
                valid.push_back(d);
        }

    if (valid.empty())
        return 0;

    std::sort(valid.begin(), valid.end());
    return valid[valid.size() / 2] / 10;
}

} // namespace

int main(int argc, char **argv)
{
    const std::string serialPort = (argc > 1) ? argv[1] : vexpi::SerialLink::kDefaultDevice;

    vexpi::SerialLink vex;
    if (!vex.open(serialPort))
        std::cerr << "Warning: " << vex.lastError() << "\n";
    else
        std::cout << "VEX brain connected on " << serialPort << "\n";

    try
    {
        vexpi::OakCamera camera;
        std::cout << "OAK-D Lite running. Press q in the preview window to quit.\n";

        cv::Mat frame;
        cv::Mat depth;

        while (true)
        {
            if (!camera.nextFrames(frame, depth))
                continue;

            const int distanceCm = centerDistanceCm(depth);

            const std::string payload = vexpi::packet::distanceOffset(distanceCm, 0);
            if (vex.isOpen() && !vex.write(payload))
                std::cerr << vex.lastError() << "\n";

            const cv::Point center(frame.cols / 2, frame.rows / 2);
            cv::circle(frame, center, 5, cv::Scalar(0, 255, 0), -1);
            cv::putText(frame, "Dist: " + std::to_string(distanceCm) + " cm",
                        center + cv::Point(10, 0), cv::FONT_HERSHEY_SIMPLEX, 0.6,
                        cv::Scalar(0, 255, 0), 2);

            cv::imshow("OAK-D Lite depth centre", frame);
            if (cv::waitKey(1) == 'q')
                break;
        }
    }
    catch (const std::exception &e)
    {
        std::cerr << "OAK-D Lite error: " << e.what() << "\n";
        return 1;
    }

    cv::destroyAllWindows();
    return 0;
}
