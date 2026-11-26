"""The pedal relayed from another device, and the tailnet login gate."""

import base64
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from stompxl.helixgen.device import relay
from stompxl.helixgen.device.frames import (
    CMD_CHUNK,
    CMD_IDLE,
    CMD_OPEN,
    MAGIC_HANDSHAKE,
    PRIMARY,
    encode_frame,
)
from stompxl.helixgen.device.usb import (
    ENDPOINT_IN,
    ENDPOINT_OUT,
    HANDSHAKE_ARG,
    DeviceError,
    DeviceInfo,
    Session,
    find_devices,
)
from stompxl import access, config
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude

XL = DeviceInfo(vendor_id=0x0E41, product_id=0x4253, product_name="HX Stomp XL",
                serial_number="X1", bus=None, address=None)


class FakeTransport:
    """The far end: answers reads from a queue, records writes."""

    def __init__(self, reads=()):
        self.reads = list(reads)
        self.calls = []

    def call(self, request, *, timeout_s):
        self.calls.append(request)
        if request["op"] == "read":
            if not self.reads:
                return {"ok": False, "timeout": True, "error": "timeout"}
            return {"ok": True, "data": self.reads.pop(0)}
        if request["op"] == "write":
            return {"ok": True, "written": len(request["data"])}
        return {"ok": True}


@pytest.fixture(autouse=True)
def no_remote():
    yield
    current = relay.current()
    if current:
        relay.detach(current[1])


def test_relayed_pedal_is_found_first_and_opened_through_the_relay(monkeypatch):
    transport = FakeTransport()
    relay.attach(XL, transport, "Android phone")
    assert find_devices() == [XL]

    started = []
    monkeypatch.setattr(Session, "_start", classmethod(lambda cls, s: started.append(s) or s))
    session = Session.open()
    assert session.info == XL
    assert isinstance(session._device, relay.RelayDevice)

    relay.detach(transport)
    assert relay.current() is None


def test_session_bytes_go_through_the_relay_unchanged():
    reply = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_OPEN, seq=0,
                         body=b"\x00\x02\x00\x00")
    transport = FakeTransport(reads=[b"stale"])
    session = Session(relay.RelayDevice(transport), XL, usb_module=relay.USB_MODULE)
    session._claim()
    assert session.drain() == 1  # the stale frame; then a timeout ends the drain
    transport.reads.append(reply)

    session._send(cmd=CMD_OPEN, body=b"\x00\x10\x00\x00", magic=MAGIC_HANDSHAKE,
                  arg=HANDSHAKE_ARG)
    assert session._read_frame().body[:4] == b"\x00\x02\x00\x00"

    ops = [c["op"] for c in transport.calls]
    # A fresh claim first: it cancels any read the page still has pending.
    assert ops[:3] == ["reclaim", "clear_halt", "clear_halt"]
    write = next(c for c in transport.calls if c["op"] == "write")
    assert write["ep"] == ENDPOINT_OUT
    assert write["data"].hex() == "0c0000280110ef03000000020001002100100000"
    assert all(c["ep"] == ENDPOINT_IN & 0x7F for c in transport.calls if c["op"] == "read")


def test_a_keepalive_mid_stream_is_not_taken_for_a_page():
    """Slow paging (a relayed pedal) lets the device slip keepalives in."""
    page = bytes(range(256))
    keepalive = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_IDLE, seq=0)
    chunk = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_CHUNK, seq=1, body=page)
    transport = FakeTransport(reads=[keepalive, keepalive, chunk])
    session = Session(relay.RelayDevice(transport), XL, usb_module=relay.USB_MODULE)
    first = b"head"
    assert session._reassemble(len(first) + len(page), first) == first + page


class OldPage(FakeTransport):
    """A page loaded before the reclaim op existed."""

    def call(self, request, *, timeout_s):
        if request["op"] == "reclaim":
            self.calls.append(request)
            return {"ok": False, "error": "unknown op reclaim"}
        return super().call(request, timeout_s=timeout_s)


def test_a_page_without_reclaim_still_works():
    transport = OldPage()
    session = Session(relay.RelayDevice(transport), XL, usb_module=relay.USB_MODULE)
    session._claim()
    assert [c["op"] for c in transport.calls] == ["reclaim", "clear_halt", "clear_halt"]


