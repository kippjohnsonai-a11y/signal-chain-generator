// Stage mode: a lean screen for gigs and rehearsal. Big Prev / Next through
// the set, the pedals to switch on and off, and one-tap fixes that know which
// block to change ("Less boom" moves the cab's low cut). No knobs.
//
// It is Live edit underneath: every tap plays on the pedal straight away, and
// leaving a song asks to keep the changes (a starred "Rehearsal" version, saved
// to the pedal's slot too) or toss them (the slot goes back as it was).
//
// Shares app.js's and closeup.js's globals (editing, current, state, api,
// cuParam, cuSet, findParam, clampRound, startLive, endLive, ...).

let stage = null; // { songId, versionId, status, error, undo: [], locked }

const stView = () => editing?.view || current.version.view;
const stFind = (test) => stView().blocks.find(test);
const stAmp = () => stFind((b) => b.category === "Amp");
const stCab = () => stFind((b) => b.category === "Cab");
const stNam = () => stFind((b) => b.nam);
const stEq = (param, test = () => true) => stFind((b) => b.category === "EQ" && findParam(b, param) && test(b));
const stNum = (blk, name) => Number(cuParam(blk, findParam(blk, name)).shown);
const midAt = (lo, hi) => (b) => { const p = findParam(b, "MidFreq"); return p && stNum(b, "MidFreq") >= lo && stNum(b, "MidFreq") <= hi; };

// Each fix: the blocks it may change, first one the preset has wins.
// [block finder, parameter, new value from the current one]
const STAGE_FIXES = {
  quieter: [[stCab, "Level", (v) => v - 1.5], [stAmp, "ChVol", (v) => v - 0.5], [stNam, "Output", (v) => v - 0.5]],
  louder: [[stCab, "Level", (v) => v + 1.5], [stAmp, "ChVol", (v) => v + 0.5], [stNam, "Output", (v) => v + 0.5]],
  boom: [[stCab, "LowCut", (v) => (v < 65 ? 80 : v + 15)], [() => stEq("LowCut"), "LowCut", (v) => (v < 65 ? 80 : v + 15)],
    [stAmp, "Bass", (v) => v - 0.5], [stNam, "Bass", (v) => v - 0.5]],
  body: [[stCab, "LowCut", (v) => v - 15], [() => stEq("LowCut"), "LowCut", (v) => v - 15],
    [stAmp, "Bass", (v) => v + 0.5], [stNam, "Bass", (v) => v + 0.5]],
  screech: [[stCab, "HighCut", (v) => (v > 12000 ? 10000 : v - 1000)], [() => stEq("HighCut"), "HighCut", (v) => (v > 12000 ? 10000 : v - 1000)],
    [stAmp, "Treble", (v) => v - 0.5], [stNam, "Treble", (v) => v - 0.5]],
  air: [[stCab, "HighCut", (v) => v + 1000], [() => stEq("HighCut"), "HighCut", (v) => v + 1000],
    [stAmp, "Treble", (v) => v + 0.5], [stNam, "Treble", (v) => v + 0.5]],
  mud: [[() => stEq("MidGain", midAt(200, 700)), "MidGain", (v) => v - 1.5], [stAmp, "Bass", (v) => v - 0.5], [stNam, "Bass", (v) => v - 0.5]],
  cut: [[stAmp, "Mid", (v) => v + 0.5], [() => stEq("MidGain", midAt(700, 2500)), "MidGain", (v) => v + 1.5], [stNam, "Mid", (v) => v + 0.5]],
};
const STAGE_PAIRS = [["quieter", "Quieter", "louder", "Louder"], ["boom", "Less boom", "body", "More body"],
  ["screech", "Less screech", "air", "More air"], ["mud", "Less mud", "cut", "Cut through"]];

function stageRemember() {
  stage.undo.push({ edits: { ...editing.edits }, committed: [...editing.committed], view: editing.view });
  if (stage.undo.length > 30) stage.undo.shift();
}

