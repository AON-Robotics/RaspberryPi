# Raspberry Pi VEX coprocessor

Raspberry Pi 5 sensor software for AON Robotics. The main `vexpi` program reads
a SparkFun Qwiic Optical Tracking Odometry Sensor (OTOS) over I²C and sends
pose packets to a VEX V5 Brain over its USB User Port. OAK-D Lite camera
programs are available as separate, optional tools.

The main program currently runs **OTOS only**. The camera programs do not run
inside `vexpi` and should not use the same V5 User Port at the same time.

## Requirements

- Raspberry Pi running Linux, with I²C enabled and a Qwiic OTOS connected
- VEX V5 Brain connected by USB if packets need to reach a robot program
- CMake 3.20 or newer and a C++17 compiler
- `i2c-tools` for the optional `i2cdetect` wiring check

On Raspberry Pi OS or Debian, install the build tools with:

```bash
sudo apt update
sudo apt install cmake g++ i2c-tools
```

The default build has no OpenCV or DepthAI dependency. Camera requirements are
listed under [Optional camera programs](#optional-camera-programs).

## Build and run

From the repository directory:

```bash
cmake -S . -B build -DVEXPI_BUILD_VISION=OFF
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
./build/vexpi
```

After the first configure, repeat `cmake --build build --parallel 2` to rebuild.
On the AON Pi, the repository directory is `/home/aonpi/RaspberryPi5VEX`, so
the usual rebuild is:

```bash
cd /home/aonpi/RaspberryPi5VEX
cmake --build build --parallel 2
```

The program calibrates the IMU at startup. **Keep the robot flat and still
during calibration.** Press Ctrl-C to stop. `vexpi` exits if OTOS fails its
startup checks. If the V5 is unplugged, it keeps reading OTOS and retries the
serial connection once a second.

### Device access

The default devices are `/dev/i2c-1` for OTOS and `/dev/ttyACM1` for the V5
User Port. Enable I²C in the Pi's system configuration. The user running
`vexpi` needs access to both devices; on Raspberry Pi OS this usually means
membership in the `i2c` and `dialout` groups:

```bash
sudo usermod -aG i2c,dialout "$USER"
```

Log out and back in for the group change to take effect. Check for the OTOS
at I²C address `0x17` with `i2cdetect -y 1`.

A V5 Brain can expose both a communications port and a User Port. The User
Port is commonly `/dev/ttyACM1`, but check the USB interface if packets are
not reaching the brain:

```bash
udevadm info -q property -n /dev/ttyACM1
```

Look for the `VEX Robotics User Port` interface. Pass device paths when your
setup uses different ones:

```bash
./build/vexpi /dev/ttyACM2 /dev/i2c-1
```

### Debug output

Normal operation logs connections, errors, and OTOS health changes without
printing every packet. Use `--debug` to inspect the pose values:

```bash
./build/vexpi --debug
./build/vexpi --debug /dev/ttyACM1 /dev/i2c-1
```

`SENT O,x,y,heading` means a serial write succeeded. `UNSENT O,x,y,heading`
means OTOS supplied a reading but the write did not complete. The values are
inches, inches, and degrees. Debug mode prints about 50 lines per second; turn
it off for normal service operation. Run `./build/vexpi --help` for the CLI
syntax.

### Start automatically on boot

After manual startup works, create `/etc/systemd/system/vexpi.service` with
the following content. Replace `pi` and `/home/pi/RaspberryPi` with the user
and absolute checkout path on your machine. For the AON Pi, use `aonpi` and
`/home/aonpi/RaspberryPi5VEX`.

```ini
[Unit]
Description=VEX Pi OTOS packet stream

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/RaspberryPi
ExecStart=/home/pi/RaspberryPi/build/vexpi
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vexpi.service
sudo systemctl status vexpi.service
journalctl -u vexpi.service -f
```

The service user needs the same I²C and serial device access as a manual run.
Keep the robot still when the Pi boots because every service start calibrates
the IMU. To debug the service, temporarily append `--debug` to `ExecStart`,
run `sudo systemctl daemon-reload` and `sudo systemctl restart vexpi.service`,
then watch `journalctl -u vexpi.service -f`.

## OTOS configuration and packet format

Robot-specific mounting offsets and drift scalars live in
[apps/robot_config.hpp](apps/robot_config.hpp). The starting values are zero
offset and 1.0 scalars. Measure them on the installed robot, edit that file,
and rebuild. Use `--debug` to compare reported movement with measured movement.
SparkFun's [calibration example](https://github.com/sparkfun/SparkFun_Qwiic_OTOS_Arduino_Library/blob/main/examples/Example3_Calibration/Example3_Calibration.ino)
explains the scalar measurement process.

The main program sends one newline-terminated packet per usable OTOS reading:

```text
O,<x inches>,<y inches>,<heading degrees>\n
```

It converts OTOS coordinates to the AON Override convention: X forward,
Y right, heading clockwise. For example, `O,12.000,3.000,-90.000` reports
12 inches forward, 3 inches right, and a heading of -90 degrees. OTOS warnings,
faults, or I²C read errors stop pose packets until readings recover. The
receiving robot program must treat old packets as stale; AON Override currently
uses a 300 ms timeout.

## Optional camera programs

Install OpenCV 4 and [DepthAI Core v2 with OpenCV support](https://github.com/luxonis/depthai-core/tree/v2_stable),
then enable the vision targets:

```bash
cmake -S . -B build -DVEXPI_BUILD_VISION=ON
cmake --build build --parallel 2
```

| Program | Use | Output |
| --- | --- | --- |
| `red_tracker` | Tracks a red target with the OAK-D Lite. | `R,<distance inches>\n` or `N,0\n` |
| `depth_center_demo` | Shows a camera preview and samples center depth; requires a graphical desktop. | Legacy `<distance cm>,<offset>\n` |

Run them from the repository directory with `./build/red_tracker` or
`./build/depth_center_demo`. Each accepts an optional V5 User Port path as its
first argument.

These are separate diagnostics. AON Override currently parses only `O` pose
packets; it ignores their camera packets. Run one serial-writing program at a
time. The camera programs have not been integrated into the `vexpi` startup
process.

## Source layout

| Path | Purpose |
| --- | --- |
| `apps/main.cpp` | Starts and stops the OTOS worker. |
| `apps/robot_config.hpp` | Robot-specific OTOS tuning. |
| `apps/vision/` | Optional camera programs. |
| `include/vexpi/otos/`, `src/otos/` | OTOS driver and streaming worker. |
| `include/vexpi/serial/`, `src/serial/` | V5 serial link and shared packet sender. |
| `include/vexpi/protocol/`, `src/protocol/` | Packet format and coordinate conversion. |
| `include/vexpi/vision/`, `src/vision/` | OAK-D Lite camera and red target tracking. |
| `include/vexpi/units.hpp` | Shared unit conversion helpers. |
| `tests/protocol/` | Hardware-independent packet and coordinate tests. |

The packet test runs through `ctest`; OTOS, USB, and camera behavior still need
to be checked on the actual Pi and robot after hardware or configuration changes.

## License

No license file has been added to this repository yet.
