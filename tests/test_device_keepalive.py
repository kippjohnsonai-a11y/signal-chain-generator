"""The host answers the pedal's keepalive while a live session sits idle (#99)."""

from __future__ import annotations

from types import SimpleNamespace

from stompxl.helixgen.device.frames import CMD_IDLE, PRIMARY, decode_frame, encode_frame
from stompxl.helixgen.device.usb import Session


class _USBError(Exception):
    pass


class _Device:
    def __init__(self, queued: list[bytes]):
        self.queued = list(queued)
        self.written: list[bytes] = []

    def write(self, _endpoint, data, _timeout):
        self.written.append(bytes(data))
        return len(data)

    def read(self, _endpoint, _size, _timeout):
        if not self.queued:
            raise _USBError("timed out")
        return self.queued.pop(0)


def _pedal_keepalive(seq: int) -> bytes:
    return encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_IDLE, seq=seq,
                        arg=0x34C, body=b"")


def test_settle_counts_keepalives_and_keepalive_answers_with_the_current_arg():
    device = _Device([_pedal_keepalive(0x27), _pedal_keepalive(0x28)])
    usb = SimpleNamespace(core=SimpleNamespace(USBError=_USBError))
    session = Session(device, SimpleNamespace(), usb_module=usb)
    session._state.seq, session._state.arg = 0x21, 0x205D

    assert session.settle() == 2
    assert session.keepalives_seen == 2

    session.keepalive()
    frame = decode_frame(device.written[-1])
    assert (frame.cmd, frame.seq, frame.arg, frame.body) == (CMD_IDLE, 0x21, 0x205D, b"")
    assert frame.src == PRIMARY.host and frame.dst == PRIMARY.device


def test_the_session_tracks_how_much_the_pedal_has_taken():
    """The pedal's arg counts the bytes it has taken; the writer checks credits by it (#117)."""
    device = _Device([_pedal_keepalive(0x27)])
    usb = SimpleNamespace(core=SimpleNamespace(USBError=_USBError))
    session = Session(device, SimpleNamespace(), usb_module=usb)
    assert session.peer_arg is None
    session.settle()
    assert (session.peer_arg, session.sent_since_peer) == (0x34C, 0)
    session._send(cmd=0x04, body=b"x" * 23)
    session.keepalive()
    assert session.sent_since_peer == 23
