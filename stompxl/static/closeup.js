// The pedal close-up: tap a block while editing and it opens big, with
// sliders for its main controls and one-tap quick picks. A bottom sheet on a
// phone, a side panel on a computer. In Live, every move plays on the pedal.
//
// Shares app.js's globals (editing, current, state, api, toast, ...).

let closeup = null; // { index, start: {param: shown}, startEdits: {key: edit}, editPicks, saving }

const CU_WIDE = window.matchMedia("(min-width: 720px)");

// -------------------------------------------------------------- values

const cuView = () => editing?.view || current.version.view;
const cuBlock = () => cuView().blocks[closeup.index];
const cuNorm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

function cuParam(blk, p) {
  const view = cuView();
  const snap = view.snapshots[current.snapshot] || { values: {} };
  const snapIndex = view.snapshots.length ? current.snapshot : null;
  const snapValues = Object.fromEntries((snap.values[blk.key] || []).map((x) => [x.name, x.value]));
  const perSnapshot = p.name in snapValues || (editing.scope === "snapshot" && p.edit.per_snapshot);
  const snapshot = perSnapshot ? snapIndex : null;
  const key = editKey({ block: blk.index, parameter: p.param, snapshot });
  const original = String(snapValues[p.name] ?? p.value);
  const pending = editing.edits[key];
  return { key, snapshot, original, shown: pending ? pending.shown : original };
}

const sameValue = (p, a, b) => (p.edit.kind === "number"
  ? Math.abs(Number(a) - Number(b)) < 1e-9 : String(a) === String(b));

function cuSet(blk, p, shown) {
  const s = cuParam(blk, p);
  if (shown === "" || sameValue(p, shown, s.original)) {
    delete editing.edits[s.key];
  } else {
    const value = p.edit.kind === "number" ? Number(shown) : p.edit.kind === "toggle" ? shown === "On" : shown;
    editing.edits[s.key] = { block: blk.index, parameter: p.param, snapshot: s.snapshot, value, shown: String(shown) };
  }
  liveSync();
}

// Steps you can hear: 0.1 on a 0-10 knob, 1 % on mix, 0.5 dB on EQ gains.
const isLog = (e) => e.min > 0 && e.max / e.min >= 50;
function fineStep(p) {
  const e = p.edit;
  if (e.min == null) return e.step || 0.1;
  const width = e.max - e.min;
  if (width <= 1) return 0.01;
  if (width <= 12) return p.param === "Time" ? 0.01 : 0.1;
  if (width <= 30) return 0.5;
  return 1;
}
const decimals = (step) => (step < 0.1 ? 2 : step < 1 ? 1 : 0);
function fmt(p, v) {
  return Number(v).toFixed(isLog(p.edit) ? 0 : decimals(fineStep(p)));
}
function clampRound(p, v) {
  const e = p.edit;
  if (e.min != null) v = Math.min(e.max, Math.max(e.min, v));
  const step = isLog(e) ? 1 : fineStep(p);
  return fmt(p, Math.round(v / step) * step);
}
const toPos = (e, v) => (isLog(e) ? Math.round(1000 * Math.log(v / e.min) / Math.log(e.max / e.min)) : v);
const fromPos = (e, pos) => (isLog(e) ? e.min * (e.max / e.min) ** (pos / 1000) : pos);

// Which of the block's controls are the main ones: quick_main lists them per
// kind of block, "A|B" meaning whichever the model has.
function findParam(blk, name) {
  for (const alt of String(name).split("|")) {
    const want = cuNorm(alt);
    const p = blk.params.find((x) => x.param === alt) || blk.params.find((x) => cuNorm(x.param) === want || cuNorm(x.name) === want);
    if (p) return p;
  }
  return null;
}

function mainParams(blk) {
  const wanted = (state.quick_main || {})[blk.category] || [];
  const found = [];
  for (const name of wanted) {
    const p = findParam(blk, name);
    if (p && !found.includes(p)) found.push(p);
  }
  if (found.length) return found;
  return blk.params.filter((p) => p.edit.kind === "number").slice(0, 6);
}

// -------------------------------------------------------------- quick picks

