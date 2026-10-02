// brain_sim: stands in for the V5 brain so the whole LLM -> server -> serial
// -> brain pipeline runs on a laptop.
//
// It links Override's real protocol code (Override/src/aon/pi/protocol.cpp and
// commands.cpp), so the parser and command set under test are the ones that
// ship to the robot. Only `Robot` is simulated: a kinematic differential drive
// with the small robot's ports and constants (Override globals.hpp /
// constants.hpp), tracking wheels, an IMU and Override-style odometry.
//
// The "serial port" is a pseudo-terminal. The server opens it exactly like
// /dev/ttyACM1, so pyserial, framing and timing are exercised for real.
//
//   brain_sim --link /tmp/brain.tty [--mode driver|disabled|autonomous]
//             [--fault NAME[,NAME...]] [--noise] [--verbose]
//   brain_sim --checksum "C,1,PING"     print the framed line and exit
//
// Faults: reversed_left_tracking, no_imu, dead_motor:<port>,
//         reversed_motor:<port>, hot_motor:<port>
// Signals: SIGUSR1 freezes/unfreezes the "brain program" (no reads, no
//          writes: a hung program); SIGUSR2 simulates the driver touching
//          a joystick (aborts a Pi motion like checkDriverOverride()).

#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <mutex>
#include <poll.h>
#include <set>
#include <sstream>
#include <string>
#include <termios.h>
#include <thread>
#include <unistd.h>
#include <vector>

#if defined(__APPLE__)
#include <util.h>
#else
#include <pty.h>
#endif

#include "aon/pi/commands.hpp"

using namespace aon::pi;

namespace {

// --- Robot constants (Override constants.hpp, small robot) --------------------
constexpr double MAX_RPM = 600;
constexpr double MOTOR_TO_DRIVE_RATIO = 0.75;
constexpr double DRIVE_WHEEL_DIAMETER = 2.75;
constexpr double DRIVE_WIDTH = 12.5;
constexpr double TRACKING_WHEEL_DIAMETER = 2;
constexpr double OFFSET_LEFT = 1.125;   // DISTANCE_LEFT_TRACKING_WHEEL_CENTER
constexpr double OFFSET_RIGHT = 1.125;  // DISTANCE_RIGHT_TRACKING_WHEEL_CENTER
constexpr double MAX_ACCEL = 2500;      // rpm/s
const std::vector<int> LEFT_PORTS = {11, -12, 13, -14};  // globals.hpp DifferentialDrive
const std::vector<int> RIGHT_PORTS = {1, -2, 3, -4};
constexpr int TRACK_LEFT_PORT = 19, TRACK_RIGHT_PORT = 18, TRACK_BACK_PORT = 5, IMU_PORT = 16;

std::uint32_t millis() {
  static const auto start = std::chrono::steady_clock::now();
  return static_cast<std::uint32_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start).count());
}

void sleepMs(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }

double rpmToInchesPerSec(double rpm) { return rpm * MOTOR_TO_DRIVE_RATIO * M_PI * DRIVE_WHEEL_DIAMETER / 60.0; }

struct Faults {
  bool reversedLeftTracking = false;
  bool noImu = false;
  std::set<int> deadMotors, reversedMotors, hotMotors;
} faults;

std::atomic<bool> frozen{false};
std::atomic<bool> verbose{false};
std::atomic<long> driverLoops{0};
std::string mode = "driver";

// --- World: physics + sensors + Override-style odometry ------------------------
class World {
 public:
  void tank(double left, double right) {
    std::lock_guard<std::mutex> g(m);
    cmdL = std::max(-MAX_RPM, std::min(MAX_RPM, left));
    cmdR = std::max(-MAX_RPM, std::min(MAX_RPM, right));
  }

  /// How much of the commanded speed a side delivers: dead motors add no
  /// torque, a reversed motor fights the others.
  static double sideFactor(const std::vector<int> &ports) {
    double sum = 0;
    for (int p : ports) {
      const int port = std::abs(p);
      if (faults.deadMotors.count(port)) continue;
      sum += faults.reversedMotors.count(port) ? -1 : 1;
    }
    return std::max(0.0, sum / ports.size());
  }

