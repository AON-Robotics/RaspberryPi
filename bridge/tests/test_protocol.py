"""Protocol framing and parsing, and a cross-check against the C++ brain code."""

import shutil
import subprocess
from pathlib import Path

import pytest

from protocol import ProtocolError, checksum, encode_command, format_arg, parse_fields, parse_line, with_checksum

BRAIN_SIM = Path(__file__).resolve().parents[2] / "sim" / "build" / "brain_sim"


def test_checksum_is_xor_of_body():
    assert checksum("C,1,PING") == 0x43 ^ 0x2C ^ 0x31 ^ 0x2C ^ 0x50 ^ 0x49 ^ 0x4E ^ 0x47
    assert with_checksum("@A,7").startswith("@A,7*")


def test_encode_command_formats_numbers_compactly():
    assert encode_command(5, "MOVE", 24.0, 180) == (with_checksum("C,5,MOVE,24,180") + "\n").encode()
    assert format_arg(-12.5) == "-12.5"
    assert format_arg(0.0001) == "0"
    with pytest.raises(ValueError):
        format_arg(float("nan"))
    with pytest.raises(ValueError):
        encode_command(1, "SENSORS", "a,b")


def test_encode_refuses_lines_the_brain_would_drop():
    with pytest.raises(ValueError):
        encode_command(1, "SENSORS", "x" * 200)


def test_parse_done_with_nested_fields():
    msg = parse_line(with_checksum("@D,12,ok,odom.x=1.50;odom.trk.left=3;motors.L.p11.temp=35;imu.rotation=nan"))
    assert (msg.kind, msg.seq, msg.status) == ("D", 12, "ok")
    assert msg.fields == {"odom": {"x": 1.5, "trk": {"left": 3}}, "motors": {"L": {"p11": {"temp": 35}}},
                          "imu": {"rotation": None}}


def test_parse_ack_partial_heartbeat_event():
    assert parse_line(with_checksum("@A,3")).seq == 3
    partial = parse_line(with_checksum("@P,3,a=1;b=2"))
    assert (partial.kind, partial.seq, partial.text) == ("P", 3, "a=1;b=2")
    hb = parse_line(with_checksum("@H,up=100;pi=0;busy=-;mode=driver"))
    assert hb.kind == "H" and hb.fields["busy"] == "-" and hb.fields["mode"] == "driver"
    ev = parse_line(with_checksum("@E,link_lost,silent_ms=1200;msg=no data"))
    assert ev.code == "link_lost" and ev.fields["silent_ms"] == 1200


def test_console_lines_are_not_errors():
    assert parse_line("currentAngleGyro: 0.000000") is None
    assert parse_line("[DEBUG] intake scan tick") is None


def test_corrupt_protocol_lines_raise():
    good = with_checksum("@D,1,ok,a=1")
    with pytest.raises(ProtocolError):
        parse_line(good[:-1] + ("0" if good[-1] != "0" else "1"))
    with pytest.raises(ProtocolError):
        parse_line("@D,1,ok,a=1")  # no checksum
    with pytest.raises(ProtocolError):
        parse_line(with_checksum("@D,notanumber,ok"))


def test_parse_fields_tolerates_odd_input():
    assert parse_fields("") == {}
    assert parse_fields("a=1;junk;b=x") == {"a": 1, "b": "x"}
    assert parse_fields("a=1;a.b=2") == {"a": {"_": 1, "b": 2}}


@pytest.mark.skipif(not BRAIN_SIM.exists() or shutil.which("cmake") is None, reason="build sim/ first")
@pytest.mark.parametrize("body", ["C,1,PING", "C,65535,MOVE,-48,300", "@D,9,err,code=busy;msg=already running MOVE",
                                  "R,42", "C,2,SENSORS,motors"])
def test_checksum_matches_the_brain_code(body):
    """The C++ framing (Override/src/aon/pi/protocol.cpp) and this module agree."""
    out = subprocess.run([str(BRAIN_SIM), "--checksum", body], capture_output=True, text=True, check=True).stdout
    assert out.strip() == with_checksum(body)