function picksFor(blk) {
  const model = blk.model_key || blk.model;
  return (state.quick_picks || []).filter((q) => {
    if (q.kind === "model" ? q.name !== model : q.name !== blk.category) return false;
    if (q.match && !model.toLowerCase().includes(q.match.toLowerCase())) return false;
    return q.changes.some((c) => findParam(blk, c.param));
  });
}

function formatChanges(changes) {
  const num = (v) => String(Math.round(v * 1000) / 1000);
  return changes.map((c) => {
    if (c.op === "set") return `${c.param} = ${typeof c.value === "number" ? num(c.value) : c.value}`;
    if (c.op === "mul") return `${c.param} x${num(c.value)}`;
    return `${c.param} ${c.value >= 0 ? "+" : "-"}${num(Math.abs(c.value))}`;
  }).join(", ");
}

// The close-up's block, or any block (the pedal pane's quick picks).
function applyPick(pick, blk = cuBlock()) {
  const moved = [];
  for (const c of pick.changes) {
    const p = findParam(blk, c.param);
    if (!p) continue;
    const now = cuParam(blk, p).shown;
    let next;
    if (p.edit.kind === "number") {
      const cur = Number(now);
      const target = c.op === "set" ? Number(c.value) : c.op === "mul" ? cur * Number(c.value) : cur + Number(c.value);
      if (!Number.isFinite(target)) continue;
      next = clampRound(p, target);
    } else if (p.edit.kind === "toggle") {
      next = /^(on|true|1)$/i.test(String(c.value)) ? "On" : "Off";
    } else {
      next = p.edit.options.find((o) => cuNorm(o) === cuNorm(c.value));
      if (next == null) continue;
    }
    if (String(next) === String(now)) continue;
    cuSet(blk, p, next);
    moved.push(`${p.name} ${now} → ${next}`);
  }
  if (closeup) cuRender();
  rerenderChainCard();
  toast(moved.length ? `${pick.label}: ${moved.join(", ")}` : `${pick.label}: already there`);
}

// -------------------------------------------------------------- rendering

// NAM on the pedal: the Model knob picks a capture by name (see namCaptures).
function cuCaptureRow(blk, p, s, head) {
  const captures = namCaptures();
  const slot = Math.round(Number(s.shown));
  const known = captures.some((c) => c.slot === slot);
  const opts = [...(known ? [] : [{ slot, name: "not in your NAM captures" }]), ...captures];
  return `<div class="cu-row ${head.moved ? "moved" : ""}" data-cu-param="${esc(p.param)}">
    ${head.html(`<span class="cu-val">${esc(slot)}</span>`)}
    <div class="cu-ctl">
      <button class="cu-step" data-cu-capstep="-1" aria-label="Previous capture">−</button>
      <select class="cu-capture" data-cu-capture="${esc(p.param)}" aria-label="NAM capture">${opts.map((c) =>
        `<option value="${c.slot}" ${c.slot === slot ? "selected" : ""}>${esc(c.slot)} · ${esc(c.name)}</option>`).join("")}</select>
      <button class="cu-step" data-cu-capstep="1" aria-label="Next capture">+</button>
    </div>
    ${captures.length ? "" : '<p class="hint">Add your captures and their IR slots in Settings to see their names here.</p>'}</div>`;
}
// A NAM block's subtitle is its capture, not the fuzz pedal it stands in for.
function cuSubtitle(blk) {
  if (blk.nam) {
    const p = blk.params.find((x) => x.param === NAM_SLOT_PARAM);
    const name = p && captureName(cuParam(blk, p).shown);
    if (name) return `NAM capture: ${name}`;
  }
  return blk.based_on || blk.category || "";
}
function cuCaptureSet(blk, slot) {
  const p = blk.params.find((x) => x.param === NAM_SLOT_PARAM);
  cuSet(blk, p, clampRound(p, slot));
  cuRender();
  rerenderChainCard();
}