  void step(double dt) {
    std::lock_guard<std::mutex> g(m);
    const double maxDelta = MAX_ACCEL * dt;
    velL += std::max(-maxDelta, std::min(maxDelta, cmdL * sideFactor(LEFT_PORTS) - velL));
    velR += std::max(-maxDelta, std::min(maxDelta, cmdR * sideFactor(RIGHT_PORTS) - velR));

    const double vL = rpmToInchesPerSec(velL), vR = rpmToInchesPerSec(velR);
    const double v = (vL + vR) / 2;
    const double omega = (vL - vR) / DRIVE_WIDTH;  // rad/s, clockwise positive

    // Truth (same frame as Override odometry: x along heading 0, theta clockwise)
    const double mid = truthTheta + omega * dt / 2;
    truthX += v * dt * std::cos(mid);
    truthY += v * dt * std::sin(mid);
    truthTheta += omega * dt;

    // Sensors
    trkL += (v + omega * OFFSET_LEFT) * dt * (faults.reversedLeftTracking ? -1 : 1);
    trkR += (v - omega * OFFSET_RIGHT) * dt;
    imu += omega * dt * 180.0 / M_PI;

    // Odometry, as Override::Odometry::update() does it: heading from the
    // IMU (encoders if there is none), distance from the tracking wheels.
    const double dL = trkL - prevL, dR = trkR - prevR;
    prevL = trkL;
    prevR = trkR;
    const double dTheta = faults.noImu ? (dL - dR) / (OFFSET_LEFT + OFFSET_RIGHT) : (imu - prevImu) * M_PI / 180.0;
    prevImu = imu;
    const double d = (dL + dR) / 2;
    const double odomMid = odomTheta + dTheta / 2;
    odomX += d * std::cos(odomMid);
    odomY += d * std::sin(odomMid);
    odomTheta += dTheta;
  }

  RobotPose odom() {
    std::lock_guard<std::mutex> g(m);
    double deg = std::fmod(odomTheta * 180.0 / M_PI, 360.0);
    if (deg > 180) deg -= 360;
    if (deg <= -180) deg += 360;
    return {odomX, odomY, deg};
  }
  RobotPose truth() {
    std::lock_guard<std::mutex> g(m);
    return {truthX, truthY, truthTheta * 180.0 / M_PI};
  }
  void tracking(double &l, double &r, double &b) {
    std::lock_guard<std::mutex> g(m);
    l = trkL;
    r = trkR;
    b = 0;
  }
  double imuRotation() {
    std::lock_guard<std::mutex> g(m);
    return faults.noImu ? NAN : imu;
  }
  double sideRpm(char side) {
    std::lock_guard<std::mutex> g(m);
    return side == 'L' ? velL : velR;
  }
  void resetOdom(double x, double y, double thetaDeg) {
    std::lock_guard<std::mutex> g(m);
    odomX = x;
    odomY = y;
    odomTheta = thetaDeg * M_PI / 180.0;
  }

 private:
  std::mutex m;
  double cmdL = 0, cmdR = 0, velL = 0, velR = 0;
  double truthX = 0, truthY = 0, truthTheta = 0;
  double trkL = 0, trkR = 0, imu = 0;
  double prevL = 0, prevR = 0, prevImu = 0;
  double odomX = 0, odomY = 0, odomTheta = 0;
} world;

/// Reported velocity of one motor: motors on a side share the shaft, so a
/// motor wired backwards reports the opposite sign.
double motorRpm(int port, char side) {
  if (faults.deadMotors.count(port)) return NAN;
  const double v = world.sideRpm(side);
  return faults.reversedMotors.count(port) ? -v : v;
}

// --- Robot implementation --------------------------------------------------------
class SimRobot : public Robot {
 public:
  std::string name() override { return "sim_small_robot"; }
  double maxRpm() override { return MAX_RPM; }
  std::string mode() override { return ::mode; }

  bool motionAllowed(std::string &why) override {
    if (::mode == "disabled") {
      why = "robot is disabled; enable it from the field controller or competition switch";
      return false;
    }
    if (::mode == "autonomous") {
      why = "autonomous is running; the Pi can only move the robot during driver control";
      return false;
    }
    return true;
  }

  RobotPose pose() override { return world.odom(); }
  double batteryPct() override { return 87; }
  void tracking(double &l, double &r, double &b) override { world.tracking(l, r, b); }
  double imuRotation() override { return world.imuRotation(); }

