"""Writing a whole preset document to the device.

The write path is the dangerous half of this protocol, and it is guarded
accordingly. A document goes into the **edit buffer** first (``op 21``) and only
reaches flash when it is committed (``op 71``). A bad document write is undone by
reloading the preset; nothing is persistent until the commit.

The four guards, none of them optional:

1. **Honour the credits.** The device answers each 512-byte unit with an empty
   ``cmd 0x08`` frame meaning "I consumed that". Never run more than one unit
   ahead, and abort if they stop. A credit's ``arg`` is the device's count of
   the bytes it has taken. On Nov 25 (#116, #117) the credits came back on time
   but lagged (16 bytes, then none, for whole 512-byte units), the host ran two
   units ahead, and the pedal locked up until it was power-cycled. So a credit
   that is behind earns the device a pause before the next unit. It's a pause
   and not a read for the missing credit: over the browser relay a read that
   times out stalls the next write, and #102 lagged the same way and finished.
2. **Always have a write timeout.** An unbounded bulk write to a device that has
   stopped draining its OUT endpoint never returns. This is what turns a stalled
   pedal into a hung program.
3. **Open the edit buffer with a read before writing.** Every ``op 21`` that has
   ever succeeded ran on a buffer a read had opened; a write straight after a
   ``goto`` stalls deterministically.
4. **Nothing reaches flash until ``op 71``.**

Two details that look like implementation noise and are not:

* **A unit is 512 payload bytes sent as 496 + 16.** A 496-byte body plus the
  16-byte header is a packet of exactly ``wMaxPacketSize``, and a bulk transfer
  made only of maximum-size packets never terminates -- it takes a short packet
  to close it. Sending 496-byte bodies back to back emits an unbroken run of
  maximum-size packets, which is a candidate cause of the "device stopped
  draining its endpoint" lockup.
* **Credits are matched strictly.** Keepalives, status-channel pushes and the
  edit apply-ACK all arrive on the same pipe and none of them is a credit.
  Counting one as permission to send is how a host gets ahead of the device.

A slow credit is not a queue that draining will clear -- backing off was tried
and failed every time. The useful response to one is to stop while the next unit
is still in hand.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

from stompxl.helixgen.device.frames import (
    CMD_CHUNK,
    CMD_DATA,
    Frame,
    encode_command,
)

logger = logging.getLogger(__name__)

#: Payload bytes the device credits as one unit.
CREDIT_UNIT: Final = 512
#: First frame of a unit. 496 + the 16-byte header is exactly wMaxPacketSize.
FIRST_FRAME_BODY: Final = 496

#: A non-final credit slower than this means the device is on its way out.
#: Measured: healthy credits land in 1-3 ms, and every wedge showed 28 ms or
#: worse one unit before it died.
SLOW_CREDIT_MS: Final = 20.0
#: The final unit's credit is legitimately slow -- that is the device committing.
#: It is excluded from the latency guard rather than given a wider bound.
#:
#: Over the browser relay every read is a network round trip (1-2 ms from a
#: browser on the Mac itself, about 50 ms from another computer on the
#: tailnet), which says nothing about the pedal, so the guard allows for it.
RELAY_SLACK_MS: Final = 60.0

#: How many frames to sift while waiting for one credit.
MAX_FRAMES_PER_CREDIT: Final = 64
#: Pause per credit unit the device is behind, before sending the next unit.
LAG_PAUSE_PER_UNIT_S: Final = 0.25
#: The longest such pause.
LAG_PAUSE_MAX_S: Final = 1.0

OP_WRITE_DOCUMENT: Final = 21
OP_SAVE_PRESET: Final = 71

#: Target key carrying the whole preset blob on an op-21 write.
K_WRITE_BLOB: Final = 110

INNER_OPCODE: Final = 0x0002


def encode_write_preset(blob: bytes, txn: int) -> bytes:
    """Build the ``op 21`` envelope: ``{102: txn, 100: 21, 101: {110: <blob>}}``.

    **The document travels inside the envelope**, as MessagePack key 110, wrapped
    as a `str`. It is not a header that announces a payload sent separately --
    getting that backwards means the device receives a command whose declared
    body never arrives, takes the frame, and stops draining its OUT endpoint.

    Hand-emitted rather than built through a MessagePack library for two reasons:
    a preset blob is not valid UTF-8, so it cannot go through a normal `str`
    encoder, and the device uses **non-minimal** `str16` framing that a canonical
    encoder would shorten.
    """
    out = bytearray(
        [
            0x83,  # fixmap(3)
            0x66,  # key 102
            0xCD,
            (txn >> 8) & 0xFF,
            txn & 0xFF,  # u16 txn
            0x64,  # key 100
            OP_WRITE_DOCUMENT,
            0x65,  # key 101
            0x81,  # fixmap(1)
            K_WRITE_BLOB,
        ]
    )
    out += _str_header(len(blob))
    out += blob
    return bytes(out)


def _str_header(length: int) -> bytes:
    """A MessagePack ``str`` length prefix, matching the device's framing.

    ``str16`` is forced across the whole 32..65535 range rather than minimised,
    because that is what the device emits.
    """
    if length < 32:
        return bytes([0xA0 | length])
    if length < 65536:
        return bytes([0xDA]) + length.to_bytes(2, "big")
    return bytes([0xDB]) + length.to_bytes(4, "big")


def encode_save_preset(bank: int, slot: int, name: str, txn: int) -> bytes:
    """Build the ``op 71`` commit: ``{107: bank, 108: slot, 109: name}``.

    The name is stored **NUL-terminated**, as HX Edit sends it.
    """
    import msgpack

    return msgpack.packb(
        {
            102: txn,
            100: OP_SAVE_PRESET,
            101: {107: bank, 108: slot, 109: name + "\0"},
        },
        use_bin_type=False,
    )


class WriteAbortedError(RuntimeError):
    """Raised when a transfer is stopped while the pedal is still recoverable.

    Raised *instead of* continuing to push bytes at a device that has stopped
    taking them. Nothing has reached flash when this is raised.
    """


@dataclass
class WriteStats:
    """What a transfer did, for logging and for tests."""

    units: int = 0
    bytes_sent: int = 0
    credits: int = 0
    strays: int = 0
    #: Credits that came back before the device had taken everything sent.
    lagging_credits: int = 0
    credit_latencies_ms: list[float] = field(default_factory=list)

    @property
    def worst_non_final_latency_ms(self) -> float:
        return max(self.credit_latencies_ms[:-1], default=0.0)


#: Packet size of a high-speed link, which the 496 + 16 split is built for.
HIGH_SPEED_PACKET: Final = 512

SLOW_LINK = (
    "The pedal is on a slow USB link (full speed, 64-byte packets), and preset "
    "writes stall at that speed: reads work, sends don't. It's usually the cable "
    "(an old one did this on Nov 22), a hub or an adapter. Plug the pedal straight "
    "in with a newer cable, then try again. Nothing was written."
)


class SlowLinkError(RuntimeError):
    """The pedal came up at USB full speed, where op 21 writes stall."""


def plan_units(payload: bytes) -> list[list[bytes]]:
    """Split a payload into credit units, each as its list of frame bodies.

    A full unit is ``[496 bytes, 16 bytes]``. The last unit carries whatever is
    left, still split so it ends on a short packet.

    Only for a high-speed link. At full speed (64-byte packets) the pedal takes
    256 bytes of a write and stops reading, however the frames are cut (smaller
    frames were tried, Nov 22, and stalled the same way), so
    :meth:`DocumentWriter.write_document` refuses up front instead.
    """
    units: list[list[bytes]] = []
    for start in range(0, len(payload), CREDIT_UNIT):
        unit = payload[start : start + CREDIT_UNIT]
        if len(unit) > FIRST_FRAME_BODY:
            units.append([unit[:FIRST_FRAME_BODY], unit[FIRST_FRAME_BODY:]])
        else:
            units.append([unit])
    return units


def is_credit_from(frame: Frame, device_channel: int) -> bool:
    """Whether ``frame`` is a real credit and not a lookalike.

    Both halves matter. Matching on the opcode alone admits the status channel's
    own page frame, which is also an empty ``cmd 0x08``.
    """
    return frame.cmd == CMD_CHUNK and not frame.body and frame.src == device_channel


class DocumentWriter:
    """Drives an ``op 21`` document write over an open :class:`~helixgen.device.usb.Session`."""

    def __init__(self, session: Any):
        self.session = session
        self.stats = WriteStats()
        #: Frames consumed while waiting for credits that were not credits. They
        #: are put back for the caller, because the apply-ACK hides in here.
        self.strays: list[Frame] = []

    # -- the transfer ------------------------------------------------------

    def slow_credit_ms(self) -> float:
        """The latency guard for this link: :data:`SLOW_CREDIT_MS` on local USB,
        plus a few network round trips when the pedal is relayed."""
        latency = float(getattr(self.session, "link_latency_ms", 0.0) or 0.0)
        if latency <= 0:
            return SLOW_CREDIT_MS
        return SLOW_CREDIT_MS + RELAY_SLACK_MS + 3 * latency

    def write_document(self, blob: bytes, *, slot: int = 0, bank: int = 0) -> WriteStats:
        """Send ``blob`` into the **edit buffer** as an ``op 21`` write.

        ``slot`` and ``bank`` are accepted for logging only: op 21 carries no
        address. The document goes to the edit buffer, and where it lands is
        decided later by the ``op 71`` commit.

        Guard 3 is the caller's to satisfy -- the edit buffer must already have
        been opened by a read.
        """
        if getattr(self.session, "out_packet_size", HIGH_SPEED_PACKET) < HIGH_SPEED_PACKET:
            raise SlowLinkError(SLOW_LINK)
        txn = self.session.next_txn()
        body = encode_command(INNER_OPCODE, encode_write_preset(blob, txn))
        units = plan_units(body)
        slow_ms = self.slow_credit_ms()

        logger.info(
            "op 21: %d byte document in a %d byte envelope, %d credit unit(s), %d frame(s)",
            len(blob),
            len(body),
            len(units),
            sum(len(frames) for frames in units),
        )

        # Where the device's byte count stands before the first unit, when the
        # session knows (a fake in a test may not).
        peer = getattr(self.session, "peer_arg", None)
        base = None if not isinstance(peer, int) else (
            peer + int(getattr(self.session, "sent_since_peer", 0) or 0)) & 0xFFFFFFFF

        for position, frames in enumerate(units):
            final = position == len(units) - 1
            for frame_body in frames:
                self.session._send(cmd=CMD_DATA, body=frame_body)
                self.stats.bytes_sent += len(frame_body)

            latency = self._await_credit(
                unit=position, final=final,
                covers=None if base is None else (base + self.stats.bytes_sent) & 0xFFFFFFFF)
            self.stats.units += 1
            self.stats.credits += 1
            self.stats.credit_latencies_ms.append(latency)

            if not final and latency > slow_ms:
                raise WriteAbortedError(
                    f"Unit {position}'s credit took {latency:.0f} ms "
                    f"(healthy is 1-3 ms plus the link's own delay, and {slow_ms:.0f} ms "
                    "or worse has always preceded a lockup). Stopping with the next unit "
                    "still in hand; nothing has reached flash."
                )

        self.txn = txn
        logger.info(
            "Edit-buffer write complete: %d unit(s), worst non-final credit %.1f ms",
            self.stats.units,
            self.stats.worst_non_final_latency_ms,
        )
        return self.stats

    def _await_credit(
        self, *, unit: int, final: bool, allow_reply: bool = False,
        covers: int | None = None,
    ) -> float:
        """Block until one real credit arrives, holding anything else.

        With ``covers``, the byte count the credit should carry: one that is
        behind it gets the device a pause before the caller sends more (see
        guard 1). Returns the wait for the credit in milliseconds, pause not
        included. Strays are kept rather than dropped: the edit apply-ACK
        arrives as one, and a write whose ACK lands mid-transfer would otherwise
        be reported as unacknowledged.
        """
        started = time.perf_counter()
        device_channel = self.session._state.channel.device

        for _ in range(MAX_FRAMES_PER_CREDIT):
            frame = self.session._receive()
            if is_credit_from(frame, device_channel):
                waited = (time.perf_counter() - started) * 1000.0
                behind = 0 if covers is None else (covers - frame.arg) & 0xFFFFFFFF
                if 0 < behind < 0x80000000 and not final:
                    self.stats.lagging_credits += 1
                    pause = min(LAG_PAUSE_MAX_S,
                                LAG_PAUSE_PER_UNIT_S * -(-behind // CREDIT_UNIT))
                    logger.warning("Unit %d: the pedal has taken %d byte(s) less than sent; "
                                   "pausing %.2f s", unit, behind, pause)
                    time.sleep(pause)
                return waited
            self.strays.append(frame)
            self.stats.strays += 1
            if allow_reply and frame.body:
                # The op-21 header is answered rather than credited.
                return (time.perf_counter() - started) * 1000.0
            logger.debug(
                "Held a non-credit frame while waiting on unit %d: cmd=0x%02x src=0x%04x len=%d",
                unit,
                frame.cmd,
                frame.src,
                frame.declared_body_len,
            )

        raise WriteAbortedError(
            f"No credit for unit {unit} after {MAX_FRAMES_PER_CREDIT} frames. "
            "The device has stopped acknowledging; nothing has reached flash."
        )
