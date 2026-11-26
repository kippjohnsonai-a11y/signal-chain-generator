"""Writer tests -- the guards, exercised against a fake session.

Every failure mode here was observed on real hardware and cost somebody a wedged
pedal, so each one gets a test that does not need a pedal to run.
"""

from __future__ import annotations

import pytest

from stompxl.helixgen.device.frames import (
    CMD_CHUNK,
    CMD_DATA,
    CMD_IDLE,
    EDIT,
    PRIMARY,
    Frame,
    encode_command,
)
from stompxl.helixgen.device.writer import (
    CREDIT_UNIT,
    FIRST_FRAME_BODY,
    INNER_OPCODE,
    SLOW_CREDIT_MS,
    DocumentWriter,
    WriteAbortedError,
    encode_write_preset,
    is_credit_from,
    plan_units,
)


def _frame(cmd: int, *, src: int = PRIMARY.device, body: bytes = b"") -> Frame:
    return Frame(
        src=src, dst=PRIMARY.host, cmd=cmd, seq=0, arg=0, body=body,
        declared_body_len=len(body),
    )


def _credit() -> Frame:
    return _frame(CMD_CHUNK)


class FakeSession:
    """Stands in for a Session: records sends, serves scripted replies."""

    def __init__(self, replies: list[Frame], *, latency: float = 0.0):
        self._replies = list(replies)
        self._latency = latency
        self.sent: list[tuple[int, bytes]] = []
        self._txn = 0

        class _State:
            channel = PRIMARY

        self._state = _State()

    def _send(self, *, cmd: int, body: bytes, **_: object) -> None:
        self.sent.append((cmd, body))

    def _receive(self, **_: object) -> Frame:
        if self._latency:
            import time

            time.sleep(self._latency)
        if not self._replies:
            raise AssertionError("writer read more frames than the test scripted")
        return self._replies.pop(0)

    def next_txn(self) -> int:
        self._txn += 1
        return self._txn

    @property
    def data_bodies(self) -> list[bytes]:
        return [body for cmd, body in self.sent if cmd == CMD_DATA]


class TestPlanUnits:
    def test_a_full_unit_splits_496_then_16(self):
        """Never two maximum-size packets in a row -- see the module docstring."""
        units = plan_units(b"\x00" * CREDIT_UNIT)
        assert len(units) == 1
        assert [len(f) for f in units[0]] == [FIRST_FRAME_BODY, 16]

    def test_short_payload_is_one_short_frame(self):
        units = plan_units(b"\x00" * 100)
        assert [len(f) for f in units[0]] == [100]

    def test_payload_exactly_at_the_split_boundary(self):
        units = plan_units(b"\x00" * FIRST_FRAME_BODY)
        assert [len(f) for f in units[0]] == [FIRST_FRAME_BODY]

    def test_trailing_partial_unit(self):
        units = plan_units(b"\x00" * (CREDIT_UNIT + 8))
        assert [len(f) for f in units[0]] == [FIRST_FRAME_BODY, 16]
        assert [len(f) for f in units[1]] == [8]

    def test_every_frame_body_stays_under_max_packet_size(self):
        """A 496-byte body plus a 16-byte header is exactly 512; never more."""
        for size in (1, 300, 512, 1000, 2446, 6817):
            for unit in plan_units(b"\x00" * size):
                for body in unit:
                    assert len(body) + 16 <= 512

    def test_units_reassemble_to_the_original_payload(self):
        payload = bytes(range(256)) * 12
        rebuilt = b"".join(body for unit in plan_units(payload) for body in unit)
        assert rebuilt == payload

    def test_unit_count_matches_the_credit_arithmetic(self):
        payload = b"\x00" * 2446
        assert len(plan_units(payload)) == (2446 + CREDIT_UNIT - 1) // CREDIT_UNIT


