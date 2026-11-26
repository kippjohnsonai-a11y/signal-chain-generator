"""Live edit: value edits go to the pedal's edit buffer as they're made."""

from __future__ import annotations

import copy
import time
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from stompxl.helixgen.device import document as document_module
from stompxl.helixgen.device import editor
from stompxl.helixgen.device.codec import tone_blocks
from stompxl.helixgen.device.editor import (
    K_ENABLED,
    K_PARAM_INDEX,
    K_SLOT,
    K_VALUE,
    MODEL_PAIRED,
    NO_PAIRED,
    OP_BYPASS,
    OP_SAVE_PRESET,
    OP_SELECT_PRESET,
    OP_SET_VALUE,
)
from stompxl.helixgen.device.usb import DeviceError
from stompxl import config, engine, live, pedal
from stompxl.db import Library
from stompxl.server import create_app

from .test_engine import FakeClaude

CHAIN = {"blocks": [{"model": "Scream 808", "parameters": {}},
                    {"model": "Cali Rectifire", "parameters": {}},
                    {"model": "Plate Reverb", "parameters": {}}],
         "snapshots": []}


class FakeSymbol:
    def __init__(self, index: int, params: list[str]):
        self.index = index
        self._params = params

    def ordinal_of(self, name: str):
        return self._params.index(name) if name in self._params else None


class FakeSession:
    def __init__(self, fail_once: bool = False):
        self.sent: list[tuple[int, dict]] = []
        self.fail_once = fail_once

    def command(self, *, op, target):
        if self.fail_once:
            self.fail_once = False
            raise DeviceError("pipe broke")
        self.sent.append((op, target))


class FakeRun:
    """Stands in for EditSession: the slot selected and its buffer open."""

    opened: ClassVar[list[FakeRun]] = []

    def __init__(self, bank, slot, name, *, fail_once=False):
        self.bank, self.slot, self.name = bank, slot, name
        self.document = b"doc"
        self.session = FakeSession(fail_once)
        self.sessions = 1
        self.wrap = False
        self.closed = False
        FakeRun.opened.append(self)

    def renew_if_needed(self):
        if self.wrap:
            self.wrap = False
            self.session.sent.append((OP_SAVE_PRESET, {"renew": True}))
            self.sessions += 1

    def commit(self):
        self.session.sent.append((OP_SAVE_PRESET, {}))

    def close(self):
        self.closed = True


class FakeDocument:
    def __init__(self, models: list[int], cabs: dict[int, int] | None = None):
        self.models = models
        self.cabs = cabs or {}

    def record_for(self, path, position):
        return position + 1 if position < len(self.models) else None

    def model_index(self, index):
        return self.models[index - 1]

    def cab_index(self, index):
        return self.cabs.get(index)

    def block_slots(self):
        return [i + 1 for i in range(len(self.models))]


def _preset(chain=CHAIN):
    return engine.build_preset(chain)[0]


def _fake_plan(preset, symbols, amps, fuse_cabs=True):
    planned = []
    for i, (name, block) in enumerate(tone_blocks(preset)):
        params = sorted(k for k in block if not k.startswith("@"))
        planned.append({"name": name, "block": block, "symbol": FakeSymbol(100 + i, params),
                        "paired": NO_PAIRED, "paired_symbol": None,
                        "cab_block": None, "cab_name": None})
    return planned, []


@pytest.fixture
def fakes(monkeypatch):
    FakeRun.opened = []
    holder = {"doc": FakeDocument([100, 101, 102])}
    monkeypatch.setattr(editor, "plan_chain", _fake_plan)
    monkeypatch.setattr(document_module, "parse", lambda raw: holder["doc"])
    return holder


def _session(preset, open_run=FakeRun):
    return live.LiveSession(version_id=1, preset=preset, slot=5, symbols=None, amps=None,
                            catalog=engine.catalog(), open_run=open_run)


def _with_gain(preset, value):
    out = copy.deepcopy(preset)
    out["data"]["tone"]["dsp0"]["block0"]["Gain"] = value
    return out


