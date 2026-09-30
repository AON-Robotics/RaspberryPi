# Pi to V5 serial protocol

The Pi is the only talker. It writes newline-terminated ASCII lines to the V5
brain's USB port, which appears on the Pi as `/dev/ttyACM0`. The brain never
replies.

The link is configured 115200 8N1, raw (no canonical mode, no flow control, no
output post-processing). Because the V5 brain enumerates as a USB CDC-ACM
device the baud rate is nominal and ignored by the hardware, but it is set
anyway so the port behaves predictably.

## Packets

### Red target tracking — `red_tracker`

| Packet | Meaning |
| --- | --- |
| `R,<inches>\n` | A red target is being tracked, `<inches>` away. |
| `N,0\n` | No usable target this frame. |

`<inches>` is an integer, rounded from the filtered millimetre distance. One
packet goes out per camera frame (15 FPS by default), including while the
tracker is in its `HOLD` state — during a one or two frame dropout the last
good distance keeps being sent rather than dropping to `N,0`.

### Centre distance — `depth_center_demo`

| Packet | Meaning |
| --- | --- |
| `<centimetres>,<offset>\n` | Distance at the frame centre. `<offset>` is always `0`. |

This is the original untagged format. It has no "no target" value: an
unreadable centre patch sends `0,0`.

## Reading it on the brain

Parse it as lines, not as fixed-size reads — a USB write can be split across
packets. Roughly, in PROS:

```cpp
// Accumulate bytes until a newline, then parse one complete line.
static std::string line;

while (true) {
    int c = fgetc(stdin);           // or your serial read of choice
    if (c == EOF) break;

    if (c != '\n') {
        line.push_back(static_cast<char>(c));
        continue;
    }

    char tag = line.empty() ? 'N' : line[0];
    int value = 0;
    if (std::sscanf(line.c_str(), "%*[^,],%d", &value) == 1) {
        if (tag == 'R') {
            targetDistanceInches = value;
            targetVisible = true;
        } else {
            targetVisible = false;
        }
    }
    line.clear();
}
```

## Timing

Treat the stream as advisory, not synchronous. If packets stop arriving — the
Pi rebooted, the cable came loose — the brain should time out and fall back to
its own sensors rather than acting on a stale distance. A few hundred
milliseconds without a packet is a reasonable threshold at 15 FPS.
