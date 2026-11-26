// The pedal pane (#/pedal): work on whatever preset is on the pedal, without
// making it a song. Jump between the pedal's presets like HX Edit's list, tune
// blocks in the close-up or with quick picks straight from the pane, and ask
// Claude what to change. Every change plays live; Save writes it to the slot.
//
// The open preset stands in for a version (id -1 on the server), so the chain
// card, the close-up, live edit and Explain from app.js / closeup.js work on it
// unchanged. Shares app.js's globals (current, editing, state, api, toast, ...).

const DESK_ID = -1;
const DESK_SLOT_KEY = "stompxl.deskSlot";
const DESK_GUITAR_KEY = "stompxl.deskGuitar";
const desk = {
  slots: null,      // [{slot, label, name}] from the pedal's own list
  filter: "",
  busy: "",         // what the pane is waiting on, shown in its head
  liveError: "",
  chats: {},        // slot -> [{role, text, suggestions?, applied?}]
  draft: "",
  asking: false,
};

const deskRemember = (k, v) => { try { localStorage.setItem(k, String(v)); } catch (_) { /* private mode */ } };
const deskRecall = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
const deskSlot = () => current.version?.pushed_slot;
const deskChat = () => (desk.chats[deskSlot()] ||= []);

// -------------------------------------------------------------- open

function deskEnsureEditing() {
  if (!editing) { editing = startEditing(); editing.implicit = true; }
}

// Show what the server says the pane holds now: the preset, and whether live is on.
function deskShow(res) {
  if (res.slots) desk.slots = res.slots;
  current.version = res.version;
  current.versionId = DESK_ID;
  current.songId = null;
  current.song = null;
  if (!res.version.view.snapshots[current.snapshot]) current.snapshot = 0;
  editing = startEditing();
  editing.implicit = true;
  editing.live = res.live ? { label: res.live.label, structural: false, waiting: "" } : null;
  desk.liveError = res.live_error || "";
  deskRemember(DESK_SLOT_KEY, res.version.pushed_slot);
  show("song-view");
  renderVersion();
  syncUrl();
  lastUrl = location.hash;
}

async function deskLoadSlots() {
  try {
    desk.slots = (await api("/api/pedal/slots")).slots;
  } catch (err) {
    desk.slots = desk.slots || null;
    throw err;
  }
}

// Opening the pane: the preset already open if there is one, else the last one used here.
async function openDesk() {
  if (typeof closeupClose === "function") closeupClose();
  if (editing?.live && !current.version?.pedal) await endLive(false);
  const now = await api("/api/desk");
  if (now.slots) desk.slots = now.slots;
  if (now.version) { deskShow(now); return; }
  current = { songId: null, versionId: DESK_ID, snapshot: 0, version: null };
  show("song-view");
  $("#song-view").classList.add("desk");
  $("#version-detail").innerHTML = `<div class="card desk-head"><h2>Pedal</h2><p class="hint">Reading the pedal's presets...</p></div>`;
  syncUrl();
  try { if (!desk.slots) await deskLoadSlots(); } catch (err) {
    $("#version-detail").innerHTML = `<div class="card desk-head"><h2>Pedal</h2>
      <p>Couldn't read the pedal: ${esc(err.message)}</p>
      <button class="primary" data-desk-retry>Try again</button></div>`;
    return;
  }
  const saved = Number(deskRecall(DESK_SLOT_KEY));
  const slot = Number.isInteger(saved) && saved >= 0 ? saved : (state.settings.default_slot ?? 0);
  await deskOpen(slot, { confirmLeave: false });
}

async function deskOpen(slot, { confirmLeave = true } = {}) {
  if (desk.busy) return;
  if (confirmLeave && editCount() && !confirm("Leave this preset without saving your changes? The pedal goes back to how it was.")) return;
  if (typeof closeupClose === "function") closeupClose();
  if (editing?.live) await endLive(false);
  const label = slotLabel(slot);
  desk.busy = `Opening ${label}...`;
  deskRenderHead();
  try {
    const res = await api("/api/desk/open", { method: "POST", body: { slot } });
    desk.busy = "";
    deskShow(res);
    logEvent(`pedal pane: opened ${label}${res.live ? " live" : ""}${res.live_error ? ": " + res.live_error : ""}`);
    if (res.live_error) toast(`${label} is open, but not live: ${res.live_error}`, true);
  } catch (err) {
    desk.busy = "";
    if (current.version?.pedal) renderVersion(); else deskRenderHead();
    toast(err.message, true);
  }
}