def _set_values(run):
    return [(t[K_SLOT], t[K_PARAM_INDEX], t[K_VALUE]) for op, t in run.session.sent
            if op == OP_SET_VALUE]


def test_sends_only_the_values_that_changed(fakes):
    preset = _preset()
    session = _session(preset)
    run = FakeRun.opened[0]
    assert session.update(preset) == {"sent": 0, "structural": False, "unsupported": [], "waiting": []}

    result = session.update(_with_gain(preset, 0.9))
    assert result["sent"] == 1
    ordinal = sorted(k for k in preset["data"]["tone"]["dsp0"]["block0"]
                     if not k.startswith("@")).index("Gain")
    assert _set_values(run) == [(1, ordinal, 0.9)]
    # Nothing new: nothing sent, and nothing saved to flash along the way.
    assert session.update(_with_gain(preset, 0.9))["sent"] == 0
    assert OP_SAVE_PRESET not in [op for op, _ in run.session.sent]


def test_a_block_change_is_not_sent_live(fakes):
    preset = _preset()
    session = _session(preset)
    swapped = _preset({**CHAIN, "blocks": [{"model": "Minotaur", "parameters": {}},
                                           *CHAIN["blocks"][1:]]})
    assert session.update(swapped)["structural"] is True
    assert FakeRun.opened[0].session.sent == []


def test_values_still_go_live_alongside_a_block_change(fakes):
    preset = _preset()
    session = _session(preset)
    swapped = _preset({**CHAIN, "blocks": [{"model": "Minotaur", "parameters": {}},
                                           *CHAIN["blocks"][1:]]})
    reverb = swapped["data"]["tone"]["dsp0"]["block2"]
    params = sorted(k for k in reverb if not k.startswith("@"))
    key = next(k for k in params if isinstance(reverb[k], float))
    reverb[key] = 0.123
    result = session.update(swapped)
    assert result["structural"] is True
    assert result["sent"] == 1
    assert _set_values(FakeRun.opened[0]) == [(3, params.index(key), 0.123)]


def test_a_snapshot_on_off_toggle_switches_the_block_live(fakes):
    preset = _preset()
    session = _session(preset)
    toggled = copy.deepcopy(preset)
    toggled["data"]["tone"]["snapshot0"]["blocks"]["dsp0"]["block2"] = False
    result = session.update(toggled)
    assert result == {"sent": 1, "structural": False, "unsupported": [], "waiting": []}
    run = FakeRun.opened[0]
    assert [(t[K_SLOT], t[K_ENABLED]) for op, t in run.session.sent if op == OP_BYPASS] \
        == [(3, False)]
    # Already off: nothing more to send.
    assert session.update(toggled)["sent"] == 0


def test_refuses_a_slot_holding_another_chain(fakes):
    fakes["doc"] = FakeDocument([100, 999, 102])
    with pytest.raises(live.LiveError, match="doesn't hold this version"):
        _session(_preset())
    assert FakeRun.opened[0].closed


def test_clears_blocks_left_over_from_the_previous_preset(fakes):
    """#99: two of the old preset's blocks survived a send; live edit clears them
    in the edit buffer rather than refusing the slot."""
    fakes["doc"] = FakeDocument([100, 101, 102, 103, 104])
    session = _session(_preset())
    deletes = [t[K_SLOT] for op, t in session.run.session.sent if op == editor.OP_DELETE_BLOCK]
    assert deletes == [4, 5]
    assert not session.run.closed


def test_refuses_a_slot_whose_extra_blocks_will_not_clear(fakes, monkeypatch):
    fakes["doc"] = FakeDocument([100, 101, 102, 103])
    monkeypatch.setattr(live.time, "sleep", lambda s: None)

    def refuse(self, slot):
        raise editor.EditError("the device refused op 28")

    monkeypatch.setattr(editor.BlockEditor, "delete_block", refuse)
    with pytest.raises(live.LiveError, match="doesn't hold this version"):
        _session(_preset())