def test_a_failed_reclaim_is_reported():
    class Broken(FakeTransport):
        def call(self, request, *, timeout_s):
            if request["op"] == "reclaim":
                return {"ok": False, "error": "NetworkError: Unable to claim interface."}
            return super().call(request, timeout_s=timeout_s)

    session = Session(relay.RelayDevice(Broken()), XL, usb_module=relay.USB_MODULE)
    with pytest.raises(DeviceError, match="claim"):
        session._claim()


def test_a_relayed_timeout_is_marked_as_one():
    device = relay.RelayDevice(FakeTransport())
    with pytest.raises(relay.RelayError) as caught:
        device.read(ENDPOINT_IN, 512, 2000)
    assert caught.value.timeout


def test_a_slot_read_that_times_out_is_tried_once_more_in_a_fresh_session(monkeypatch):
    from stompxl import pedal

    opened = []

    class FakeSession:
        def __init__(self, fail):
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read_slot(self, *, bank=0, slot):
            if self.fail:
                raise DeviceError("Bulk read failed: read timed out after 2000 ms")
            return b"doc"

    def open_session():
        opened.append(1)
        return FakeSession(fail=len(opened) == 1)

    monkeypatch.setattr(Session, "open", staticmethod(open_session))
    monkeypatch.setattr(pedal.time, "sleep", lambda _s: None)
    assert pedal._with_retry(lambda s: s.read_slot(slot=3)) == b"doc"
    assert len(opened) == 2

    # Anything but a timeout is not retried.
    opened.clear()
    monkeypatch.setattr(Session, "open", staticmethod(
        lambda: opened.append(1) or (_ for _ in ()).throw(DeviceError("in use by another program"))))
    with pytest.raises(DeviceError, match="in use"):
        pedal._with_retry(lambda s: s.read_slot(slot=3))
    assert len(opened) == 1


def test_a_timed_out_read_tells_you_what_to_do():
    from stompxl import pedal

    error = pedal._read_error(5, DeviceError("Bulk read failed: read timed out after 2000 ms"))
    assert str(error).startswith("Could not read slot 2B: Bulk read failed: read timed out")
    assert "Unplug it and plug it back in" in str(error)
    assert "Unplug" not in str(pedal._read_error(5, DeviceError("slot is weird")))


def test_a_relayed_timeout_is_a_device_error():
    session = Session(relay.RelayDevice(FakeTransport()), XL, usb_module=relay.USB_MODULE)
    with pytest.raises(DeviceError, match="timeout"):
        session._read_raw()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(access.ALLOWED_ENV, raising=False)
    app = create_app(Library(tmp_path / "lib.sqlite3"), caller_factory=lambda: FakeClaude([]))
    return TestClient(app)


def test_websocket_relay_carries_reads_and_writes(client):
    with client.websocket_connect("/api/usb-relay") as ws:
        ws.send_json({"type": "hello", "vendorId": 0x0E41, "productId": 0x4253,
                      "productName": "HX Stomp XL", "serialNumber": "X1", "where": "Android phone"})
        assert ws.receive_json()["type"] == "attached"
        info, transport, where = relay.current()
        assert (info.product_name, where) == ("HX Stomp XL", "Android phone")
        status = client.get("/api/pedal").json()
        assert status["connected"] and status["where"] == "Android phone"

        device = relay.RelayDevice(transport)
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            written=device.write(ENDPOINT_OUT, b"\x01\x02", 2000),
            read=device.read(ENDPOINT_IN, 512, 2000)))
        worker.start()

        write = ws.receive_json()
        assert write["op"] == "write" and base64.b64decode(write["data"]) == b"\x01\x02"
        ws.send_json({"id": write["id"], "ok": True, "written": 2})
        read = ws.receive_json()
        assert read["op"] == "read" and read["size"] == 512
        ws.send_json({"id": read["id"], "ok": True, "data": base64.b64encode(b"pong").decode()})
        worker.join(timeout=5)
        assert result == {"written": 2, "read": b"pong"}

    for _ in range(100):  # the server notices the close on its own loop
        if relay.current() is None:
            break
        threading.Event().wait(0.02)
    assert relay.current() is None


def test_websocket_relay_rejects_other_devices(client):
    with client.websocket_connect("/api/usb-relay") as ws:
        ws.send_json({"type": "hello", "vendorId": 0x1234, "productId": 1})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert relay.current() is None


