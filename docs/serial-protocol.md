# Pi ⇄ V5 brain serial protocol

The Pi talks to the brain over the brain's USB **user port**. That's the port the
robot program's `stdin`/`stdout` use. A brain plugged in directly shows up as
two CDC-ACM devices:
- the system port, usually `/dev/ttyACM0`, used to upload programs
- the user port, usually `/dev/ttyACM1`, the default everywhere in this repo

Through a controller there is only one port. To be sure which is which, run
`ls -l /dev/serial/by-id/`: the names say which interface each one is.

The link is 115200 8N1, raw: no canonical mode, no flow control, no output
post-processing. The baud rate is nominal on a CDC-ACM device but is set anyway
so the port behaves the same everywhere.

Everything is newline-terminated ASCII lines.

## Who uses the port

| Program | Direction | Lines | Section |
| --- | --- | --- | --- |
| `vexpi` (boot service) | Pi → brain | `O,<x>,<y>,<heading>`, 50 Hz | [Sensor packets](#sensor-packets) |
| `red_tracker` (optional) | Pi → brain | `R,<inches>` / `N,0` | [Sensor packets](#sensor-packets) |
| `bridge/server` (LLM bridge) | both ways | `C,...` commands; `@...` replies | [Commands](#commands-llm-bridge) |
| the robot program itself | brain → Pi | its own `printf`/`cout` output | not protocol; kept as "brain console" |

### Sharing the port

The C++ programs share one connection inside `vexpi` through `PacketSender`.
Wire new sensor workers there, not as new processes (see the
[README](../README.md#where-to-put-your-code)).

The bridge server is a separate Python process and opens the same port. That
is safe because of three rules:
- **Whole lines only.** Every writer sends each line with a single `write()`.
  Linux keeps one `write()` to a tty in one piece for anything under 2 KB (the
  tty write lock), so lines from different processes never interleave. Every
  line here is at most 128 bytes.
- **Only the bridge server reads.** `vexpi` and `red_tracker` never read, so they
  don't steal the brain's replies.
- **The brain tells the senders apart by the first character:**
  - `C`: a bridge command
  - another capital letter followed by a comma: a sensor packet
  - lines the brain sends back start with `@`

## Sensor packets

Pi → brain only. They have no checksum and get no reply.

| Sender | Packet | Meaning |
| --- | --- | --- |
| `vexpi` | `O,<x>,<y>,<heading>` | OTOS pose: inches forward, inches right, degrees clockwise. Three decimals. |
| `red_tracker` | `R,<inches>` | A red target is being tracked at this integer distance. |
| `red_tracker` | `N,0` | No usable target this frame. |
| `depth_center_demo` | `<centimetres>,<offset>` | Legacy untagged centre distance, for bench tests only. The brain ignores it. |

- **Example:** `O,12.000,3.000,-90.000` means 12 inches forward, 3 inches right,
  and a heading of -90°. OTOS warnings, faults and I²C read errors suppress pose
  packets until readings recover.
- **The brain's Pi link** (`Override/src/aon/pi/`) parses these packets and exposes
  the latest one:
  - **`O` packets:** the `pi_otos` sensor (`seen`, `x`, `y`, `heading`, `age_ms`),
    and `aon::pi::latestOtosPose()` for odometry fusion.
  - **`R`/`N` packets:** the `pi_target` sensor (`seen`, `visible`, `inches`,
    `age_ms`).
  - **Other tags:** counted (`link.unknown_packets`) and otherwise ignored. They
    get no reply, so a 50 Hz stream never causes reply traffic.
- **Staleness:** treat a reading as stale when no fresh packet arrives. Override
  uses 300 ms for the OTOS pose.
- **Deadman:** sensor packets do **not** keep the brain's deadman alive (see
  [Safety rules](#safety-rules-enforced-on-the-brain)). Only bridge commands do,
  so a dead bridge server is noticed even while `vexpi` keeps streaming.
- **Receiving:** accumulate bytes until a newline, then parse one complete line.
  A serial read may hold part of a line or several lines. Check the leading tag
  and the number of fields before using the values.

## Commands (LLM bridge)

Code:
- Pi: `bridge/server/protocol.py`
- Brain: `Override/include/aon/pi/protocol.hpp`

Keep them in sync, and bump `PROTOCOL_VERSION` in both when the format changes.
The server refuses to drive a brain with a different version.

### Framing

Every command and reply line ends in `*HH`: the XOR of every byte before the
`*`, as two uppercase hex digits. Example: `C,1,PING*62`.

| Line | Direction | Meaning |
| --- | --- | --- |
| `C,<seq>,<VERB>[,<arg>...]*HH` | Pi → brain | Command. `seq` is 1–65535 and wraps around. Max 128 bytes. |
| `@A,<seq>*HH` | brain → Pi | Ack: a motion command was accepted and queued. |
| `@P,<seq>,k=v;...*HH` | brain → Pi | Partial: more fields of a long reply, sent before its `@D`. |
| `@D,<seq>,<status>[,k=v;...]*HH` | brain → Pi | Done. `status` is `ok`, `err`, `aborted` or `timeout`. |
| `@H,k=v;...*HH` | brain → Pi | Heartbeat, 5 Hz, only while the bridge has sent a command in the last 5 s. |
| `@E,<code>[,k=v;...]*HH` | brain → Pi | Event: `aborted`, `link_lost`. |
| anything else | brain → Pi | The robot program's own output. Not an error; the server keeps the last 50 lines as "brain console". |

- **Fields** are `key=value` pairs separated by `;`. Dots in a key nest it, so
  `motors.L.p11.temp=35` becomes `{"motors": {"L": {"p11": {"temp": 35}}}}`.
  - Values never contain `, ; = *`; the brain replaces them with `_`.
  - `nan` means no reading.
- **Line length:** brain lines are at most about 200 bytes. A longer reply
  (`SENSORS` with every motor) is split into `@P` lines followed by the `@D`,
  because one long line can overflow the brain's USB output buffer and arrive
  cut off.
- **Errors** look like `@D,<seq>,err,code=<code>;msg=<text>`.
  - Codes: `bad_checksum`, `bad_seq`, `too_long`, `unknown_verb`, `bad_args`,
    `busy`, `refused`, `unknown_sensor`.
  - Lines that are neither commands nor sensor packets (e.g. the untagged
    `123,0`) are counted as `link.ignored_lines` and get no reply.
  - A `C` line too broken to carry a sequence number is answered with seq `0`.

### Verbs

Distances are in inches, positive forward. Angles are in degrees,
**positive clockwise** (the IMU and OTOS convention). Speeds are drivetrain motor RPM.

| Verb | Args | Kind | Does | Main reply fields |
| --- | --- | --- | --- | --- |
| `PING` | | immediate | Link check | `proto`, `robot`, `max_rpm`, `up` |
| `STATUS` | | immediate | Pose and mode | `mode`, `pi`, `busy`, `x`, `y`, `th`, `bat`, `can_move`, `why` |
| `SENSORS` | `[name]` | immediate | Every registered sensor, or one | `<sensor>.<field>` |
| `STOP` | | immediate | Abort the Pi's motion | `was_moving` |
| `MOVE` | `<in> [rpm]` | motion | `drivetrain.move()` | `target`, `traveled`, `reached`, `x0..th1`, `trk.left/right/back`, `imu`, `dur_ms` |
| `TURN` | `<deg> [rpm]` | motion | `drivetrain.turn()` | `target`, `turned`, `reached`, `drift`, plus the MOVE pose fields |
| `PROBE` | `[rpm] [ms]` | motion | Spins the left side alone, then the right side | `L.motors.p<port>`, `L.trk.*`, `L.imu`, same for `R` |
| `RESET_ODOM` | `[x] [y] [th]` | motion | `drivetrain.resetPose()` (re-tares the IMU, ~3 s) | `x`, `y`, `th` |

- **Immediate** verbs reply `@D` right away, from the reader task.
- **Motion** verbs reply `@A` right away and `@D` when finished. Only one runs at a
  time; a second one gets `err,code=busy`.

Sensors registered on the brain:
- `odom`, `tracking`, `imu`, `motors`, `battery`, `competition`
- `link`: protocol counters
- `pi_target` and `pi_otos`: the latest sensor packets
- `sim`: ground truth, in the simulator only

### Safety rules (enforced on the brain)

The Pi is an add-on. Whatever it sends, or stops sending, the brain program
keeps working.

- **When motion is refused** (`err,code=refused`): while the robot is disabled,
  during autonomous, and while the IMU is calibrating.
- **Who has control:** while a Pi motion runs, `opcontrol()` skips the driver
  code. As soon as the motion ends, control goes back to the driver.
- **What aborts a Pi motion:** any of the events below stops the motors and gives
  control back to the driver:
  - `STOP` (`reason=stop`)
  - a joystick moving past the deadband (`driver_override`)
  - the X button (`x_button`)
  - **no command from the bridge for 1 s** (`link_lost`). This is the deadman.
    The bridge server sends `PING` every 300 ms to keep it fed. Sensor packets
    don't count.
- **STOP only stops the Pi's own motion.** It never touches driver control or
  autons.
- **Bad lines get an `err` reply and are otherwise ignored:** command lines over
  128 bytes, bad checksums, unknown verbs and wrong arguments.
- **Writes never block a robot task.** If the USB buffer stays full, the line is
  dropped.

### Health, as the server sees it

| Hop | Detected by | Error |
| --- | --- | --- |
| serial port | port missing, or a read/write fails | `hop=serial`, `serial_down` / `serial_lost` |
| brain program | no `@A`/`@D` within 500 ms | `hop=brain`, `no_reply` |
| brain program | no data for 1 s during a motion | `hop=brain`, `brain_silent` |
| brain program | `PING` reports another protocol version | `hop=brain`, protocol mismatch |

The server reports every hop at `GET /health` (ok/not-ok, no token needed) and
`GET /health/details` (with the token).