function cuRow(blk, p) {
  const s = cuParam(blk, p);
  const start = closeup.start[p.param];
  const moved = !sameValue(p, s.shown, start);
  const reset = `<button class="cu-reset" data-cu-reset="${esc(p.param)}" ${moved ? "" : "hidden"} title="Back to where you started (${esc(start)})" aria-label="Reset ${esc(p.name)}">↺</button>`;
  const head = (val) => `<div class="cu-top"><span class="cu-name">${esc(p.name)}</span>${val}${reset}</div>`;
  if (blk.nam && p.param === NAM_SLOT_PARAM) return cuCaptureRow(blk, p, s, { moved, html: head });
  if (p.edit.kind === "number" && p.edit.min != null) {
    const e = p.edit;
    const step = isLog(e) ? 1 : fineStep(p);
    const [lo, hi] = isLog(e) ? [0, 1000] : [e.min, e.max];
    const mark = ((toPos(e, Number(start)) - lo) / (hi - lo)) * 100;
    return `<div class="cu-row ${moved ? "moved" : ""}" data-cu-param="${esc(p.param)}">
      ${head(`<button class="cu-val" data-cu-type="${esc(p.param)}" title="Type a value">${esc(s.shown)}</button>`)}
      <div class="cu-ctl">
        <button class="cu-step" data-cu-step="-1" data-param="${esc(p.param)}" aria-label="${esc(p.name)} down">−</button>
        <div class="cu-track"><input type="range" min="${lo}" max="${hi}" step="${step}" value="${toPos(e, Number(s.shown))}" data-cu-range="${esc(p.param)}" aria-label="${esc(p.name)}"><span class="cu-mark" style="left:calc(9px + (100% - 18px) * ${Math.min(100, Math.max(0, mark)) / 100})" title="Where you started"></span></div>
        <button class="cu-step" data-cu-step="1" data-param="${esc(p.param)}" aria-label="${esc(p.name)} up">+</button>
      </div></div>`;
  }
  const options = p.edit.kind === "toggle" ? ["On", "Off"] : p.edit.kind === "choice" ? p.edit.options : null;
  if (options && options.length <= 8) {
    return `<div class="cu-row ${moved ? "moved" : ""}">${head("")}
      <div class="cu-opts">${options.map((o) => `<button data-cu-choice="${esc(p.param)}" data-value="${esc(o)}" class="${o === s.shown ? "active" : ""}">${esc(o)}</button>`).join("")}</div></div>`;
  }
  if (options) {
    return `<div class="cu-row ${moved ? "moved" : ""}">${head("")}
      <select data-cu-select="${esc(p.param)}">${options.map((o) => `<option ${o === s.shown ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
  }
  return `<div class="cu-row ${moved ? "moved" : ""}">${head(`<button class="cu-val" data-cu-type="${esc(p.param)}">${esc(s.shown)}</button>`)}</div>`;
}

function cuPicks(blk) {
  const picks = picksFor(blk);
  const model = blk.model_key || blk.model;
  if (closeup.editPicks) {
    const rows = picks.map((q) => `<form class="cu-pick-edit" data-cu-pick-form="${q.id}">
        <div class="cu-pick-for">${q.kind === "model" ? esc(q.name) + " only" : "Every " + esc(q.name) + (q.match ? ` (“${esc(q.match)}”)` : "")}</div>
        <input name="label" value="${esc(q.label)}" maxlength="40" aria-label="Name">
        <input name="changes" value="${esc(formatChanges(q.changes))}" aria-label="What it changes">
        <div class="row"><button class="primary" type="submit">Save</button><button type="button" class="danger" data-cu-pick-del="${q.id}">Delete</button></div>
      </form>`).join("");
    return `<div class="cu-picks-edit">${rows || '<p class="hint">No quick picks for this pedal yet.</p>'}
      <p class="hint">Write changes like <code>Presence -1</code>, <code>LowCut = 100</code> or <code>Decay x0.75</code>, separated by commas. The full list is in My tone → Quick picks.</p>
      <button data-cu-edit-picks>Done</button></div>`;
  }
  const chips = picks.map((q) => `<button class="cu-chip ${q.kind === "model" ? "own" : ""}" data-cu-pick="${q.id}" title="${esc(formatChanges(q.changes))}">${esc(q.label)}</button>`).join("");
  const save = closeup.saving ? cuSaveForm(blk, model) : "";
  return `<div class="cu-chips">${chips}
      <button class="cu-chip add" data-cu-save-pick title="Save what you've changed in this pedal as a quick pick">+ Save as pick</button>
      ${picks.length ? '<button class="cu-chip ghost" data-cu-edit-picks title="Rename, change or delete quick picks">✎</button>' : ""}
    </div>${save}`;
}

function cuMoved(blk) {
  return blk.params.map((p) => [p, cuParam(blk, p).shown]).filter(([p, shown]) => !sameValue(p, shown, closeup.start[p.param]));
}

function cuSaveForm(blk, model) {
  const moved = cuMoved(blk);
  const numeric = moved.some(([p]) => p.edit.kind === "number");
  return `<form class="cu-save" id="cu-save-form">
    <p class="hint">Saves these changes: ${moved.map(([p, v]) => `${esc(p.name)} ${esc(closeup.start[p.param])} → ${esc(v)}`).join(", ")}</p>
    <input name="label" maxlength="40" placeholder="Name, e.g. Gig volume" required>
    <div class="cu-save-opts">
      ${numeric ? `<label class="toggle"><input type="radio" name="mode" value="set" checked> Set these values</label>
      <label class="toggle"><input type="radio" name="mode" value="nudge"> Nudge by the same amounts</label>` : '<input type="hidden" name="mode" value="set">'}
      <label class="toggle"><input type="radio" name="for" value="model" checked> ${esc(model)} only</label>
      <label class="toggle"><input type="radio" name="for" value="category"> Every ${esc(blk.category)} block</label>
    </div>
    <div class="row"><button class="primary" type="submit">Save pick</button><button type="button" class="ghost" data-cu-save-cancel>Cancel</button></div>
  </form>`;
}

function cuRender() {
  const el = $("#closeup");
  if (!closeup || !editing || !cuView().blocks[closeup.index]) return closeupClose();
  const blk = cuBlock();
  const view = cuView();
  const snap = view.snapshots[current.snapshot] || { enabled: {} };
  const toggleSnapshot = blk.stomp ? null : (view.snapshots.length ? current.snapshot : null);
  const pendingOn = editing.edits[editKey({ block: blk.index, snapshot: toggleSnapshot })];
  const on = pendingOn ? pendingOn.enabled : blk.stomp ? blk.enabled : snap.enabled[blk.key] !== false;
  const main = mainParams(blk);
  const rest = blk.params.filter((p) => !main.includes(p));
  const open = el.querySelector("details.cu-all")?.open;
  el.style.setProperty("--cat", CAT_COLORS[blk.category] || "#768390");
  el.innerHTML = `
    <div class="cu-head" id="cu-head">
      <button class="cu-nav" data-cu-nav="-1" aria-label="Previous pedal" ${view.blocks.length < 2 ? "disabled" : ""}>◀</button>
      <div class="cu-title"><b>${esc(blk.model)}</b><small>${esc(cuSubtitle(blk))}</small></div>
      <button class="cu-nav" data-cu-nav="1" aria-label="Next pedal" ${view.blocks.length < 2 ? "disabled" : ""}>▶</button>
      <button class="cu-close" data-cu-close aria-label="Close">✕</button>
    </div>
    <div class="cu-bar">
      <button class="tag toggle-block ${pendingOn ? "edited" : ""}" data-toggle-block="${blk.index}" data-snapshot="${toggleSnapshot ?? ""}" data-on="${on ? 1 : 0}" style="color:${on ? "var(--on)" : "var(--muted)"}">${on ? "ON" : "OFF"}</button>
      ${editing.live ? `<button class="cu-hold" data-cu-hold title="Hold to hear this pedal as it was when you opened it">Hold for original</button><span class="cu-live">Live on ${esc(editing.live.label)}</span>` : '<span class="hint">Switch on Live below to hear changes as you make them.</span>'}
      <button data-explain="${blk.index}">Explain</button>
    </div>
    ${cuPicks(blk)}
    <div class="cu-rows">${main.map((p) => cuRow(blk, p)).join("")}</div>
    ${rest.length ? `<details class="cu-all" ${open ? "open" : ""}><summary>All settings (${rest.length} more)</summary><div class="cu-rows">${rest.map((p) => cuRow(blk, p)).join("")}</div></details>` : ""}
    <div class="cu-foot"><button data-cu-reset-all ${cuMoved(blk).length ? "" : "disabled"}>↺ Undo changes to this pedal</button></div>`;
}

// Just the parts of one row that a drag changes, so the slider keeps its grip.
function cuRefreshRow(blk, p) {
  const row = $(`#closeup [data-cu-param="${CSS.escape(p.param)}"]`);
  if (!row) return;
  const shown = cuParam(blk, p).shown;
  const moved = !sameValue(p, shown, closeup.start[p.param]);
  row.querySelector(".cu-val").textContent = shown;
  row.classList.toggle("moved", moved);
  row.querySelector(".cu-reset").hidden = !moved;
}

// -------------------------------------------------------------- open / close

function closeupOpen(index) {
  // Tuning needs no Edit step: it starts a light edit that ends on close if
  // nothing changed.
  if (!editing) { editing = startEditing(); editing.implicit = true; rerenderChainCard(); }
  const blk = cuView().blocks[index];
  if (!blk) return;
  const start = {};
  const startEdits = {};
  for (const p of blk.params) {
    const s = cuParam(blk, p);
    start[p.param] = s.shown;
    if (editing.edits[s.key]) startEdits[s.key] = { ...editing.edits[s.key] };
  }
  closeup = { index, start, startEdits, editPicks: false, saving: false };
  $("#closeup").hidden = false;
  $("#closeup-scrim").hidden = CU_WIDE.matches;
  document.body.classList.add("closeup-open");
  for (const b of document.querySelectorAll(".chain .block.focused")) b.classList.remove("focused");
  document.querySelector(`.chain .block[data-index="${index}"]`)?.classList.add("focused");
  cuRender();
  $("#closeup").scrollTop = 0;
}

function closeupClose() {
  if (!closeup) return;
  closeup = null;
  $("#closeup").hidden = true;
  $("#closeup-scrim").hidden = true;
  document.body.classList.remove("closeup-open");
  if (editing?.implicit && !editCount() && !editing.live) editing = null;
  if (editing || current.version) rerenderChainCard();
  else for (const b of document.querySelectorAll(".chain .block.focused")) b.classList.remove("focused");
}

CU_WIDE.addEventListener("change", () => { if (closeup) $("#closeup-scrim").hidden = CU_WIDE.matches; });

// Keep the panel in step with the chain card (snapshot tabs, block toggles,
// block changes, saves).
const _rerenderChainCard = rerenderChainCard;
rerenderChainCard = function () {
  _rerenderChainCard();
  if (closeup) {
    document.querySelector(`.chain .block[data-index="${closeup.index}"]`)?.classList.add("focused");
    cuRender();
  }
};

// -------------------------------------------------------------- events

function cuStep(param, dir, big = false) {
  const blk = cuBlock();
  const p = blk.params.find((x) => x.param === param);
  const cur = Number(cuParam(blk, p).shown);
  const next = isLog(p.edit) ? cur * (dir > 0 ? (big ? 1.25 : 1.03) : 1 / (big ? 1.25 : 1.03)) : cur + dir * fineStep(p) * (big ? 10 : 1);
  cuSet(blk, p, clampRound(p, next));
  const range = $(`#closeup [data-cu-range="${CSS.escape(param)}"]`);
  if (range) range.value = toPos(p.edit, Number(cuParam(blk, p).shown));
  cuRefreshRow(blk, p);
}

let cuRepeat = null;
function cuStopRepeat() { clearTimeout(cuRepeat); clearInterval(cuRepeat); cuRepeat = null; }

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, .block");
  if (!t) return;
  // A tap on a block's body opens its close-up; its own buttons keep their jobs.
  if (t.classList.contains("block") && t.dataset.index != null && t.closest("#chain-card .chain")) {
    if (e.target.closest("input, select, summary, details table, .drag-handle")) return;
    closeupOpen(Number(t.dataset.index));
    return;
  }
  if (!t.closest("#closeup") && t.dataset.closeup == null) return;
  try {
    if (t.dataset.closeup != null) {
      closeupOpen(Number(t.dataset.closeup));
    } else if (t.dataset.cuClose != null) {
      closeupClose();
    } else if (t.dataset.cuNav) {
      const n = cuView().blocks.length;
      closeupOpen((closeup.index + Number(t.dataset.cuNav) + n) % n);
    } else if (t.dataset.cuChoice) {
      const blk = cuBlock();
      cuSet(blk, blk.params.find((x) => x.param === t.dataset.cuChoice), t.dataset.value);
      rerenderChainCard();
    } else if (t.dataset.cuType) {
      const blk = cuBlock();
      const p = blk.params.find((x) => x.param === t.dataset.cuType);
      const typed = prompt(`${p.name}${p.edit.min != null ? ` (${p.edit.min} to ${p.edit.max})` : ""}`, cuParam(blk, p).shown);
      if (typed == null || typed.trim() === "" || Number.isNaN(Number(typed))) return;
      cuSet(blk, p, clampRound(p, Number(typed)));
      rerenderChainCard();
    } else if (t.dataset.cuReset) {
      const blk = cuBlock();
      cuSet(blk, blk.params.find((x) => x.param === t.dataset.cuReset), closeup.start[t.dataset.cuReset]);
      rerenderChainCard();
    } else if (t.dataset.cuResetAll != null) {
      const blk = cuBlock();
      for (const p of blk.params) cuSet(blk, p, closeup.start[p.param]);
      rerenderChainCard();
    } else if (t.dataset.cuPick) {
      applyPick(state.quick_picks.find((q) => q.id === Number(t.dataset.cuPick)));
    } else if (t.dataset.cuEditPicks != null) {
      closeup.editPicks = !closeup.editPicks;
      closeup.saving = false;
      cuRender();
    } else if (t.dataset.cuSavePick != null) {
      if (!cuMoved(cuBlock()).length) { toast("Change some settings in this pedal first, then save them as a pick"); return; }
      closeup.saving = true;
      cuRender();
      $("#cu-save-form input[name=label]").focus();
    } else if (t.dataset.cuSaveCancel != null) {
      closeup.saving = false;
      cuRender();
    } else if (t.dataset.cuPickDel) {
      const q = state.quick_picks.find((x) => x.id === Number(t.dataset.cuPickDel));
      if (!confirm(`Delete the quick pick "${q.label}"?`)) return;
      state.quick_picks = (await api(`/api/quick-picks/${q.id}`, { method: "DELETE" })).quick_picks;
      cuRender();
      renderQuickPicks();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

$("#closeup-scrim").addEventListener("click", closeupClose);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && closeup && !document.querySelector("dialog[open]")) closeupClose();
});