class TestCreditMatching:
    def test_an_empty_chunk_frame_from_our_channel_is_a_credit(self):
        assert is_credit_from(_frame(CMD_CHUNK), PRIMARY.device)

    def test_a_chunk_frame_from_another_channel_is_not(self):
        """The status channel sends its own empty cmd 0x08 page frames."""
        assert not is_credit_from(_frame(CMD_CHUNK, src=EDIT.device), PRIMARY.device)

    def test_a_keepalive_is_not_a_credit(self):
        assert not is_credit_from(_frame(CMD_IDLE), PRIMARY.device)

    def test_the_apply_ack_is_not_a_credit(self):
        ack = _frame(CMD_DATA, body=b"\x83\x66\x01\x67\x01")
        assert not is_credit_from(ack, PRIMARY.device)

    def test_a_chunk_frame_carrying_payload_is_not_a_credit(self):
        assert not is_credit_from(_frame(CMD_CHUNK, body=b"\x01"), PRIMARY.device)


class TestTransfer:
    def test_a_clean_write_sends_every_unit_and_counts_its_credits(self):
        blob = b"\x00" * (CREDIT_UNIT * 2)
        session = FakeSession([_credit()] * 8)
        writer = DocumentWriter(session)
        stats = writer.write_document(blob, slot=3)

        # The envelope wraps the blob, so more bytes go out than came in.
        assert stats.bytes_sent > len(blob)
        assert stats.units == stats.credits
        assert stats.units == len(plan_units(
            encode_command(0x0002, encode_write_preset(blob, 1))
        ))

    def test_frames_go_out_in_496_16_pairs(self):
        blob = b"\x00" * 600  # with the envelope this exceeds one full unit
        session = FakeSession([_credit()] * 6)
        DocumentWriter(session).write_document(blob, slot=0)

        bodies = [len(b) for b in session.data_bodies]
        assert bodies[0] == FIRST_FRAME_BODY
        assert all(n + 16 <= 512 for n in bodies)

    def test_the_whole_envelope_is_chunked_not_a_header_then_payload(self):
        """The document rides *inside* the op-21 envelope.

        Sending a header that announces a payload delivered separately is what
        leaves the device waiting on a body that never arrives.
        """
        blob = b"\xAB" * 100
        session = FakeSession([_credit()] * 4)
        DocumentWriter(session).write_document(blob, slot=0)

        sent = b"".join(session.data_bodies)
        assert blob in sent
        assert sent == encode_command(0x0002, encode_write_preset(blob, 1))

    def test_strays_are_held_not_dropped(self):
        """The apply-ACK arrives as a stray and the caller still needs it."""
        ack = _frame(CMD_DATA, body=b"\x83\x66\x01\x67\x01")
        session = FakeSession([ack, _credit(), _credit()])
        writer = DocumentWriter(session)
        writer.write_document(b"\x00" * 10, slot=0)

        assert ack in writer.strays
        assert writer.stats.strays >= 1

    def test_a_status_push_does_not_satisfy_a_credit_wait(self):
        """Counting a foreign frame as a credit is how a host gets ahead."""
        push = _frame(CMD_CHUNK, src=EDIT.device)
        session = FakeSession([push, _credit(), _credit()])
        writer = DocumentWriter(session)
        writer.write_document(b"\x00" * 10, slot=0)

        assert push in writer.strays
        assert writer.stats.credits == 1


class TestGuards:
    def test_credits_running_out_aborts_rather_than_hanging(self):
        session = FakeSession([_frame(CMD_IDLE)] * 80)
        with pytest.raises(WriteAbortedError, match="stopped acknowledging"):
            DocumentWriter(session).write_document(b"\x00" * 10, slot=0)

    def test_a_slow_non_final_credit_aborts_the_transfer(self):
        """Backing off was tried and failed 3 for 3; the response is to stop."""
        latency = (SLOW_CREDIT_MS + 15) / 1000.0
        session = FakeSession([_credit()] * 8, latency=latency)
        with pytest.raises(WriteAbortedError, match="credit took"):
            DocumentWriter(session).write_document(b"\x00" * (CREDIT_UNIT * 2), slot=0)

    def test_the_final_credit_may_be_slow(self):
        """A slow last credit is the device committing -- 2-195 ms is normal."""
        latency = (SLOW_CREDIT_MS + 15) / 1000.0
        session = FakeSession([_credit()] * 2, latency=latency)
        stats = DocumentWriter(session).write_document(b"\x00" * 10, slot=0)
        assert stats.units == 1  # single unit, so its credit is the final one

    def test_abort_message_says_nothing_reached_flash(self):
        session = FakeSession([_frame(CMD_IDLE)] * 80)
        with pytest.raises(WriteAbortedError) as excinfo:
            DocumentWriter(session).write_document(b"\x00" * 10, slot=0)
        assert "flash" in str(excinfo.value)

    def test_the_writer_never_sends_a_save(self):
        """Committing is the session's call, not the writer's -- guard 4."""
        from stompxl.helixgen.device.writer import OP_SAVE_PRESET

        session = FakeSession([_credit()] * 8)
        DocumentWriter(session).write_document(b"\x00" * CREDIT_UNIT, slot=0)
        sent = b"".join(session.data_bodies)
        assert bytes([0x64, OP_SAVE_PRESET]) not in sent