function stageFix(id) {
  for (const [find, name, next] of STAGE_FIXES[id]) {
    const blk = find();
    const p = blk && findParam(blk, name);
    if (!p || p.edit.kind !== "number") continue;
    const now = stNum(blk, name);
    const to = stRound(p, next(now));
    if (Number(to) === now) { toast(`${p.name} is already as far as it goes`); return; }
    stageRemember();
    cuSet(blk, p, to);
    toast(`${shortModel(blk)} ${p.name}: ${fmt(p, now)} → ${to}`);
    renderStage();
    return;
  }
  toast("Nothing in this preset to change for that", true);
}

const shortModel = (blk) => (blk.nam ? "NAM" : blk.stomp && blk.model_key === "Gain" ? "Volume boost" : blk.model);

// dB moves land on half dB; anything else on the close-up's own steps.
function stRound(p, v) {
  if (!/^(Level|MidGain|LowGain|HighGain)$/.test(p.param)) return clampRound(p, v);
  const e = p.edit;
  return String(Math.min(e.max ?? v, Math.max(e.min ?? v, Math.round(v * 2) / 2)));
}

// On/off for a block: a stomp (boosts, looper) for every snapshot, anything
// else in the snapshot the pedal is playing.
function stageOn(blk) {
  const snapshot = blk.stomp ? null : current.snapshot;
  const pending = editing.edits[editKey({ block: blk.index, snapshot })];
  if (pending) return pending.enabled;
  if (blk.stomp) return blk.enabled;
  const snap = stView().snapshots[current.snapshot];
  return snap ? snap.enabled[blk.key] !== false : blk.enabled;
}

function stageToggle(blk) {
  const snapshot = blk.stomp ? null : current.snapshot;
  const key = editKey({ block: blk.index, snapshot });
  stageRemember();
  if (key in editing.edits) delete editing.edits[key];
  else editing.edits[key] = { block: blk.index, snapshot, enabled: !stageOn(blk) };
  liveSync();
  renderStage();
}

// Poly Capo: -2..+2 semitones from where the version has it.
function stageKey(step) {
  const capo = stFind((b) => b.model_key === "Poly Capo");
  const p = capo && findParam(capo, "Interval");
  if (!p) return;
  stageRemember();
  cuSet(capo, p, String(Number(cuParam(capo, p).original) + step));
  renderStage();
}

function stageUndo() {
  const last = stage.undo.pop();
  if (!last) return;
  editing.edits = last.edits;
  editing.committed = last.committed;
  editing.view = last.view;
  liveSync();
  renderStage();
}

async function stageAddBoost() {
  stageRemember();
  await blockChange([{ stage_boost: true }]);
  renderStage();
}

function stageSetNav() {
  const sets = setsOf(stage.songId);
  const set = sets.find((f) => f.id === activeSet) || sets[0] || null;
  const list = set ? setList(set.id) : [];
  const i = list.findIndex((s) => s.id === stage.songId);
  return { set, list, i, prev: list[i - 1], next: list[i + 1] };
}