// Sliders: every movement plays (live sends are throttled in liveSync); the
// chain card catches up on release.
$("#closeup").addEventListener("input", (e) => {
  const param = e.target.dataset.cuRange;
  if (!param) return;
  const blk = cuBlock();
  const p = blk.params.find((x) => x.param === param);
  cuSet(blk, p, clampRound(p, fromPos(p.edit, Number(e.target.value))));
  cuRefreshRow(blk, p);
});
$("#closeup").addEventListener("change", (e) => {
  if (e.target.dataset.cuCapture) { cuCaptureSet(cuBlock(), Number(e.target.value)); return; }
  if (e.target.dataset.cuRange) rerenderChainCard();
  if (e.target.dataset.cuSelect) {
    const blk = cuBlock();
    cuSet(blk, blk.params.find((x) => x.param === e.target.dataset.cuSelect), e.target.value);
    rerenderChainCard();
  }
});
// Shift+arrow on a slider takes big steps.
$("#closeup").addEventListener("keydown", (e) => {
  const param = e.target.dataset.cuRange;
  if (!param || !e.shiftKey || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
  e.preventDefault();
  cuStep(param, e.key === "ArrowUp" || e.key === "ArrowRight" ? 1 : -1, true);
});

// − / +: a tap is one step; holding repeats.
$("#closeup").addEventListener("pointerdown", (e) => {
  const capStep = e.target.closest("[data-cu-capstep]");
  if (capStep) {
    // − / + walk your captures, skipping empty IR slots.
    e.preventDefault();
    const blk = cuBlock();
    const p = blk.params.find((x) => x.param === NAM_SLOT_PARAM);
    const slots = namCaptures().map((c) => c.slot);
    if (!slots.length) return;
    const cur = Math.round(Number(cuParam(blk, p).shown));
    const dir = Number(capStep.dataset.cuCapstep);
    const next = dir > 0 ? slots.find((x) => x > cur) ?? slots[0] : [...slots].reverse().find((x) => x < cur) ?? slots[slots.length - 1];
    cuCaptureSet(blk, next);
    return;
  }
  const step = e.target.closest("[data-cu-step]");
  if (step) {
    e.preventDefault();
    const go = () => cuStep(step.dataset.param, Number(step.dataset.cuStep));
    go();
    cuRepeat = setTimeout(() => { cuRepeat = setInterval(go, 70); }, 400);
    return;
  }
  const hold = e.target.closest("[data-cu-hold]");
  if (hold && editing?.live && !closeup.held) {
    // Swap this pedal's values back to where they started, until let go.
    e.preventDefault();
    const blk = cuBlock();
    const mine = (k) => k.startsWith(`${blk.index}|`);
    closeup.held = Object.fromEntries(Object.entries(editing.edits).filter(([k]) => mine(k)));
    for (const k of Object.keys(closeup.held)) delete editing.edits[k];
    Object.assign(editing.edits, structuredClone(closeup.startEdits));
    hold.classList.add("active");
    liveSync();
  }
});
function cuRelease() {
  if (cuRepeat) { cuStopRepeat(); rerenderChainCard(); }
  if (closeup?.held) {
    const blk = cuBlock();
    for (const k of Object.keys(editing.edits)) if (k.startsWith(`${blk.index}|`)) delete editing.edits[k];
    Object.assign(editing.edits, closeup.held);
    closeup.held = null;
    $("#closeup [data-cu-hold]")?.classList.remove("active");
    liveSync();
  }
}
for (const ev of ["pointerup", "pointercancel", "pointerleave"]) $("#closeup").addEventListener(ev, cuRelease);
window.addEventListener("blur", cuRelease);

// Swipe the header sideways for the next pedal, or down to close.
let cuSwipe = null;
$("#closeup").addEventListener("pointerdown", (e) => {
  if (e.target.closest("#cu-head") && !e.target.closest("button")) cuSwipe = { x: e.clientX, y: e.clientY };
});
$("#closeup").addEventListener("pointerup", (e) => {
  if (!cuSwipe) return;
  const dx = e.clientX - cuSwipe.x;
  const dy = e.clientY - cuSwipe.y;
  cuSwipe = null;
  if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
    const n = cuView().blocks.length;
    closeupOpen((closeup.index + (dx < 0 ? 1 : -1) + n) % n);
  } else if (dy > 80 && !CU_WIDE.matches) {
    closeupClose();
  }
});

