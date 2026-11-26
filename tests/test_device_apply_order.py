"""apply_tone's placement: placeholders first, amp first, abort on DSP refusal."""

from types import SimpleNamespace

import pytest

from stompxl.helixgen.device import editor


class _Symbol:
    def __init__(self, name, index):
        self.symbol, self.index = name, index

    def ordinal_of(self, _name):
        return None


class _Symbols:
    def __init__(self):
        self.placeholder = _Symbol("HD2_VolPanGainMono", 999)

    def resolve(self, host, stereo=None):
        return self.placeholder if host == editor.PLACEHOLDER_MODEL else None


def _entry(name, model, index):
    return {"name": name, "block": {"@model": model}, "symbol": _Symbol(model, index),
            "paired": editor.NO_PAIRED, "paired_symbol": None, "cab_block": None, "cab_name": None}


@pytest.fixture
def rig(monkeypatch):
    swaps: list[tuple[int, int]] = []
    state = SimpleNamespace(refuse=None, reject=None, committed=False, closed=False, deleted=[], selected=[], slept=[])

    class FakeRun:
        def __init__(self, *a, **k):
            self.document = b"doc"
            self.session = SimpleNamespace(close=lambda: None)
            self.sessions = 1

        def renew_if_needed(self):
            pass

        def commit(self):
            state.committed = True

        def close(self):
            state.closed = True

        def _open(self, _info):
            return SimpleNamespace(close=lambda: None)

    class FakeEditor:
        def __init__(self, _session):
            pass

        def swap_model(self, slot, model_index, paired=editor.NO_PAIRED):
            if model_index == state.refuse:
                raise editor.DspBudgetError("-306")
            if model_index == state.reject:
                raise editor.EditError("refused")
            swaps.append((slot, model_index))

        def try_set_value(self, *a):
            return True

        def set_enabled(self, *a):
            pass

        def select_preset(self, bank, slot):
            state.selected.append(slot)

        def delete_block(self, slot):
            state.deleted.append(slot)

    parsed = SimpleNamespace(record_for=lambda path, pos: pos + 1, block_slots=lambda: [])
    planned = [_entry("dsp0.block0", "HD2_CompressorRedSqueeze", 10),
               _entry("dsp0.block1", "L6SPB_PolyDowntune", 20),
               _entry("dsp0.block2", "HD2_AmpBritPlexiBrt", 30)]
    monkeypatch.setattr(editor, "EditSession", FakeRun)
    monkeypatch.setattr(editor, "BlockEditor", FakeEditor)
    monkeypatch.setattr(editor, "plan_chain", lambda *a, **k: (planned, []))
    monkeypatch.setattr(editor, "_apply_layout", lambda *a, **k: None)
    monkeypatch.setattr(editor.time, "sleep", lambda seconds: state.slept.append(seconds))
    import stompxl.helixgen.device.document as document

    monkeypatch.setattr(document, "parse", lambda raw: parsed)
    return swaps, state


def test_placeholders_go_in_first_then_the_amp(rig):
    swaps, state = rig
    editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert swaps[:3] == [(1, 999), (2, 999), (3, 999)]
    assert swaps[3] == (3, 30)          # the amp (slot 3) is placed before the effects
    assert state.committed


def test_a_block_that_does_not_fit_last_is_tried_first(rig, monkeypatch):
    """#114: a preset straight off the pedal was refused when Poly Downtune went in last."""
    swaps, state = rig
    refused = []
    original = editor.BlockEditor.swap_model

    def swap(self, slot, model_index, paired=editor.NO_PAIRED):
        if model_index == 20 and any(m in (10, 30) for _, m in swaps[-2:]):
            refused.append(slot)
            raise editor.DspBudgetError("-306")
        original(self, slot, model_index, paired)

    monkeypatch.setattr(editor.BlockEditor, "swap_model", swap)
    report = editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert refused == [2] and state.committed
    # The placed blocks go back to the placeholder, then the pitch block goes in first.
    assert swaps[3:] == [(3, 30), (1, 10), (3, 999), (1, 999), (2, 20), (3, 30), (1, 10)]
    assert len(report.swapped) == 3


def test_a_dsp_refusal_saves_nothing(rig):
    swaps, state = rig
    state.refuse = 20                   # the poly pitch block won't fit, even first
    with pytest.raises(editor.DspBudgetError, match="ran out of DSP placing L6SPB_PolyDowntune"):
        editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert not state.committed and state.closed


