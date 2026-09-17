#include <iostream>
#include <fcntl.h>
#include <unistd.h>
#include <string>
#include <vector>
#include <algorithm>

#include <opencv2/opencv.hpp>
#include <depthai/depthai.hpp>

int main() {
    // ---------------------------------------------------------
    // 1. Open USB Serial port to VEX Brain
    // ---------------------------------------------------------
    int vex_port = open("/dev/ttyACM0", O_RDWR | O_NOCTTY);

    if(vex_port < 0) {
        std::cerr << "Warning: Could not open /dev/ttyACM0. Check cable."
                  << std::endl;
    } else {
        std::cout << "VEX Brain connected on /dev/ttyACM0" << std::endl;
    }

    // ---------------------------------------------------------
    // 2. Create DepthAI pipeline
    // ---------------------------------------------------------
    dai::Pipeline pipeline;

    auto camRgb = pipeline.create<dai::node::ColorCamera>();
    auto monoLeft = pipeline.create<dai::node::MonoCamera>();
    auto monoRight = pipeline.create<dai::node::MonoCamera>();
    auto depth = pipeline.create<dai::node::StereoDepth>();

    // Host output streams - REQUIRED FOR DEPTHAI V2
    auto xoutRgb = pipeline.create<dai::node::XLinkOut>();
    auto xoutDepth = pipeline.create<dai::node::XLinkOut>();

    xoutRgb->setStreamName("rgb");
    xoutDepth->setStreamName("depth");

    // ---------------------------------------------------------
    // 3. RGB camera
    // ---------------------------------------------------------
    camRgb->setBoardSocket(dai::CameraBoardSocket::CAM_A);

    camRgb->setPreviewSize(640, 480);
    camRgb->setInterleaved(false);
    camRgb->setColorOrder(
        dai::ColorCameraProperties::ColorOrder::BGR
    );

    // ---------------------------------------------------------
    // 4. Stereo cameras
    // ---------------------------------------------------------
    monoLeft->setBoardSocket(dai::CameraBoardSocket::CAM_B);
    monoLeft->setResolution(
        dai::MonoCameraProperties::SensorResolution::THE_400_P
    );

    monoRight->setBoardSocket(dai::CameraBoardSocket::CAM_C);
    monoRight->setResolution(
        dai::MonoCameraProperties::SensorResolution::THE_400_P
    );

    // ---------------------------------------------------------
    // 5. Stereo depth
    // ---------------------------------------------------------
    depth->setDefaultProfilePreset(
        dai::node::StereoDepth::PresetMode::HIGH_DENSITY
    );

    // Helps depth alignment
    depth->setLeftRightCheck(true);

    // Align depth coordinates to RGB camera
    depth->setDepthAlign(dai::CameraBoardSocket::CAM_A);

    // Make aligned depth match our RGB preview
    depth->setOutputSize(640, 480);

    // Stereo cameras -> depth
    monoLeft->out.link(depth->left);
    monoRight->out.link(depth->right);

    // ---------------------------------------------------------
    // 6. Send RGB + Depth to Raspberry Pi
    // ---------------------------------------------------------
    camRgb->preview.link(xoutRgb->input);
    depth->depth.link(xoutDepth->input);

    // ---------------------------------------------------------
    // 7. Start device
    // ---------------------------------------------------------
    dai::Device device(pipeline);

    auto qRgb = device.getOutputQueue(
        "rgb",
        4,
        false
    );

    auto qDepth = device.getOutputQueue(
        "depth",
        4,
        false
    );

    std::cout << "OAK-D Lite running with DepthAI v2.32.0..."
              << std::endl;

    // ---------------------------------------------------------
    // 8. Processing loop
    // ---------------------------------------------------------
    while(true) {

        auto inRgb = qRgb->get<dai::ImgFrame>();
        auto inDepth = qDepth->get<dai::ImgFrame>();

        cv::Mat frame = inRgb->getCvFrame();

        // 16-bit depth, millimeters
        cv::Mat depthFrame = inDepth->getFrame();

        int center_x = depthFrame.cols / 2;
        int center_y = depthFrame.rows / 2;

        // -----------------------------------------------------
        // Sample center 10x10 region
        // -----------------------------------------------------
        int roiSize = 10;

        cv::Rect roi(
            center_x - roiSize / 2,
            center_y - roiSize / 2,
            roiSize,
            roiSize
        );

        cv::Mat subDepth = depthFrame(roi);

        // -----------------------------------------------------
        // Get median valid depth
        // -----------------------------------------------------
        std::vector<uint16_t> validDepths;

        for(int y = 0; y < subDepth.rows; y++) {
            for(int x = 0; x < subDepth.cols; x++) {

                uint16_t d = subDepth.at<uint16_t>(y, x);

                // 0 = invalid depth
                if(d > 0) {
                    validDepths.push_back(d);
                }
            }
        }

        int distance_cm = 0;

        if(!validDepths.empty()) {

            std::sort(validDepths.begin(), validDepths.end());

            uint16_t median_mm =
                validDepths[validDepths.size() / 2];

            distance_cm =
                static_cast<int>(median_mm / 10);
        }

        // -----------------------------------------------------
        // 9. Send data to VEX
        // -----------------------------------------------------
        std::string payload =
            std::to_string(distance_cm) + ",0\n";

        if(vex_port >= 0) {

            ssize_t bytesWritten =
                write(
                    vex_port,
                    payload.c_str(),
                    payload.length()
                );

            if(bytesWritten < 0) {
                std::cerr << "Serial write failed."
                          << std::endl;
            }
        }

        // -----------------------------------------------------
        // 10. Display targeting overlay
        // -----------------------------------------------------
        int frameCenterX = frame.cols / 2;
        int frameCenterY = frame.rows / 2;

        cv::circle(
            frame,
            cv::Point(frameCenterX, frameCenterY),
            5,
            cv::Scalar(0, 255, 0),
            -1
        );

        cv::putText(
            frame,
            "Dist: " + std::to_string(distance_cm) + " cm",
            cv::Point(frameCenterX + 10, frameCenterY),
            cv::FONT_HERSHEY_SIMPLEX,
            0.6,
            cv::Scalar(0, 255, 0),
            2
        );

        cv::imshow("OAK-D Lite C++ Feed", frame);

        if(cv::waitKey(1) == 'q') {
            break;
        }
    }

    // ---------------------------------------------------------
    // Cleanup
    // ---------------------------------------------------------
    if(vex_port >= 0) {
        close(vex_port);
    }

    cv::destroyAllWindows();

    return 0;
}