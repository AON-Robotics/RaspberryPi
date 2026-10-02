"""Pi <-> V5 brain line protocol (Python side).

Mirror of Override/include/aon/pi/protocol.hpp. The full spec is in
docs/serial-protocol.md; in short:

    Pi -> brain   C,<seq>,<VERB>[,<arg>...]*HH
    brain -> Pi   @A,<seq>*HH                      ack: motion accepted
                  @P,<seq>,k=v;...*HH              partial: more fields of a long @D
                  @D,<seq>,<status>[,k=v;...]*HH   done: ok|err|aborted|timeout
                  @H,k=v;...*HH                    heartbeat (5 Hz while linked)
                  @E,<code>[,k=v;...]*HH           event (abort, link lost)

HH is the XOR of every byte before '*', in uppercase hex. Lines that do not
start with '@' are the brain program's own printf/cout output: they are not
errors, just console text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

PROTOCOL_VERSION = 1
MAX_TX_LINE = 128  # brain drops longer lines (MAX_RX_LINE in protocol.hpp)


class ProtocolError(ValueError):
    """A line that claims to be protocol ('@...') but is corrupt."""


def checksum(body: str) -> int:
    value = 0
    for byte in body.encode("ascii", "replace"):
        value ^= byte
    return value


def with_checksum(body: str) -> str:
    return f"{body}*{checksum(body):02X}"


def format_arg(value) -> str:
    """Numbers in a compact, comma-free form: 24 -> '24', 1.5 -> '1.5'."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"argument {value!r} is not a finite number")
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    text = str(value)
    if any(c in text for c in ",*\r\n"):
        raise ValueError(f"argument {text!r} contains a protocol separator")
    return text


def encode_command(seq: int, verb: str, *args) -> bytes:
    """Builds one Pi -> brain command line, newline included."""
    body = ",".join(["C", str(seq), verb, *(format_arg(a) for a in args)])
    line = with_checksum(body)
    if len(line) > MAX_TX_LINE:
        raise ValueError(f"command too long for the brain ({len(line)} > {MAX_TX_LINE} bytes)")
    return (line + "\n").encode("ascii")


def _convert(text: str):
    if text == "nan":
        return None  # JSON has no NaN; None means "no reading"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return text


def parse_fields(text: str) -> dict:
    """'odom.x=1.5;odom.y=2;ok=1' -> {'odom': {'x': 1.5, 'y': 2}, 'ok': 1}."""
    result: dict = {}
    if not text:
        return result
    for pair in text.split(";"):
        key, sep, value = pair.partition("=")
        if not sep or not key:
            continue
        parts = key.split(".")
        node = result
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {} if child is None else {"_": child}
                node[part] = child
            node = child
        leaf = parts[-1]
        if isinstance(node.get(leaf), dict):
            node[leaf]["_"] = _convert(value)
        else:
            node[leaf] = _convert(value)
    return result


@dataclass
class Message:
    kind: str                 # 'A' ack, 'P' partial, 'D' done, 'H' heartbeat, 'E' event
    seq: int | None = None    # A, P and D
    status: str | None = None  # D: ok | err | aborted | timeout
    code: str | None = None   # E: aborted | link_lost | ...
    fields: dict = field(default_factory=dict)
    raw: str = ""
    text: str = ""            # P and D: the raw 'k=v;...' part, for merging partials


def parse_line(line: str) -> Message | None:
    """Parses one brain -> Pi line.

    Returns None for console text (not starting with '@'). Raises
    ProtocolError for an '@' line with a bad checksum or shape.
    """
    line = line.strip("\r\n")
    if not line.startswith("@"):
        return None
    star = line.rfind("*")
    if star < 0 or star + 3 != len(line):
        raise ProtocolError(f"missing checksum: {line!r}")
    body, digest = line[:star], line[star + 1:]
    try:
        if int(digest, 16) != checksum(body):
            raise ProtocolError(f"checksum mismatch: {line!r}")
    except ValueError:
        raise ProtocolError(f"bad checksum digits: {line!r}") from None

    parts = body.split(",", 3)
    kind = parts[0][1:]
    try:
        if kind == "A" and len(parts) == 2:
            return Message("A", seq=int(parts[1]), raw=line)
        if kind == "P" and len(parts) >= 3:
            text = body.split(",", 2)[2]
            return Message("P", seq=int(parts[1]), raw=line, text=text)
        if kind == "D" and len(parts) >= 3:
            text = parts[3] if len(parts) > 3 else ""
            return Message("D", seq=int(parts[1]), status=parts[2], fields=parse_fields(text), raw=line, text=text)
        if kind == "H":
            return Message("H", fields=parse_fields(body.partition(",")[2]), raw=line)
        if kind == "E" and len(parts) >= 2:
            rest = body.split(",", 2)
            return Message("E", code=rest[1], fields=parse_fields(rest[2] if len(rest) > 2 else ""), raw=line)
    except ValueError:
        pass
    raise ProtocolError(f"malformed line: {line!r}")
