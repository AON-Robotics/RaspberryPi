# Raspberry Pi VEX coprocessor

The team's Raspberry Pi is already set up. The project lives at
`/home/aonpi/RaspberryPi5VEX`, and `vexp.service` automatically runs
`build/vexpi` when the Pi boots.

The main runtime reads OTOS and sends pose packets to the VEX V5 User Port.
Keep the robot flat and still when starting it so OTOS can calibrate.

## Turn the runtime on or off

```bash
sudo systemctl start vexp.service    # On
sudo systemctl stop vexp.service     # Off until started again or the Pi reboots
sudo systemctl restart vexp.service  # Restart and recalibrate
```

Check whether it is running and view its logs:

```bash
systemctl status vexp.service
journalctl -u vexp.service -f
```

Press Ctrl-C to leave the log viewer; the runtime keeps running.
For boot on/off controls and updating code, see [deployment.md](docs/deployment.md).

## Where to put your code

| What you are adding | Where it belongs |
| --- | --- |
| A sensor or other component | Implementation in `src/<component>/`, headers in `include/vexpi/<component>/` |
| Startup and shutdown for a component that runs on the robot | `apps/main.cpp` |
| Robot OTOS offsets and scalars | `apps/robot_config.hpp` |
| Camera processing | `src/vision/` and `include/vexpi/vision/` |
| Shared V5 serial communication | `src/serial/` and `include/vexpi/serial/` |
| Packet formats | `src/protocol/` and `include/vexpi/protocol/` |
| A standalone demo or diagnostic | `apps/` (camera demos go in `apps/vision/`) |
| Tests | `tests/<component>/` |
| Sources and libraries to build | `CMakeLists.txt` |

To run your component **alongside OTOS at boot**, wire its worker into
`apps/main.cpp` and include its sources/library in the `vexpi` build. Adding a
file or a standalone executable alone does not make it start automatically.
See [Adding a component to the boot runtime](docs/deployment.md#adding-a-component-to-the-boot-runtime)
for the steps.

Camera programs (`red_tracker` and `depth_center_demo`) are currently separate
diagnostics. They do not run inside `vexpi`. Components running together must
share the runtime's `PacketSender` instead of opening competing V5 connections.

## Apply your changes

```bash
cd /home/aonpi/RaspberryPi5VEX
sudo systemctl stop vexp.service
cmake -S . -B build && cmake --build build -j"$(nproc)" && ctest --test-dir build --output-on-failure && sudo systemctl start vexp.service
```

If configure, build, or tests fail, fix the error and rerun the command. The
runtime stays stopped until all steps succeed. No service reinstall is needed
for normal C++ changes.

OTOS sends `O,<x inches>,<y inches>,<heading degrees>` packets. Packet formats
and V5 receiver guidance are in [serial-protocol.md](docs/serial-protocol.md).