// -------------------------------------------------------------- rendering

function deskPresetList() {
  if (!desk.slots) {
    return `<p class="hint">The preset list comes from the pedal. <button class="small" data-desk-names>Load names</button></p>`;
  }
  const q = desk.filter.trim().toLowerCase();
  const here = deskSlot();
  const items = desk.slots.filter((s) => !q || `${s.label} ${s.name}`.toLowerCase().includes(q));
  return `<div class="desk-grid">${items.map((s) => `<button class="desk-slot ${s.slot === here ? "on" : ""}" data-desk-open="${s.slot}" ${desk.busy ? "disabled" : ""}>
      <small>${esc(s.label)}</small><span>${esc(s.name || "(empty)")}</span></button>`).join("") || '<p class="hint">No preset matches.</p>'}</div>`;
}

function deskHeadHtml() {
  const v = current.version;
  const slot = deskSlot();
  const name = v ? v.preset_name || "" : "";
  const live = editing?.live;
  const status = desk.busy ? `<span class="hint">${esc(desk.busy)}</span>`
    : live ? `<span class="desk-live">● Live on ${esc(live.label)}</span> <span class="hint">every change plays on the pedal</span>`
      : v ? `<span class="hint">Not live${desk.liveError ? `: ${esc(desk.liveError)}` : ""}</span> <button class="small" data-desk-live>Go live</button>` : "";
  const count = desk.slots ? desk.slots.length : 0;
  return `
    <div class="desk-now">
      <button class="desk-step" data-desk-step="-1" aria-label="Previous preset" ${v && !desk.busy ? "" : "disabled"}>◀</button>
      <div class="grow">
        <small class="hint">On the pedal</small>
        <h2>${v ? `${esc(slotLabel(slot))} · ${esc(name)}` : "Pick a preset"}</h2>
        <div class="desk-status">${status}</div>
      </div>
      <button class="desk-step" data-desk-step="1" aria-label="Next preset" ${v && !desk.busy ? "" : "disabled"}>▶</button>
    </div>
    <details class="desk-presets" ${v ? "" : "open"}>
      <summary>Presets on the pedal${count ? ` (${count})` : ""}</summary>
      <div class="row desk-tools">
        <input type="search" id="desk-filter" placeholder="Find a preset" value="${esc(desk.filter)}" autocomplete="off">
        <button class="small ghost" data-desk-names title="Read the names from the pedal again">↻ Names</button>
      </div>
      ${deskPresetList()}
    </details>`;
}

function deskRenderHead() {
  const head = $("#desk-head");
  if (!head) return;
  const open = head.querySelector("details.desk-presets")?.open;
  head.innerHTML = deskHeadHtml();
  if (open != null) head.querySelector("details.desk-presets").open = open;
}

// Quick picks for every block, one tap from the pane (no close-up needed).
function deskPicksHtml() {
  const view = cuView();
  const rows = view.blocks.map((blk) => {
    const picks = picksFor(blk);
    if (!picks.length) return "";
    return `<div class="desk-pick-row" style="--cat:${CAT_COLORS[blk.category] || "#768390"}">
      <button class="desk-pick-block" data-closeup="${blk.index}" title="Open this pedal big">${esc(blk.model_key || blk.model)}</button>
      <div class="cu-chips">${picks.map((q) => `<button class="cu-chip ${q.kind === "model" ? "own" : ""}" data-desk-pick="${q.id}" data-block="${blk.index}" title="${esc(formatChanges(q.changes))}">${esc(q.label)}</button>`).join("")}</div>
    </div>`;
  }).join("");
  return `<div class="chain-head"><h3>Quick picks</h3><span class="hint">one tap, plays on the pedal</span></div>
    ${rows || '<p class="hint">No quick picks match these blocks. Add some in My tone → Quick picks, or with “+ Save as pick” in a pedal close-up.</p>'}`;
}

function deskSuggestionHtml(s, mi, si) {
  const changes = s.changes.map((c) => `<li>${esc(c.label)}</li>`).join("");
  return `<div class="desk-sugg ${s.applied ? "applied" : ""}">
    <div class="grow"><b>${esc(s.label)}</b>${s.why ? ` <span class="hint">${esc(s.why)}</span>` : ""}<ul>${changes}</ul></div>
    <button class="${s.applied ? "ghost" : "primary"} small" data-desk-apply="${mi}:${si}">${s.applied ? "Applied ✓" : "Apply"}</button>
  </div>`;
}