def test_a_paired_cab_edit_goes_to_the_amp_slot(fakes, monkeypatch):
    chain = {"blocks": [{"model": "Cali Rectifire", "parameters": {}},
                        {"model": "4x12 Cali V30", "parameters": {}}], "snapshots": []}
    preset = _preset(chain)
    (amp_name, amp), (cab_name, cab) = tone_blocks(preset)
    cab_params = sorted(k for k in cab if not k.startswith("@"))

    def plan(preset, symbols, amps, fuse_cabs=True):
        return [{"name": amp_name, "block": amp, "symbol": FakeSymbol(100, []),
                 "paired": 700, "paired_symbol": FakeSymbol(700, cab_params),
                 "cab_block": cab, "cab_name": cab_name}], []

    monkeypatch.setattr(editor, "plan_chain", plan)
    fakes["doc"] = FakeDocument([100], cabs={1: 700})
    session = _session(preset)
    edited = copy.deepcopy(preset)
    key = cab_params[0]
    edited["data"]["tone"]["dsp0"][cab_name.split(".")[1]][key] = 0.123
    assert session.update(edited)["sent"] == 1
    op, target = FakeRun.opened[0].session.sent[0]
    assert op == OP_SET_VALUE and target[K_SLOT] == 1
    assert target[editor.K_MODEL_SEL] == MODEL_PAIRED


def test_keep_saves_and_discard_reloads_the_slot(fakes):
    preset = _preset()
    session = _session(preset)
    session.update(_with_gain(preset, 0.9))
    session.keep()
    assert FakeRun.opened[0].session.sent[-1][0] == OP_SAVE_PRESET

    other = _session(preset)
    other.update(_with_gain(preset, 0.9))
    other.discard()
    assert FakeRun.opened[1].session.sent[-1][0] == OP_SELECT_PRESET


def test_discard_after_a_renewal_writes_the_originals_back(fakes):
    preset = _preset()
    original = preset["data"]["tone"]["dsp0"]["block0"]["Gain"]
    session = _session(preset)
    run = FakeRun.opened[0]
    session.update(_with_gain(preset, 0.9))
    run.wrap = True  # the next edit renews, which saves the buffer
    edited = _with_gain(preset, 0.9)
    edited["data"]["tone"]["dsp0"]["block2"]["Mix"] = 0.05
    session.update(edited)
    session.discard()
    mix = preset["data"]["tone"]["dsp0"]["block2"]["Mix"]
    assert [v for _, _, v in _set_values(run)][-2:] == [original, mix]
    assert run.session.sent[-1][0] == OP_SAVE_PRESET
    assert session.saved == session.original


def test_a_dropped_connection_reconnects_and_replays(fakes):
    preset = _preset()
    opened = {"n": 0}

    def open_run(bank, slot, name):
        opened["n"] += 1
        return FakeRun(bank, slot, name)

    session = _session(preset, open_run)
    session.update(_with_gain(preset, 0.9))
    FakeRun.opened[0].session.fail_once = True
    edited = _with_gain(preset, 0.9)
    edited["data"]["tone"]["dsp0"]["block2"]["Mix"] = 0.05
    assert session.update(edited)["sent"] == 1
    assert opened["n"] == 2
    fresh = FakeRun.opened[1]
    # The fresh buffer came from flash, so the earlier Gain edit is sent again.
    assert [v for _, _, v in _set_values(fresh)] == [0.9, 0.05]


def test_an_idle_session_reads_what_the_pedal_queued(fakes, monkeypatch):
    session = _session(_preset())
    run = FakeRun.opened[0]
    drained = []
    run.session.settle = lambda: drained.append(1)
    session.heartbeat()
    assert drained == []          # just opened: nothing to keep alive yet
    session.io_at -= live.HEARTBEAT_SECONDS
    session.heartbeat()
    assert drained == [1]


