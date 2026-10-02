# Pi ⇄ V5 brain serial protocol

The Pi talks to the brain over the brain's USB **user port**. That's the port the
robot program's `stdin`/`stdout` use. A brain plugged in directly shows up as
two CDC-ACM devices: the system port (usually `/dev/ttyACM0`, used to upload
programs) and the user port (usually `/dev/ttyACM1`). Through a controller
there is only one. `/dev/serial/by-id/` names them unambiguously.

The link is 115200 8N1, raw: no canonical mode, no flow control, no output
post-processing. The baud rate is nominal on a CDC-ACM device but is set anyway
so the port behaves the same everywhere.

Everything is newline-terminated ASCII lines. Two kinds of traffic share the
link:

| Traffic | Direction | Who | Section |
| --- | --- | --- | --- |
| Commands and replies (LLM bridge) | both ways | `bridge/server` ⇄ `Override/src/aon/pi` | [Commands](#commands-llm-bridge) |
| Red target distance | Pi → brain | `red_tracker` | [Legacy packets](#legacy-packets-red_tracker) |

Both can run at the same time. `red_tracker` only writes whole lines, so it can
share the port with the server.

## Commands (LLM bridge)

Code: `bridge/server/protocol.py` (Pi) and
`Override/include/aon/pi/protocol.hpp` (brain). Keep them in sync and bump
`PROTOCOL_VERSION` in both when the format changes. The server refuses to drive
a brain with a different version.

### Framing

Every line ends in `*HH`: the XOR of every byte before the `*`, as two
uppercase hex digits. Example: `C,1,PING*62`.

| Line | Direction | Meaning |
| --- | --- | --- |
| `C,<seq>,<VERB>[,<arg>...]*HH` | Pi → brain | Command. `seq` is 1–65535 and wraps around. Max 128 bytes. |
| `@A,<seq>*HH` | brain → Pi | Ack: a motion command was accepted and queued. |
| `@P,<seq>,k=v;...*HH` | brain → Pi | Partial: more fields of a long reply, sent before its `@D`. |
| `@D,<seq>,<status>[,k=v;...]*HH` | brain → Pi | Done. `status` is `ok`, `err`, `aborted` or `timeout`. |
| `@H,k=v;...*HH` | brain → Pi | Heartbeat, 5 Hz, only while the Pi has sent something in the last 5 s. |
| `@E,<code>[,k=v;...]*HH` | brain → Pi | Event: `aborted`, `link_lost`. |
| anything else | brain → Pi | The robot program's own `printf`/`cout` output. Not an error. The Pi keeps the last 50 lines as "brain console". |

- **Fields** are `key=value` pairs separated by `;`. Dots in a key nest it,
  so `motors.L.p11.temp=35` becomes `{"motors": {"L": {"p11": {"temp": 35}}}}`.
  Values never contain `, ; = *`; the brain replaces them with `_`. `nan` means
  no reading.
- **Line length:** brain lines are at most about 200 bytes. A longer reply
  (`SENSORS` with every motor) is split into `@P` lines followed by the `@D`,
  because one long line can overflow the brain's USB output buffer and arrive
  cut off.
- **Errors:** `@D,<seq>,err,code=<code>;msg=<text>`. Codes: `bad_checksum`,
  `bad_seq`, `bad_line`, `too_long`, `unknown_verb`, `bad_args`, `busy`,
  `refused`, `unknown_sensor`. Lines too broken to have a sequence number are
  answered with seq `0`.

### Verbs

Distances are in inches, positive forward. Angles are in degrees,
**positive clockwise** (the IMU's convention). Speeds are drivetrain motor RPM.

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

- **Immediate** verbs reply `@D` at once, from the reader task.
- **Motion** verbs reply `@A` at once and `@D` when finished. Only one runs at
  a time; a second one gets `err,code=busy`.

Sensors registered on the brain: `odom`, `tracking`, `imu`, `motors`,
`battery`, `competition`, `link` (protocol counters) and `pi_target` (the last
`red_tracker` packet). The simulator adds `sim` (ground truth).

### Safety rules (enforced on the brain)

The Pi is an add-on. Whatever it sends, or stops sending, the brain program
keeps working.

- Motion is refused (`err,code=refused`) when the robot is disabled, during
  autonomous, or while the IMU is calibrating.
- While a Pi motion runs, `opcontrol()` skips the driver code. As soon as the
  motion ends, control goes back to the driver.
- A Pi motion is aborted, the motors stop, and control goes back to the driver
  when any of these happens:
  - `STOP` (`reason=stop`)
  - a joystick moves past the deadband (`driver_override`)
  - the X button is pressed (`x_button`)
  - **nothing arrives from the Pi for 1 s** (`link_lost`). This is the
    deadman. The server sends `PING` every 300 ms to keep it fed.
- `STOP` never touches driver control or autons. It only stops motion the Pi
  started.
- Lines longer than 128 bytes, bad checksums, unknown verbs and wrong
  arguments get an `err` reply and are otherwise ignored.
- Writes never block a robot task. If the USB buffer stays full, the line is
  dropped.

### Health, as the server sees it

| Hop | Detected by | Error |
| --- | --- | --- |
| serial port | port missing, or read/write fails | `hop=serial`, `serial_down` / `serial_lost` |
| brain program | no `@A`/`@D` within 500 ms | `hop=brain`, `no_reply` |
| brain program | no data for 1 s during a motion | `hop=brain`, `brain_silent` |
| brain program | `PING` reports another protocol version | `hop=brain`, protocol mismatch |

`GET /health` on the server reports every hop.

## Legacy packets (`red_tracker`)

Pi → brain only, no checksum (the brain accepts one if present):

| Packet | Meaning |
| --- | --- |
| `R,<inches>` | A red target is being tracked `<inches>` away. |
| `N,0` | No usable target this frame. |

- `<inches>` is an integer, rounded from the filtered distance in millimetres.
- One packet goes out per camera frame (15 FPS), including the tracker's
  `HOLD` state: during a one- or two-frame dropout the last good distance keeps
  being sent instead of `N,0`.
- The brain's Pi link parses these and exposes the latest one as the
  `pi_target` sensor (`visible`, `inches`, `age_ms`). A brain-side routine can
  read the same values.
- Treat the stream as advisory. If `age_ms` grows past a few hundred
  milliseconds (the Pi rebooted, the cable came loose), fall back to the
  brain's own sensors.

`depth_center_demo` still sends the original untagged `<centimetres>,0`
format. It is meant for bench testing the camera, not for the brain.