  // Same shape as Override's driveProfiled(): progress from odometry,
  // timeout distance/3 s, stop at the end.
  void move(double inches, double rpm) override {
    const double sign = inches < 0 ? -1 : 1;
    const double target = std::fabs(inches);
    const RobotPose start = pose();
    const std::uint32_t timeoutMs = static_cast<std::uint32_t>(target / 3.0 * 1000);
    const std::uint32_t t0 = millis();
    while (!aborted && millis() - t0 < timeoutMs) {
      const RobotPose p = pose();
      const double traveled = std::hypot(p.x - start.x, p.y - start.y);
      const double remaining = target - traveled;
      if (remaining <= 0) break;
      // Slow down near the target (decel ~ 40 in/s^2).
      const double vMax = std::sqrt(2 * 40.0 * remaining) / rpmToInchesPerSec(1);
      const double cmd = std::max(20.0, std::min(rpm, vMax));
      world.tank(sign * cmd, sign * cmd);
      sleepMs(10);
    }
    world.tank(0, 0);
  }

  // Same shape as Override's turnProfiled(): progress from the IMU, timeout
  // sqrt(angle/2) s.
  void turn(double degrees, double rpm) override {
    const double sign = degrees < 0 ? -1 : 1;
    const double target = std::fabs(degrees);
    const double start = headingNow();
    const std::uint32_t timeoutMs = static_cast<std::uint32_t>(std::sqrt(target / 2) * 1000);
    const std::uint32_t t0 = millis();
    while (!aborted && millis() - t0 < timeoutMs) {
      const double turned = std::fabs(headingNow() - start);
      const double remaining = target - turned;
      if (remaining <= 0) break;
      const double cmd = std::max(15.0, std::min(rpm, remaining * 4));
      world.tank(sign * cmd, -sign * cmd);
      sleepMs(10);
    }
    world.tank(0, 0);
  }

  void resetOdometry(double x, double y, double theta) override {
    sleepMs(1000);  // Override waits 3 s for the IMU tare; shorter here
    world.resetOdom(x, y, theta);
  }

  void spinSide(char side, double rpm, int ms, KV &motorPeaks) override {
    const std::vector<int> &ports = side == 'L' ? LEFT_PORTS : RIGHT_PORTS;
    std::vector<double> peaks(ports.size(), 0.0);
    const std::uint32_t t0 = millis();
    while (millis() - t0 < static_cast<std::uint32_t>(ms) && !aborted) {
      world.tank(side == 'L' ? rpm : 0, side == 'R' ? rpm : 0);
      for (std::size_t i = 0; i < ports.size(); i++) {
        const double v = motorRpm(std::abs(ports[i]), side);
        if (std::isfinite(v) && std::fabs(v) > std::fabs(peaks[i])) peaks[i] = v;
      }
      sleepMs(10);
    }
    world.tank(0, 0);
    for (int i = 0; i < 30 && !aborted; i++) sleepMs(10);
    for (std::size_t i = 0; i < ports.size(); i++) {
      // A dead motor reports nothing, like PROS_ERR on the brain.
      if (faults.deadMotors.count(std::abs(ports[i]))) continue;
      motorPeaks.add("p" + std::to_string(std::abs(ports[i])), peaks[i], 0);
    }
  }

  void stopMotors() override { world.tank(0, 0); }
  void requestAbort() override { aborted = true; }
  void clearAbort() override { aborted = false; }

  // Field names must match Override/src/aon/pi/pi-link.cpp.
  void registerSensors(Link &link) override {
    link.registerSensor("odom", [this](KV &kv) {
      const RobotPose p = pose();
      double l, r, b;
      tracking(l, r, b);
      kv.add("x", p.x).add("y", p.y).add("th", p.theta);
      kv.add("trk.left", l).add("trk.right", r).add("trk.back", b);
    });
    link.registerSensor("tracking", [](KV &kv) {
      kv.add("left.installed", true).add("right.installed", true).add("back.installed", true);
      kv.add("left.port", TRACK_LEFT_PORT).add("right.port", TRACK_RIGHT_PORT).add("back.port", TRACK_BACK_PORT);
      kv.add("wheel_diameter", TRACKING_WHEEL_DIAMETER, 3);
      kv.add("offset.left", OFFSET_LEFT, 3).add("offset.right", OFFSET_RIGHT, 3);
    });
    link.registerSensor("imu", [this](KV &kv) {
      kv.add("installed", !faults.noImu).add("port", IMU_PORT).add("calibrating", false);
      double heading = std::fmod(pose().theta + 360.0, 360.0);
      kv.add("heading", faults.noImu ? NAN : heading).add("rotation", imuRotation());
    });
    link.registerSensor("motors", [](KV &kv) {
      for (char side : {'L', 'R'}) {
        for (int p : side == 'L' ? LEFT_PORTS : RIGHT_PORTS) {
          const int port = std::abs(p);
          const std::string key = std::string(1, side) + ".p" + std::to_string(port);
          const bool connected = !faults.deadMotors.count(port);
          kv.add(key + ".connected", connected).add(key + ".reversed", p < 0);
          if (!connected) continue;
          const bool hot = faults.hotMotors.count(port) > 0;
          kv.add(key + ".temp", hot ? 62.0 : 35.0, 0).add(key + ".ma", 120).add(key + ".rpm", motorRpm(port, side), 0);
          kv.add(key + ".mv", 0).add(key + ".over_temp", hot).add(key + ".over_current", false);
        }
      }
    });
    link.registerSensor("battery", [](KV &kv) { kv.add("pct", 87.0, 0).add("mv", 12600).add("ma", 900).add("temp", 30.0, 0); });
    link.registerSensor("competition", [this](KV &kv) { kv.add("mode", mode()).add("connected", false); });
    // Simulator only: ground truth, to compare with odometry in tests.
    link.registerSensor("sim", [](KV &kv) {
      const RobotPose t = world.truth();
      kv.add("truth_x", t.x).add("truth_y", t.y).add("truth_th", t.theta).add("driver_loops", driverLoops.load());
    });
  }

