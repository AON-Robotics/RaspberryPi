#pragma once

#include <memory>

#include <depthai/depthai.hpp>
#include <opencv2/core.hpp>

namespace vexpi
{

struct OakConfig
{
    int fps = 15;

    // The RGB preview and the aligned depth output must be the same size AND
    // share the RGB sensor's 16:9 aspect ratio (it runs at 1080p), or the two
    // images will not line up pixel for pixel.
    int width = 640;
    int height = 360;

    // 0..255; lower is stricter, i.e. fewer but more trustworthy depth pixels.
    int confidenceThreshold = 200;

    // Lets depth work closer in (~20 cm instead of ~35 cm). Incompatible with
    // the on-device median filter, which is switched off when this is set.
    bool extendedDisparity = true;

    bool leftRightCheck = true;

    dai::MonoCameraProperties::SensorResolution monoResolution =
        dai::MonoCameraProperties::SensorResolution::THE_480_P;

    dai::node::StereoDepth::PresetMode preset = dai::node::StereoDepth::PresetMode::DEFAULT;
};

// Owns the DepthAI pipeline and device for an OAK-D Lite, and hands back RGB
// and depth frames captured at the same instant.
//
// The frames are paired on-device by a Sync node, so a depth reading always
// belongs to the colour image it is sampled against.
class OakCamera
{
  public:
    // Builds the pipeline and starts the device. Throws std::runtime_error
    // (via the DepthAI API) if no device is available.
    explicit OakCamera(const OakConfig &config = {});
    ~OakCamera();

    OakCamera(const OakCamera &) = delete;
    OakCamera &operator=(const OakCamera &) = delete;

    // Blocks until the next synchronised pair arrives. Returns false if the
    // group was incomplete, in which case the caller should simply retry.
    // `depth` is CV_16UC1 in millimetres; 0 means "no reading".
    bool nextFrames(cv::Mat &rgb, cv::Mat &depth);

    const OakConfig &config() const { return config_; }

  private:
    OakConfig config_;
    dai::Pipeline pipeline_;
    std::unique_ptr<dai::Device> device_;
    std::shared_ptr<dai::DataOutputQueue> queue_;
};

} // namespace vexpi
