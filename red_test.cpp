#include <iostream>
#include <iomanip>
#include <cmath>
#include <chrono>
#include <thread>
#include <string>
#include <vector>
#include <deque>
#include <algorithm>
#include <fcntl.h>
#include <unistd.h>
#include <termios.h>

#include <opencv2/opencv.hpp>
#include <depthai/depthai.hpp>

bool configureSerial(int fd) {
    termios tty{};
    if (tcgetattr(fd, &tty) != 0) {
        return false;
    }

    cfsetospeed(&tty, B115200);
    cfsetispeed(&tty, B115200);

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

    return tcsetattr(fd, TCSANOW, &tty) == 0;
}

int medianOf(std::deque<int> values) {
    std::sort(values.begin(), values.end());
    return values[values.size() / 2];
}

int main(int argc, char** argv) {
    constexpr int FPS = 15;
    // RGB preview and depth output MUST have the same size and the same 16:9
    // aspect ratio as the RGB sensor (1080p), otherwise pixels don't line up.
    constexpr int FRAME_W = 640;
    constexpr int FRAME_H = 360;

    constexpr int MIN_VALID_DEPTH = 150;    // ~6 inches (extended disparity enabled)
    constexpr int MAX_VALID_DEPTH = 1820;   // 71 inches (1820 mm)
    constexpr int HISTORY_SIZE = 5;         // Median of last 5 readings
    constexpr int MIN_DEPTH_PIXELS = 8;
    constexpr int MAX_JUMP_MM = 600;        // Max plausible change (~24 in) per frame
    constexpr int CONFIDENCE_THRESHOLD = 200; // 0..255, lower = stricter (fewer bad depth pixels)
    constexpr double MIN_CONTOUR_AREA = 600; // Scaled for 640x360
    constexpr double MAX_CENTER_JUMP_PX = 120; // Stay on the same blob if it's within this distance
    constexpr int MAX_LOST_TOLERANCE = 3;   // Hold last distance across 1-2 frame dropouts
    constexpr int LOOP_DELAY_MS = 100;      // Pause after each loop (~10 updates/sec)

    std::deque<int> distanceHistory;
    int outlierStreak = 0;
    int lostFrames = 0;
    int lastDistanceMm = 0;                 // Last distance actually sent, held during dropouts
    bool haveLastCenter = false;
    cv::Point2f lastCenter;

    std::string serialPort = "/dev/ttyACM0";
    if (argc > 1) {
        serialPort = argv[1];
    }

    int vexPort = open(serialPort.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
    if (vexPort >= 0) {
        if (!configureSerial(vexPort)) {
            std::cerr << "Warning: Could not configure serial port.\n";
        }
    } else {
        std::cerr << "Warning: Could not open serial port " << serialPort << "\n";
    }

    dai::Pipeline pipeline;

    auto rgb = pipeline.create<dai::node::ColorCamera>();
    auto left = pipeline.create<dai::node::MonoCamera>();
    auto right = pipeline.create<dai::node::MonoCamera>();
    auto stereo = pipeline.create<dai::node::StereoDepth>();
    auto sync = pipeline.create<dai::node::Sync>();
    auto syncOut = pipeline.create<dai::node::XLinkOut>();

    syncOut->setStreamName("sync");

    rgb->setBoardSocket(dai::CameraBoardSocket::CAM_A);
    rgb->setResolution(dai::ColorCameraProperties::SensorResolution::THE_1080_P);
    rgb->setPreviewSize(FRAME_W, FRAME_H);
    rgb->setFps(FPS);
    rgb->setInterleaved(false);
    rgb->setColorOrder(dai::ColorCameraProperties::ColorOrder::BGR);

    left->setBoardSocket(dai::CameraBoardSocket::CAM_B);
    left->setResolution(dai::MonoCameraProperties::SensorResolution::THE_480_P);
    left->setFps(FPS);

    right->setBoardSocket(dai::CameraBoardSocket::CAM_C);
    right->setResolution(dai::MonoCameraProperties::SensorResolution::THE_480_P);
    right->setFps(FPS);

    // Preset first - it overwrites the config, so the overrides below must come after it
    stereo->setDefaultProfilePreset(dai::node::StereoDepth::PresetMode::DEFAULT);
    stereo->setLeftRightCheck(true);
    stereo->setExtendedDisparity(true);     // Lets depth work closer (~20 cm instead of ~35 cm)
    stereo->setDepthAlign(dai::CameraBoardSocket::CAM_A);
    stereo->setOutputSize(FRAME_W, FRAME_H);
    // The on-device median filter can't run with extended disparity + subpixel,
    // so turn it off explicitly. The median over mask pixels and history below covers it.
    stereo->initialConfig.setMedianFilter(dai::MedianFilter::MEDIAN_OFF);
    stereo->initialConfig.setConfidenceThreshold(CONFIDENCE_THRESHOLD);

    left->out.link(stereo->left);
    right->out.link(stereo->right);

    // Pair each RGB frame with the depth frame captured at the same moment
    sync->setSyncThreshold(std::chrono::milliseconds(1000 / (2 * FPS)));
    rgb->preview.link(sync->inputs["rgb"]);
    stereo->depth.link(sync->inputs["depth"]);
    sync->out.link(syncOut->input);

    try {
        dai::Device device(pipeline, dai::UsbSpeed::SUPER);

        auto qSync = device.getOutputQueue("sync", 1, false);

        while (true) {
            try {
                auto group = qSync->get<dai::MessageGroup>();
                auto rgbFrame = group->get<dai::ImgFrame>("rgb");
                auto depthFrameData = group->get<dai::ImgFrame>("depth");
                if (!rgbFrame || !depthFrameData) {
                    continue;
                }

                cv::Mat frame = rgbFrame->getCvFrame();
                cv::Mat depthFrame = depthFrameData->getFrame();

                if (frame.size() != depthFrame.size()) {
                    std::cerr << "RGB " << frame.cols << "x" << frame.rows
                              << " and depth " << depthFrame.cols << "x" << depthFrame.rows
                              << " sizes differ; skipping frame\n";
                    continue;
                }

                cv::Mat hsv;
                cv::cvtColor(frame, hsv, cv::COLOR_BGR2HSV);

                cv::Mat redLow, redHigh, redMask;
                cv::inRange(hsv, cv::Scalar(0, 100, 80), cv::Scalar(10, 255, 255), redLow);
                cv::inRange(hsv, cv::Scalar(170, 100, 80), cv::Scalar(179, 255, 255), redHigh);
                redMask = redLow | redHigh;

                cv::morphologyEx(redMask, redMask, cv::MORPH_OPEN, cv::Mat(), cv::Point(-1, -1), 2);

                std::vector<std::vector<cv::Point>> contours;
                cv::findContours(redMask, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);

                // Pick the target: stay on the blob nearest last frame's target,
                // otherwise take the largest one.
                int bestIdx = -1;
                double bestArea = 0;
                int nearestIdx = -1;
                double nearestDist = MAX_CENTER_JUMP_PX;

                for (int i = 0; i < static_cast<int>(contours.size()); i++) {
                    double area = cv::contourArea(contours[i]);
                    if (area < MIN_CONTOUR_AREA) {
                        continue;
                    }
                    if (area > bestArea) {
                        bestArea = area;
                        bestIdx = i;
                    }
                    if (haveLastCenter) {
                        cv::Moments m = cv::moments(contours[i]);
                        cv::Point2f c(m.m10 / m.m00, m.m01 / m.m00);
                        double dist = cv::norm(c - lastCenter);
                        if (dist < nearestDist) {
                            nearestDist = dist;
                            nearestIdx = i;
                        }
                    }
                }

                int targetIdx = (nearestIdx >= 0) ? nearestIdx : bestIdx;

                bool redDetected = false;
                int rawDistanceMm = 0;
                bool gotReading = false;

                if (targetIdx >= 0) {
                    const auto& target = contours[targetIdx];
                    cv::Rect box = cv::boundingRect(target);
                    cv::Moments m = cv::moments(target);
                    lastCenter = cv::Point2f(m.m10 / m.m00, m.m01 / m.m00);
                    haveLastCenter = true;
                    redDetected = true;

                    int marginX = box.width / 4;
                    int marginY = box.height / 4;
                    int leftX = std::max(0, box.x + marginX);
                    int rightX = std::min(depthFrame.cols, box.x + box.width - marginX);
                    int topY = std::max(0, box.y + marginY);
                    int bottomY = std::min(depthFrame.rows, box.y + box.height - marginY);

                    std::vector<uint16_t> validDepths;
                    for (int y = topY; y < bottomY; y++) {
                        for (int x = leftX; x < rightX; x++) {
                            if (redMask.at<uint8_t>(y, x) == 0) {
                                continue;
                            }

                            uint16_t d = depthFrame.at<uint16_t>(y, x);
                            if (d >= MIN_VALID_DEPTH && d <= MAX_VALID_DEPTH) {
                                validDepths.push_back(d);
                            }
                        }
                    }

                    if (validDepths.size() >= MIN_DEPTH_PIXELS) {
                        std::sort(validDepths.begin(), validDepths.end());
                        rawDistanceMm = validDepths[validDepths.size() / 2];

                        if (distanceHistory.empty()) {
                            outlierStreak = 0;
                            distanceHistory.push_back(rawDistanceMm);
                            gotReading = true;
                        } else if (std::abs(rawDistanceMm - medianOf(distanceHistory)) > MAX_JUMP_MM) {
                            // Reject sudden single-frame spikes unless they persist
                            outlierStreak++;
                            if (outlierStreak >= 3) {
                                outlierStreak = 0;
                                distanceHistory.clear();
                                distanceHistory.push_back(rawDistanceMm);
                                gotReading = true;
                            }
                        } else {
                            outlierStreak = 0;
                            distanceHistory.push_back(rawDistanceMm);
                            if (distanceHistory.size() > HISTORY_SIZE) {
                                distanceHistory.pop_front();
                            }
                            gotReading = true;
                        }
                    }
                }

                std::string status;
                if (gotReading) {
                    lostFrames = 0;
                    lastDistanceMm = medianOf(distanceHistory);
                    status = "TRACK";
                } else {
                    lostFrames++;
                    if (lostFrames >= MAX_LOST_TOLERANCE || lastDistanceMm == 0) {
                        distanceHistory.clear();
                        outlierStreak = 0;
                        lastDistanceMm = 0;
                        haveLastCenter = false;
                        status = redDetected ? "NO DEPTH" : "NO TARGET";
                    } else {
                        status = "HOLD";    // Brief dropout: keep sending last distance
                    }
                }

                // Format packet in integer inches
                std::string payload;
                if (lastDistanceMm > 0) {
                    int distanceInches = static_cast<int>(std::round(lastDistanceMm / 25.4));
                    payload = "R," + std::to_string(distanceInches) + "\n";
                } else {
                    payload = "N,0\n";
                }

                // Send packet to VEX
                if (vexPort >= 0) {
                    ssize_t written = write(vexPort, payload.c_str(), payload.length());
                    if (written < 0) {
                        std::cerr << "Serial write error\n";
                    }
                }

                std::cout << std::fixed << std::setprecision(1) << std::left << std::setw(9) << status;
                if (rawDistanceMm > 0) {
                    std::cout << " | RAW: " << rawDistanceMm / 25.4 << " in (" << rawDistanceMm << " mm)";
                }
                if (lastDistanceMm > 0) {
                    std::cout << " | FILTERED: " << lastDistanceMm / 25.4 << " in (" << lastDistanceMm << " mm)";
                }
                std::cout << " | PACKET: " << payload << std::flush;

            } catch (const std::exception& e) {
                std::cerr << "Device communication error: " << e.what() << "\n";
                break;
            }
        }
    } catch (const std::exception& e) {
        std::cerr << "Failed to start OAK-D Lite: " << e.what() << "\n";
    }

    if (vexPort >= 0) {
        close(vexPort);
    }

    return 0;
}