function deskChatHtml() {
  const chat = deskChat();
  const msgs = chat.map((m, mi) => m.role === "user"
    ? `<div class="desk-msg me">${esc(m.text)}</div>`
    : `<div class="desk-msg ai">${esc(m.text).replace(/\n/g, "<br>")}${(m.suggestions || []).map((s, si) => deskSuggestionHtml(s, mi, si)).join("")}</div>`).join("");
  const saved = Number(deskRecall(DESK_GUITAR_KEY));
  const guitars = state.guitars.map((g) => `<option value="${g.id}" ${g.id === saved ? "selected" : ""}>${esc(g.name)}</option>`).join("");
  return `<div class="chain-head"><h3>Ask Claude</h3>
      <div class="actions">${state.guitars.length ? `<select id="desk-guitar" aria-label="Guitar" title="The guitar you're playing">${guitars}</select>` : ""}
      ${chat.length ? '<button class="small ghost" data-desk-newchat>New chat</button>' : ""}</div></div>
    <div class="desk-msgs" id="desk-msgs">${msgs || '<p class="hint">Ask about the preset on the pedal: “too much fizz on the high strings”, “how do I get more chug for the chorus?”, “why is it quieter than 1A?”. Claude answers and suggests changes you can tap to hear.</p>'}
      ${desk.asking ? '<div class="desk-msg ai thinking">Thinking...</div>' : ""}</div>
    <form id="desk-ask" class="desk-ask">
      <textarea name="q" rows="2" placeholder="Ask about this preset..." ${desk.asking ? "disabled" : ""}>${esc(desk.draft)}</textarea>
      <button class="primary" type="submit" ${desk.asking ? "disabled" : ""}>Ask</button>
    </form>`;
}

function renderDesk() {
  const v = current.version;
  deskEnsureEditing();
  $("#version-detail").innerHTML = `
    <div class="card desk-head" id="desk-head">${deskHeadHtml()}</div>
    <div class="card" id="chain-card">${renderChainCard(v)}</div>
    <div class="card" id="desk-picks">${deskPicksHtml()}</div>
    <div class="card" id="desk-chat">${deskChatHtml()}</div>
    <details class="card fold">
      <summary><h3>Keep it</h3><span class="hint">as a song in your library</span></summary>
      <p class="hint">The pane works on the pedal only. To keep this preset (with any changes) as a song, with versions, notes and the song plan:</p>
      <button data-desk-song>Save as a song…</button>
      ${v.notes ? `<details><summary class="hint">Reading notes</summary><div class="notes">${esc(v.notes)}</div></details>` : ""}
    </details>`;
  updateSaveBar();
}

function deskRenderChat() {
  const el = $("#desk-chat");
  if (!el) return;
  el.innerHTML = deskChatHtml();
  const msgs = $("#desk-msgs");
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}

// The bar at the bottom while the pane is open (app.js's updateSaveBar).
function deskSaveBar(n, live) {
  return `
    <span class="sb-count">${n ? `<b>${n}</b> change${n === 1 ? "" : "s"}` : "No changes yet"}</span>
    <label class="toggle live-toggle ${live ? "on" : ""}" title="Each change plays on the pedal straight away. Save writes it to the slot; Discard puts the slot back."><input type="checkbox" id="live-toggle" ${live ? "checked" : ""}> ${live ? `Live on ${esc(live.label)}` : "Live"}</label>
    <span class="sb-actions">
      ${state?.account && !state.account.guest ? `<button class="ghost sb-report" data-report type="button" title="Something not working? Send Claude what just happened">Report</button>` : ""}
      <button id="edit-discard" class="ghost" ${n ? "" : "disabled"}>Discard</button>
      <button id="edit-save" class="primary" ${n ? "" : "disabled"} title="Write these changes to ${esc(slotLabel(deskSlot() ?? 0))} on the pedal">Save to ${esc(slotLabel(deskSlot() ?? 0))}</button>
    </span>`;
}

// Keep the pane's head (live status) and picks in step with the chain card.
const _deskRerender = rerenderChainCard;
rerenderChainCard = function () {
  _deskRerender();
  if (current.version?.pedal) {
    deskRenderHead();
    const picks = $("#desk-picks");
    if (picks) picks.innerHTML = deskPicksHtml();
  }
};

// -------------------------------------------------------------- saving

