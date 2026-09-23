# RaspberryPi

Raspberry Pi 5 coprocessor software for AON Robotics.

The Pi does the work the V5 brain cannot: stereo depth from an OAK-D Lite and
optical odometry from a SparkFun Qwiic OTOS. It reduces both to small ASCII
packets and streams them to the brain over USB serial, so the auton routine on
the brain stays simple and just reads numbers.

## What is in here

| Program | Does |
| --- | --- |
| `red_tracker` | Finds the nearest red target with the OAK-D Lite and streams its distance to the brain. The main program. |
| `otos_monitor` | Live pose read-out from the OTOS. Use it to check wiring and to measure the drift scalars. |
| `otos_stream` | Calibrates the OTOS at startup, then sends pose to Override at 50 Hz. |
| `depth_center_demo` | Distance to whatever is at the centre of the frame, with a preview window. The fallback for checking the camera itself. |

## Layout

```
include/vexpi/     Public headers
  serial_link.hpp      RAII USB serial link to the V5 brain
  vex_packet.hpp       Wire format builders
  units.hpp            metres/radians <-> inches/degrees
  otos.hpp             SparkFun Qwiic OTOS driver (I2C)
  oak_camera.hpp       OAK-D Lite pipeline, synchronised RGB + depth
  red_target_tracker.hpp   Red blob detection, depth sampling, filtering
src/               Implementations of the above
apps/              One main() per program, each a thin wrapper
docs/              Serial protocol, including the brain-side parser
```

Two libraries get built. `vexpi_core` is the serial link, packet format and
OTOS driver — it depends on nothing but pthreads. `vexpi_vision` adds the
camera and tracking, and pulls in OpenCV and DepthAI. `otos_monitor` links only
the former, so odometry work does not need the camera SDK present.

## Building

Needs CMake 3.20+, a C++17 compiler, OpenCV 4, and
[depthai-core](https://github.com/luxonis/depthai-core) v2 installed where
CMake can find it.

```bash
cmake -S . -B build
cmake --build build -j4
```

Binaries land in `build/`. To build without the camera SDK:

```bash
cmake -S . -B build -DVEXPI_BUILD_VISION=OFF
```

## Running

```bash
./build/red_tracker                  # default /dev/ttyACM1 (V5 User Port)
./build/red_tracker /dev/ttyACM2     # or name the detected V5 User Port

./build/otos_monitor                 # default /dev/i2c-1
./build/otos_stream                  # default /dev/ttyACM1 and /dev/i2c-1
```

Neither program needs the brain attached — if the serial port cannot be
opened they warn once and carry on, which is how you tune at a desk.

A directly connected V5 Brain normally exposes two serial devices. Use the
one whose interface is `VEX Robotics User Port`, commonly `/dev/ttyACM1`;
`/dev/ttyACM0` is commonly the communications/programming port and can open
successfully without delivering packets to user-program `stdin`. On the Pi,
`udevadm info -q property -n /dev/ttyACM1` normally reports the user port as
`ID_USB_INTERFACE_NUM=02`; the communications port is normally `00`. Pass the
matching device to `red_tracker` if its name differs.

Your user needs to be in the `dialout` group for the serial port and `i2c` for
the OTOS:

```bash
sudo usermod -aG dialout,i2c $USER   # log out and back in
i2cdetect -y 1                       # OTOS should answer at 0x17
```

## Tuning

**Red thresholds** live in `RedTrackerConfig` in
[red_target_tracker.hpp](include/vexpi/red_target_tracker.hpp). Red straddles
the wrap-around point of the HSV hue circle, so it takes two bands. Field
lighting moves these — if the mask picks up the floor, raise the saturation
and value minimums before touching hue.

**Depth range** is clamped to 150–1820 mm. The lower bound is what extended
disparity makes reachable on an OAK-D Lite; below it the stereo pair has no
overlap and the readings are fiction.

**OTOS offsets and scalars** are robot-specific and live in `tuneOtos()` in
[apps/otos_monitor.cpp](apps/otos_monitor.cpp). Measure them on your robot:
push a known distance and set the linear scalar to actual/reported, spin a
known number of turns for the angular one.

## Notes on the camera

The RGB preview and the aligned depth output must be the same size and share
the RGB sensor's 16:9 aspect ratio, since it runs at 1080p. Use 640x360, not
640x480 — at 4:3 the two images do not line up pixel for pixel and the depth
sampled inside a colour mask belongs to something else.

Extended disparity and the on-device median filter are mutually exclusive, so
the filter is switched off whenever extended disparity is on. The tracker makes
up for it on the host: the reported distance is a median over the masked pixels
and then a rolling median over the last five frames.

`otos_stream` sends `O,<x inches>,<y inches>,<heading degrees>\n` to Override.
Keep the robot stationary during startup calibration. Run this program before
autonomous; it requires exclusive access to the V5 User Port.