$("#closeup").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    if (f.id === "cu-save-form") {
      const blk = cuBlock();
      const nudge = f.elements.mode.value === "nudge";
      const changes = cuMoved(blk).map(([p, v]) => {
        if (nudge && p.edit.kind === "number") {
          const d = Number(v) - Number(closeup.start[p.param]);
          return { param: p.param, op: "add", value: Math.round(d * 1000) / 1000 };
        }
        return { param: p.param, op: "set", value: p.edit.kind === "number" ? Number(v) : v };
      });
      const forModel = f.elements.for.value === "model";
      const res = await api("/api/quick-picks", { method: "POST", body: {
        kind: forModel ? "model" : "category", name: forModel ? (blk.model_key || blk.model) : blk.category,
        label: f.elements.label.value, changes } });
      state.quick_picks = res.quick_picks;
      closeup.saving = false;
      cuRender();
      renderQuickPicks();
      toast(`Saved the quick pick "${f.elements.label.value.trim()}"`);
    } else if (f.dataset.cuPickForm) {
      const res = await api(`/api/quick-picks/${f.dataset.cuPickForm}`, { method: "PUT",
        body: { label: f.elements.label.value, changes: f.elements.changes.value } });
      state.quick_picks = res.quick_picks;
      cuRender();
      renderQuickPicks();
      toast("Quick pick saved");
    }
  } catch (err) {
    toast(err.message, true);
  }
});

