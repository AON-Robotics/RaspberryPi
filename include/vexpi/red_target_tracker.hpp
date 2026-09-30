#pragma once

#include <deque>

#include <opencv2/core.hpp>

namespace vexpi
{

struct RedTrackerConfig
{
    // Depth readings outside this band are discarded outright.
    int minValidDepthMm = 150;  // ~6 in, reachable with extended disparity on
    int maxValidDepthMm = 1820; // 71 in

    int historySize = 5;     // Rolling median window over accepted readings
    int minDepthPixels = 8;  // Fewest masked pixels that makes a reading usable
    int maxJumpMm = 600;     // Largest plausible change (~24 in) between frames
    int outlierStreakToAccept = 3; // Consecutive outliers before believing them

    double minContourArea = 600;    // Tuned for 640x360
    double maxCenterJumpPx = 120;   // Stay on the same blob within this radius
    int maxLostTolerance = 3;       // Hold the last distance across brief dropouts

    // HSV thresholds. Red wraps around the hue circle, so it takes two bands.
    cv::Scalar lowerRedMin{0, 100, 80};
    cv::Scalar lowerRedMax{10, 255, 255};
    cv::Scalar upperRedMin{170, 100, 80};
    cv::Scalar upperRedMax{179, 255, 255};

    int morphOpenIterations = 2; // Erode/dilate away speckle in the mask
};

enum class TrackState
{
    Track,    // Fresh, accepted reading this frame
    Hold,     // Brief dropout; still reporting the last good distance
    NoDepth,  // Red blob found, but not enough valid depth pixels on it
    NoTarget, // No red blob large enough to be a target
};

const char *toString(TrackState state);

struct TrackResult
{
    TrackState state = TrackState::NoTarget;

    int rawDistanceMm = 0;      // This frame's spatial median; 0 if none
    int filteredDistanceMm = 0; // Temporal median actually reported; 0 if none

    bool hasTarget = false; // A red blob was selected this frame
    cv::Point2f center;     // Centroid of that blob
    cv::Rect box;           // Its bounding box

    bool hasDistance() const { return filteredDistanceMm > 0; }
};

// Finds the red target in an RGB frame, reads its distance out of the aligned
// depth frame, and smooths that distance over time.
//
// Two things make the output steady enough to drive on: the distance is the
// median of the depth pixels that fall inside the mask (not a single centre
// pixel), and the blob chosen each frame is the one nearest last frame's
// target, so a second red object entering the view does not steal the lock.
class RedTargetTracker
{
  public:
    explicit RedTargetTracker(RedTrackerConfig config = {});

    // `bgr` and `depth` must be the same size, with `depth` CV_16UC1 in mm.
    TrackResult update(const cv::Mat &bgr, const cv::Mat &depth);

    // The red mask from the most recent update(), for overlays and tuning.
    const cv::Mat &mask() const { return mask_; }

    // Forgets the lock and the distance history.
    void reset();

    const RedTrackerConfig &config() const { return config_; }

  private:
    void buildMask(const cv::Mat &bgr);
    int selectTarget(const std::vector<std::vector<cv::Point>> &contours) const;
    bool sampleDepth(const cv::Mat &depth, const cv::Rect &box, int &medianMm) const;
    bool acceptReading(int rawMm);

    RedTrackerConfig config_;

    cv::Mat hsv_;
    cv::Mat mask_;
    cv::Mat lowBand_;
    cv::Mat highBand_;

    std::deque<int> distanceHistory_;
    int outlierStreak_ = 0;
    int lostFrames_ = 0;
    int lastDistanceMm_ = 0;
    bool haveLastCenter_ = false;
    cv::Point2f lastCenter_;
};

} // namespace vexpi