class TestLinks:
    """A full-speed link is refused up front; a relayed one gets latency slack."""

    def test_a_full_speed_link_is_refused_before_anything_is_sent(self):
        from stompxl.helixgen.device.writer import SlowLinkError

        session = FakeSession([_credit()] * 20)
        session.out_packet_size = 64
        with pytest.raises(SlowLinkError, match="cable"):
            DocumentWriter(session).write_document(bytes(1500), slot=0)
        assert session.sent == []

    def test_a_relayed_link_allows_for_its_round_trip(self):
        """About 50 ms a call from another computer: not a wedged pedal."""
        latency = (SLOW_CREDIT_MS + 30) / 1000.0
        session = FakeSession([_credit()] * 8, latency=latency)
        session.link_latency_ms = 50.0
        stats = DocumentWriter(session).write_document(b"\x00" * (CREDIT_UNIT * 2), slot=0)
        assert stats.units == 3

    def test_local_usb_keeps_the_tight_guard(self):
        session = FakeSession([])
        assert DocumentWriter(session).slow_credit_ms() == SLOW_CREDIT_MS


class TestLaggingCredits:
    """Nov 25 (#116, #117): credits on time, but behind what was sent; then a lockup."""

    @staticmethod
    def _session(args: list[int], monkeypatch):
        from stompxl.helixgen.device import writer

        pauses: list[float] = []
        monkeypatch.setattr(writer.time, "sleep", pauses.append)
        replies = [Frame(src=PRIMARY.device, dst=PRIMARY.host, cmd=CMD_CHUNK, seq=0, arg=a,
                         body=b"", declared_body_len=0) for a in args]
        session = FakeSession(replies)
        session.peer_arg = 0x258
        session.sent_since_peer = 0
        return session, pauses

    def _blob_units(self, units: int) -> tuple[bytes, int]:
        # The envelope adds a few bytes; size the blob so the body is `units` units.
        blob = b"\x00" * (units * CREDIT_UNIT - 64)
        body_len = len(encode_command(INNER_OPCODE, encode_write_preset(blob, 1)))
        return blob, -(-body_len // CREDIT_UNIT)

    def test_credits_that_keep_up_get_no_pause(self, monkeypatch):
        blob, n = self._blob_units(3)
        session, pauses = self._session([0x258 + CREDIT_UNIT * (i + 1) for i in range(n)],
                                        monkeypatch)
        stats = DocumentWriter(session).write_document(blob)
        assert stats.lagging_credits == 0 and pauses == []

    def test_a_credit_behind_what_was_sent_pauses_before_the_next_unit(self, monkeypatch):
        blob, n = self._blob_units(4)
        # #117's pattern: 496 taken of the first unit, then 512, then nothing more.
        args = [*[0x448, 0x458, 0x648, 0x648][: n - 1], 0x258 + 4096]
        session, pauses = self._session(args[:n], monkeypatch)
        stats = DocumentWriter(session).write_document(blob)
        assert stats.lagging_credits == n - 1
        assert pauses and all(0 < p <= 1.0 for p in pauses)
        assert pauses[-1] > pauses[0]          # further behind, longer pause

    def test_without_a_byte_count_nothing_changes(self, monkeypatch):
        blob, n = self._blob_units(2)
        session, pauses = self._session([0] * n, monkeypatch)
        session.peer_arg = None
        DocumentWriter(session).write_document(blob)
        assert pauses == []
