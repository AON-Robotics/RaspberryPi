#include "vexpi/red_target_tracker.hpp"

#include <algorithm>
#include <cstdlib>

#include <opencv2/imgproc.hpp>

namespace vexpi
{
namespace
{

// Takes the deque by value: sorting a copy leaves the history in arrival order.
int medianOf(std::deque<int> values)
{
    std::sort(values.begin(), values.end());
    return values[values.size() / 2];
}

} // namespace

const char *toString(TrackState state)
{
    switch (state)
    {
    case TrackState::Track:
        return "TRACK";
    case TrackState::Hold:
        return "HOLD";
    case TrackState::NoDepth:
        return "NO DEPTH";
    case TrackState::NoTarget:
        return "NO TARGET";
    }
    return "UNKNOWN";
}

RedTargetTracker::RedTargetTracker(RedTrackerConfig config) : config_(std::move(config)) {}

void RedTargetTracker::reset()
{
    distanceHistory_.clear();
    outlierStreak_ = 0;
    lostFrames_ = 0;
    lastDistanceMm_ = 0;
    haveLastCenter_ = false;
}

void RedTargetTracker::buildMask(const cv::Mat &bgr)
{
    cv::cvtColor(bgr, hsv_, cv::COLOR_BGR2HSV);

    cv::inRange(hsv_, config_.lowerRedMin, config_.lowerRedMax, lowBand_);
    cv::inRange(hsv_, config_.upperRedMin, config_.upperRedMax, highBand_);
    mask_ = lowBand_ | highBand_;

    cv::morphologyEx(mask_, mask_, cv::MORPH_OPEN, cv::Mat(), cv::Point(-1, -1),
                     config_.morphOpenIterations);
}

// Prefer the blob nearest last frame's target, so the lock survives a second
// red object appearing; fall back to the largest blob when there is no lock.
int RedTargetTracker::selectTarget(const std::vector<std::vector<cv::Point>> &contours) const
{
    int largestIdx = -1;
    double largestArea = 0;
    int nearestIdx = -1;
    double nearestDist = config_.maxCenterJumpPx;

    for (int i = 0; i < static_cast<int>(contours.size()); i++)
    {
        const double area = cv::contourArea(contours[i]);
        if (area < config_.minContourArea)
            continue;

        if (area > largestArea)
        {
            largestArea = area;
            largestIdx = i;
        }

        if (haveLastCenter_)
        {
            const cv::Moments m = cv::moments(contours[i]);
            const cv::Point2f c(m.m10 / m.m00, m.m01 / m.m00);
            const double dist = cv::norm(c - lastCenter_);
            if (dist < nearestDist)
            {
                nearestDist = dist;
                nearestIdx = i;
            }
        }
    }

    return (nearestIdx >= 0) ? nearestIdx : largestIdx;
}

// Sample only masked pixels inside the middle half of the bounding box: the
// edges of a blob straddle the background, where depth belongs to whatever is
// behind the target.
bool RedTargetTracker::sampleDepth(const cv::Mat &depth, const cv::Rect &box, int &medianMm) const
{
    const int marginX = box.width / 4;
    const int marginY = box.height / 4;
    const int leftX = std::max(0, box.x + marginX);
    const int rightX = std::min(depth.cols, box.x + box.width - marginX);
    const int topY = std::max(0, box.y + marginY);
    const int bottomY = std::min(depth.rows, box.y + box.height - marginY);

    std::vector<uint16_t> validDepths;
    for (int y = topY; y < bottomY; y++)
    {
        for (int x = leftX; x < rightX; x++)
        {
            if (mask_.at<uint8_t>(y, x) == 0)
                continue;

            const uint16_t d = depth.at<uint16_t>(y, x);
            if (d >= config_.minValidDepthMm && d <= config_.maxValidDepthMm)
                validDepths.push_back(d);
        }
    }

    if (validDepths.size() < static_cast<size_t>(config_.minDepthPixels))
        return false;

    std::sort(validDepths.begin(), validDepths.end());
    medianMm = validDepths[validDepths.size() / 2];
    return true;
}

// Reject a sudden spike unless it repeats: a genuine move that large shows up
// on consecutive frames, whereas a bad depth patch does not.
bool RedTargetTracker::acceptReading(int rawMm)
{
    if (distanceHistory_.empty())
    {
        outlierStreak_ = 0;
        distanceHistory_.push_back(rawMm);
        return true;
    }

    if (std::abs(rawMm - medianOf(distanceHistory_)) > config_.maxJumpMm)
    {
        outlierStreak_++;
        if (outlierStreak_ >= config_.outlierStreakToAccept)
        {
            outlierStreak_ = 0;
            distanceHistory_.clear();
            distanceHistory_.push_back(rawMm);
            return true;
        }
        return false;
    }

    outlierStreak_ = 0;
    distanceHistory_.push_back(rawMm);
    if (static_cast<int>(distanceHistory_.size()) > config_.historySize)
        distanceHistory_.pop_front();
    return true;
}

TrackResult RedTargetTracker::update(const cv::Mat &bgr, const cv::Mat &depth)
{
    TrackResult result;

    buildMask(bgr);

    std::vector<std::vector<cv::Point>> contours;
    cv::findContours(mask_, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);

    const int targetIdx = selectTarget(contours);
    bool gotReading = false;

    if (targetIdx >= 0)
    {
        const auto &target = contours[targetIdx];
        const cv::Moments m = cv::moments(target);

        result.hasTarget = true;
        result.box = cv::boundingRect(target);
        result.center = cv::Point2f(m.m10 / m.m00, m.m01 / m.m00);

        lastCenter_ = result.center;
        haveLastCenter_ = true;

        int rawMm = 0;
        if (sampleDepth(depth, result.box, rawMm))
        {
            result.rawDistanceMm = rawMm;
            gotReading = acceptReading(rawMm);
        }
    }

    if (gotReading)
    {
        lostFrames_ = 0;
        lastDistanceMm_ = medianOf(distanceHistory_);
        result.state = TrackState::Track;
    }
    else
    {
        lostFrames_++;
        if (lostFrames_ >= config_.maxLostTolerance || lastDistanceMm_ == 0)
        {
            distanceHistory_.clear();
            outlierStreak_ = 0;
            lastDistanceMm_ = 0;
            haveLastCenter_ = false;
            result.state = result.hasTarget ? TrackState::NoDepth : TrackState::NoTarget;
        }
        else
        {
            result.state = TrackState::Hold;
        }
    }

    result.filteredDistanceMm = lastDistanceMm_;
    return result;
}

} // namespace vexpi
