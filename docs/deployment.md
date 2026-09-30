# Running and updating the team's Pi

The Pi already has its dependencies, hardware access, and boot service set up.
Use the existing checkout at `/home/aonpi/RaspberryPi5VEX`.
`vexp.service` runs `build/vexpi` as `aonpi` automatically at boot.

## On, off, and logs

| Action | Command |
| --- | --- |
| Start now | `sudo systemctl start vexp.service` |
| Stop now | `sudo systemctl stop vexp.service` |
| Restart after changes | `sudo systemctl restart vexp.service` |
| Check status | `systemctl status vexp.service` |
| Follow logs | `journalctl -u vexp.service -f` |
| View this boot's logs | `journalctl -b -u vexp.service --no-pager` |
| Stop now and stay off after reboot | `sudo systemctl disable --now vexp.service` |
| Restore automatic startup and start now | `sudo systemctl enable --now vexp.service` |

These commands control the coprocessor program, not power to the Pi.
A normal `stop` leaves automatic startup enabled for the next reboot.
Ctrl-C exits the log viewer without stopping the program.

Keep the robot flat and still whenever the runtime starts: OTOS calibrates at
startup. Look for `OTOS stream started`, then `V5 User Port connected` when the
V5 is available. OTOS startup retries every two seconds; V5 connection retries
occur when healthy OTOS readings are available. A process crash restarts the
runtime after five seconds.

## Adding a component to the boot runtime

The service starts one executable: `build/vexpi`. To make your work run alongside
the existing OTOS worker:

1. Put the component's implementation in `src/<component>/` and its public
   headers in `include/vexpi/<component>/`. Keep sensor logic in that component.
2. Add its `.cpp` sources to `vexpi_core` in `CMakeLists.txt`, or create a separate
   library and link it to `vexpi`. Camera code belongs in `vexpi_vision`; integrating
   it into the main runtime also requires linking that library to `vexpi` under
   `VEXPI_BUILD_VISION` and guarding optional camera code when vision is disabled.
3. In `apps/main.cpp`, create and start the component's worker. Use
   `OtosStream` as an example of owning a background thread with `start()` and
   `stop()`. Give each simultaneous component its own worker; calling a blocking
   sensor loop directly in `main()` prevents later components from starting.
4. Keep independent workers outside the OTOS initialization/retry loop so a
   missing OTOS does not delay them or repeatedly create them. The current
   `main()` manages only OTOS, so extend its lifecycle management when adding
   another worker.
5. Pass the existing `packets` (`PacketSender`) to any worker sending V5 packets.
   It serializes writes between threads. Add new packet formats in `protocol/`
   and update the V5 receiver to understand them.
6. Make the worker retry missing hardware without stopping the other components.
   On shutdown, signal every worker to stop and join its thread before the shared
   `PacketSender` is destroyed. Keep I/O bounded so service stop can finish.
7. Rebuild and start the runtime using the commands below. Check logs and confirm
   both the existing OTOS stream and the new component work together.

Putting an executable in `apps/` or enabling a CMake target only builds it;
it does not add it to boot startup. The existing `apps/vision/` programs are
standalone demos. To run their processing alongside OTOS, integrate the camera
component into `vexpi` using the steps above. The service continues to launch
`build/vexpi`.

## Rebuild after editing

Stop the running program before rebuilding:

```bash
cd /home/aonpi/RaspberryPi5VEX
sudo systemctl stop vexp.service
cmake -S . -B build && cmake --build build -j"$(nproc)" && ctest --test-dir build --output-on-failure && sudo systemctl start vexp.service
```

The `&&` chain starts the runtime only after configure, build, and tests succeed.
Fix any failure and rerun it. This preserves the existing CMake options. If your
change integrates vision, configure with `-DVEXPI_BUILD_VISION=ON` as well.

```bash
systemctl status vexp.service
journalctl -u vexp.service -f
```

Normal code changes need no service reinstall. Only if you edit
`deploy/vexp.service`, apply that updated unit with `./scripts/install_service.sh`
from the checkout root; it also restarts the runtime.

## Run manually for debugging

Stop the service first so only one program uses the sensor and V5 port:

```bash
cd /home/aonpi/RaspberryPi5VEX
sudo systemctl stop vexp.service
./build/vexpi --debug
```

Press Ctrl-C to stop the manual program, then restore normal operation:

```bash
sudo systemctl start vexp.service
```

`--debug` prints each OTOS packet and whether its serial write succeeded.
Confirm new hardware behavior on the robot as well as running the tests.