def test_a_refused_block_gets_no_footswitch_and_leaves_no_placeholder(rig, monkeypatch):
    """Its slot held the Gain placeholder, which then took the block's switch."""
    _swaps, state = rig
    state.reject = 20  # the pedal refuses dsp0.block1
    seen = {}
    monkeypatch.setattr(editor, "_apply_layout",
                        lambda session, preset, placed, targets, **k: seen.update(placed=placed, targets=targets))
    report = editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert report.skipped == ["dsp0.block1"]
    assert state.deleted == [2]  # its slot is emptied
    assert "dsp0.block1" not in seen["placed"] and "dsp0.block1" not in seen["targets"]
    assert seen["placed"] == {"dsp0.block0": 1, "dsp0.block2": 3}


def test_the_layout_session_loads_the_slot_before_reading_it(rig, monkeypatch):
    _swaps, state = rig
    order = []
    monkeypatch.setattr(editor, "_apply_layout",
                        lambda *a, **k: order.append(("layout", list(state.selected))))
    editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert order == [("layout", [5])]


def test_a_failed_layout_write_keeps_the_committed_chain(rig, monkeypatch):
    _swaps, state = rig

    def stall(*a, **k):
        raise RuntimeError("Short bulk write: 256 of 512 bytes")

    monkeypatch.setattr(editor, "_apply_layout", stall)
    report = editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert state.committed and state.closed
    assert report.layout_error == "Short bulk write: 256 of 512 bytes"


def test_the_layout_waits_for_the_commit_to_settle(rig, monkeypatch):
    _swaps, state = rig
    order = []
    monkeypatch.setattr(editor, "LAYOUT_SETTLE_SECONDS", 2.0)
    monkeypatch.setattr(editor, "LAYOUT_SELECT_SETTLE_SECONDS", 1.5)
    monkeypatch.setattr(editor, "_apply_layout",
                        lambda *a, **k: order.append(("layout", state.committed,
                                                      list(state.slept))))
    editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert order == [("layout", True, [2.0, 1.5])]


def test_a_layout_write_stopped_on_a_slow_credit_is_tried_again(rig, monkeypatch):
    from stompxl.helixgen.device.writer import WriteAbortedError

    _swaps, state = rig
    tries = []

    def slow_once(*a, report=None, **k):
        tries.append(1)
        report.footswitches.append("FS1")
        if len(tries) == 1:
            raise WriteAbortedError("Unit 1's credit took 44 ms")

    monkeypatch.setattr(editor, "_apply_layout", slow_once)
    report = editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert len(tries) == 2
    assert report.layout_error == ""
    assert report.footswitches == ["FS1"]     # the first try's partial work is gone


def test_a_layout_write_that_stays_slow_is_reported(rig, monkeypatch):
    from stompxl.helixgen.device.writer import WriteAbortedError

    def slow(*a, **k):
        raise WriteAbortedError("Unit 1's credit took 44 ms")

    monkeypatch.setattr(editor, "_apply_layout", slow)
    report = editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert report.layout_error == "Unit 1's credit took 44 ms"


def test_an_old_block_the_pedal_would_not_clear_is_tried_again_after_the_chain(rig, monkeypatch):
    """#99: two of the previous preset's blocks were still on the pedal after a send."""
    _swaps, state = rig
    parsed = SimpleNamespace(record_for=lambda path, pos: pos + 1, block_slots=lambda: [1, 2, 3, 7, 8])
    import stompxl.helixgen.device.document as document

    monkeypatch.setattr(document, "parse", lambda raw: parsed)
    refused = {7}
    deletes = []
    real = editor.BlockEditor.delete_block

    def delete(self, slot):
        deletes.append(slot)
        if slot in refused:
            refused.discard(slot)        # the first edit of the run is refused once
            raise editor.EditError("refused")
        real(self, slot)

    monkeypatch.setattr(editor.BlockEditor, "delete_block", delete)
    editor.apply_tone({"data": {}}, _Symbols(), slot=5)
    assert deletes == [7, 8, 7]
    assert state.deleted == [8, 7]


def test_leftover_blocks_are_reported():
    report = editor.EditReport(leftover=[7, 8])
    assert "OLD BLOCKS STILL ON THE PEDAL (couldn't be cleared): slot 7, 8" in report.summary()


def test_the_pause_after_the_select_answers_the_pedal_s_keepalives(monkeypatch):
    """#101: a keepalive went unanswered in this pause and the write then locked up."""
    clock = {"t": 0.0}
    monkeypatch.setattr(editor.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(editor.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    answered = []
    session = SimpleNamespace(keepalives_seen=0, keepalive=lambda: answered.append(clock["t"]))

    def settle():
        if clock["t"] >= 1.0 and not answered:
            session.keepalives_seen += 1

    session.settle = settle
    editor._wait_answering(session, 1.5)
    assert answered == [1.0]
    assert clock["t"] == 1.5