def _keepalive_session(monkeypatch, *, wrap=False):
    session = _session(_preset())
    pedal_session = FakeRun.opened[0].session
    pedal_session.keepalives_seen = 0
    pedal_session.near_sequence_wrap = wrap
    answered = []

    def settle():
        pedal_session.keepalives_seen += 1   # the pedal sent one while we were quiet

    pedal_session.settle = settle
    pedal_session.keepalive = lambda: answered.append(1)
    session.io_at -= live.HEARTBEAT_SECONDS
    return session, answered


def test_an_idle_session_answers_the_pedal_s_keepalive(fakes, monkeypatch):
    """#99: reading the pedal's keepalives was not enough to keep it answering."""
    session, answered = _keepalive_session(monkeypatch)
    session.heartbeat()
    assert answered == [1]
    assert len(FakeRun.opened) == 1


def test_an_idle_session_near_the_wrap_starts_fresh_without_saving(fakes, monkeypatch):
    session, answered = _keepalive_session(monkeypatch, wrap=True)
    session.heartbeat()
    assert answered == []
    assert len(FakeRun.opened) == 2
    assert all(op != OP_SAVE_PRESET for run in FakeRun.opened for op, _ in run.session.sent)


def test_a_quiet_session_reconnects_before_the_next_edit(fakes):
    preset = _preset()
    session = _session(preset)
    session.update(_with_gain(preset, 0.9))

    def dead():
        raise DeviceError("timed out")

    FakeRun.opened[0].session.settle = dead
    session.io_at -= live.HEARTBEAT_SECONDS
    session.heartbeat()
    assert len(FakeRun.opened) == 2
    # The fresh session got the unsaved edit back.
    assert [v for _, _, v in _set_values(FakeRun.opened[1])] == [0.9]


def test_a_value_refused_right_after_a_reconnect_is_tried_again(fakes, monkeypatch):
    preset = _preset()
    session = _session(preset)
    monkeypatch.setattr(live.time, "sleep", lambda _s: None)
    FakeRun.opened[0].session.fail_once = True
    calls = {"n": 0}
    real = live.LiveSession._set

    def flaky(self, name, key, raw):
        calls["n"] += 1
        if calls["n"] == 2:       # the first try on the fresh session
            return False
        return real(self, name, key, raw)

    monkeypatch.setattr(live.LiveSession, "_set", flaky)
    assert session.update(_with_gain(preset, 0.9))["sent"] == 1


def test_live_holds_the_pedal_until_stopped(fakes):
    preset = _preset()
    live.start(1, preset, 5, make=lambda v, p, s, b: _session(p))
    try:
        with pytest.raises(pedal.PedalError, match="Live edit is holding"):
            pedal.list_slots()
        assert live.update(1, _with_gain(preset, 0.9))["sent"] == 1
        with pytest.raises(live.LiveEnded):
            live.update(2, preset)
    finally:
        assert live.stop(keep=True) == 5
    assert not pedal._live.is_set()
    with pytest.raises(live.LiveEnded):
        live.update(1, preset)
    assert live.stop(keep=False) is None


def test_an_idle_session_lets_go(fakes, monkeypatch):
    preset = _preset()
    monkeypatch.setattr(live, "IDLE_SECONDS", 0.05)
    live.start(1, preset, 5, make=lambda v, p, s, b: _session(p))
    for _ in range(100):
        if live.current() is None:
            break
        time.sleep(0.02)
    assert live.current() is None and not pedal._live.is_set()
    # Let go, not reverted: the pedal keeps playing what it had.
    assert FakeRun.opened[0].session.sent == []


@pytest.fixture
def client(tmp_path, monkeypatch, fakes):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(live, "_make", lambda v, p, s, b: _session(p))
    lib = Library(tmp_path / "lib.sqlite3")
    app = create_app(lib, caller_factory=lambda: FakeClaude(["Scream 808", "Cali Rectifire",
                                                             "Plate Reverb"]))
    yield TestClient(app), lib
    live.stop(keep=False)