function renderStage() {
  const box = $("#stage");
  if (!stage || !current.version) { box.innerHTML = ""; return; }
  const view = stView();
  const listed = state.songs.find((s) => s.id === stage.songId) || {};
  const { set, list, i, prev, next } = stageSetNav();
  const changed = editCount();
  const capo = stFind((b) => b.model_key === "Poly Capo");
  const capoP = capo && findParam(capo, "Interval");
  const capoNow = capoP ? Number(cuParam(capo, capoP).shown) - Number(cuParam(capo, capoP).original) : 0;
  const boost = stFind((b) => b.stage_boost);
  const status = {
    live: `<span class="st-live on">● Live on ${esc(editing?.live?.label || "")}</span>`,
    connecting: `<span class="st-live">Connecting to the pedal…</span>`,
    unsent: `<span class="st-live bad">Not on the pedal yet: send it from the song first</span>`,
    error: `<span class="st-live bad">${esc(stage.error || "Not connected")} <button class="ghost small" data-stage="retry">Retry</button></span>`,
  }[editing?.live ? "live" : stage.status] || "";
  const tiles = view.blocks.map((b) => {
    const on = stageOn(b);
    return `<button class="st-tile ${on ? "on" : ""}" data-stage-block="${b.index}" style="--cat:${CAT_COLORS[b.category] || "#768390"}" aria-pressed="${on}">
      <span class="st-tile-name">${esc(b.stage_boost ? "Stage boost" : shortModel(b))}</span>
      <span class="st-tile-sub">${b.footswitch ? "FS " + b.footswitch : esc(b.category || "")}</span></button>`;
  }).join("");
  const snaps = view.snapshots.map((s, n) => `<span class="st-snap ${n === current.snapshot ? "on" : ""}">${n + 1} ${esc(s.name || "")}</span>`).join("");
  box.innerHTML = `
    <div class="st-top">
      <button class="st-exit" data-stage="exit" aria-label="Leave Stage mode">✕</button>
      <div class="st-where">${set ? `<b>${esc(set.name)}</b> · ${i + 1} of ${list.length}` : "Stage"}</div>
      <button class="st-lock ${stage.locked ? "on" : ""}" data-stage="lock" aria-pressed="${stage.locked}">${stage.locked ? "🔒 Locked" : "🔓 Lock"}</button>
    </div>
    <div class="st-song">
      <h1>${esc(current.song?.title || listed.title || "")}</h1>
      <div class="st-meta">${listed.pushed_slot != null ? `<span class="slot-badge">${slotLabel(listed.pushed_slot)}</span>` : ""} ${status}</div>
    </div>
    <div class="st-nav">
      <button class="st-step" data-stage-song="${prev ? prev.id : ""}" ${prev ? "" : "disabled"}>◀ <span>${prev ? esc(prev.title) : "Prev"}</span></button>
      <button class="st-step" data-stage-song="${next ? next.id : ""}" ${next ? "" : "disabled"}><span>${next ? esc(next.title) : "Next"}</span> ▶</button>
    </div>
    ${snaps ? `<div class="st-snaps" title="Change snapshots with the pedal's footswitches">${snaps}</div><p class="st-hint">Snapshots change on the pedal's footswitches. On/off here is for snapshot ${current.snapshot + 1}, the one it opens on.</p>` : ""}
    <h2 class="st-h">Pedals</h2>
    <div class="st-tiles">${tiles}</div>
    <h2 class="st-h">Quick fixes</h2>
    <div class="st-fixes">
      ${boost ? `<button class="st-fix wide ${stageOn(boost) ? "on" : ""}" data-stage-block="${boost.index}" aria-pressed="${stageOn(boost)}">Gain boost ${stageOn(boost) ? "ON" : "off"}</button>`
        : `<button class="st-fix wide ghost" data-stage="add-boost" title="Adds an off drive just before the amp. It plays after Keep, and a send.">+ Add Stage boost</button>`}
      ${STAGE_PAIRS.map(([a, la, b, lb]) => `<button class="st-fix" data-stage-fix="${a}">${la}</button><button class="st-fix" data-stage-fix="${b}">${lb}</button>`).join("")}
    </div>
    ${capoP ? `<h2 class="st-h">Key (Poly Capo)</h2><div class="st-key">${[-2, -1, 0, 1, 2].map((k) =>
      `<button class="${k === capoNow ? "on" : ""}" data-stage-key="${k}">${k > 0 ? "+" + k : k === 0 ? "As saved" : k}</button>`).join("")}</div>` : ""}
    <div class="st-foot">
      <button class="ghost" data-stage="undo" ${stage.undo.length ? "" : "disabled"}>↶ Undo</button>
      <span class="st-count">${changed ? `${changed} change${changed === 1 ? "" : "s"}` : "No changes"}</span>
      <button class="primary" data-stage="keep" ${changed ? "" : "disabled"}>Keep</button>
    </div>`;
  document.body.classList.toggle("stage-locked", !!stage.locked);
}

async function openStage(songId) {
  const listed = state.songs.find((s) => s.id === songId);
  if (!listed) throw new Error("That song isn't in your library");
  const song = await api(`/api/songs/${songId}`);
  const versionId = listed.open_version_id || song.favorite_version_id || song.versions.at(-1)?.id;
  if (!versionId) throw new Error("This song has no versions yet");
  const v = await api(`/api/versions/${versionId}`);
  current = { songId, versionId, snapshot: v.view.playing_snapshot || 0, version: v, song };
  editing = startEditing();
  editing.stage = true;
  stage = { songId, versionId, status: "connecting", error: "", undo: [], locked: !!stage?.locked };
  show("stage-view");
  renderStage();
  if ((v.chain.nam || {}).mode === "usb") { stage.status = "error"; stage.error = "NAM-on-computer presets live in HX Edit"; renderStage(); return; }
  if (v.pushed_slot == null) { stage.status = "unsent"; renderStage(); return; }
  await stageConnect();
}

async function stageConnect() {
  stage.status = "connecting";
  renderStage();
  try {
    await startLive();
    stage.status = editing?.live ? "live" : "error";
  } catch (err) {
    stage.status = "error";
    stage.error = err.message;
  }
  if (stage) renderStage();
}

// Leaving the song: keep (a Rehearsal version, saved on the pedal) or toss.
// Resolves false to stay.
async function stageLeave() {
  if (!editing?.stage) return true;
  if (editCount()) {
    const choice = await new Promise((resolve) => {
      const d = $("#stage-leave");
      $("#stage-leave-count").textContent = `${editCount()} change${editCount() === 1 ? "" : "s"}`;
      for (const b of d.querySelectorAll("[data-leave]")) b.onclick = () => { d.close(); resolve(b.dataset.leave); };
      d.oncancel = () => resolve("stay");
      d.showModal();
    });
    if (choice === "stay") return false;
    if (choice === "keep") return stageKeep();
  }
  if (editing.live) await endLive(false);
  editing = null;
  return true;
}

async function stageKeep() {
  const edits = [...editing.committed, ...pendingEdits()];
  const structural = editing.committed.length > 0;
  clearTimeout(liveSync.timer);
  liveSync.timer = null;
  while (liveSync.busy) await new Promise((r) => setTimeout(r, 30));
  if (liveSync.dirty) await liveFlush();
  let result;
  try {
    result = await api(`/api/versions/${current.versionId}/edit`, { method: "POST", body: { edits, rehearsal: !structural } });
  } catch (err) { toast(err.message, true); return false; }
  let note = "";
  if (editing.live) {
    try {
      const done = await api("/api/live/stop", { method: "POST", body: { keep: true, version_id: editing.live.structural ? null : result.version_id } });
      note = editing.live.structural ? " Send it to the pedal from the song to hear the new block." : ` and on the pedal (${done.label})`;
    } catch (err) { note = `, but not on the pedal: ${err.message}`; }
  }
  editing = null;
  await loadState();
  toast(`Kept as a new version${note}`);
  return true;
}

async function stageClick(t) {
  if (t.dataset.stage === "lock") { stage.locked = !stage.locked; renderStage(); return; }
  if (stage.locked) { toast("Locked: tap 🔓 to change things"); return; }
  if (t.dataset.stageSong) { await navigate(`#/stage/${t.dataset.stageSong}`); return; }
  if (t.dataset.stageFix) { stageFix(t.dataset.stageFix); return; }
  if (t.dataset.stageKey != null) { stageKey(Number(t.dataset.stageKey)); return; }
  if (t.dataset.stageBlock) { stageToggle(stView().blocks[Number(t.dataset.stageBlock)]); return; }
  const what = t.dataset.stage;
  if (what === "undo") stageUndo();
  else if (what === "retry") await stageConnect();
  else if (what === "add-boost") await stageAddBoost();
  else if (what === "keep") { if (await stageKeep()) await openStage(stage.songId); }
  else if (what === "exit") await navigate(set0Url());
}

const set0Url = () => (activeSet ? "#/sets" : `#/song/${stage?.songId || ""}`);

document.addEventListener("click", async (e) => {
  const t = e.target.closest("#stage button");
  if (!t || t.disabled) return;
  try { await stageClick(t); } catch (err) { toast(err.message, true); }
});
