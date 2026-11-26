"""Surgical block edits -- path A.

A whole-document write (``op 21``) can change a block's **values** but not its
**model**: the device accepts the transfer, commits it, and stores an empty
preset. Whatever it validates about a model is not carried in the block record,
and is undecoded. Changing a model is what these ops are for.

The sequence for putting a generated tone on the pedal:

1. ``op 20`` select the target slot, so the edit buffer holds it.
2. **Read**, which is what opens the buffer. Never edit straight after a select.
3. Per block: ``op 40`` swap the model, then ``op 30`` for each parameter the
   tone names, then ``op 41`` for its bypass state.
4. ``op 71`` commit the buffer to the slot.

``op 40`` **resets the block's parameters to the new model's defaults**, which is
why the value edits follow the swap and not the other way round -- and why a tone
that names only some parameters still lands on sensible values for the rest.

Every edit is acknowledged, and **the acknowledgement is matched by the
transaction id echoed at key 102**, never by arrival order. The device
interleaves keepalives, credits and status pushes on the same pipe; taking one of
those as the ACK reports an edit as applied on the strength of a frame that says
nothing about it, and shifts every later reply by one.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

OP_SELECT_PRESET: Final = 20
OP_DELETE_BLOCK: Final = 28
OP_SET_VALUE: Final = 30
OP_SWAP_MODEL: Final = 40
OP_BYPASS: Final = 41
OP_SAVE_PRESET: Final = 71

#: Target keys.
K_SLOT: Final = 98
K_BANK: Final = 107
K_PRESET: Final = 108
K_NAME: Final = 109
#: The model-ref sub-map, nested *inside* the target. Distinct from the
#: envelope's own key 100, which is the operation.
K_MODEL_REF: Final = 100
K_MODEL_FLAG: Final = 23
K_MODEL_INDEX: Final = 25
K_PAIRED_INDEX: Final = 26
#: On a set-value: which index space key 28 addresses, and the value itself.
K_IS_PARAM: Final = 29
K_MODEL_SEL: Final = 26
K_PARAM_INDEX: Final = 28
K_VALUE: Final = 119
#: Carries **enabled**, despite the op being called bypass. See set_enabled.
K_ENABLED: Final = 59

#: Sub-model selectors for a set-value.
MODEL_MAIN: Final = 0
MODEL_PAIRED: Final = 1

#: ``-306`` on a swap means the model does not fit the DSP budget.
ERR_DSP_BUDGET: Final = -306

NO_PAIRED: Final = -1

#: Pause between committing the chain and writing the layout. The commit
#: (op 71) returns before the pedal has finished storing the preset and
#: rebuilding its DSP; a layout write started straight after it (about a second
#: later, on Nov 23) saw its third credit take 107 ms and the pedal wedge.
#: Write tests with no commit before them ran clean at 1-3 ms per credit.
LAYOUT_SETTLE_SECONDS: Final = 2.0
#: Pause after loading the slot for the layout write, before reading it.
LAYOUT_SELECT_SETTLE_SECONDS: Final = 1.5
#: A layout write stopped on a slow credit (nothing in flash) is tried again.
LAYOUT_ATTEMPTS: Final = 2
#: How often a pause with a session open checks for the pedal's keepalives.
KEEPALIVE_POLL_SECONDS: Final = 0.5


class EditError(RuntimeError):
    """Raised when the device refuses an edit."""


class DspBudgetError(EditError):
    """Raised when a model will not fit in the remaining DSP budget."""


#: A near-free model parked in each slot before the tone's blocks go in.
PLACEHOLDER_MODEL: Final = "HD2_VolPanGain"

#: Which blocks are placed first: the ones a preset can least do without.
_PLACEMENT_ORDER: Final = ("Amp", "Cab", "Distortion", "Dynamics", "Routing", "Delay",
                           "Reverb", "Modulation", "Wah", "Volume/Pan", "EQ", "Filter",
                           "Pitch/Synth")


def _placement_rank(block: dict, catalog: Any) -> int:
    model = str(block.get("@model", ""))
    category = None
    if catalog is not None:
        for candidate in catalog.models():
            if candidate.internal_name == model:
                category = candidate.category
                break
    if category is None:
        if "Amp" in model or "Preamp" in model:
            category = "Amp"
        elif "Cab" in model:
            category = "Cab"
    return _PLACEMENT_ORDER.index(category) if category in _PLACEMENT_ORDER else len(_PLACEMENT_ORDER)


@dataclass
class EditReport:
    """What an apply actually managed to do."""

    swapped: list[str] = field(default_factory=list)
    values_set: int = 0
    #: Blocks the tone asked to be switched off.
    bypassed: list[str] = field(default_factory=list)
    footswitches: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    #: Parameters put under snapshot control, as ``"dsp0.block2.Drive"``.
    snapshot_controls: list[str] = field(default_factory=list)
    #: Controller assignments the tone makes that could not be written.
    unsupported_controllers: list[str] = field(default_factory=list)
    #: Parameters put on an expression pedal, as ``"dsp0.block2.Pedal -> EXP 1"``.
    expression_controls: list[str] = field(default_factory=list)
    #: How many USB sessions the run needed; it renews before the sequence wrap.
    sessions: int = 1
    #: Parameters skipped because their value had no wire representation.
    unsupported: list[str] = field(default_factory=list)
    #: Blocks that could not be placed at all.
    skipped: list[str] = field(default_factory=list)
    #: The Input block's noise gate as written, e.g. ``"on at -55 dB"``.
    input_gate: str = ""
    #: The main output as written, e.g. ``"level -3 dB, pan 0.50"``.
    output: str = ""
    #: Preset-level settings carried over from an imported preset.
    device_settings: int = 0
    #: Cab blocks absorbed into an amp's own slot, as ``"block -> cab symbol"``.
    fused: list[str] = field(default_factory=list)
    #: Cab parameters that the paired cab model has no home for.
    dropped_cab_params: list[str] = field(default_factory=list)
    #: Why the footswitch/snapshot write failed, when it did. The chain itself
    #: was already committed by then, so the preset plays; only the layout is
    #: missing.
    layout_error: str = ""
    #: The document the layout write tried to send, kept for diagnosis.
    layout_document: bytes = b""
    #: Slots still holding a block from the slot's previous preset after the
    #: run, which the pedal then plays on top of the tone.
    leftover: list[int] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{len(self.swapped)} model(s) swapped, {self.values_set} value(s) set, "
            f"{len(self.bypassed)} block(s) left off"
        ]
        lines.extend(f"  fused {entry}" for entry in self.fused)
        if self.dropped_cab_params:
            lines.append(
                f"  cab params with no paired equivalent: "
                f"{', '.join(self.dropped_cab_params)}"
            )
        if self.footswitches:
            lines.append(f"  footswitches: {', '.join(self.footswitches)}")
        if self.snapshots:
            lines.append(f"  snapshots: {', '.join(self.snapshots)}")
        if self.input_gate:
            lines.append(f"  input noise gate: {self.input_gate}")
        if self.output:
            lines.append(f"  output: {self.output}")
        if self.device_settings:
            lines.append(f"  preset settings carried over from the import: {self.device_settings}")
        if self.snapshot_controls:
            lines.append(f"  snapshot-controlled: {', '.join(self.snapshot_controls)}")
        if self.expression_controls:
            lines.append(f"  expression pedal: {', '.join(self.expression_controls)}")
        if self.unsupported_controllers:
            lines.append(
                f"  controllers not written: {', '.join(self.unsupported_controllers)}"
            )
        lines.extend(f"  swapped {name}" for name in self.swapped)
        if self.unsupported:
            lines.append(f"  unsupported values: {', '.join(self.unsupported)}")
        lines.extend(f"  SKIPPED {name}" for name in self.skipped)
        if self.leftover:
            lines.append("  OLD BLOCKS STILL ON THE PEDAL (couldn't be cleared): slot "
                         + ", ".join(str(i) for i in self.leftover))
        return "\n".join(lines)


def enum_index(catalog: Any, model_name: str, parameter: str, label: str) -> int | None:
    """Resolve a parameter value written as a label to its wire index.

    An ``.hlx`` names enum values the way the UI shows them -- a compressor's
    ratio is ``"2:1"``, not ``0``. The catalog carries the mapping, so this is a
    lookup rather than a guess; without a catalog the value simply has no wire
    form and is reported.
    """
    if catalog is None:
        return None
    try:
        model = catalog.get(model_name)
        definition = model.get_parameter(parameter)
    except Exception:  # noqa: BLE001 - an unknown model or parameter is not fatal
        return None
    index = getattr(definition, "reverse_map", {}).get(label)
    return index if isinstance(index, int) else None


def _wire_candidates(value: Any) -> list[Any]:
    """Wire forms to try for an ``.hlx`` value, most likely first.

    A bool is only ever a switch. A number may be either a continuous parameter
    (float32) or an enum index (int), and the JSON type does not settle it --
    ``Angle: 45`` is a continuous mic angle while ``Mic: 3`` is an enum. Strings
    name an enum by its label and have no wire form without the catalog's value
    list, so they get none.
    """
    if isinstance(value, bool):
        return [value]
    if isinstance(value, int):
        # 0/1 may be a switch written as a number -- an amp's hum switch is one.
        if value in (0, 1):
            return [float(value), value, bool(value)]
        return [float(value), value]
    if isinstance(value, float):
        return [float(value), int(value)] if value.is_integer() else [float(value)]
    return []


def wire_value(value: Any) -> Any | None:
    """Convert an ``.hlx`` parameter value to its wire form.

    **The type must match the parameter exactly** -- the device refuses a value
    of the wrong type rather than coercing it. Continuous knobs are floats,
    enum/list parameters are ints, switches are bools. A string (an enum named by
    its label, like a compressor's ``"2:1"``) has no representation here without
    the catalog's value list, so it comes back ``None`` to be reported rather
    than guessed at.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return float(value)
    return None


class BlockEditor:
    """Drives surgical edits over an open :class:`~helixgen.device.usb.Session`."""

    def __init__(self, session: Any):
        self.session = session

    # -- primitives --------------------------------------------------------

    def select_preset(self, bank: int, slot: int) -> None:
        """Load a preset into the edit buffer. **Changes what the pedal shows.**"""
        self._edit(OP_SELECT_PRESET, {K_BANK: bank, K_PRESET: slot})

    def swap_model(self, slot: int, model_index: int, paired: int = NO_PAIRED) -> None:
        """Replace the model of the block in ``slot``.

        The device resets that block's parameters to the new model's defaults, so
        any value the tone specifies must be sent afterwards.

        ``23`` is the paired-model-active flag and mirrors ``paired >= 0``: with
        it false the device stores the paired index but never instantiates the
        cab.
        """
        try:
            self._edit(
                OP_SWAP_MODEL,
                {
                    K_SLOT: slot,
                    K_MODEL_REF: {
                        K_MODEL_FLAG: paired >= 0,
                        K_MODEL_INDEX: model_index,
                        K_PAIRED_INDEX: paired,
                    },
                },
            )
        except EditError as exc:
            if str(ERR_DSP_BUDGET) in str(exc):
                raise DspBudgetError(
                    f"Model {model_index} does not fit the remaining DSP budget "
                    f"at slot {slot}."
                ) from exc
            raise

    def set_value(
        self,
        slot: int,
        param_index: int,
        value: Any,
        *,
        model_sel: int = MODEL_MAIN,
    ) -> None:
        """Set one parameter of a block."""
        self._edit(
            OP_SET_VALUE,
            {
                K_SLOT: slot,
                K_IS_PARAM: True,
                K_MODEL_SEL: model_sel,
                K_PARAM_INDEX: param_index,
                K_VALUE: value,
            },
        )

    def try_set_value(
        self, slot: int, param_index: int, raw: Any, *, model_sel: int = MODEL_MAIN
    ) -> bool:
        """Set a parameter, resolving its wire type by trying the plausible ones.

        The device enforces a parameter's type exactly and refuses anything else,
        but an ``.hlx`` does not say which type a parameter is: a mic distance
        reads as ``1`` and an amp drive as ``0.2``, and both are continuous. Since
        a refusal is cheap and harmless -- the device rejects the edit and
        changes nothing -- the type is resolved by attempting the candidates
        rather than by guessing from the JSON type alone.

        Returns whether any candidate was accepted.
        """
        for candidate in _wire_candidates(raw):
            try:
                self.set_value(slot, param_index, candidate, model_sel=model_sel)
            except EditError:
                continue
            return True
        return False

    def set_enabled(self, slot: int, enabled: bool) -> None:
        """Switch a block on or off.

        An explicit state, not a toggle -- the device takes the bool.

        **Key 59 carries *enabled*, not *bypassed*.** The op is named "bypass"
        in the prior art, and its capture is a bypass press sending ``true`` then
        ``false``, which says nothing about polarity. Measured directly against a
        block's stored ``enabled`` flag: ``59: true`` turns the block **on**.
        Reading the name as "bypassed" inverts every block in the preset, which
        is silent -- the write succeeds and the chain is correct, it is just
        entirely switched off.
        """
        self._edit(OP_BYPASS, {K_SLOT: slot, K_ENABLED: enabled})

    def save_preset(self, bank: int, slot: int, name: str) -> None:
        """Commit the edit buffer to flash. Nothing is persistent before this."""
        self._edit(OP_SAVE_PRESET, {K_BANK: bank, K_PRESET: slot, K_NAME: name + "\x00"})

    # -- transport ---------------------------------------------------------

    def _edit(self, op: int, target: Any) -> Any:
        from stompxl.helixgen.device.usb import DeviceRefusedError

        try:
            return self.session.command(op=op, target=target)
        except DeviceRefusedError as exc:
            raise EditError(f"the device refused op {op}: {exc}") from exc

    def delete_block(self, slot: int) -> None:
        """Empty a slot.

        Surgical: the device clears the slot and drops any footswitch binding of
        that block alone, leaving other bindings intact.
        """
        self._edit(OP_DELETE_BLOCK, {K_SLOT: slot})


class EditSession:
    """A session that renews itself before the sequence wrap kills it.

    Applying a tone costs far more frames than one session survives: two full
    document reads, a swap and a handful of value edits per block, and a
    whole-document write at the end. The header's sequence byte wraps at 256 and
    the device stops answering across the rollover, so a long edit run has to be
    split across sessions.

    Renewing is not just reconnecting. The edit buffer is the device's, tied to
    the loaded preset, so the run **commits before it lets go** and re-selects
    and re-reads afterwards -- the read being what reopens the buffer. Dropping
    the session without committing would discard every edit since the last save.
    """

    def __init__(self, bank: int, slot: int, name: str, info: Any = None):
        from stompxl.helixgen.device.usb import Session

        self._open = Session.open
        self.bank = bank
        self.slot = slot
        self.name = name
        self.info = info
        self.session = self._open(info)
        self.sessions = 1
        try:
            self._prepare()
        except BaseException:
            # A failed select or read must not keep the interface claimed: the
            # next open would then fail as if HX Edit held the pedal.
            self.session.close()
            raise

    def _prepare(self) -> None:
        """Load the slot and open its edit buffer."""
        BlockEditor(self.session).select_preset(self.bank, self.slot)
        # The read is what opens the edit buffer; editing straight after a
        # select stalls deterministically.
        self.document = self.session.read_slot(bank=self.bank, slot=self.slot)

    def renew_if_needed(self) -> None:
        if not self.session.near_sequence_wrap:
            return
        BlockEditor(self.session).save_preset(self.bank, self.slot, self.name)
        self.session.close()
        self.session = self._open(self.info)
        self.sessions += 1
        try:
            self._prepare()
        except BaseException:
            self.session.close()
            raise

    def commit(self) -> None:
        BlockEditor(self.session).save_preset(self.bank, self.slot, self.name)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> EditSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def plan_chain(
    preset: dict,
    symbols: Any,
    amps: Any,
    *,
    fuse_cabs: bool = True,
) -> tuple[list[dict], list[str]]:
    """Work out what goes in each slot, fusing each amp with its own cab.

    An amp and its cab are **one block** on the pedal, not two: the cab rides in
    the amp's slot as a paired model, which is how a preset with an amp and six
    effects still fits a Stomp's eight slots. A tone that lists them separately
    is describing the same thing in two slots, so the cab is absorbed and the
    chain closes up behind it.

    The cab used is the amp's own default (``cablink``), not whatever cab the
    tone named. That is not a shortcut: a paired cab is a **different model**
    from a standalone one -- no mic, position or angle -- and only 20 of the 92
    standalone cabs have a paired counterpart at all, so honouring the tone's
    choice would fail for most of them.

    Returns the planned entries and the names of the absorbed cab blocks.
    """
    from stompxl.helixgen.device.codec import _resolve_symbol, tone_blocks

    blocks = tone_blocks(preset)
    resolved = [(n, b, _resolve_symbol(symbols, b)) for n, b in blocks]

    planned: list[dict] = []
    absorbed: list[str] = []
    index = 0
    while index < len(resolved):
        block_name, block, symbol = resolved[index]
        paired = NO_PAIRED
        cab_block = None
        cab_name = None

        if fuse_cabs and amps is not None and amps.is_amp(symbol.symbol):
            cab_symbol = amps.paired_cab(symbol.symbol)
            entry = symbols.by_symbol(cab_symbol) if cab_symbol else None
            if entry is not None:
                paired = entry.index
                # A cab the tone lists right after the amp is that same cab
                # described separately; take its settings and drop the block.
                if index + 1 < len(resolved):
                    following = resolved[index + 1]
                    if following[2].symbol.startswith("HD2_Cab"):
                        cab_block = following[1]
                        cab_name = following[0]
                        asked = following[2].symbol
                        note = f"{following[0]} -> {entry.symbol}"
                        if asked != entry.symbol:
                            # The amp's own cab replaces the tone's choice; say
                            # so, because it is an audible substitution.
                            note += f" (tone asked for {asked})"
                        absorbed.append(note)
                        index += 1

        planned.append(
            {
                "name": block_name,
                "block": block,
                "symbol": symbol,
                "paired": paired,
                "paired_symbol": symbols.by_index(paired) if paired >= 0 else None,
                "cab_block": cab_block,
                "cab_name": cab_name,
            }
        )
        index += 1

    return planned, absorbed


def apply_tone(
    preset: dict,
    symbols: Any,
    *,
    bank: int = 0,
    slot: int,
    name: str | None = None,
    catalog: Any = None,
    info: Any = None,
    amps: Any = None,
    fuse_cabs: bool = True,
    device_settings: dict | None = None,
) -> EditReport:
    """Put a generated tone on the pedal with surgical edits, then commit.

    ``device_settings`` are preset-level values read off another preset on the
    same device (undecoded, keyed ``{"5": {"30": 7}, ...}``); each is written
    only where the slot holds the same setting with the same type.

    This is the path that can change models, which a whole-document write cannot.
    The order matters: the select loads the slot, the read opens its edit buffer,
    each swap resets that block's parameters to the new model's defaults, and only
    then are the tone's own values sent.
    """
    report = EditReport()
    preset_name = name or preset.get("data", {}).get("meta", {}).get("name") or "helixgen"

    run = EditSession(bank, slot, preset_name, info)
    if run.document is None:
        run.close()
        raise EditError(f"slot {slot} is unpopulated; select a slot holding a preset")

    try:
        placed, targets = _apply_chain(run, preset, symbols, report, catalog=catalog,
                                       amps=amps, fuse_cabs=fuse_cabs)
    except BaseException:
        # Whatever stopped the run (the pedal dropping, a refusal), let go of
        # the interface: a claim left behind makes the next send fail as if
        # HX Edit held the pedal. Nothing was committed.
        run.close()
        raise
    from stompxl.helixgen.device.writer import WriteAbortedError

    try:
        # A fresh session for the layout: the run above has spent most of its
        # frame budget, and the write is another dozen on top of a full read.
        run.session.close()
        for attempt in range(1, LAYOUT_ATTEMPTS + 1):
            before = copy.deepcopy(report)
            try:
                _layout_session(run, info, bank, slot)
                _apply_layout(
                    run.session,
                    preset,
                    placed,
                    targets,
                    bank=bank,
                    slot=slot,
                    name=preset_name,
                    catalog=catalog,
                    report=report,
                    symbols=symbols,
                    device_settings=device_settings,
                )
                break
            except WriteAbortedError as exc:
                # Stopped on a slow credit with nothing in flash: the pedal was
                # still busy, and a second send went through (#94).
                if attempt == LAYOUT_ATTEMPTS:
                    raise
                logger.info("Layout write to slot %d stopped early (%s); trying again",
                            slot, exc)
                report.__dict__.update(before.__dict__)
                with contextlib.suppress(Exception):
                    run.session.close()
    except Exception as exc:  # noqa: BLE001 - the chain is committed; say what is missing
        logger.warning("Layout write to slot %d failed: %s", slot, exc)
        report.layout_error = str(exc) or type(exc).__name__
    finally:
        run.close()

    report.sessions = run.sessions
    return report


def _layout_session(run: EditSession, info: Any, bank: int, slot: int) -> None:
    """Open a fresh session on ``slot`` for the layout write, giving the pedal
    time to settle on both sides of loading the slot."""
    # Let the pedal finish storing the commit (or the aborted write) first.
    time.sleep(LAYOUT_SETTLE_SECONDS)
    run.session = run._open(info)
    run.sessions += 1
    # Load the slot before reading it, as the edit run does: the read only
    # opens the edit buffer of the preset the pedal has loaded, and an op 21
    # onto a buffer that is not open stalls partway through its first frame.
    BlockEditor(run.session).select_preset(bank, slot)
    # Loading rebuilds the DSP: on Nov 23 (#94) the select's own credit took
    # 200 ms, and a write started straight after it ran slow.
    _wait_answering(run.session, LAYOUT_SELECT_SETTLE_SECONDS)


def _wait_answering(session: Any, seconds: float) -> None:
    """Pause with a session open, answering the pedal's keepalives meanwhile.

    The pedal sends one after about 1.5 s of quiet. On Nov 23 (#101) one went
    unanswered during this pause, the write that followed got a 103 ms credit,
    and the pedal locked up. Live edit answers them for the same reason.
    """
    settle = getattr(session, "settle", None)
    answer = getattr(session, "keepalive", None)
    relayed = type(getattr(session, "_device", None)).__name__ == "RelayDevice"
    if settle is None or answer is None or relayed:
        # Over the browser relay a read that times out stalls the next write.
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(KEEPALIVE_POLL_SECONDS, left))
        before = session.keepalives_seen
        settle()
        if session.keepalives_seen > before:
            answer()