def _version(lib):
    preset = _preset()
    song = lib.add_song(title="Test", artist="", query="Test")
    return lib.add_version(song_id=song, parent_id=None, kind="manual", label="v", guitar_id=None,
                           rig_id=None, options={}, brief={}, chain=CHAIN, preset=preset,
                           notes="", warnings=[])


def test_server_live_round_trip(client):
    http, lib = client
    vid = _version(lib)
    res = http.post(f"/api/versions/{vid}/live", json={"edits": []})
    assert res.status_code == 400 and "Send this version" in res.json()["detail"]

    lib.mark_pushed(vid, 5)
    res = http.post(f"/api/versions/{vid}/live", json={"edits": []})
    assert res.status_code == 200, res.text
    assert res.json()["label"] == "2B"
    assert http.get("/api/live").json()["active"] is True

    edit = {"block": 0, "parameter": "Gain", "value": 9}
    res = http.post(f"/api/versions/{vid}/live/update", json={"edits": [edit]})
    assert res.status_code == 200 and res.json()["sent"] == 1

    saved = http.post(f"/api/versions/{vid}/edit", json={"edits": [edit]}).json()
    stop = http.post("/api/live/stop", json={"keep": True, "version_id": saved["version_id"]})
    assert stop.json()["label"] == "2B"
    assert lib.version(saved["version_id"])["pushed_slot"] == 5
    assert http.get("/api/live").json() == {"active": False}
    res = http.post(f"/api/versions/{vid}/live/update", json={"edits": [edit]})
    assert res.status_code == 409


class IRDocument(FakeDocument):
    """The last block is an IR record (``19: 7``), which keeps its model in the record."""

    def __init__(self, models, ir_model):
        super().__init__(models)
        self.ir_model = ir_model

    def record_for(self, path, position):
        return position + 1 if position <= len(self.models) else None

    def model_index(self, index):
        return self.models[index - 1] if index <= len(self.models) else None

    def slot_array(self, path=0):
        array = [{19: 0, 20: {}}] + [{19: 6, 20: {}} for _ in self.models]
        array.append({19: 7, 20: {7: {}, 8: self.ir_model, 9: 22, 10: True}})
        return array


def test_an_ir_block_counts_as_the_chain_s_block(fakes):
    fakes["doc"] = IRDocument([100, 101], ir_model=102)
    session = _session(_preset())
    assert sorted(t.index for t in session.targets.values()) == [1, 2, 3]


def test_an_ir_block_with_another_model_is_a_mismatch(fakes):
    fakes["doc"] = IRDocument([100, 101], ir_model=777)
    with pytest.raises(live.LiveError, match="doesn't hold this version"):
        _session(_preset())


def test_an_extra_ir_block_is_cleared(fakes):
    fakes["doc"] = IRDocument([100, 101, 102], ir_model=504)
    session = _session(_preset())
    deletes = [t[K_SLOT] for op, t in session.run.session.sent if op == editor.OP_DELETE_BLOCK]
    assert deletes == [4]


def _bypasses(run):
    return [(t[K_SLOT], t[K_ENABLED]) for op, t in run.session.sent if op == OP_BYPASS]


def test_an_on_off_in_another_snapshot_waits_for_save_and_send(fakes):
    """op 41 switches the block in the snapshot the pedal plays, so a toggle
    meant for snapshot 2 must not be sent while the pedal is on snapshot 1."""
    preset = _preset()
    assert "snapshot1" in preset["data"]["tone"]
    session = _session(preset)
    assert session.snapshot == 0
    toggled = copy.deepcopy(preset)
    toggled["data"]["tone"]["snapshot1"]["blocks"]["dsp0"]["block2"] = False
    result = session.update(toggled)
    assert result["sent"] == 0
    assert result["structural"] is True
    assert result["waiting"] == ["snapshot 2 (the pedal is on snapshot 1)"]
    assert _bypasses(FakeRun.opened[0]) == []


