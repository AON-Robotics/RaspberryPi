# Pi to V5 serial protocol

The Pi writes newline-terminated ASCII packets to the V5 Brain's USB User Port.
The default device is `/dev/ttyACM1`; confirm the interface on your Pi as
described in the [README](../README.md#device-access). The serial link is
configured as raw 115200 8N1.

Only one program should write to the User Port at a time. The main `vexpi`
program sends OTOS pose packets. The optional vision programs use the same
packet builders but run separately.

## Packets

| Sender | Packet | Meaning |
| --- | --- | --- |
| `vexpi` | `O,<x>,<y>,<heading>\n` | OTOS pose: inches forward, inches right, degrees clockwise. Values have three decimal places. |
| `red_tracker` | `R,<inches>\n` | A red target is being tracked at the given integer distance. |
| `red_tracker` | `N,0\n` | No usable target this frame. |
| `depth_center_demo` | `<centimetres>,<offset>\n` | Legacy untagged center distance. The offset is currently zero. |

For example, `O,12.000,3.000,-90.000` means 12 inches forward, 3 inches
right, and a heading of -90 degrees. OTOS warnings, faults, and I²C read
errors suppress pose packets until readings recover.

## Receiver behavior

Accumulate bytes until a newline, then parse one complete line. A serial read
may contain part of a line or multiple lines. Check the leading tag and the
expected number of fields before using the values. Treat an old pose as stale
when no fresh `O` packet arrives; AON Override currently uses a 300 ms timeout.
The optional camera packets are ignored by that receiver.
