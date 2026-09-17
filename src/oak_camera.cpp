#include "vexpi/oak_camera.hpp"

#include <chrono>

namespace vexpi
{

OakCamera::OakCamera(const OakConfig &config) : config_(config)
{
    auto rgb = pipeline_.create<dai::node::ColorCamera>();
    auto left = pipeline_.create<dai::node::MonoCamera>();
    auto right = pipeline_.create<dai::node::MonoCamera>();
    auto stereo = pipeline_.create<dai::node::StereoDepth>();
    auto sync = pipeline_.create<dai::node::Sync>();
    auto syncOut = pipeline_.create<dai::node::XLinkOut>();

    syncOut->setStreamName("sync");

    rgb->setBoardSocket(dai::CameraBoardSocket::CAM_A);
    rgb->setResolution(dai::ColorCameraProperties::SensorResolution::THE_1080_P);
    rgb->setPreviewSize(config_.width, config_.height);
    rgb->setFps(static_cast<float>(config_.fps));
    rgb->setInterleaved(false);
    rgb->setColorOrder(dai::ColorCameraProperties::ColorOrder::BGR);

    left->setBoardSocket(dai::CameraBoardSocket::CAM_B);
    left->setResolution(config_.monoResolution);
    left->setFps(static_cast<float>(config_.fps));

    right->setBoardSocket(dai::CameraBoardSocket::CAM_C);
    right->setResolution(config_.monoResolution);
    right->setFps(static_cast<float>(config_.fps));

    // The preset overwrites the whole config, so every override must come after it.
    stereo->setDefaultProfilePreset(config_.preset);
    stereo->setLeftRightCheck(config_.leftRightCheck);
    stereo->setExtendedDisparity(config_.extendedDisparity);
    stereo->setDepthAlign(dai::CameraBoardSocket::CAM_A);
    stereo->setOutputSize(config_.width, config_.height);

    // The on-device median filter cannot run alongside extended disparity, so
    // turn it off explicitly; RedTargetTracker's spatial and temporal medians
    // cover the same ground on the host.
    if (config_.extendedDisparity)
        stereo->initialConfig.setMedianFilter(dai::MedianFilter::MEDIAN_OFF);

    stereo->initialConfig.setConfidenceThreshold(config_.confidenceThreshold);

    left->out.link(stereo->left);
    right->out.link(stereo->right);

    // Pair each RGB frame with the depth frame captured at the same moment.
    sync->setSyncThreshold(std::chrono::milliseconds(1000 / (2 * config_.fps)));
    rgb->preview.link(sync->inputs["rgb"]);
    stereo->depth.link(sync->inputs["depth"]);
    sync->out.link(syncOut->input);

    device_ = std::make_unique<dai::Device>(pipeline_, dai::UsbSpeed::SUPER);
    queue_ = device_->getOutputQueue("sync", 1, false);
}

OakCamera::~OakCamera() = default;

bool OakCamera::nextFrames(cv::Mat &rgb, cv::Mat &depth)
{
    auto group = queue_->get<dai::MessageGroup>();
    if (!group)
        return false;

    auto rgbFrame = group->get<dai::ImgFrame>("rgb");
    auto depthFrame = group->get<dai::ImgFrame>("depth");
    if (!rgbFrame || !depthFrame)
        return false;

    rgb = rgbFrame->getCvFrame();
    depth = depthFrame->getFrame();
    return true;
}

} // namespace vexpi