 private:
  std::atomic<bool> aborted{false};

  double headingNow() {
    const double r = world.imuRotation();
    return std::isfinite(r) ? r : world.odom().theta;
  }
};

// --- Serial (pty) -------------------------------------------------------------------
int masterFd = -1;
std::mutex writeMutex;

bool quietLine(const std::string &line) {
  return !verbose && (line.rfind("@H", 0) == 0 || line.find(",PING") != std::string::npos ||
                      line.find(",ok,proto=") != std::string::npos);
}

void writeLine(const std::string &line) {
  if (frozen) return;
  std::lock_guard<std::mutex> g(writeMutex);
  // Like a USB output buffer: wait a little for room, then drop the rest.
  std::size_t sent = 0;
  const std::uint32_t start = millis();
  while (sent < line.size() && millis() - start < 50) {
    const ssize_t n = ::write(masterFd, line.data() + sent, line.size() - sent);
    if (n > 0) sent += static_cast<std::size_t>(n);
    else sleepMs(1);
  }
  if (sent < line.size()) std::printf("[sim] tx DROPPED %zu of %zu bytes (nobody reading)\n", line.size() - sent, line.size());
  if (!quietLine(line)) std::printf("[sim] tx %s", line.c_str());
}

void onSignal(int sig) {
  if (sig == SIGUSR1) frozen = !frozen;
}
std::atomic<bool> driverTouch{false};
void onSignal2(int) { driverTouch = true; }

void parseFaults(const std::string &list) {
  std::stringstream ss(list);
  std::string item;
  while (std::getline(ss, item, ',')) {
    const std::size_t colon = item.find(':');
    const std::string name = item.substr(0, colon);
    const int port = colon == std::string::npos ? 0 : std::atoi(item.c_str() + colon + 1);
    if (name == "reversed_left_tracking") faults.reversedLeftTracking = true;
    else if (name == "no_imu") faults.noImu = true;
    else if (name == "dead_motor") faults.deadMotors.insert(port);
    else if (name == "reversed_motor") faults.reversedMotors.insert(port);
    else if (name == "hot_motor") faults.hotMotors.insert(port);
    else std::fprintf(stderr, "unknown fault %s\n", name.c_str());
  }
}

}  // namespace