def test_login_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(access.ALLOWED_ENV, "Owner@Example.com, other@example.com")
    app = create_app(Library(tmp_path / "lib.sqlite3"), caller_factory=lambda: FakeClaude([]))
    # Through Tailscale Serve: a ts.net host and X-Forwarded-For on every request.
    tailnet = TestClient(app, base_url="https://mini.tail1234.ts.net:8780",
                         headers={"X-Forwarded-For": "100.64.0.2"})

    assert tailnet.get("/api/state").status_code == 403
    assert tailnet.get("/api/state", headers={"Tailscale-User-Login": "elle@example.com"}).status_code == 403
    assert tailnet.get("/api/state", headers={"Tailscale-User-Login": "owner@example.com"}).status_code == 200
    assert tailnet.get("/api/version").status_code == 200

    # A forged loopback Host still came through the proxy, so it's still checked.
    forged = TestClient(app, base_url="http://127.0.0.1:8780", headers={"X-Forwarded-For": "100.64.0.2"})
    assert forged.get("/api/state").status_code == 403

    with pytest.raises(WebSocketDisconnect), tailnet.websocket_connect("/api/usb-relay") as ws:
        ws.receive_json()


def test_login_gate_lets_a_browser_on_the_mac_itself_in(monkeypatch):
    gate = access.TailnetLoginGate(None, frozenset({"owner@example.com"}))
    local = {"client": ("127.0.0.1", 50000)}
    assert access._direct_local(local, {"host": "127.0.0.1:8780"})
    assert access._direct_local(local, {"host": "[::1]:8780"})
    assert not access._direct_local(local, {"host": "mini.tail1234.ts.net:8780"})
    assert not access._direct_local(local, {"host": "127.0.0.1:8780", "x-forwarded-for": "100.64.0.2"})
    assert not access._direct_local({"client": ("100.64.0.2", 1)}, {"host": "127.0.0.1:8780"})
    assert gate.allowed == {"owner@example.com"}


def test_no_allowlist_means_no_check(client):
    assert client.get("/api/state").status_code == 200


def test_relayed_calls_are_traced_for_the_log():
    relay.TRACE.clear()
    device = relay.RelayDevice(FakeTransport(reads=[b"\x0c\x00\x00\x28"]))
    device.write(ENDPOINT_OUT, b"\x01\x02", 2000)
    assert device.read(ENDPOINT_IN, 512, 2000) == b"\x0c\x00\x00\x28"
    with pytest.raises(relay.RelayError):
        device.read(ENDPOINT_IN, 512, 2000)
    lines = relay.trace_text().splitlines()
    assert len(lines) == 3
    assert "write" in lines[0] and "0102" in lines[0]
    assert "0c000028" in lines[1]
    assert "TIMEOUT" in lines[2]


def test_frames_are_traced_with_their_headers():
    from stompxl.helixgen.device import usb

    usb.FRAME_TRACE.clear()
    reply = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_OPEN, seq=3,
                         body=b"\x00\x02\x00\x00")
    session = Session(relay.RelayDevice(FakeTransport(reads=[reply])), XL,
                      usb_module=relay.USB_MODULE)
    session._send(cmd=CMD_OPEN, body=b"\x00\x10\x00\x00")
    session._read_frame()
    with pytest.raises(DeviceError):
        session._read_raw()
    lines = usb.frame_trace_text().splitlines()
    assert "OUT" in lines[0] and "src=1001 dst=03ef cmd=02" in lines[0]
    assert "IN" in lines[1] and "src=03ef dst=1001 cmd=02 seq=03" in lines[1] and "len=4" in lines[1]
    assert "FAIL" in lines[2]


def test_settle_collects_a_queued_keepalive_and_keeps_the_accounting():
    keepalive = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_IDLE, seq=0)
    reply = encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_OPEN, seq=1,
                         body=b"\x00\x02\x00\x00")
    session = Session(relay.RelayDevice(FakeTransport(reads=[keepalive, reply])), XL,
                      usb_module=relay.USB_MODULE)
    session._state.arg = 0x1000
    assert session.settle() == 2
    assert session._state.arg == 0x1004  # the body is credited, as _receive would