def _apply_chain(run: EditSession, preset: dict, symbols: Any, report: EditReport, *,
                 catalog: Any, amps: Any, fuse_cabs: bool
                 ) -> tuple[dict[str, int], dict[str, ControllerTarget]]:
    """Swap, set and switch the chain into the edit buffer, then commit it.

    Returns where each placed block landed and where its controllers aim, for
    the layout write that follows.
    """
    from stompxl.helixgen.device.document import parse

    parsed = parse(run.document)
    planned, absorbed = plan_chain(preset, symbols, amps, fuse_cabs=fuse_cabs)
    report.fused.extend(absorbed)

    # Positions are assigned in chain order rather than taken from the tone's own
    # @position: fusing a cab into its amp closes a gap, and leaving one would
    # waste the slot the fusion just freed.
    wanted: dict[int, dict] = {}
    for position, entry in enumerate(planned):
        index = parsed.record_for(0, position)
        if index is None:
            report.skipped.append(entry["name"])
            continue
        wanted[index] = entry

    # Slots the donor fills that the tone does not: empty them, so the pedal ends
    # up with the generated chain rather than a merge of two.
    stuck: list[int] = []
    for index in parsed.block_slots():
        if index not in wanted:
            run.renew_if_needed()
            try:
                BlockEditor(run.session).delete_block(index)
            except EditError as exc:
                # These are the run's first edits, and the first edit on a fresh
                # session can be refused (#91); try again once the chain is in.
                logger.warning("Could not clear slot %d yet: %s", index, exc)
                stuck.append(index)

    # The donor's blocks still occupy DSP until they are replaced, so a heavy
    # donor can starve the tone's amp of budget. Swap every wanted slot to a
    # near-free placeholder first, then place the tone's blocks most important
    # first (amp, cab, drives...), so anything that cannot fit is an extra.
    placeholder = symbols.resolve(PLACEHOLDER_MODEL, stereo=False)
    if placeholder is not None:
        for index in sorted(wanted):
            run.renew_if_needed()
            try:
                BlockEditor(run.session).swap_model(index, placeholder.index)
            except EditError as exc:
                logger.debug("Placeholder for slot %d refused: %s", index, exc)

    placed_ok: list[str] = []
    placed_names: set[str] = set()
    placed_slots: list[int] = []
    dropped: set[int] = set()

    def place(index: int) -> None:
        """Put one tone block in its slot; DspBudgetError if it won't fit."""
        entry = wanted[index]
        block_name, block, symbol = entry["name"], entry["block"], entry["symbol"]
        run.renew_if_needed()
        editor = BlockEditor(run.session)
        try:
            editor.swap_model(index, symbol.index, paired=entry["paired"])
        except DspBudgetError:
            raise  # an EditError too, but it's the caller's to handle
        except EditError as exc:
            logger.warning("%s: %s", block_name, exc)
            report.skipped.append(block_name)
            dropped.add(index)
            # The slot still holds the placeholder (a Gain block); left there, a
            # footswitch or snapshot meant for this block would drive it instead.
            run.renew_if_needed()
            try:
                BlockEditor(run.session).delete_block(index)
            except EditError as cleanup:
                logger.debug("Could not clear slot %d: %s", index, cleanup)
            return
        placed_ok.append(symbol.symbol)
        placed_names.add(block_name)
        placed_slots.append(index)
        paired_symbol = entry["paired_symbol"]
        detail = f"{block_name} -> {symbol.symbol}"
        if paired_symbol is not None:
            detail += f" + {paired_symbol.symbol}"
        report.swapped.append(detail)

        for key, raw in block.items():
            if key.startswith("@"):
                continue
            ordinal = symbol.ordinal_of(key)
            if ordinal is None:
                continue
            if isinstance(raw, str):
                resolved = enum_index(catalog, block["@model"], key, raw)
                if resolved is None:
                    report.unsupported.append(f"{block_name}.{key}")
                    continue
                raw = resolved
            run.renew_if_needed()
            if not BlockEditor(run.session).try_set_value(index, ordinal, raw):
                report.unsupported.append(f"{block_name}.{key}")
                continue
            report.values_set += 1

        # The paired cab's own parameters, where the absorbed cab block named
        # any the paired model actually has.
        if entry["cab_block"] is not None and paired_symbol is not None:
            run.renew_if_needed()
            _apply_cab_values(
                BlockEditor(run.session), index, entry["cab_block"], paired_symbol, report
            )

        # Always sent, not only when the tone says so: a swap leaves the block
        # in whatever state the slot's previous occupant had, and a generated
        # preset's blocks should be live unless it asks otherwise.
        enabled = bool(block.get("@enabled", True))
        run.renew_if_needed()
        try:
            BlockEditor(run.session).set_enabled(index, enabled)
            if not enabled:
                report.bypassed.append(block_name)
        except EditError as exc:
            logger.debug("%s enable refused: %s", block_name, exc)

    order = sorted(wanted, key=lambda i: (_placement_rank(wanted[i]["block"], catalog), i))
    retried = False
    while True:
        mark = (len(report.swapped), report.values_set, len(report.unsupported),
                len(report.bypassed))
        current = None
        try:
            for current in order:
                if current not in dropped:
                    place(current)
            break
        except DspBudgetError as exc:
            symbol = wanted[current]["symbol"]
            if not retried and placeholder is not None and placed_slots:
                # The pedal's DSP check depends on the order blocks go in: a
                # preset that plays fine on the pedal (#114: NAM, a rotary
                # reverb and Poly Downtune) was refused when the pitch block
                # went in last. Take the placed blocks out again and put the
                # refused one in first.
                retried = True
                logger.info("%s didn't fit after %s; trying it first", symbol.symbol,
                            ", ".join(placed_ok))
                for index in placed_slots:
                    run.renew_if_needed()
                    try:
                        BlockEditor(run.session).swap_model(index, placeholder.index)
                    except EditError as undo:
                        logger.debug("Placeholder for slot %d refused: %s", index, undo)
                del report.swapped[mark[0]:], report.unsupported[mark[2]:]
                del report.bypassed[mark[3]:]
                report.values_set = mark[1]
                placed_ok.clear()
                placed_names.clear()
                placed_slots.clear()
                order = [current, *(i for i in order if i != current)]
                continue
            # Saving now would store a preset missing a block it was designed
            # around; leave the slot as it was instead.
            run.close()
            error = DspBudgetError(
                f"The pedal ran out of DSP placing {symbol.symbol}"
                + (f" (after {', '.join(placed_ok)})" if placed_ok else "")
                + ". Nothing was saved; the slot keeps its previous preset."
            )
            error.placed = list(placed_ok)
            error.refused = symbol.symbol
            raise error from exc

    for index in stuck:
        run.renew_if_needed()
        try:
            BlockEditor(run.session).delete_block(index)
            logger.info("Cleared slot %d on the second try", index)
        except EditError as exc:
            logger.warning("Could not clear slot %d: %s", index, exc)

    run.commit()

    # The chain is now correct on the device. The footswitch layout and the
    # snapshots live in the document rather than behind edit ops, so they go back
    # as a whole-document write -- which the device accepts, because by this
    # point no model is changing.
    # Absorbed cabs are deliberately absent from `placed`, so a footswitch the
    # tone bound to one is simply not written. A paired cab is part of the amp's
    # block and cannot be switched on its own, and remapping it onto the amp
    # would just give two switches that toggle the same thing.
    # Only blocks the pedal accepted: a skipped block's slot is empty now.
    placed = {entry["name"]: index for index, entry in wanted.items()
              if entry["name"] in placed_names}
    # Where a controller aimed at a tone block lands: the block's own slot and
    # model, or -- for a cab fused into its amp -- the amp's slot and paired model.
    targets: dict[str, ControllerTarget] = {}
    for index, entry in wanted.items():
        if entry["name"] not in placed_names:
            continue
        targets[entry["name"]] = ControllerTarget(index, entry["symbol"], MODEL_MAIN, entry["block"])
        if entry["cab_name"] is not None and entry["paired_symbol"] is not None:
            targets[entry["cab_name"]] = ControllerTarget(
                index, entry["paired_symbol"], MODEL_PAIRED, entry["cab_block"]
            )
    return placed, targets