async function deskFlushLive() {
  clearTimeout(liveSync.timer);
  liveSync.timer = null;
  while (liveSync.busy) await new Promise((r) => setTimeout(r, 30));
  if (liveSync.dirty) await liveFlush();
}

async function deskSave() {
  const edits = [...editing.committed, ...pendingEdits()];
  const label = slotLabel(deskSlot());
  if (editing.live) await deskFlushLive();
  desk.busy = `Saving to ${label}...`;
  deskRenderHead();
  try {
    let res = await api("/api/desk/save", { method: "POST", body: { edits } });
    if (res.job_id) {
      desk.busy = `Writing the whole preset to ${label} (block changes)...`;
      deskRenderHead();
      res = await pollJob(res.job_id);
    }
    desk.busy = "";
    if (typeof closeupClose === "function") closeupClose();
    deskShow(res);
    const n = res.changes.length;
    toast(`Saved ${n} change${n === 1 ? "" : "s"} to ${label} on the pedal`);
    if (res.live_error) toast(`Saved to ${label}, but live edit didn't come back on: ${res.live_error}`, true);
  } catch (err) {
    desk.busy = "";
    deskRenderHead();
    toast(err.message, true);
  }
}

async function deskDiscard() {
  if (editCount() && !confirm("Discard your changes? The pedal goes back to how the preset was.")) return;
  if (typeof closeupClose === "function") closeupClose();
  const wasLive = !!editing?.live;
  if (wasLive) await endLive(false);
  editing = startEditing();
  editing.implicit = true;
  renderVersion();
  if (wasLive) await deskGoLive();
}

async function deskGoLive() {
  desk.busy = "Going live...";
  deskRenderHead();
  try {
    const res = await api("/api/desk/live", { method: "POST", body: { edits: [...editing.committed, ...pendingEdits()] } });
    desk.busy = "";
    desk.liveError = "";
    deskEnsureEditing();
    editing.live = { label: res.live.label, structural: res.structural, waiting: (res.waiting || []).join("; ") };
    logEvent(`pedal pane: live on ${res.live.label}`);
  } catch (err) {
    desk.busy = "";
    desk.liveError = err.message;
    toast(err.message, true);
  }
  rerenderChainCard();
}

async function deskSaveSong() {
  if (!state.guitars.length) { toast("Add a guitar first (menu → Guitars)", true); return; }
  const title = prompt("Song title", current.version.preset_name || "");
  if (title == null) return;
  const saved = Number(deskRecall(DESK_GUITAR_KEY));
  const guitar = state.guitars.find((g) => g.id === saved) || state.guitars[0];
  const rig = state.rigs.find((r) => r.kind === "full") || state.rigs[0];
  const res = await api("/api/desk/song", { method: "POST", body: {
    edits: [...editing.committed, ...pendingEdits()], title, guitar_id: guitar.id, rig_id: rig.id } });
  await loadState();
  toast(`Saved as a song (${guitar.name}, ${rig.name}). The pedal pane is still open at #/pedal.`);
  renderSongs();
  return res;
}

// -------------------------------------------------------------- AI chat

async function deskAsk(text) {
  const chat = deskChat();
  chat.push({ role: "user", text });
  desk.draft = "";
  desk.asking = true;
  deskRenderChat();
  const slot = deskSlot();
  try {
    const guitar = Number($("#desk-guitar")?.value) || null;
    const messages = chat.map((m) => ({ role: m.role, text: m.text }));
    const { job_id } = await api("/api/desk/chat", { method: "POST", body: {
      messages, edits: [...editing.committed, ...pendingEdits()], snapshot: current.snapshot, guitar_id: guitar } });
    const res = await pollJob(job_id);
    const view = cuView();
    for (const s of res.suggestions) for (const c of s.changes) c.model = view.blocks[c.block]?.model;
    (desk.chats[slot] ||= []).push({ role: "assistant", text: res.reply || "(no answer)", suggestions: res.suggestions });
    logEvent(`pedal chat: ${res.suggestions.length} suggestion(s)${res.dropped ? `, ${res.dropped} change(s) dropped` : ""}`);
  } catch (err) {
    (desk.chats[slot] ||= []).push({ role: "assistant", text: `Couldn't ask Claude: ${err.message}` });
  } finally {
    desk.asking = false;
    if (deskSlot() === slot) deskRenderChat();
  }
}