int main(int argc, char **argv) {
  std::setvbuf(stdout, nullptr, _IOLBF, 0);
  std::string linkPath;
  bool noise = false;
  for (int i = 1; i < argc; i++) {
    const std::string arg = argv[i];
    if (arg == "--checksum" && i + 1 < argc) {
      std::printf("%s\n", withChecksum(argv[++i]).c_str());
      return 0;
    }
    if (arg == "--link" && i + 1 < argc) linkPath = argv[++i];
    else if (arg == "--mode" && i + 1 < argc) mode = argv[++i];
    else if (arg == "--fault" && i + 1 < argc) parseFaults(argv[++i]);
    else if (arg == "--noise") noise = true;
    else if (arg == "--verbose") verbose = true;
    else {
      std::fprintf(stderr, "usage: brain_sim --link PATH [--mode M] [--fault F] [--noise] [--verbose]\n");
      return 2;
    }
  }

  int slaveFd = -1;
  char slaveName[256] = {0};
  if (openpty(&masterFd, &slaveFd, slaveName, nullptr, nullptr) != 0) {
    std::perror("openpty");
    return 1;
  }
  termios tio{};
  tcgetattr(slaveFd, &tio);
  cfmakeraw(&tio);
  tcsetattr(slaveFd, TCSANOW, &tio);
  // Keep our slave fd open so the master never sees a hangup while the
  // server reconnects. Writes must never block the "brain".
  fcntl(masterFd, F_SETFL, fcntl(masterFd, F_GETFL) | O_NONBLOCK);
  if (!linkPath.empty()) {
    ::unlink(linkPath.c_str());
    if (::symlink(slaveName, linkPath.c_str()) != 0) {
      std::perror("symlink");
      return 1;
    }
  }
  std::signal(SIGUSR1, onSignal);
  std::signal(SIGUSR2, onSignal2);

  SimRobot robot;
  std::mutex linkMutex;
  Link::Hooks hooks;
  hooks.millis = millis;
  hooks.writeLine = writeLine;
  hooks.lock = [&] { linkMutex.lock(); };
  hooks.unlock = [&] { linkMutex.unlock(); };
  hooks.log = [](const std::string &message) { std::printf("[sim] brain log: %s\n", message.c_str()); };
  hooks.abortMotion = [&] {
    robot.requestAbort();
    robot.stopMotors();
  };
  hooks.clearAbort = [&] { robot.clearAbort(); };
  hooks.heartbeatFields = [&](KV &kv) {
    const RobotPose p = robot.pose();
    kv.add("mode", robot.mode()).add("x", p.x).add("y", p.y).add("th", p.theta).add("bat", robot.batteryPct(), 0);
  };
  Link link(hooks);
  registerStandardCommands(link, robot);

  std::printf("PTY %s\n", slaveName);
  std::printf("[sim] brain simulator up: mode=%s link=%s\n", mode.c_str(), linkPath.empty() ? slaveName : linkPath.c_str());

  // Physics at 200 Hz.
  std::thread physics([] {
    while (true) {
      world.step(0.005);
      sleepMs(5);
    }
  });

  // Reader task: bytes from the Pi -> Link::feed.
  std::thread reader([&] {
    std::string line;
    char buffer[256];
    while (true) {
      if (frozen) {
        sleepMs(20);
        continue;
      }
      pollfd pfd{masterFd, POLLIN, 0};
      if (::poll(&pfd, 1, 50) <= 0 || !(pfd.revents & POLLIN)) continue;
      const ssize_t n = ::read(masterFd, buffer, sizeof(buffer));
      if (n <= 0) {
        sleepMs(20);
        continue;
      }
      for (ssize_t i = 0; i < n; i++) {
        const char c = buffer[i];
        if (c == '\n') {
          if (!quietLine(line)) std::printf("[sim] rx %s\n", line.c_str());
          line.clear();
        } else if (c != '\r' && line.size() < 512) {
          line.push_back(c);
        }
        link.feed(c);
      }
    }
  });

  // Worker task: one Pi motion at a time.
  std::thread worker([&] {
    while (true) {
      if (!link.runPendingMotion()) sleepMs(10);
    }
  });

  // Ticker task: heartbeat + deadman.
  std::thread ticker([&] {
    while (true) {
      if (!frozen) link.tick();
      sleepMs(20);
    }
  });

  // "opcontrol": the driver loop runs whenever the Pi has no control. This
  // is what must keep running whatever happens on the Pi side.
  std::thread opcontrol([&] {
    std::uint32_t lastReport = 0, lastNoise = 0;
    int noiseIndex = 0;
    const char *noiseLines[] = {"currentAngleGyro: 0.000000", "[DEBUG] intake scan tick", "[WARN] orbit not configured"};
    while (true) {
      if (driverTouch.exchange(false)) link.abort("driver_override");
      if (!link.hasControl()) driverLoops++;
      const std::uint32_t now = millis();
      if (now - lastReport >= 1000) {
        lastReport = now;
        const RobotPose o = world.odom(), t = world.truth();
        std::printf("[sim] status driver_loops=%ld pi_control=%d frozen=%d odom=(%.2f,%.2f,%.1f) truth=(%.2f,%.2f,%.1f)\n",
                    driverLoops.load(), link.hasControl() ? 1 : 0, frozen ? 1 : 0, o.x, o.y, o.theta, t.x, t.y, t.theta);
      }
      if (noise && now - lastNoise >= 700) {
        lastNoise = now;
        writeLine(std::string(noiseLines[noiseIndex++ % 3]) + "\n");
      }
      sleepMs(10);
    }
  });

  physics.join();
  return 0;
}