def _apply_cab_values(
    editor: BlockEditor, slot: int, cab_block: dict, cab_symbol: Any, report: EditReport
) -> None:
    """Carry an absorbed cab block's settings onto the paired cab.

    Selecting the paired sub-model (``26: 1``) is what distinguishes these from
    the amp's own parameters; without it a mic-distance edit lands on the amp's
    Mid, because both are parameter 2 of their respective models.

    A paired cab has no mic, position or angle -- those belong to a standalone
    cab -- so parameters with no equivalent are reported rather than forced.
    """
    for key, raw in cab_block.items():
        if key.startswith("@"):
            continue
        ordinal = cab_symbol.ordinal_of(key)
        if ordinal is None:
            report.dropped_cab_params.append(key)
            continue
        if editor.try_set_value(slot, ordinal, raw, model_sel=MODEL_PAIRED):
            report.values_set += 1
        else:
            report.unsupported.append(f"cab.{key}")


@dataclass(frozen=True)
class ControllerTarget:
    """The device address of a tone block's parameters."""

    slot: int
    symbol: Any
    model_sel: int
    block: dict


def _apply_layout(
    session: Any,
    preset: dict,
    placed: dict[str, int],
    targets: dict[str, ControllerTarget],
    *,
    bank: int,
    slot: int,
    name: str,
    catalog: Any,
    report: EditReport,
    symbols: Any = None,
    device_settings: dict | None = None,
) -> None:
    """Write the footswitch layout and snapshots, which the edit ops do not carry.

    ``placed`` maps each tone block's name to the device slot it ended up in, so a
    switch bound to ``dsp0.block2`` reaches the block that actually holds it --
    the two are independent on the wire, and real presets routinely bind switch 1
    to a block deep in the chain.
    """
    from stompxl.helixgen.device.document import (
        FOOTSWITCH_TEMPLATE_BY_PRODUCT,
        dump,
        parse,
        snapshot_source,
    )

    raw = session.read_slot(bank=bank, slot=slot)
    if raw is None:
        return
    document = parse(raw)
    # What the chain run committed, seen fresh: anything here the tone didn't
    # place is the previous preset's, still playing (#99).
    report.leftover = sorted(set(document.block_slots()) - set(placed.values()))
    if report.leftover:
        logger.warning("Slot %d still holds blocks the tone doesn't: %s", slot,
                       ", ".join(f"slot {i} model {document.model_index(i)}"
                                 for i in report.leftover))
    tone = preset.get("data", {}).get("tone", {})
    product_id = getattr(getattr(session, "info", None), "product_id", None)

    assigned: set[int] = set()
    for dsp_name, blocks in (tone.get("footswitch") or {}).items():
        if not isinstance(blocks, dict):
            continue
        for block_id, assignment in blocks.items():
            if not isinstance(assignment, dict):
                continue
            switch = assignment.get("@fs_index")
            target = placed.get(f"{dsp_name}.{block_id}")
            if switch is None or target is None:
                continue
            ok = document.set_footswitch(
                int(switch) - 1,
                block_slot=target,
                label=str(assignment.get("@fs_label") or ""),
                color=assignment.get("@fs_ledcolor"),
                enabled=bool(assignment.get("@fs_enabled", True)),
                template=FOOTSWITCH_TEMPLATE_BY_PRODUCT.get(product_id),
            )
            if ok:
                assigned.add(int(switch) - 1)
                report.footswitches.append(f"FS{switch}->{dsp_name}.{block_id}")

    # A switch the tone does not mention must not keep the previous preset's
    # binding, which would point at a block that is no longer there -- even
    # when the tone binds no switch at all.
    for position in range(len(document.footswitches())):
        if position not in assigned and document.footswitches()[position] is not None:
            document.clear_footswitch(position)

    for index in range(len(document.snapshots())):
        snapshot = tone.get(f"snapshot{index}")
        if not isinstance(snapshot, dict):
            continue
        states: dict[int, bool] = {}
        for dsp_name, blocks in (snapshot.get("blocks") or {}).items():
            if not isinstance(blocks, dict):
                continue
            for block_id, on in blocks.items():
                target = placed.get(f"{dsp_name}.{block_id}")
                if target is not None:
                    states[target] = bool(on)
        if document.set_snapshot(
            index,
            name=snapshot.get("@name"),
            tempo=snapshot.get("@tempo"),
            valid=snapshot.get("@valid"),
            block_states=states,
        ):
            report.snapshots.append(str(snapshot.get("@name") or index))

    _apply_snapshot_controllers(
        document, tone, targets, catalog, report, source=snapshot_source(product_id)
    )

    gate = (tone.get("dsp0") or {}).get("inputA")
    if isinstance(gate, dict) and "noiseGate" in gate:
        threshold = gate.get("threshold")
        threshold = float(threshold) if isinstance(threshold, (int, float)) else None
        if document.set_input_gate(bool(gate["noiseGate"]), threshold):
            report.input_gate = (
                f"on at {threshold:g} dB" if gate["noiseGate"] and threshold is not None
                else "on" if gate["noiseGate"] else "off"
            )

    _apply_io(document, tone, symbols, report)
    _apply_device_settings(document, device_settings, report)

    current = tone.get("global", {}).get("@current_snapshot")
    if isinstance(current, int) and not isinstance(current, bool):
        document.set_current_snapshot(current)

    if not document.modified:
        return

    report.layout_document = dump(document)
    session.push_document(
        report.layout_document, slot=slot, name=name, bank=bank, donor=raw, verify=False
    )