// -------------------------------------------------------------- My tone → Quick picks

function renderQuickPicks() {
  const list = $("#qp-list");
  if (!list) return;
  const picks = state.quick_picks || [];
  const groups = new Map();
  for (const q of picks) {
    const title = q.kind === "model" ? `${q.name} only` : `Every ${q.name} block`;
    if (!groups.has(title)) groups.set(title, []);
    groups.get(title).push(q);
  }
  list.innerHTML = picks.length ? [...groups].map(([title, items]) => `<li class="group">${esc(title)}</li>${items.map((q) => `<li>
      <span class="grow"><b>${esc(q.label)}</b>${q.match ? ` <small class="muted">(models with “${esc(q.match)}”)</small>` : ""}<br><code>${esc(formatChanges(q.changes))}</code></span>
      <button data-qp-edit="${q.id}">Edit</button>
      <button class="danger" data-qp-del="${q.id}">Delete</button></li>`).join("")}`).join("")
    : '<li class="muted">No quick picks. Add one below, or restore the starting set.</li>';
  $("#qp-count").textContent = picks.length || "";
}

function qpResetForm() {
  const f = $("#qp-form");
  f.reset();
  f.elements.id.value = "";
  $("#qp-form-title").textContent = "Add a quick pick";
  $("#qp-submit").textContent = "Add pick";
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  try {
    if (t.dataset.qpEdit) {
      const q = state.quick_picks.find((x) => x.id === Number(t.dataset.qpEdit));
      const f = $("#qp-form");
      f.elements.id.value = q.id;
      f.elements.kind.value = q.kind;
      f.elements.name.value = q.name;
      f.elements.label.value = q.label;
      f.elements.changes.value = formatChanges(q.changes);
      f.elements.match.value = q.match || "";
      $("#qp-form-title").textContent = "Edit quick pick";
      $("#qp-submit").textContent = "Save pick";
      gearOptions(q.kind === "model" ? "model" : "category").then(() => {
        $("#qp-options").innerHTML = $("#gear-options").innerHTML;
      });
      f.scrollIntoView({ block: "nearest" });
    } else if (t.dataset.qpDel) {
      const q = state.quick_picks.find((x) => x.id === Number(t.dataset.qpDel));
      if (!confirm(`Delete the quick pick "${q.label}"?`)) return;
      state.quick_picks = (await api(`/api/quick-picks/${q.id}`, { method: "DELETE" })).quick_picks;
      renderQuickPicks();
    } else if (t.id === "qp-reset") {
      qpResetForm();
    } else if (t.id === "qp-restore") {
      if (!confirm("Put back the starting quick picks? Ones you changed or deleted come back as they were; your own stay.")) return;
      state.quick_picks = (await api("/api/quick-picks/restore", { method: "POST", body: {} })).quick_picks;
      renderQuickPicks();
      toast("Starting quick picks restored");
    }
  } catch (err) {
    toast(err.message, true);
  }
});

$("#qp-kind")?.addEventListener("change", async (e) => {
  await gearOptions(e.target.value === "model" ? "model" : "category");
  $("#qp-options").innerHTML = $("#gear-options").innerHTML;
});
$("#qp-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { kind: f.elements.kind.value, name: f.elements.name.value, label: f.elements.label.value,
    changes: f.elements.changes.value, match: f.elements.match.value };
  try {
    const id = f.elements.id.value;
    const res = id ? await api(`/api/quick-picks/${id}`, { method: "PUT", body })
      : await api("/api/quick-picks", { method: "POST", body });
    state.quick_picks = res.quick_picks;
    renderQuickPicks();
    qpResetForm();
    toast(id ? "Quick pick saved" : "Quick pick added");
  } catch (err) {
    toast(err.message, true);
  }
});
