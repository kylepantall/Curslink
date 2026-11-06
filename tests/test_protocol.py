import pytest

from curslink.protocol import PROTOCOL_VERSION, ProtocolError, decode, encode


def test_roundtrip():
    msg = decode(encode("move", x=10, y=20.5))
    assert msg.t == "move"
    assert msg.data == {"x": 10, "y": 20.5}


def test_frames_are_newline_terminated_single_lines():
    raw = encode("key", key="\n", pressed=True)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[1,2]\n",
        b'{"t":"nope"}\n',
        b'{"t":"move","x":"1","y":2}\n',
        b'{"t":"move","x":true,"y":2}\n',
        b'{"t":"button","button":"left","pressed":1}\n',
        b'{"t":"button","button":"laser","pressed":true}\n',
        b'{"t":"key","key":"","pressed":true}\n',
        b'{"t":"enter","x":1,"y":2,"effect":"explode"}\n',
        b'{"t":"hello","v":999,"device_id":"x"}\n',
        b'{"t":"hello","v":1,"device_id":""}\n',
    ],
)
def test_rejects_bad_frames(line):
    with pytest.raises(ProtocolError):
        decode(line)


def test_hello_ok():
    msg = decode(encode("hello", v=PROTOCOL_VERSION, device_id="laptop", screens=[]))
    assert msg.get("device_id") == "laptop"


def test_encode_rejects_unknown_type():
    with pytest.raises(ProtocolError):
        encode("rm_rf")