INPUT_SYMBOL = "HelixStomp_AppDSPFlowInput"
OUTPUT_SYMBOL = "HelixStomp_AppDSPFlowOutputMain"
JOIN_SYMBOL = "HD2_AppDSPFlowJoin"


SETTINGS_SECTIONS = ("5", "6")


def _apply_device_settings(document: Any, settings: dict | None, report: EditReport) -> None:
    """Copy preset-level settings from the preset this tone was read from."""
    written = 0
    for section, values in (settings or {}).items():
        # Only the undecoded settings sections an import records.
        if str(section) not in SETTINGS_SECTIONS:
            continue
        target = document.preset.get(int(section))
        if not isinstance(target, dict) or not isinstance(values, dict):
            continue
        for key, value in values.items():
            k = int(key) if str(key).isdigit() else None
            if k in target and type(target[k]) is type(value) and target[k] != value:
                target[k] = value
                written += 1
    if written:
        document.dirty = True
        report.device_settings = written


def _apply_io(document: Any, tone: dict, symbols: Any, report: EditReport) -> None:
    """The input gate's decay and the main output's level and pan.

    Their place in each record's values comes from Helix.sym, trusted for the
    input only where it agrees with the gate's confirmed layout (on/off, then
    threshold).
    """
    from stompxl.helixgen.device.document import INPUT_RECORD, OUTPUT_RECORD

    if symbols is None:
        return
    dsp0 = tone.get("dsp0") or {}
    gate = dsp0.get("inputA")
    entry = symbols.by_symbol(INPUT_SYMBOL)
    if (isinstance(gate, dict) and isinstance(gate.get("decay"), (int, float)) and entry
            and entry.ordinal_of("noiseGate") == 0 and entry.ordinal_of("threshold") == 1):
        ordinal = entry.ordinal_of("decay")
        if ordinal is not None and document.set_io_value(INPUT_RECORD, ordinal, gate["decay"]):
            report.input_gate += f", decay {gate['decay']:.2f}" if report.input_gate else ""

    # The path mix: written so the slot's previous preset can't leave its
    # own mix level behind (one was +3 dB).
    join = dsp0.get("join")
    entry = symbols.by_symbol(JOIN_SYMBOL)
    if isinstance(join, dict) and entry is not None:
        for key, value in join.items():
            ordinal = entry.ordinal_of(key) if not key.startswith("@") else None
            if (ordinal is not None and isinstance(value, (int, float))
                    and not isinstance(value, bool)):
                document.set_join_value(ordinal, value)

    output = dsp0.get("outputA")
    entry = symbols.by_symbol(OUTPUT_SYMBOL)
    if not isinstance(output, dict) or entry is None:
        return
    written = []
    for key, text in (("gain", "level {:+g} dB"), ("pan", "pan {:.2f}")):
        value = output.get(key)
        ordinal = entry.ordinal_of(key)
        if (isinstance(value, (int, float)) and ordinal is not None
                and document.set_io_value(OUTPUT_RECORD, ordinal, value)):
            written.append(text.format(value))
    if written:
        report.output = ", ".join(written)