// Apply a suggestion: the same edits a slider or quick pick makes, so they play live.
function deskApply(s) {
  deskEnsureEditing();
  const view = cuView();
  const moved = [];
  for (const c of s.changes) {
    const blk = view.blocks[c.block];
    if (!blk || (c.model && blk.model !== c.model)) continue; // the chain changed since
    if ("enabled" in c) {
      const snapshot = blk.stomp ? null : (view.snapshots.length ? current.snapshot : null);
      const key = editKey({ block: blk.index, snapshot });
      const snap = view.snapshots[current.snapshot] || { enabled: {} };
      const was = blk.stomp ? blk.enabled : snap.enabled[blk.key] !== false;
      if (c.enabled === was) delete editing.edits[key];
      else editing.edits[key] = { block: blk.index, snapshot, enabled: c.enabled };
      moved.push(`${blk.model_key || blk.model} ${c.enabled ? "on" : "off"}`);
      continue;
    }
    const p = blk.params.find((x) => x.param === c.param);
    if (!p) continue;
    const now = cuParam(blk, p).shown;
    const next = p.edit.kind === "number" ? clampRound(p, Number(c.value)) : c.value;
    if (String(next) === String(now)) continue;
    cuSet(blk, p, next);
    moved.push(`${p.name} ${now} → ${next}`);
  }
  s.applied = true;
  liveSync();
  rerenderChainCard();
  deskRenderChat();
  toast(moved.length ? `${s.label}: ${moved.join(", ")}` : `${s.label}: already there`);
}

// -------------------------------------------------------------- events

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  try {
    if (t.dataset.deskGo != null) {
      closeTopMenu();
      if (current.version?.pedal) return;
      if (editCount() && !confirm("Leave without saving your edits?")) return;
      history.pushState(null, "", "#/pedal");
      await route();
      lastUrl = location.hash;
    } else if (t.dataset.deskOpen) {
      const slot = Number(t.dataset.deskOpen);
      if (slot !== deskSlot()) await deskOpen(slot);
    } else if (t.dataset.deskStep) {
      const n = desk.slots?.length || 128;
      await deskOpen((deskSlot() + Number(t.dataset.deskStep) + n) % n);
    } else if (t.dataset.deskNames != null) {
      if (editing?.live) {
        if (editCount() && !confirm("Reading the names lets go of the pedal, which drops your unsaved changes. Go on?")) return;
        await endLive(false);
        editing.live = null;
      }
      desk.busy = "Reading the preset names...";
      deskRenderHead();
      try { await deskLoadSlots(); } finally { desk.busy = ""; }
      if (current.version?.pedal) { renderVersion(); await deskGoLive(); } else deskRenderHead();
    } else if (t.dataset.deskRetry != null) {
      await openDesk();
    } else if (t.dataset.deskLive != null) {
      await deskGoLive();
    } else if (t.dataset.deskPick) {
      deskEnsureEditing();
      const pick = state.quick_picks.find((q) => q.id === Number(t.dataset.deskPick));
      const blk = cuView().blocks[Number(t.dataset.block)];
      if (pick && blk) applyPick(pick, blk);
    } else if (t.dataset.deskApply) {
      const [mi, si] = t.dataset.deskApply.split(":").map(Number);
      const s = deskChat()[mi]?.suggestions?.[si];
      if (s) deskApply(s);
    } else if (t.dataset.deskNewchat != null) {
      desk.chats[deskSlot()] = [];
      deskRenderChat();
    } else if (t.dataset.deskSong != null) {
      await deskSaveSong();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

document.addEventListener("input", (e) => {
  if (e.target.id === "desk-filter") {
    desk.filter = e.target.value;
    const list = $("#desk-head .desk-presets");
    const grid = list?.querySelector(".desk-grid, p.hint");
    if (grid) grid.outerHTML = deskPresetList();
  } else if (e.target.closest?.("#desk-ask")) {
    desk.draft = e.target.value;
  }
});

document.addEventListener("change", (e) => {
  if (e.target.id === "desk-guitar") deskRemember(DESK_GUITAR_KEY, e.target.value);
});

document.addEventListener("submit", (e) => {
  if (e.target.id !== "desk-ask") return;
  e.preventDefault();
  const text = e.target.elements.q.value.trim();
  if (text && !desk.asking) deskAsk(text);
});

document.addEventListener("keydown", (e) => {
  // Enter asks; Shift+Enter is a new line.
  if (e.key === "Enter" && !e.shiftKey && e.target.closest?.("#desk-ask textarea")) {
    e.preventDefault();
    e.target.form.requestSubmit();
  }
});