def test_a_document_write_leaves_no_timed_out_read_pending(monkeypatch):
    """Over the relay a timed-out read stays pending, which stalls op 21."""
    from stompxl.helixgen.device import writer as writer_module

    order = []
    session = Session(relay.RelayDevice(FakeTransport()), XL, usb_module=relay.USB_MODULE)
    monkeypatch.setattr(Session, "settle", lambda self: order.append("settle") or 0)

    class Stop(Exception):
        pass

    def write_document(self, payload, **_kw):
        order.append("write")
        raise Stop

    monkeypatch.setattr(writer_module.DocumentWriter, "write_document", write_document)
    with pytest.raises(Stop):
        session.push_document(b"doc", slot=3, name="x", donor=b"old")
    assert order == ["write"]


def test_the_session_reads_the_link_packet_size():
    class Endpoint:
        def __init__(self, address, size):
            self.bEndpointAddress, self.wMaxPacketSize = address, size

    class Device:
        def get_active_configuration(self):
            return {(0, 0): [Endpoint(0x81, 64), Endpoint(0x01, 64)]}

    assert Session(Device(), XL, usb_module=object()).out_packet_size == 64
    relayed = relay.RelayDevice(FakeTransport())
    assert Session(relayed, XL, usb_module=relay.USB_MODULE).out_packet_size == 512
    transport = FakeTransport()
    transport.packet_size = 64
    assert Session(relay.RelayDevice(transport), XL,
                   usb_module=relay.USB_MODULE).out_packet_size == 64


def _page(seq, body):
    return encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=0x04, seq=seq, body=body)


def _credit(seq=0):
    return encode_frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_CHUNK, seq=seq)


def test_a_credit_mid_stream_waits_for_the_page_behind_it():
    """Nov 22: over the relay a credit came where a page was due, and the pages
    followed unasked. Taking the credit for the end cut the read to 1005 bytes."""
    pages = [bytes([i]) * 256 for i in range(3)]
    transport = FakeTransport(reads=[_page(1, pages[0]), _credit(2), _page(3, pages[1]),
                                     _page(4, pages[2][:40])])
    session = Session(relay.RelayDevice(transport), XL, usb_module=relay.USB_MODULE)
    first = b"head"
    declared = len(first) + 256 * 2 + 40
    assert session._reassemble(declared, first) == first + pages[0] + pages[1] + pages[2][:40]
    # Three page requests, not four: the page after the credit came by itself.
    assert sum(1 for c in transport.calls if c["op"] == "write") == 3


def test_a_credit_with_nothing_behind_it_asks_again():
    page = bytes(range(256))
    transport = FakeTransport(reads=[_credit(1)])
    session = Session(relay.RelayDevice(transport), XL, usb_module=relay.USB_MODULE)

    real_call = transport.call

    def call(request, *, timeout_s):
        # The page only comes once it's asked for a second time.
        if request["op"] == "write" and sum(c["op"] == "write" for c in transport.calls) == 1:
            transport.reads.append(_page(2, page))
        return real_call(request, timeout_s=timeout_s)

    transport.call = call
    assert session._reassemble(4 + 256, b"head") == b"head" + page


def test_a_listing_is_not_cut_short_by_a_credit():
    import msgpack

    rows = [{slot: {109: f"Preset {slot}"}} for slot in range(40)]
    envelope = msgpack.packb({102: 1, 103: 0, 104: rows})
    head, rest = envelope[:200], envelope[200:]
    chunks = [rest[i:i + 256] for i in range(0, len(rest), 256)]
    reads = [_page(1, chunks[0]), _credit(2)] + [_page(3 + i, c) for i, c in enumerate(chunks[1:])]
    session = Session(relay.RelayDevice(FakeTransport(reads=reads)), XL,
                      usb_module=relay.USB_MODULE)
    assert session.read_stream(complete=lambda data: False) == rest
    assert msgpack.unpackb(head + rest, strict_map_key=False)[104] == rows


def test_a_listing_that_is_already_whole_ends_on_a_credit():
    import msgpack

    from stompxl.helixgen.device.usb import _parses_whole

    whole = msgpack.packb({102: 1, 104: [{0: {109: "A"}}]})
    assert _parses_whole(whole)
    assert not _parses_whole(whole[:-1])
    assert not _parses_whole(whole + b"\x00")