def _apply_snapshot_controllers(
    document: Any,
    tone: dict,
    targets: dict[str, ControllerTarget],
    catalog: Any,
    report: EditReport,
    *,
    source: int | None = None,
) -> None:
    """Put the tone's snapshot-controlled parameters under snapshot control.

    Without this every snapshot recalls the same knob positions and only its
    on/off states differ. Each ``tone.controller`` entry with ``@controller: 9``
    becomes a device assignment, and each snapshot's ``controllers`` value its
    per-snapshot value.

    The device refuses a value of the wrong type, and the ``.hlx`` does not say
    which type a parameter is. The document read back after the edits does: it
    holds the block's own value at that ordinal in the device's type, so every
    snapshot value and the range are converted to match it.
    """
    from stompxl.helixgen.device.document import (
        CONTROLLER_SNAPSHOT,
        EXPRESSION_SOURCES,
        SNAPSHOT_SOURCES,
    )

    if source is None:
        source = CONTROLLER_SNAPSHOT

    # The donor's assignments name blocks by slot; left in place they would drive
    # whatever the new tone put there.
    document.clear_controllers()
    snapshot_count = len(document.snapshots())

    for dsp_name, blocks in (tone.get("controller") or {}).items():
        if not isinstance(blocks, dict):
            continue
        for block_id, parameters in blocks.items():
            if not isinstance(parameters, dict):
                continue
            for key, assignment in parameters.items():
                label = f"{dsp_name}.{block_id}.{key}"
                target = targets.get(f"{dsp_name}.{block_id}")
                requested = assignment.get("@controller") if isinstance(assignment, dict) else None
                if target is None or requested not in SNAPSHOT_SOURCES | EXPRESSION_SOURCES:
                    report.unsupported_controllers.append(label)
                    continue
                # An expression pedal keeps its own source; every snapshot holds the
                # block's own value for it, as HX Edit writes it.
                assignment_source = requested if requested in EXPRESSION_SOURCES else source

                ordinal = target.symbol.ordinal_of(key)
                if ordinal is None and key == "Pedal":
                    # HX Edit shows the expression sweep as "Position".
                    ordinal = target.symbol.ordinal_of("Position")
                stored = document.values(target.slot, cab=target.model_sel == MODEL_PAIRED)
                if ordinal is None or stored is None or ordinal >= len(stored):
                    report.unsupported_controllers.append(label)
                    continue
                kind = type(stored[ordinal])
                model_name = str(target.block.get("@model", ""))
                values = []
                for index in range(snapshot_count):
                    snapshot = tone.get(f"snapshot{index}") or {}
                    entry = (
                        (snapshot.get("controllers") or {})
                        .get(dsp_name, {})
                        .get(block_id, {})
                        .get(key)
                    )
                    raw = entry.get("@value") if isinstance(entry, dict) else None
                    values.append(
                        stored[ordinal]
                        if raw is None
                        else _as_wire_type(raw, kind, catalog, model_name, key)
                    )
                minimum = _as_wire_type(assignment.get("@min"), kind, catalog, model_name, key)
                maximum = _as_wire_type(assignment.get("@max"), kind, catalog, model_name, key)
                if minimum is None or maximum is None or any(v is None for v in values):
                    report.unsupported_controllers.append(label)
                    continue

                assigned = document.add_snapshot_controller(
                    slot=target.slot,
                    parameter=ordinal,
                    model_sel=target.model_sel,
                    minimum=minimum,
                    maximum=maximum,
                    values=values,
                    source=assignment_source,
                )
                if assigned is None:
                    report.unsupported_controllers.append(label)
                elif assignment_source in EXPRESSION_SOURCES:
                    report.expression_controls.append(f"{label} -> EXP {assignment_source}")
                else:
                    report.snapshot_controls.append(label)


def _as_wire_type(
    raw: Any, kind: type, catalog: Any, model_name: str, parameter: str
) -> Any | None:
    """Convert ``raw`` to the device's type for a parameter, or None if it has none.

    An enum named by its label resolves through the catalog, as block values do.
    """
    if isinstance(raw, str):
        raw = enum_index(catalog, model_name, parameter, raw)
    if raw is None:
        return None
    try:
        if kind is bool:
            return bool(raw)
        if kind is int:
            number = float(raw)
            return int(number) if number.is_integer() else None
        if kind is float:
            return float(raw)
    except (TypeError, ValueError):
        return None
    return None