def test_live_follows_the_snapshot_the_preset_opens_on(fakes):
    preset = _preset()
    preset["data"]["tone"].setdefault("global", {})["@current_snapshot"] = 1
    session = _session(preset)
    toggled = copy.deepcopy(preset)
    toggled["data"]["tone"]["snapshot1"]["blocks"]["dsp0"]["block2"] = False
    assert session.update(toggled)["sent"] == 1
    assert _bypasses(FakeRun.opened[0]) == [(3, False)]


def test_a_refused_value_is_reported_once(fakes):
    preset = _preset()
    session = _session(preset)
    edited = copy.deepcopy(preset)
    edited["data"]["tone"]["dsp0"]["block0"]["NoSuchKnob"] = 1.0
    assert session.update(edited)["unsupported"] == ["NoSuchKnob"]
    assert session.update(edited)["unsupported"] == []


def test_saving_after_the_pedal_dropped_reconnects_and_saves(fakes):
    preset = _preset()
    session = _session(preset)
    session.update(_with_gain(preset, 0.9))

    def broken():
        raise DeviceError("Bulk read failed: read timed out after 2000 ms")

    FakeRun.opened[0].commit = broken
    session.keep()
    fresh = FakeRun.opened[1]
    assert [v for _, _, v in _set_values(fresh)] == [0.9]
    assert fresh.session.sent[-1][0] == OP_SAVE_PRESET


def test_discarding_after_the_pedal_dropped_reloads_the_slot(fakes):
    preset = _preset()
    session = _session(preset)
    session.update(_with_gain(preset, 0.9))
    FakeRun.opened[0].session.fail_once = True
    session.discard()
    fresh = FakeRun.opened[1]
    assert _set_values(fresh) == []  # the edit is not replayed
    assert fresh.session.sent[-1][0] == OP_SELECT_PRESET


def test_a_failed_live_start_is_kept_for_diagnostics(fakes, tmp_path, monkeypatch):
    from stompxl import diagnostics

    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(diagnostics, "_failures", diagnostics.collections.deque(maxlen=5))
    monkeypatch.setattr(diagnostics, "_loaded", True)

    monkeypatch.setattr(live.time, "sleep", lambda _s: None)
    tries = []

    def make(*_a):
        tries.append(1)
        raise DeviceError("Bulk read failed: read timed out after 2000 ms")

    with pytest.raises(DeviceError):
        live.start(1, _preset(), 5, make=make)
    assert len(tries) == 2
    assert [f["what"] for f in diagnostics.failures()] == [
        "Starting live edit on slot 2B (retried)", "Starting live edit on slot 2B"]
    assert not pedal._live.is_set()


def test_live_start_tries_a_dropped_read_once_more(fakes, monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _s: None)
    monkeypatch.setattr(live, "_note", lambda *a: None)
    tries = []

    def make(v, p, s, b):
        tries.append(1)
        if len(tries) == 1:
            raise DeviceError("Bulk read failed: read timed out after 2000 ms")
        return _session(p)

    preset = _preset()
    try:
        assert live.start(1, preset, 5, make=make).slot == 5
    finally:
        live.stop(keep=False)
    assert len(tries) == 2


def test_a_stomp_switched_on_plays_live_without_waiting_on_other_snapshots(fakes):
    """The Stage boost keeps its on/off across snapshots: switching it on is one
    live bypass, with nothing left waiting for Save & send."""
    chain = {"blocks": [{"model": "Kinky Boost", "parameters": {}, "enabled": False,
                         "stage_boost": True, "no_snapshot_bypass": True},
                        *CHAIN["blocks"][1:]],
             "snapshots": [{"name": f"S{i}", "blocks": {}} for i in range(4)]}
    preset = _preset(chain)
    session = _session(preset)
    on = _preset({**chain, "blocks": [{**chain["blocks"][0], "enabled": True}, *chain["blocks"][1:]]})
    result = session.update(on)
    assert result == {"sent": 1, "structural": False, "unsupported": [], "waiting": []}
    assert _bypasses(FakeRun.opened[0]) == [(1, True)]
