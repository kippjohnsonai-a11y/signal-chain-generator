"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CAT_COLORS = {
  Amp: "#e0a526", Cab: "#b07d1a", Distortion: "#e5534b", Dynamics: "#6cb6ff", Modulation: "#a371f7",
  Delay: "#3fb68b", Reverb: "#39c5cf", Wah: "#f778ba", "Pitch/Synth": "#db6d28", EQ: "#8b949e",
  Filter: "#c297ff", "Volume/Pan": "#768390", Routing: "#768390",
};

let state = null;
let current = { songId: null, versionId: null, snapshot: 0, version: null };
// Library browsing: the tags picked in the sidebar (a song shows when one of
// its versions has all of them), folders folded shut (remembered per
// browser), and versions picked for a bulk delete or tag.
const tagFilter = new Set();
const lower = (s) => String(s).toLowerCase();
let selecting = null;
// Hand edits in progress. Value edits since the last block change sit in
// `edits` (keyed, so re-editing a value replaces it); a block change (swap,
// add, remove, footswitch, expression) moves them into the ordered
// `committed` list with it, and `view` becomes the server's preview of the
// result, since block positions may have moved.
let editing = null;
const startEditing = () => ({ edits: {}, committed: [], view: null, scope: "all", live: null });
const editCount = () => (editing ? editing.committed.length + Object.keys(editing.edits).length : 0);
const pendingEdits = () => Object.values(editing.edits).map(({ shown, ...e }) => e);
let modelCache = null;
const TOGGLES = ["wah", "expression", "looper", "capo", "full_rig", "stage_boost"];
const TOGGLE_NAMES = { wah: "Wah", expression: "Expression", looper: "Looper", capo: "Poly Capo", full_rig: "Full-rig captures only", stage_boost: "Stage boost" };

// What this browser showed and sent lately, for Send diagnostics: a problem
// that never raised an error ("the pedal isn't changing") still leaves a trail.
const clientLog = [];
function logEvent(text) {
  const t = new Date();
  const stamp = [t.getHours(), t.getMinutes(), t.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  clientLog.push(`${stamp} ${text}`);
  if (clientLog.length > 60) clientLog.shift();
}
// The last error shown anywhere, so the report can start from it.
let lastError = null;

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (err) {
    logEvent(`${opts.method || "GET"} ${path} failed: ${err.message}`);
    throw new Error(`Couldn't reach the app (${err.message}). Check the connection and try again.`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    if (!path.startsWith("/api/diagnostics")) logEvent(`${opts.method || "GET"} ${path} -> ${res.status}: ${message}`);
    throw new Error(message);
  }
  return res.json();
}

function toast(message, error = false) {
  const el = $("#toast");
  logEvent(`${error ? "error shown" : "shown"}: ${message}`);
  if (error) lastError = { message, at: Date.now() };
  el.textContent = message;
  el.className = "toast" + (error ? " error" : "");
  if (error && state?.account && !state.account.guest) {
    // One tap hands Claude the failure, instead of a terminal on the Mac mini.
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost toast-action";
    btn.textContent = "Send diagnostics";
    btn.addEventListener("click", (e) => { e.stopPropagation(); el.hidden = true; openDiagnostics(message); });
    el.append(btn);
  }
  el.hidden = false;
  clearTimeout(toast.timer);
  // An error stays up until tapped: on a phone, a few seconds is easy to miss.
  if (!error) toast.timer = setTimeout(() => (el.hidden = true), 4000);
}
$("#toast").addEventListener("click", () => { $("#toast").hidden = true; });


function show(view) {
  for (const id of VIEWS) $("#" + id).hidden = id !== view;
  document.body.classList.toggle("on-stage", view === "stage-view");
  if (!routing && view !== "new-view") document.body.dataset.screen = "detail";
  updateSaveBar();
  if (view !== "song-view") syncUrl();
  lastUrl = location.hash;
  syncWakeLock();
  syncTab();
}

const VIEWS = ["new-view", "song-view", "job-view", "sets-view", "more-view", "stage-view"];

// ---------------------------------------------------------------- phone tabs
// On a phone: Songs, Sets, Pedal and More along the bottom. A song has its own
// tabs (Play, Tweak, Notes, Versions) so it opens on the snapshots and chain.

function syncTab() {
  const r = routeOf();
  const tab = r.view === "pedal" || current.version?.pedal ? "pedal"
    : r.view === "sets" || r.view === "stage" ? "sets" : r.view === "more" ? "more" : "songs";
  document.body.dataset.tab = tab;
  for (const b of document.querySelectorAll("#tabbar [data-tab]")) {
    b.classList.toggle("on", b.dataset.tab === tab);
    if (b.dataset.tab === tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
}

function setPane(pane) {
  $("#song-view").dataset.pane = pane;
  for (const b of document.querySelectorAll("[data-pane-tab]")) {
    const on = b.dataset.paneTab === pane;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
}

async function navigate(url) {
  if (editing?.stage) { if (!(await stageLeave())) return; }
  else if (editCount() && !confirm("Leave without saving your edits?")) return;
  if (editing?.live) await endLive(false);
  editing = null;
  if (typeof closeupClose === "function") closeupClose();
  if (url !== location.hash) history.pushState(null, "", url);
  await route();
  lastUrl = location.hash;
  window.scrollTo(0, 0);
}

document.addEventListener("click", async (e) => {
  const nav = e.target.closest("[data-nav]");
  const pane = e.target.closest("[data-pane-tab]");
  try {
    if (nav) await navigate(nav.dataset.nav);
    else if (pane) {
      setPane(pane.dataset.paneTab);
      const head = $("#song-panes");
      if (head.getBoundingClientRect().top < 0) head.scrollIntoView({ block: "start" });
    } else if (e.target.closest("#more-relay")) $("#relay-btn").click();
    else if (e.target.closest("#more-remote")) toast(`${REMOTE_NOTE} Plug the pedal into the computer running the app; sends go through its USB cable.`);
    else if (e.target.closest("#more-homescreen")) toast("In Safari, tap the Share button, then Add to Home Screen. The app then opens full screen like any other app. If you paired this phone on home Wi-Fi, pair the Home Screen app once more: it keeps its own sign-in.");
    else if (e.target.closest("#new-set")) {
      if (await newFolder()) { await loadState(); }
    } else if (e.target.closest("#sets-list button")) await setsClick(e.target.closest("#sets-list button"));
  } catch (err) { toast(err.message, true); }
});

// Sets tab: each set list in play order. Edit mode reorders and removes; songs
// come in through "+ Add songs" or a song's ⋯ menu. A song can be in many sets.
let editingSet = null;
function renderSets() {
  const box = $("#sets-list");
  if (!box) return;
  if (!state.folders.length) {
    box.innerHTML = `<p class="empty">No set lists yet. Tap <b>+ New set</b>, then add songs.</p>`;
    return;
  }
  box.innerHTML = state.folders.map((f) => {
    const songs = setList(f.id);
    const edit = editingSet === f.id;
    const rows = songs.map((x, i) => edit
      ? `<li class="set-edit-row"><span class="n">${i + 1}</span><span class="grow">${esc(x.title)}<span class="sub">${esc(x.artist || "")}</span></span>
          <button class="ghost small" data-set-move="${f.id}:${i}:-1" aria-label="Move up" ${i ? "" : "disabled"}>▲</button>
          <button class="ghost small" data-set-move="${f.id}:${i}:1" aria-label="Move down" ${i < songs.length - 1 ? "" : "disabled"}>▼</button>
          <button class="ghost small danger" data-set-drop="${f.id}:${i}" aria-label="Take out of this set">✕</button></li>`
      : `<li data-song="${x.id}" data-set="${f.id}"><span class="n">${i + 1}</span><span class="grow">${esc(x.title)}<span class="sub">${esc(x.artist || "")}</span></span>
          <span class="slot-badge${x.pushed_slot == null ? " none" : ""}">${x.pushed_slot == null ? "—" : slotLabel(x.pushed_slot)}</span><span class="chev" aria-hidden="true">›</span></li>`).join("");
    return `<div class="card set-card" data-set-card="${f.id}">
      <div class="set-card-head"><h2>${esc(f.name)}</h2><span class="hint">${songs.length} song${songs.length === 1 ? "" : "s"}</span></div>
      ${songs.length ? `<ol class="set-songs">${rows}</ol>` : `<p class="hint">Empty. Tap <b>+ Add songs</b>.</p>`}
      <div class="set-actions">
        ${edit
          ? `<button class="primary small" data-set-done="${f.id}">Done</button>
             <button class="ghost small" data-set-rename="${f.id}">Rename</button>
             <button class="ghost small danger" data-set-delete="${f.id}">Delete set</button>`
          : `<button class="ghost small" data-set-add="${f.id}">+ Add songs</button>
             <button class="ghost small" data-set-edit="${f.id}">${songs.length > 1 ? "Edit order" : "Edit"}</button>
             ${songs.length ? `<button class="ghost small" data-set-push="${f.id}">Send set to pedal</button>
               <button class="primary small" data-set-stage="${f.id}">▶ Stage</button>` : ""}`}
      </div>
    </div>`;
  }).join("");
}

// A checkbox list in a dialog. Resolves with the ticked values, or null on Cancel.
function pickMany(title, hint, items, extra) {
  const d = $("#pick-dialog");
  $("#pick-title").textContent = title;
  $("#pick-hint").textContent = hint || "";
  $("#pick-hint").hidden = !hint;
  $("#pick-filter").value = "";
  $("#pick-filter").hidden = items.length < 8;
  const list = $("#pick-list");
  list.innerHTML = items.map((it) => `<li><label class="pick-row"><input type="checkbox" value="${it.value}" ${it.checked ? "checked" : ""}>
      <span class="grow">${esc(it.label)}${it.sub ? `<span class="sub">${esc(it.sub)}</span>` : ""}</span></label></li>`).join("")
    || `<li class="hint">Nothing to pick.</li>`;
  $("#pick-extra").hidden = !extra;
  if (extra) $("#pick-extra").textContent = extra.label;
  return new Promise((resolve) => {
    const done = (val) => { d.close(); cleanup(); resolve(val); };
    const save = (e) => { e.preventDefault(); done([...list.querySelectorAll("input:checked")].map((b) => Number(b.value))); };
    const cancel = () => done(null);
    const more = async () => {
      const added = await extra.run();
      if (added == null) return;
      list.insertAdjacentHTML("beforeend", `<li><label class="pick-row"><input type="checkbox" value="${added.value}" checked><span class="grow">${esc(added.label)}</span></label></li>`);
    };
    const filter = () => {
      const q = lower($("#pick-filter").value.trim());
      for (const li of list.children) li.hidden = !!q && !lower(li.textContent).includes(q);
    };
    const cleanup = () => {
      $("#pick-form").removeEventListener("submit", save);
      $("#pick-cancel").removeEventListener("click", cancel);
      d.removeEventListener("cancel", cancel);
      $("#pick-extra").removeEventListener("click", more);
      $("#pick-filter").removeEventListener("input", filter);
    };
    $("#pick-form").addEventListener("submit", save);
    $("#pick-cancel").addEventListener("click", cancel);
    d.addEventListener("cancel", cancel);
    $("#pick-extra").addEventListener("click", more);
    $("#pick-filter").addEventListener("input", filter);
    d.showModal();
  });
}

async function saveSet(setId, songIds) {
  await api(`/api/sets/${setId}/songs`, { method: "PUT", body: { song_ids: songIds } });
  state.set_songs = { ...(state.set_songs || {}), [setId]: songIds };
  renderSets();
}

async function addSongsToSet(setId) {
  const have = new Set((state.set_songs || {})[setId] || []);
  const songs = [...state.songs].sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" }));
  const picked = await pickMany("Add songs", "Ticked songs are in this set. New ones go at the end.",
    songs.map((x) => ({ value: x.id, label: x.title, sub: x.artist, checked: have.has(x.id) })));
  if (!picked) return;
  const keep = ((state.set_songs || {})[setId] || []).filter((id) => picked.includes(id));
  await saveSet(setId, [...keep, ...picked.filter((id) => !have.has(id))]);
}

async function editSongSets() {
  const songId = current.songId;
  const mine = new Set(setsOf(songId).map((f) => f.id));
  const picked = await pickMany("Set lists", "Tick each set this song is in. It goes at the end of a set you add it to.",
    state.folders.map((f) => ({ value: f.id, label: f.name, sub: `${setList(f.id).length} songs`, checked: mine.has(f.id) })),
    { label: "+ New set", run: async () => { const name = (prompt("Name of the new set, e.g. Friday gig") || "").trim(); if (!name) return null;
        const { id } = await api("/api/folders", { method: "POST", body: { name } }); return { value: id, label: name }; } });
  if (!picked) return;
  await api(`/api/songs/${songId}/sets`, { method: "PUT", body: { set_ids: picked } });
  await loadState();
  if (current.song) renderSongHead(current.song);
  toast(picked.length ? `In ${picked.length} set${picked.length === 1 ? "" : "s"}` : "Not in any set");
}

async function pushSet(setId) {
  const songs = setList(setId);
  const f = state.folders.find((x) => x.id === setId);
  const last = state.slot_count - songs.length;
  if (last < 0) return toast("This set has more songs than the pedal has slots", true);
  const def = Math.min(state.settings.default_slot ?? 0, last);
  const d = $("#set-push-dialog");
  $("#set-push-title").textContent = `Send "${f.name}" to the pedal`;
  $("#set-push-start").innerHTML = Array.from({ length: last + 1 }, (_, s) =>
    `<option value="${s}" ${s === def ? "selected" : ""}>${slotLabel(s)} to ${slotLabel(s + songs.length - 1)}</option>`).join("");
  const preview = () => {
    const s0 = Number($("#set-push-start").value);
    $("#set-push-list").innerHTML = songs.map((x, i) => `<li><span class="slot-badge">${slotLabel(s0 + i)}</span> ${esc(x.title)}</li>`).join("");
  };
  preview();
  $("#set-push-start").onchange = preview;
  const ok = await new Promise((resolve) => {
    $("#set-push-go").onclick = () => { d.close(); resolve(true); };
    $("#set-push-cancel").onclick = () => { d.close(); resolve(false); };
    d.oncancel = () => resolve(false);
    d.showModal();
  });
  if (!ok) return;
  const start_slot = Number($("#set-push-start").value);
  const res = await runJob(`Sending ${f.name}`, () => api(`/api/sets/${setId}/push`, { method: "POST", body: { start_slot } }));
  await loadState();
  if (res !== null) { await navigate("#/sets"); toast(`Sent ${songs.length} songs from ${slotLabel(start_slot)}`); }
  else await navigate("#/sets");
}

async function setsClick(t) {
  const [id, i, dir] = (t.dataset.setMove || t.dataset.setDrop || "").split(":").map(Number);
  if (t.dataset.setEdit) { editingSet = Number(t.dataset.setEdit); renderSets(); }
  else if (t.dataset.setDone) { editingSet = null; renderSets(); }
  else if (t.dataset.setAdd) await addSongsToSet(Number(t.dataset.setAdd));
  else if (t.dataset.setPush) await pushSet(Number(t.dataset.setPush));
  else if (t.dataset.setStage) {
    const id = Number(t.dataset.setStage);
    setActiveSet(id);
    await navigate(`#/stage/${state.set_songs[id][0]}`);
  }
  else if (t.dataset.setMove) {
    const ids = [...state.set_songs[id]];
    [ids[i], ids[i + dir]] = [ids[i + dir], ids[i]];
    await saveSet(id, ids);
  } else if (t.dataset.setDrop) {
    await saveSet(id, state.set_songs[id].filter((_, k) => k !== i));
  } else if (t.dataset.setRename) {
    const f = state.folders.find((x) => x.id === Number(t.dataset.setRename));
    const name = (prompt("Rename this set", f.name) || "").trim();
    if (name && name !== f.name) { await api(`/api/folders/${f.id}`, { method: "PUT", body: { name } }); await loadState(); }
  } else if (t.dataset.setDelete) {
    const f = state.folders.find((x) => x.id === Number(t.dataset.setDelete));
    if (!confirm(`Delete the set "${f.name}"? Its songs stay in your library.`)) return;
    await api(`/api/folders/${f.id}`, { method: "DELETE" });
    editingSet = null;
    if (activeSet === f.id) setActiveSet(null);
    await loadState();
  }
}

// iPhone and iPad (iPadOS reports itself as a Mac with a touch screen).
const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const STANDALONE = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
const REMOTE_NOTE = IOS
  ? "iPhones and iPads can't connect to the pedal themselves, so this one works as a remote for the computer."
  : "This browser can't connect to the pedal itself, so it works as a remote for the computer.";

function renderMore() {
  if (!$("#more-view")) return;
  $("#more-guitars").textContent = state.guitars.map((g) => g.name).join(", ") || "Add your guitars";
  $("#more-help").hidden = $("#report-btn").hidden;
  $("#more-diag").hidden = !!(state.account || {}).guest;
  const caps = (state.settings.nam_captures || []).length;
  $("#more-nam").textContent = caps ? `${caps} capture${caps === 1 ? "" : "s"}` : "Add your NAM captures";
  $("#more-version").textContent = $("#version").textContent;
  const relay = typeof PedalRelay !== "undefined" ? PedalRelay.status() : { supported: false };
  $("#more-relay").hidden = !relay.supported;
  $("#more-relay").firstChild.textContent = relay.active ? "Stop using pedal on this device" : "Use pedal on this device";
  $("#more-remote").hidden = relay.supported;
  $("#more-homescreen").hidden = !(IOS && !STANDALONE);
}

// Keep the screen on while a song is open, so the phone doesn't dim and lock
// mid-song at rehearsal. The browser drops the lock when the tab is hidden, so
// it is asked for again when the page comes back.
let wakeLock = null;
let wakeLockBusy = false;
async function syncWakeLock() {
  if (!("wakeLock" in navigator) || wakeLockBusy) return;
  const want = document.visibilityState === "visible" && (!$("#song-view").hidden || !$("#stage-view").hidden);
  wakeLockBusy = true;
  try {
    if (want && !wakeLock) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } else if (!want && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch { wakeLock = null; } finally { wakeLockBusy = false; }
}
document.addEventListener("visibilitychange", syncWakeLock);

function formData(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const box of form.querySelectorAll("input[type=checkbox]")) data[box.name] = box.checked;
  return data;
}

function slotLabel(slot) { return `${Math.floor(slot / 4) + 1}${"ABCD"[slot % 4]}`; }

// ---------------------------------------------------------------- state

async function loadState() {
  state = await api("/api/state");
  const v = state.version || {};
  const label = $("#version");
  if (label) label.textContent = v.commit && v.commit !== "unknown" ? `v${v.commit}${v.date ? " · " + v.date : ""}` : "";
  const guitarOptions = state.guitars.map((g) => `<option value="${g.id}">${esc(g.name)} (${esc(g.pickups)})</option>`).join("");
  const rigOptions = state.rigs.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
  for (const sel of ["#guitar-select", "#fork-guitar", "#blank-guitar", "#import-guitar"]) {
    const prev = $(sel).value;
    $(sel).innerHTML = guitarOptions || `<option value="">Add a guitar first</option>`;
    if (prev) $(sel).value = prev;
  }
  for (const sel of ["#rig-select", "#fork-rig", "#blank-rig", "#import-rig"]) {
    const prev = $(sel).value;
    $(sel).innerHTML = rigOptions;
    if (prev) $(sel).value = prev;
  }
  renderPickChips();
  $("#pref-guitar").innerHTML = `<option value="">All guitars</option>` + state.guitars.map((g) => `<option value="${g.id}">${esc(g.name)} only</option>`).join("");
  $("#effort-select").innerHTML = state.settings.efforts.map((e) => `<option>${e}</option>`).join("");
  $("#exp-select").innerHTML = Object.entries(state.settings.exp_pedals)
    .map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
  $("#gate-select").innerHTML = Object.entries(state.settings.gate_modes)
    .map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
  $("#report-btn").hidden = !!(state.account || {}).guest;
  renderTagFilter();
  renderSongs();
  renderSets();
  renderMore();
  renderGuitars();
  renderPrefs();
  const noKey = !state.settings.has_api_key && !(state.account || {}).guest;
  maybeOpenSetup();
  if (noKey && !loadState.toldNoKey) {
    loadState.toldNoKey = true;
    toast("No Anthropic API key yet. Songs, sets, editing and the pedal all work; add a key in Settings to build presets with Claude.");
  }
  $("#no-key-hint").hidden = !noKey;
}

const hasTags = (tags, wanted) => [...wanted].every((w) => tags.some((t) => lower(t) === w));
const versionMatches = (v) => !tagFilter.size || hasTags((v.tags || []).map((t) => t.tag), tagFilter);

function songMatches(s, text) {
  if (tagFilter.size && !s.tag_sets.some((ts) => hasTags(ts, tagFilter))) return false;
  return !text || lower(`${s.title} ${s.artist} ${s.tags.join(" ")} ${s.guitar_id ? guitarName(s.guitar_id) : ""} ${s.pushed_slot != null ? slotLabel(s.pushed_slot) : ""}`).includes(text);
}

function renderTagFilter() {
  for (const t of [...tagFilter]) if (!state.tags.some((x) => lower(x.tag) === t)) tagFilter.delete(t);
  const chip = (key, label) => `<button class="chip${songChip === key ? " on" : ""}" data-song-chip="${key}">${label}</button>`;
  $("#tag-filter").innerHTML = chip("all", "All") + chip("starred", "★ Starred") + chip("recent", "Recent")
    + (state.guitars.length > 1 ? state.guitars.map((g) => chip("g" + g.id, esc(g.name))).join("") : "")
    + state.tags.map((t) => `<button class="chip${t.auto ? " auto" : ""}${tagFilter.has(lower(t.tag)) ? " on" : ""}"
      data-filter-tag="${esc(t.tag)}" title="Show songs with a version tagged ${esc(t.tag)}">${esc(t.tag)}<b>${t.count}</b></button>`).join("");
  $("#tag-options").innerHTML = state.tags.map((t) => `<option value="${esc(t.tag)}">`).join("");
}

// A set's songs in play order (a song can be in several sets).
function setList(setId) {
  const byId = new Map(state.songs.map((x) => [x.id, x]));
  return ((state.set_songs || {})[setId] || []).map((id) => byId.get(id)).filter(Boolean);
}
const setsOf = (songId) => state.folders.filter((f) => ((state.set_songs || {})[f.id] || []).includes(songId));

// The set a song was opened from, so Prev / Next walk that set.
let activeSet = null;
try { activeSet = Number(sessionStorage.getItem("activeSet")) || null; } catch (_) { /* no storage */ }
function setActiveSet(id) {
  activeSet = id;
  try { if (id) sessionStorage.setItem("activeSet", String(id)); else sessionStorage.removeItem("activeSet"); } catch (_) { /* no storage */ }
}

const RECENT_COUNT = 10;
let songChip = "all"; // all | starred | recent | g<guitar id>


function renderSongs() {
  const text = lower($("#song-filter").value.trim());
  let songs = state.songs.filter((x) => songMatches(x, text));
  if (songChip === "starred") songs = songs.filter((x) => x.starred);
  else if (songChip.startsWith("g")) songs = songs.filter((x) => x.guitar_id === Number(songChip.slice(1)));
  if (songChip === "recent") songs = songs.filter((x) => x.opened_at).sort((a, b) => b.opened_at - a.opened_at).slice(0, RECENT_COUNT);
  else songs = [...songs].sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" }));
  $("#song-list").innerHTML = songs.map((x) => `<li data-song="${x.id}" class="song-row ${x.id === current.songId ? "active" : ""}">
      <div class="grow"><span class="title">${x.starred ? '<span class="star" aria-label="Starred">★</span> ' : ""}${esc(x.title)}</span><div class="sub">${esc(x.artist || "")}</div></div>
      <span class="row-guitar">${x.guitar_id ? esc(guitarName(x.guitar_id)) : ""}</span>
      <span class="slot-badge${x.pushed_slot == null ? " none" : ""}" title="${x.pushed_slot == null ? "Not sent to the pedal yet" : "On the pedal in " + slotLabel(x.pushed_slot)}">${x.pushed_slot == null ? "—" : slotLabel(x.pushed_slot)}</span></li>`).join("")
    || `<li class="sub empty-row">${state.songs.length ? "No songs match"
      : 'No songs yet. Tap <b>+ New song</b> to make your first one, or see <button type="button" class="ghost small" data-help="first-song">how it works</button>.'}</li>`;
}

async function starSong(songId) {
  const song = state.songs.find((s) => s.id === songId);
  await api(`/api/songs/${songId}/star`, { method: "PUT", body: { starred: !song.starred } });
  song.starred = song.starred ? 0 : 1;
  renderSongs();
  if (current.song && current.songId === songId) renderSongHead(current.song);
}


async function newFolder() {
  const name = (prompt("Name of the new set, e.g. Friday gig") || "").trim();
  if (!name) return null;
  const { id } = await api("/api/folders", { method: "POST", body: { name } });
  return id;
}

function renderSelectBar() {
  $("#select-bar").hidden = !selecting;
  $("#select-versions").hidden = !!selecting;
  if (selecting) $("#select-count").textContent = `${selecting.size} selected`;
}

// Refresh the library after a change, staying on the open song and version
// when they still exist.
async function reloadSong(versionId = current.versionId) {
  await loadState();
  if (!current.songId) return;
  const song = await api(`/api/songs/${current.songId}`);
  const keep = song.versions.some((v) => v.id === versionId) ? versionId : null;
  current.versionId = keep;
  await openSong(current.songId, keep);
}

// ---------------------------------------------------------------- guitars & prefs

function renderGuitars() {
  $("#guitar-list").innerHTML = state.guitars.map((g) => `<li>
      <span class="grow"><b>${esc(g.name)}</b> <span class="muted">${esc(g.pickups)}${g.pickup_notes ? " · " + esc(g.pickup_notes) : ""} · ${esc(g.tuning)}</span></span>
      <button data-edit-guitar="${g.id}">Edit</button><button class="danger" data-del-guitar="${g.id}">Delete</button></li>`).join("")
    || `<li class="muted">No guitars yet. Add the ones you own below.</li>`;
}

const GEAR_KINDS = { nam: "NAM capture", model: "Model", category: "Every" };

function guitarName(id) { return (state.guitars.find((g) => g.id === id) || {}).name || "a deleted guitar"; }

function renderPrefs() {
  const prefs = state.preferences;
  const row = (p) => `<li class="${p.active ? "" : "inactive"}">
      <span class="grow">${esc(p.text)}</span>
      <button data-edit-pref="${p.id}">Edit</button>
      <button data-toggle-pref="${p.id}" data-active="${p.active ? 0 : 1}">${p.active ? "Pause" : "Resume"}</button>
      <button class="danger" data-del-pref="${p.id}">Delete</button></li>`;
  // Every-guitar tweaks first, then each guitar's own under its name.
  const groups = [["Every guitar", prefs.filter((p) => !p.guitar_id)]];
  for (const id of [...new Set(prefs.filter((p) => p.guitar_id).map((p) => p.guitar_id))]) {
    groups.push([`${guitarName(id)} only`, prefs.filter((p) => p.guitar_id === id)]);
  }
  $("#pref-list").innerHTML = prefs.length
    ? groups.filter(([, items]) => items.length).map(([title, items]) => `<li class="group">${esc(title)}</li>${items.map(row).join("")}`).join("")
    : `<li class="muted">None yet. Add your own below, or start from the Suggested tab.</li>`;
  $("#tweak-count").textContent = prefs.filter((p) => p.active).length || "";
  renderGear();
  renderStarters();
  if (typeof renderQuickPicks === "function") renderQuickPicks();
}

function renderGear() {
  const notes = state.gear_notes || [];
  const label = (n) => n.kind === "category" ? `Every ${esc(n.name)} block` : `${GEAR_KINDS[n.kind]}: ${esc(n.name)}`;
  $("#gear-list").innerHTML = notes.map((n) => `<li class="${n.active ? "" : "inactive"}">
      <span class="grow"><b>${label(n)}</b><br>${esc(n.text)}</span>
      <button data-edit-gear="${n.id}">Edit</button>
      <button data-toggle-gear="${n.id}" data-active="${n.active ? 0 : 1}">${n.active ? "Pause" : "Resume"}</button>
      <button class="danger" data-del-gear="${n.id}">Delete</button></li>`).join("")
    || `<li class="muted">No gear notes yet.</li>`;
  $("#gear-count").textContent = notes.filter((n) => n.active).length || "";
}

// The names the gear-note box suggests for each kind.
async function gearOptions(kind) {
  let names;
  if (kind === "nam") {
    names = state.settings.nam_captures.map((c) => c.name);
  } else {
    if (!modelCache) modelCache = (await api("/api/models")).models;
    names = kind === "category" ? [...new Set(modelCache.map((m) => m.category))] : modelCache.map((m) => m.name);
  }
  $("#gear-options").innerHTML = [...new Set(names)].sort((x, y) => x.localeCompare(y)).map((n) => `<option value="${esc(n)}">`).join("");
  $("#gear-name").placeholder = { nam: "e.g. Dual Rectifier", model: "e.g. Scream 808", category: "e.g. Reverb" }[kind];
}

// A suggestion counts as added once a tweak or note with the same text exists.
function starterAdded(s) {
  const pool = s.kind ? (state.gear_notes || []) : state.preferences;
  return pool.some((p) => p.text.trim() === s.text.trim());
}

function starterGuitar(s) {
  if (!s.pickups) return null;
  const re = s.pickups === "single" ? /single/i : /^HH|active humbucker/i;
  return state.guitars.find((g) => re.test(g.pickups))?.id ?? null;
}

// The gear notes that apply to this version's chain, shown on its page.
function gearNotesCard(v) {
  const notes = (state.gear_notes || []).filter((n) => n.active);
  const blocks = v.view.blocks || [];
  const capture = lower(v.view.nam?.capture?.name || "");
  const hits = notes.filter((n) => n.kind === "nam" ? capture && lower(n.name) === capture
    : blocks.some((b) => lower(n.kind === "category" ? b.category || "" : b.model) === lower(n.name)));
  if (!hits.length) return "";
  return `<div class="card"><h3>Your gear notes in this preset</h3><ul>${hits.map((n) =>
    `<li><b>${n.kind === "category" ? "Every " + esc(n.name) + " block" : esc(n.name)}:</b> ${esc(n.text)}</li>`).join("")}</ul>
    <p class="hint">Claude follows these when it builds or tweaks a preset that uses this gear. Change them in My tone.</p></div>`;
}

function renderStarters() {
  const st = state.tone_starters || { tweaks: [], gear_notes: [] };
  const left = [...st.tweaks, ...st.gear_notes].filter((s) => !starterAdded(s)).length;
  $("#starter-count").textContent = left || "";
  const card = (s, i, kind) => {
    const added = starterAdded(s);
    const g = kind === "tweak" ? starterGuitar(s) : null;
    const scope = kind === "tweak"
      ? (s.pickups ? (g ? `${esc(guitarName(g))} only` : `for ${s.pickups === "single" ? "single-coil" : "humbucker"} guitars (no match yet: adds for every guitar)`) : "every guitar")
      : (s.kind === "category" ? `every ${esc(s.name)} block` : `${GEAR_KINDS[s.kind]}: ${esc(s.name)}`);
    return `<li class="${added ? "inactive" : ""}"><span class="grow"><span class="muted">${scope}</span><br>${esc(s.text)}</span>
      ${added ? `<span class="muted">Added</span>` : `<button data-add-starter="${kind}:${i}">Add</button>`}</li>`;
  };
  $("#starter-list").innerHTML = `
    <h3>Tweaks for every song</h3><ul class="list">${st.tweaks.map((s, i) => card(s, i, "tweak")).join("")}</ul>
    <h3>Gear notes</h3><ul class="list">${st.gear_notes.map((s, i) => card(s, i, "gear")).join("")}</ul>
    ${left ? `<button id="add-all-starters" class="ghost">Add all ${left}</button>` : ""}`;
}

async function addStarter(kind, i) {
  const st = state.tone_starters;
  if (kind === "tweak") {
    const s = st.tweaks[i];
    await api("/api/preferences", { method: "POST", body: { text: s.text, guitar_id: starterGuitar(s) } });
  } else {
    const s = st.gear_notes[i];
    await api("/api/gear-notes", { method: "POST", body: { kind: s.kind, name: s.name, text: s.text } });
  }
}

function resetPrefForm() {
  const f = $("#pref-form");
  f.reset();
  f.elements.id.value = "";
  $("#pref-form-title").textContent = "Add a tweak";
  $("#pref-submit").textContent = "Add tweak";
}

function resetGearForm() {
  const f = $("#gear-form");
  f.reset();
  f.elements.id.value = "";
  $("#gear-form-title").textContent = "Add a gear note";
  $("#gear-submit").textContent = "Add note";
  gearOptions(f.elements.kind.value);
}

// ---------------------------------------------------------------- jobs

// A running job is remembered in localStorage, so a phone that dims its screen,
// backgrounds Chrome or reloads the tab mid-job picks it back up (resumeJob)
// instead of losing it. The server keeps running jobs either way.
const JOB_KEY = "stompxl.job";
const rememberJob = (job) => { try { job ? localStorage.setItem(JOB_KEY, JSON.stringify(job)) : localStorage.removeItem(JOB_KEY); } catch (_) { /* private mode */ } };
const rememberedJob = () => { try { return JSON.parse(localStorage.getItem(JOB_KEY) || "null"); } catch (_) { return null; } };

async function followJob(jobId) {
  // A dropped connection (screen off, Wi-Fi to cellular) is not the job
  // failing: keep asking for a few minutes before giving up on it.
  let offlineSince = null;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch (err) {
      if (!(err instanceof TypeError)) throw err;  // the server answered: a real error
      offlineSince ??= Date.now();
      if (Date.now() - offlineSince > 5 * 60 * 1000) throw new Error("Lost touch with the app. Check the song list: it may have finished anyway.");
      continue;
    }
    offlineSince = null;
    $("#job-log").innerHTML = job.log.map((l) => `<li>${esc(l)}</li>`).join("");
    if (job.status !== "running") return job;
  }
}

// A Claude cost in dollars, for people: "$0.12", or "under a cent".
function costText(usd) {
  return usd < 0.005 ? "under a cent" : `$${usd.toFixed(2)}`;
}

async function runJob(title, start) {
  show("job-view");
  $("#job-title").textContent = title;
  $("#job-log").innerHTML = "";
  let job;
  try {
    const { job_id } = await start();
    rememberJob({ id: job_id, title });
    job = await followJob(job_id);
  } catch (err) {
    job = { status: "error", error: err.message };
  }
  rememberJob(null);
  if (job.status === "done" && job.cost_usd != null) toast(`Done. Claude cost about ${costText(job.cost_usd)}.`);
  if (job.status === "error") {
    runJob.lastError = job;
    toast(job.error, true);
    if (current.songId) await openSong(current.songId, current.versionId); else show("new-view");
    return null;
  }
  return job.result;
}

// After a reload, show the job that was running and open what it made.
async function resumeJob() {
  const saved = rememberedJob();
  if (!saved || resumeJob.active) return false;
  resumeJob.active = true;
  try {
    show("job-view");
    $("#job-title").textContent = saved.title || "Working";
    $("#job-log").innerHTML = "";
    let job;
    try { job = await followJob(saved.id); } catch (err) { job = { status: "error", error: err.message }; }
    rememberJob(null);
    if (job.status === "error") {
      // 404: the app restarted and the job went with it.
      toast(/No such job/.test(job.error) ? "The app restarted while that was running. Please try again." : job.error, true);
      show("new-view");
      return true;
    }
    await loadState();
    const r = job.result || {};
    if (r.song_id) await openSong(r.song_id, r.version_id); else show("new-view");
    toast("Finished while you were away");
    return true;
  } finally {
    resumeJob.active = false;
  }
}

// ---------------------------------------------------------------- song view

async function openSong(songId, versionId = null) {
  if (current.songId !== songId) selecting = null;
  const fresh = current.songId !== songId;
  current.songId = songId;
  const song = await api(`/api/songs/${songId}`);
  $("#song-view").classList.remove("desk");
  if (fresh) setPane("play");
  show("song-view");
  current.song = song;
  renderSongHead(song);
  renderSelectBar();
  renderSongs();
  renderTree(song);
  if (fresh) {
    // Recent, on every device.
    api(`/api/songs/${songId}/opened`, { method: "POST" }).catch(() => {});
    const listed = state.songs.find((s) => s.id === songId);
    if (listed) listed.opened_at = Date.now() / 1000;
    window.scrollTo(0, 0);
  }
  // With tags picked in the sidebar, open a version that has them.
  const matching = song.versions.filter(versionMatches);
  const favorite = song.versions.find((v) => v.id === song.favorite_version_id);
  const target = versionId || (favorite && versionMatches(favorite) ? favorite.id : null)
    || (matching.at(-1) || {}).id || song.favorite_version_id || (song.versions.at(-1) || {}).id;
  if (target) await openVersion(target, song);
  else { $("#version-detail").innerHTML = ""; syncUrl(); }
}

// The version a guitar's chip opens: the starred one if it's for that guitar,
// else that guitar's newest.
function versionForGuitar(song, guitarId) {
  const fav = song.versions.find((v) => v.id === song.favorite_version_id);
  if (fav && fav.guitar_id === guitarId) return fav;
  return song.versions.filter((v) => v.guitar_id === guitarId).at(-1) || null;
}

function renderSongHead(song) {
  $("#song-title").textContent = song.title;
  $("#song-artist").textContent = song.artist;
  const listed = state.songs.find((s) => s.id === song.id) || {};
  const star = $("#song-star");
  star.textContent = listed.starred ? "★" : "☆";
  star.classList.toggle("on", !!listed.starred);
  star.setAttribute("aria-label", listed.starred ? "Unstar this song" : "Star this song");
  // Set list: Prev / Next through the set it was opened from (else its first set), in play order.
  const sets = setsOf(song.id);
  const folder = sets.find((f) => f.id === activeSet) || sets[0];
  const list = folder ? setList(folder.id) : [];
  const i = list.findIndex((s) => s.id === song.id);
  const prev = list[i - 1];
  const next = list[i + 1];
  $("#set-nav").innerHTML = folder && list.length > 1 ? `
    <button class="set-step" ${prev ? `data-song="${prev.id}" title="${esc(prev.title)}"` : "disabled"}>◀<span> ${prev ? esc(prev.title) : "Prev"}</span></button>
    <span class="set-pos"><b>${esc(folder.name)}</b> ${i + 1} of ${list.length}</span>
    <button class="set-step" ${next ? `data-song="${next.id}" title="${esc(next.title)}"` : "disabled"}><span>${next ? esc(next.title) : "Next"} </span>▶</button>` : "";
  // One chip per guitar: a tap opens that guitar's version, no Claude call.
  const v = current.version && current.version.song_id === song.id ? current.version : null;
  $("#guitar-chips").innerHTML = state.guitars.length > 1 ? state.guitars.map((g) => {
    const target = versionForGuitar(song, g.id);
    const on = v && v.guitar_id === g.id;
    return target
      ? `<button class="guitar-chip${on ? " on" : ""}" data-version="${target.id}" title="${esc(g.name)}: ${esc(target.label || "v" + target.id)}">${esc(g.name)}</button>`
      : `<button class="guitar-chip add" data-remake-for="${g.id}" title="Remake this version for the ${esc(g.name)} (Claude, about a minute)">+ ${esc(g.name)}</button>`;
  }).join("") : "";
}

// ---------------------------------------------------------------- addresses
// Every song and version has its own address (#/song/12/v/34), so Back,
// refresh and bookmarks work. On a phone the song list is its own screen (#/).

const PHONE = window.matchMedia("(max-width: 767px)");

function routeOf(hash = location.hash) {
  const m = hash.match(/^#\/song\/(\d+)(?:\/v\/(\d+))?/);
  if (m) return { song: Number(m[1]), version: m[2] ? Number(m[2]) : null };
  const st = hash.match(/^#\/stage\/(\d+)/);
  if (st) return { view: "stage", stage: Number(st[1]) };
  if (hash.startsWith("#/new")) return { view: "new" };
  if (hash.startsWith("#/pedal")) return { view: "pedal" };
  if (hash.startsWith("#/sets")) return { view: "sets" };
  if (hash.startsWith("#/more")) return { view: "more" };
  return { view: "songs" };
}

function urlNow() {
  if (!$("#song-view").hidden && current.version?.pedal) return "#/pedal";
  if (!$("#song-view").hidden && current.songId) return `#/song/${current.songId}${current.versionId ? "/v/" + current.versionId : ""}`;
  if (!$("#new-view").hidden && document.body.dataset.screen !== "songs") return "#/new";
  if (!$("#stage-view").hidden && stage) return `#/stage/${stage.songId}`;
  if (!$("#sets-view").hidden) return "#/sets";
  if (!$("#more-view").hidden) return "#/more";
  return "#/";
}

// Record where we are. A new song is a new history entry; another version of
// the same song replaces it, so Back goes to the song list, not through versions.
function syncUrl() {
  if (routing) return;
  const url = urlNow();
  if (url === location.hash) return;
  const sameSong = routeOf(url).song && routeOf(url).song === routeOf().song;
  if (sameSong || !location.hash) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
}

let routing = false;
async function route() {
  if (!location.hash) history.replaceState(null, "", "#/");
  const r = routeOf();
  routing = true;
  try {
    if (r.song) {
      document.body.dataset.screen = "detail";
      if (r.song !== current.songId || !current.song || !$("#stage-view").hidden) await openSong(r.song, r.version);
      else if (r.version && r.version !== current.versionId) await openVersion(r.version);
      else show("song-view");
    } else if (r.view === "new") {
      document.body.dataset.screen = "detail";
      showNew();
    } else if (r.view === "stage") {
      document.body.dataset.screen = "detail";
      await openStage(r.stage);
    } else if (r.view === "pedal") {
      document.body.dataset.screen = "detail";
      await openDesk();
    } else if (r.view === "sets" || r.view === "more") {
      document.body.dataset.screen = "detail";
      current = { songId: null, versionId: null, snapshot: 0, version: null };
      if (r.view === "sets") renderSets(); else renderMore();
      show(r.view + "-view");
    } else {
      // The song list: on a computer it sits beside the New song form.
      document.body.dataset.screen = "songs";
      if (!PHONE.matches || !current.songId) showNew(); else show("song-view");
      if (PHONE.matches) window.scrollTo(0, 0);
    }
  } catch (err) {
    toast(err.message, true);
    document.body.dataset.screen = "songs";
    showNew();
  } finally { routing = false; syncTab(); }
}

function showNew() {
  current = { songId: null, versionId: null, snapshot: 0, version: null };
  renderSongs();
  show("new-view");
}

let lastUrl = location.hash;
window.addEventListener("popstate", async () => {
  if (editing?.stage ? !(await stageLeave()) : editCount() && !confirm("Leave without saving your edits?")) {
    history.pushState(null, "", lastUrl);
    return;
  }
  if (editing?.live) await endLive(false);
  editing = null;
  if (typeof closeupClose === "function") closeupClose();
  await route();
  lastUrl = location.hash;
});

function goBackToSongs() {
  // Came from the list in this tab: really go back, so the list keeps its scroll.
  if (history.length > 1 && cameFromList) { history.back(); return; }
  history.pushState(null, "", "#/");
  route();
}
let cameFromList = false;

function renderTree(song) {
  const children = {};
  for (const v of song.versions) (children[v.parent_id || 0] ||= []).push(v);
  const guitar = (id) => (state.guitars.find((g) => g.id === id) || {}).name || "?";
  const items = [];
  const walk = (parent, depth) => {
    for (const v of children[parent] || []) {
      const when = new Date(v.created_at * 1000).toLocaleDateString();
      const picked = selecting && selecting.has(v.id);
      const cls = [v.id === current.versionId && !selecting ? "active" : "", picked ? "picked" : "", versionMatches(v) ? "" : "dim"].join(" ");
      items.push(`<li data-version="${v.id}" style="padding-left:${0.5 + depth}rem" class="${cls}">
        ${selecting ? `<input type="checkbox" class="pick" tabindex="-1" ${picked ? "checked" : ""}>` : ""}${depth ? "↳ " : ""}${v.id === song.favorite_version_id ? '<span class="star">★</span> ' : ""}${esc(v.label || "v" + v.id)}
        <div class="meta">#${v.id} · ${esc(v.kind)} · ${esc(guitar(v.guitar_id))} · ${when}${v.pushed_slot != null ? " · on " + slotLabel(v.pushed_slot) : ""}</div>
        ${(v.tags || []).length ? `<div class="chips">${v.tags.map((t) => `<span class="chip${t.auto ? " auto" : ""}">${esc(t.tag)}</span>`).join("")}</div>` : ""}</li>`);
      walk(v.id, depth + 1);
    }
  };
  walk(0, 0);
  $("#version-tree").innerHTML = items.join("");
  current.song = song;
}

async function openVersion(versionId, song = current.song) {
  const v = await api(`/api/versions/${versionId}`);
  current.versionId = versionId;
  current.version = v;
  current.snapshot = 0;
  if (typeof closeupClose === "function") closeupClose();
  if (editing?.live) await endLive(false);
  editing = null;
  renderTree(song);
  renderVersion();
  renderSongHead(song);
  syncUrl();
  lastUrl = location.hash;
}

// A card that belongs to one of the song's phone tabs (empty stays empty).
const paneWrap = (pane, html) => html ? `<div class="pane-wrap" data-pane="${pane}">${html}</div>` : "";

function renderVersion() {
  const v = current.version;
  $("#song-view").classList.toggle("desk", !!v.pedal);
  if (v.pedal) return renderDesk(); // the pedal pane: see desk.js
  const b = v.brief;
  const guitar = state.guitars.find((g) => g.id === v.guitar_id);
  const rig = state.rigs.find((r) => r.id === v.rig_id);
  const opts = Object.entries(v.options).filter(([, on]) => on).map(([k]) => k).join(", ") || "none";
  const recs = TOGGLES.filter((k) => b[k]).map((k) => `<div class="rec ${b[k]?.recommended ? "yes" : ""}">
      <b>${TOGGLE_NAMES[k]}</b>: ${b[k]?.recommended ? "recommended" : "not needed"}${v.options[k] ? " · included" : ""}<br>${esc(b[k]?.how)}</div>`).join("");
  const sections = (b.sections || []).map((s, i) => `<div class="section"><b>${i + 1}. ${esc(s.name)}</b> - ${esc(s.role)}<br>
      ${esc(s.description)}<br><span class="muted">Pickup: ${esc(s.pickup)} · Knobs: ${esc(s.guitar_knobs)}</span></div>`).join("");

  const isFav = current.song && current.song.favorite_version_id === v.id;
  const usbOnly = v.view.nam?.mode === "usb";
  $("#version-detail").innerHTML = `
    <div class="card version-head" data-pane="play">
      <div class="vh-top">
        <div class="grow">
          <h2>${isFav ? '<span class="star" title="Opens first">★</span> ' : ""}${esc(v.label || "Version " + v.id)}</h2>
          <div class="muted vh-meta">${esc(guitar?.name || "?")} · ${esc(rig?.name || "?")} · ${Math.round(v.view.tempo)} bpm · on the pedal as “${esc(v.preset_name || "?")}”${v.pushed_slot != null ? " in " + slotLabel(v.pushed_slot) : ""}</div>
        </div>
        <div class="vh-actions">
          <button class="primary" id="push-btn"${usbOnly ? ' disabled title="Uses both paths: download the .hlx and load it in HX Edit"' : ""}>Send to pedal</button>
          ${isFav ? "" : '<button id="fav-btn" title="Open this version first next time">☆ Favorite</button>'}
          <div class="menu-wrap">
            <button class="ghost more-btn" id="more-btn" aria-label="More" aria-expanded="false">⋯</button>
            <div class="menu" id="more-menu" hidden>
              <a href="/api/versions/${v.id}/hlx" class="menu-item">Download .hlx</a>
              <button id="duplicate-btn" class="menu-item" title="An exact copy under a new name, no AI">Duplicate</button>
              <button id="other-guitar-btn" class="menu-item" title="Same signal chain, with the settings re-dialled for another of your guitars"${state.guitars.length < 2 ? " disabled" : ""}>Remake for another guitar</button>
              <button id="fork-btn" class="menu-item" title="Re-dial for a different guitar, rig or toggles with Claude">Fork…</button>
              <button id="rename-btn" class="menu-item">Rename</button>
              <button id="delete-version-btn" class="menu-item danger">Delete this version</button>
              <hr>
              <button id="song-sets-btn" class="menu-item">Set lists…</button>
              <button id="new-take" class="menu-item">New take of this song</button>
              <button id="delete-song" class="menu-item danger">Delete song</button>
            </div>
          </div>
        </div>
      </div>
      <div class="tag-row">${(v.tags || []).map((t) => t.auto
        ? `<span class="chip auto" title="Set automatically from the chain">${esc(t.tag)}</span>`
        : `<span class="chip">${esc(t.tag)}<button data-untag="${esc(t.tag)}" title="Remove tag">×</button></span>`).join("")}
        <input id="add-tag" list="tag-options" placeholder="+ tag" maxlength="32" autocomplete="off"></div>
    </div>

    ${paneWrap("play", helpTip("song-play", `The buttons across the top of the chain are the song's <b>snapshots</b>, one sound per part of the song (switch them on the pedal with FS1 and FS4). Each box is a <b>block</b>: an effect, the amp or the cabinet. <b>Explain</b> says what a block does, <b>Tune</b> opens it with sliders, and <b>Send to pedal</b> puts the preset on your pedal.`, "song"))}
    <div class="card" id="chain-card" data-pane="play">${renderChainCard(v)}</div>

    <div class="card" data-pane="notes">
      <div class="chain-head"><h3>My notes</h3><span class="hint" id="notes-status"></span></div>
      <textarea id="user-notes" class="user-notes" placeholder="Your own notes: what worked live, which pickup, knob positions, things to try next...">${esc(v.user_notes || "")}</textarea>
      <div class="row" style="justify-content:flex-end"><button id="save-notes" style="flex:0 0 auto">Save notes</button></div>
    </div>

    <div class="card" data-pane="tweak">
      <h3>Tweak it with Claude</h3>
      <p class="hint">Say what you'd like to change in your own words, as you would to a bandmate. Claude makes a new version and lists every value it changed; this version is kept, so you can always go back.</p>
      <form id="refine-form">
        <textarea name="feedback" required placeholder="e.g. too muddy on the Les Paul · the solo needs to jump out more · less reverb in the verse"></textarea>
        <div class="row" style="align-items:center">
          <label class="toggle"><input type="checkbox" name="remember"> Remember this for every song (saved in My tone)</label>
          <label class="toggle"><input type="checkbox" name="remember_for_guitar" checked> ...only for this guitar</label>
          <button class="primary" type="submit" style="flex:0 0 auto">Apply tweak</button>
        </div>
      </form>
    </div>
    ${paneWrap("play", namCard(v))}
    ${paneWrap("notes", gearNotesCard(v))}

    ${b.origin ? `<div class="card" data-pane="notes">
      <h3>Song plan</h3>
      <p class="hint">${b.origin === "import" ? "Imported" : "Started blank"}: no song plan yet. Claude can research the song and write one (original gear, sections, pickup and knob tips). Your preset isn't changed.</p>
      <button id="plan-btn">Have Claude write the plan</button>
    </div>` : `<details class="card fold" data-pane="notes">
      <summary><h3>Song plan</h3><span class="hint">sections, pickups, playing tips</span></summary>
      <p><b>Original gear:</b> ${esc(b.reference_gear)}</p>
      <p><b>Tone:</b> ${esc(b.tone_summary)}</p>
      <p><b>Tailored for your ${esc(guitar?.name || "guitar")}:</b> ${esc(b.guitar_adaptation)}</p>
      <div class="sections">${sections}</div>
      <h3 style="margin-top:1rem">Wah, expression, looper and capo</h3>
      <div class="recs">${recs}</div>
      ${(b.playing_tips || []).length ? `<h3 style="margin-top:1rem">Playing tips</h3><ul>${b.playing_tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
    </details>`}

    <details class="card fold" data-pane="tweak">
      <summary><h3>Why these choices</h3><span class="hint">extras: ${esc(opts)} · ${esc(b.tuning || "")} · DSP ~${v.view.dsp_estimate}%${v.chain.gate ? " · gate " + (v.chain.gate.enabled ? "on " + v.chain.gate.threshold_db + " dB" : "off") : ""}</span></summary>
      ${v.feedback ? `<p><b>Feedback:</b> ${esc(v.feedback)}</p>` : ""}
      <div class="notes">${esc(v.notes)}</div>
      ${v.warnings.length ? `<details><summary class="hint">Build notes (${v.warnings.length})</summary><ul class="warn-list">${v.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>` : ""}
    </details>`;
  updateSaveBar();
}

// ---------------------------------------------------------------- save bar
// While editing, the Live switch, Discard and Save sit in a bar at the bottom
// of the screen, so they're in reach from the pedal close-up.

function updateSaveBar() {
  const bar = $("#save-bar");
  const on = !!editing && !$("#song-view").hidden;
  bar.hidden = !on;
  document.body.classList.toggle("has-savebar", on);
  if (!on) return;
  const n = editCount();
  const live = editing.live;
  const html = current.version?.pedal ? deskSaveBar(n, live) : `
    <span class="sb-count">${n ? `<b>${n}</b> change${n === 1 ? "" : "s"}` : "No changes yet"}</span>
    <label class="toggle live-toggle ${live ? "on" : ""}" title="Each value you change plays on the pedal straight away. Save writes it to the slot; Discard puts the slot back."><input type="checkbox" id="live-toggle" ${live ? "checked" : ""}> ${live ? `Live on ${esc(live.label)}` : "Live"}</label>
    <span class="sb-actions">
      <button id="edit-discard" class="ghost">${n ? "Discard" : "Done"}</button>
      ${live
        ? `<button id="edit-save" class="primary" ${n ? "" : "disabled"} title="Saves a new version and keeps it in ${esc(live.label)}">Save</button>`
        : `<button id="edit-save" ${n ? "" : "disabled"} title="Save as a new version">Save</button>
           <button id="edit-save-push" class="primary" ${n ? "" : "disabled"}>Save &amp; send</button>`}
    </span>`;
  if (bar._html !== html) { bar.innerHTML = html; bar._html = html; }
}

async function onLiveToggle(box) {
  box.disabled = true;
  try {
    if (box.checked) {
      await startLive();
    } else {
      const ok = await endLive(false);
      rerenderChainCard();
      // On a failure the error stays up instead: the slot may not be back.
      if (ok) toast("Live off: the pedal's slot is back how it was. Your edits are still here to save.");
    }
  } catch (err) {
    box.checked = false;
    toast(err.message, true);
  } finally { box.disabled = false; updateSaveBar(); }
}

$("#save-bar").addEventListener("change", (e) => {
  if (e.target.id === "live-toggle" && editing) onLiveToggle(e.target);
});

function renderChainCard(v) {
  const view = editing?.view || v.view;
  const bar = editing
    ? `<label class="toggle" title="Values that already change per snapshot always edit just this snapshot"><input type="checkbox" id="edit-scope" ${editing.scope === "snapshot" ? "checked" : ""}> This snapshot only</label>
       ${editing.implicit ? `<button id="edit-start" class="ghost small" title="Add, move or swap blocks; tempo and snapshot names">Edit chain</button>` : ""}`
    : `<button id="edit-start" class="ghost" title="Add, move or swap blocks, type values, tempo and snapshot names">Edit chain</button>`;
  const structural = editing && !editing.implicit;
  return `
    <div class="chain-head"><h3>Signal chain</h3><div class="actions">${bar}</div></div>
    ${structural ? presetRow(view) : ""}
    <div class="snap-tabs" role="tablist" style="--snaps:${view.snapshots.length || 1}">${view.snapshots.map((s, i) => `<button role="tab" aria-selected="${i === current.snapshot}" data-snap="${i}" class="${i === current.snapshot ? "active" : ""}" title="Snapshot ${i + 1}"><small>${i + 1}</small>${esc(editing?.edits[`snapname|${i}`]?.shown ?? s.name)}</button>`).join("")}</div>
    <div class="chain ${structural ? "editing" : ""}">${renderIo((view.io || [])[0])}${renderChain(view)}${structural && view.blocks.length < 8 ? '<button class="add-block" id="add-block">+ Add block</button>' : ""}${renderIo((view.io || [])[1])}</div>
    <p class="hint">${structural
      ? "Type any value, or click ON/OFF to switch a block in this snapshot. Drag a block by its ⠿ handle (or use ◀ ▶) to move it. \"Change block\" swaps the model, sets its footswitch and expression pedal, or removes it. Amber values change per snapshot, so they edit this snapshot only; tick \"This snapshot only\" to do the same for any other value. Saving makes a new version; the one you started from is kept."
      : "Tap Tune for sliders and quick picks. A dark lamp means off in this snapshot; amber values change per snapshot. FS2/FS3 are the top stomps, FS5/FS6 the bottom; FS1/FS4 change snapshots."}</p>`;
}

function rerenderChainCard() {
  $("#chain-card").innerHTML = renderChainCard(current.version);
  updateSaveBar();
}

// Typing values into the chain, block menus and moves: Edit chain, not just
// a close-up open (whose edits still show in the chain).
const editingChain = () => !!editing && !editing.implicit;

const editKey = (e) => `${e.block}|${e.parameter ?? "@enabled"}|${e.snapshot ?? "all"}`;

function editInput(blk, p, value, original, snapshot) {
  const where = blk.io ? `data-io="${blk.io}"` : `data-block="${blk.index}"`;
  const data = `${where} data-param="${esc(p.param)}" data-kind="${p.edit.kind}" data-snapshot="${snapshot ?? ""}" data-original="${esc(original)}"`;
  if (p.edit.kind === "choice" || p.edit.kind === "toggle") {
    const options = p.edit.kind === "toggle" ? ["On", "Off"] : p.edit.options;
    return `<select ${data}>${options.map((o) => `<option ${o === value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  }
  const range = p.edit.min != null ? `min="${p.edit.min}" max="${p.edit.max}" step="${p.edit.step}"` : `step="any"`;
  return `<input type="number" ${range} value="${value === "-" ? "" : esc(value)}" ${data}>`;
}

// Tempo and snapshot names, editable in edit mode.
function presetRow(view) {
  const tempo = editing.edits.tempo?.shown ?? String(Math.round(view.tempo || 120));
  const names = view.snapshots.map((s, i) => {
    const pending = editing.edits[`snapname|${i}`];
    return `<label>Snapshot ${i + 1} <input data-global="snapname" data-index="${i}" maxlength="10" value="${esc(pending?.shown ?? s.name)}" data-original="${esc(s.name)}" class="${pending ? "edited" : ""}"></label>`;
  }).join("");
  return `<details class="preset-fold"${editing.edits.tempo || Object.keys(editing.edits).some((k) => k.startsWith("snapname|")) ? " open" : ""}><summary>Tempo and snapshot names</summary><div class="preset-row">
    <label>Tempo (BPM) <input type="number" min="30" max="300" data-global="tempo" value="${esc(tempo)}" data-original="${Math.round(view.tempo || 120)}" class="${editing.edits.tempo ? "edited" : ""}"></label>
    ${names}
  </div></details>`;
}

function recordGlobalEdit(input) {
  const kind = input.dataset.global;
  const key = kind === "tempo" ? "tempo" : `snapname|${input.dataset.index}`;
  const shown = input.value.trim();
  if (shown === input.dataset.original || shown === "") {
    delete editing.edits[key];
  } else {
    editing.edits[key] = kind === "tempo" ? { tempo: Number(shown), shown }
      : { snapshot_name: Number(input.dataset.index), name: shown, shown };
  }
  input.classList.toggle("edited", key in editing.edits);
  if (kind === "snapname") {
    const tab = document.querySelector(`[data-snap="${input.dataset.index}"]`);
    if (tab) tab.innerHTML = `<small>${Number(input.dataset.index) + 1}</small>${esc(shown || input.dataset.original)}`;
  }
  updateSaveBar();
}

const ioKey = (io, param) => `io:${io}|${param}|all`;

// The Input (gate) and main Output, editable like a block but always present.
function renderIo(blk) {
  if (!blk) return "";
  const rows = blk.params.map((p) => {
    const pending = editing?.edits[ioKey(blk.io, p.param)];
    const value = pending ? pending.shown : p.value;
    const cell = editingChain() ? editInput(blk, p, String(value), p.value, null) : esc(value);
    return `<tr class="${pending ? "edited" : ""}"><td>${esc(p.name)}</td><td>${cell}</td></tr>`;
  }).join("");
  return `<div class="block io" style="--cat:#768390" title="${esc(blk.why)}">
    <div class="name">${esc(blk.title)}</div>
    <div class="based">${blk.io === "input" ? "Noise gate" : "Preset level"}</div>
    <div class="block-tools"><button data-explain-io="${blk.io}" title="What these settings do, with recommendations">Explain</button></div>
    <table>${rows}</table>
  </div>`;
}

// NAM on the pedal: the Model knob is the IR slot holding the capture, so it
// gets a list of the captures (Settings > NAM captures) by name.
const NAM_SLOT_PARAM = "MinFreq";
function namCaptures() {
  return (state.settings?.nam_captures || []).filter((c) => c.slot != null)
    .sort((a, b) => a.slot - b.slot);
}
function captureName(slot) {
  return namCaptures().find((c) => c.slot === Math.round(Number(slot)))?.name || "";
}
function withCapture(blk, p, value, cell) {
  if (!blk.nam || p.param !== NAM_SLOT_PARAM) return cell;
  const name = captureName(value);
  return name ? `${cell} <span class="capture-name">${esc(name)}</span>` : cell;
}

// The NAM firmware's block is a Xenomorph Fuzz on the pedal. The app reads one
// as NAM; this button says which it really is.
function namSwitch(view, blk) {
  if (blk.model_key !== "Xenomorph Fuzz" || view.nam?.mode === "usb") return "";
  return blk.nam
    ? `<button data-nam-block="${blk.index}" data-nam="0" title="This block is a real Xenomorph Fuzz, not NAM">Is a fuzz</button>`
    : `<button data-nam-block="${blk.index}" data-nam="1" title="This block is the NAM A2 Lite block (the NAM firmware shows NAM as Xenomorph Fuzz)">Is NAM</button>`;
}

function renderChain(view) {
  const snap = view.snapshots[current.snapshot] || { enabled: {}, values: {} };
  const snapIndex = view.snapshots.length ? current.snapshot : null;
  return view.blocks.map((blk) => {
    // The boost and looper follow their footswitch, not the snapshots.
    const toggleSnapshot = blk.stomp ? null : snapIndex;
    const pendingOn = editing?.edits[editKey({ block: blk.index, snapshot: toggleSnapshot })];
    const on = pendingOn ? pendingOn.enabled : blk.stomp ? blk.enabled : snap.enabled[blk.key] !== false;
    const snapValues = Object.fromEntries((snap.values[blk.key] || []).map((p) => [p.name, p.value]));
    let restEdited = false;
    const rows = (list, inRest) => list.map((p) => {
      const perSnapshot = p.name in snapValues || (editing?.scope === "snapshot" && p.edit.per_snapshot);
      const snapshot = perSnapshot ? snapIndex : null;
      const pending = editing?.edits[editKey({ block: blk.index, parameter: p.param, snapshot })];
      const value = pending ? pending.shown : snapValues[p.name] ?? p.value;
      if (pending && inRest) restEdited = true;
      const cls = [p.name in snapValues ? "snap" : p.changed ? "changed" : "", pending ? "edited" : ""].join(" ");
      const label = `${esc(p.name)}${p.controlled_by && p.controlled_by !== "snapshot" ? " (" + esc(p.controlled_by) + ")" : ""}`;
      const original = String(snapValues[p.name] ?? p.value);
      const cell = withCapture(blk, p, value, editingChain() ? editInput(blk, p, String(value), original, snapshot) : esc(value));
      return `<tr class="${cls}"><td>${label}</td><td>${cell}</td></tr>`;
    }).join("");
    const main = blk.params.filter((p) => p.changed || p.name in snapValues || (p.controlled_by && p.controlled_by !== "snapshot"));
    const rest = blk.params.filter((p) => !main.includes(p));
    const restRows = rows(rest, true);
    const tags = [
      blk.footswitch ? `<span class="tag">FS ${blk.footswitch}</span>` : "",
      blk.expression ? `<span class="tag" title="Expression pedal ${blk.expression} moves its ${esc(blk.expression_label || "Position")}">EXP ${blk.expression}${blk.expression_label && blk.expression_label !== "Position" ? ": " + esc(blk.expression_label) : ""}</span>` : "",
    ].join("");
    const state = editingChain()
      ? `<button class="tag toggle-block ${pendingOn ? "edited" : ""}" data-toggle-block="${blk.index}" data-snapshot="${toggleSnapshot ?? ""}" data-on="${on ? 1 : 0}" title="${blk.stomp ? "Its footswitch turns it on and off; this sets how the preset loads" : "Switch it in this snapshot"}" style="color:${on ? "var(--on)" : "var(--muted)"}">${on ? "ON" : "OFF"}</button>`
      : `<span class="tag" style="color:${on ? "var(--on)" : "var(--muted)"}">${on ? "ON" : "OFF"}</span>`;
    const last = view.blocks.length - 1;
    const moves = editingChain() ? `<button data-move-block="${blk.index}" data-to="${blk.index - 1}" ${blk.index === 0 ? "disabled" : ""} title="Move one place earlier" aria-label="Move earlier">◀</button><button data-move-block="${blk.index}" data-to="${blk.index + 1}" ${blk.index === last ? "disabled" : ""} title="Move one place later" aria-label="Move later">▶</button>` : "";
    return `<div class="block ${on ? "" : "off"}" data-index="${blk.index}" style="--cat:${CAT_COLORS[blk.category] || "#768390"}" title="${esc(blk.why)}">
      <div class="name"><span class="drag-handle" data-drag="${blk.index}" title="Drag to move this block" aria-hidden="true">⠿</span>${esc(blk.model)}</div>
      <div class="based">${esc(blk.based_on || blk.category || "")}</div>
      <div class="tags">${tags}${state}</div>
      <div class="block-tools"><button data-closeup="${blk.index}" title="Open this pedal big, with sliders and quick picks">Tune</button><button data-explain="${blk.index}" title="What this block and its settings do, with recommendations">Explain</button>${namSwitch(view, blk)}${editingChain() ? `<button data-block-menu="${blk.index}">Change block…</button>${moves}` : ""}</div>
      <table>${rows(main, false)}</table>
      ${rest.length ? `<details ${restEdited ? "open" : ""}><summary>${rest.length} more</summary><table>${restRows}</table></details>` : ""}
    </div>`;
  }).join("");
}

function recordEdit(input) {
  if (input.dataset.io) return recordIoEdit(input);
  const block = Number(input.dataset.block);
  const snapshot = input.dataset.snapshot === "" ? null : Number(input.dataset.snapshot);
  const parameter = input.dataset.param;
  const key = editKey({ block, parameter, snapshot });
  const shown = input.value;
  if (shown === input.dataset.original || shown === "") {
    delete editing.edits[key];
  } else {
    const value = input.dataset.kind === "number" ? Number(shown)
      : input.dataset.kind === "toggle" ? shown === "On" : shown;
    editing.edits[key] = { block, parameter, snapshot, value, shown };
  }
  input.closest("tr").classList.toggle("edited", key in editing.edits);
  updateSaveBar();
}

function recordIoEdit(input) {
  const io = input.dataset.io;
  const parameter = input.dataset.param;
  const key = ioKey(io, parameter);
  const shown = input.value;
  if (shown === input.dataset.original || shown === "") {
    delete editing.edits[key];
  } else {
    const value = input.dataset.kind === "toggle" ? shown === "On" : Number(shown);
    editing.edits[key] = { io, parameter, value, shown };
  }
  input.closest("tr").classList.toggle("edited", key in editing.edits);
  updateSaveBar();
}

async function saveEdits(thenPush) {
  const edits = [...editing.committed, ...pendingEdits()];
  // Values only (no blocks added, moved or swapped) is a quick tweak: the
  // server names it "Rehearsal <date>" and stars it.
  const rehearsal = editing.committed.length === 0;
  const result = await api(`/api/versions/${current.versionId}/edit`, { method: "POST", body: { edits, rehearsal } });
  const snapshot = current.snapshot;
  const live = editing.live;
  closeupClose();
  let onPedal = "";
  let pedalError = "";
  if (live) {
    // Let the last values reach the pedal before saving them there.
    clearTimeout(liveSync.timer);
    liveSync.timer = null;
    while (liveSync.busy) await new Promise((r) => setTimeout(r, 30));
    if (liveSync.dirty) await liveFlush();
    try {
      // The pedal is already playing these values: save them to its slot.
      // A block change isn't on it yet, so that version still needs a push.
      const done = await api("/api/live/stop", { method: "POST", body: { keep: true, version_id: live.structural ? null : result.version_id } });
      if (!live.structural) onPedal = done.label;
    } catch (err) { pedalError = err.message; }
  }
  editing = null;
  await loadState();
  await openSong(result.song_id, result.version_id);
  if (current.version.view.snapshots[snapshot]) { current.snapshot = snapshot; renderVersion(); }
  const saved = `Saved as a new version (${result.changes.length} change${result.changes.length === 1 ? "" : "s"})`;
  if (pedalError) toast(`${saved}, but not on the pedal: ${pedalError}`, true);
  else toast(onPedal ? `${saved}, and saved to ${onPedal} on the pedal` : saved);
  // Block changes (or other snapshots) made in Live aren't on the pedal yet.
  if ((thenPush || live?.structural) && !onPedal && !pedalError) $("#push-btn").click();
}

// ---------------------------------------------------------------- live edit
// While Live is ticked, every value edit is sent to the pedal as you make it.
// The server works out which values changed, so this always sends the whole
// edit list.

async function startLive() {
  const versionId = current.versionId;
  const edits = [...editing.committed, ...pendingEdits()];
  const res = await api(`/api/versions/${versionId}/live`, { method: "POST", body: { edits } });
  if (!editing || current.versionId !== versionId) { await endLive(false); return; }
  editing.live = { label: res.label, structural: res.structural, waiting: (res.waiting || []).join("; ") };
  logEvent(`live on ${res.label}: sent ${res.sent}${liveNotes(res)}`);
  rerenderChainCard();
  toast(res.structural
    ? `Live on ${res.label}: value changes play now. Not on the pedal until you Save & send: ${res.waiting.join("; ")}`
    : `Live on ${res.label}: changes play on the pedal now`);
}

// Returns whether the pedal confirmed it; an error is shown, and stays up.
async function endLive(keep) {
  clearTimeout(liveSync.timer);
  liveSync.timer = null;
  liveSync.dirty = false;
  while (liveSync.busy) await new Promise((r) => setTimeout(r, 30));
  if (editing) editing.live = null;
  try {
    await api("/api/live/stop", { method: "POST", body: { keep } });
    logEvent(`live ${keep ? "saved" : "discarded"}`);
    return true;
  } catch (err) { toast(err.message, true); return false; }
}

const liveNotes = (res) => [
  res.unsupported?.length ? `, refused: ${res.unsupported.join(", ")}` : "",
  res.waiting?.length ? `, waiting: ${res.waiting.join("; ")}` : "",
].join("");

// Sends at most one update at a time: while one is in flight, later edits
// wait and go together, so a slider drag plays smoothly without queueing up.
function liveSync() {
  updateSaveBar();
  if (!editing?.live) return;
  liveSync.dirty = true;
  if (!liveSync.busy && !liveSync.timer) liveSync.timer = setTimeout(liveFlush, 60);
}

async function liveFlush() {
  liveSync.timer = null;
  if (!editing?.live || !liveSync.dirty) return;
  liveSync.dirty = false;
  liveSync.busy = true;
  const edits = [...editing.committed, ...pendingEdits()];
  try {
    const res = await api(`/api/versions/${current.versionId}/live/update`, { method: "POST", body: { edits } });
    logEvent(`live update: sent ${res.sent}${liveNotes(res)}`);
    const waiting = (res.waiting || []).join("; ");
    if (editing?.live) {
      if (res.structural && editing.live.waiting !== waiting) toast(`Not on the pedal until you Save & send: ${waiting}. Other changes still play live.`);
      editing.live.structural = res.structural;
      editing.live.waiting = waiting;
    }
    if (res.unsupported.length) toast(`The pedal didn't take: ${res.unsupported.join(", ")}`, true);
  } catch (err) {
    if (editing) editing.live = null;
    liveSync.dirty = false;
    rerenderChainCard();
    toast(err.message, true);
  } finally {
    liveSync.busy = false;
    if (liveSync.dirty) liveSync.timer = setTimeout(liveFlush, 0);
  }
}

window.addEventListener("pagehide", () => {
  // Closing the tab mid-session: put the slot back rather than hold the pedal.
  if (editing?.live) navigator.sendBeacon("/api/live/stop", new Blob([JSON.stringify({ keep: false })], { type: "application/json" }));
});

// ---------------------------------------------------------------- events

function closeMenu(menuSel, btnSel) {
  const menu = $(menuSel);
  if (menu && !menu.hidden) { menu.hidden = true; $(btnSel)?.setAttribute("aria-expanded", "false"); }
}
function closeTopMenu() {
  $(".topbar").classList.remove("menu-open");
  $("#menu-btn").setAttribute("aria-expanded", "false");
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMenu("#more-menu", "#more-btn"); closeTopMenu(); }
});

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, li");
  if (!t) {
    if (!e.target.closest("#more-menu")) closeMenu("#more-menu", "#more-btn");
    if (!e.target.closest("#top-menu")) closeTopMenu();
    return;
  }
  if (((t.dataset.version && !selecting) || t.dataset.song || t.id === "new-song") && editCount()
      && !confirm("Leave without saving your edits?")) return;
  // Any tap closes an open menu, except one on its own toggle.
  if (t.id !== "more-btn") closeMenu("#more-menu", "#more-btn");
  if (t.id !== "menu-btn" && !t.closest("#top-menu")) closeTopMenu();
  try {
    if (t.id === "more-btn") {
      const menu = $("#more-menu");
      menu.hidden = !menu.hidden;
      t.setAttribute("aria-expanded", String(!menu.hidden));
    } else if (t.id === "menu-btn") {
      const open = !$(".topbar").classList.contains("menu-open");
      $(".topbar").classList.toggle("menu-open", open);
      t.setAttribute("aria-expanded", String(open));
    } else if (t.dataset.back != null) {
      goBackToSongs();
    } else if (t.dataset.starSong) {
      await starSong(Number(t.dataset.starSong));
    } else if (t.id === "song-star") {
      await starSong(current.songId);
    } else if (t.dataset.songChip) {
      songChip = songChip === t.dataset.songChip && t.dataset.songChip !== "all" ? "all" : t.dataset.songChip;
      renderTagFilter();
      renderSongs();
    } else if (t.id === "stage-btn") {
      await navigate(`#/stage/${current.songId}`);
    } else if (t.id === "song-sets-btn") {
      await editSongSets();
    } else if (t.dataset.remakeFor) {
      $("#other-guitar-btn").dataset.guitar = t.dataset.remakeFor;
      $("#other-guitar-btn").click();
    } else if (t.dataset.open) {
      closeTopMenu();
      const dialog = $("#" + t.dataset.open);
      if (t.dataset.open === "settings-dialog") {
        fillSettings();
        // From More: one section as its own page. From the menu: all of it.
        dialog.dataset.section = t.dataset.section || "";
        $("#settings-title").textContent = t.dataset.title || "Settings";
      }
      dialog.showModal();
      if (t.dataset.tone) document.querySelector(`[data-tone-tab="${t.dataset.tone}"]`)?.click();
    } else if (t.dataset.closeDialog != null) {
      t.closest("dialog").close();
    } else if (t.dataset.filterTag != null) {
      const tag = lower(t.dataset.filterTag);
      if (tagFilter.has(tag)) tagFilter.delete(tag); else tagFilter.add(tag);
      renderTagFilter();
      renderSongs();
      if (current.song) renderTree(current.song);
    } else if (t.dataset.version && selecting) {
      const id = Number(t.dataset.version);
      if (selecting.has(id)) selecting.delete(id); else selecting.add(id);
      renderSelectBar();
      renderTree(current.song);
    } else if (t.id === "select-versions") {
      selecting = new Set();
      renderSelectBar();
      renderTree(current.song);
    } else if (t.id === "select-done") {
      selecting = null;
      renderSelectBar();
      renderTree(current.song);
    } else if (t.id === "select-all") {
      const shown = current.song.versions.filter(versionMatches);
      const all = shown.every((v) => selecting.has(v.id));
      for (const v of shown) if (all) selecting.delete(v.id); else selecting.add(v.id);
      renderSelectBar();
      renderTree(current.song);
    } else if (t.id === "tag-selected" || t.id === "untag-selected") {
      if (!selecting.size) return toast("Pick some versions first");
      const adding = t.id === "tag-selected";
      const tag = (prompt(`${adding ? "Add a tag to" : "Remove a tag from"} ${selecting.size} version${selecting.size === 1 ? "" : "s"}`) || "").trim();
      if (!tag) return;
      await api("/api/versions/bulk-tags", { method: "POST", body: { ids: [...selecting], [adding ? "add" : "remove"]: [tag] } });
      await reloadSong();
      toast(adding ? `Tagged ${tag}` : `Removed ${tag}`);
    } else if (t.id === "delete-selected") {
      const n = selecting.size;
      if (!n) return toast("Pick some versions first");
      if (!confirm(`Delete ${n} version${n === 1 ? "" : "s"}? Versions forked from them are kept.`)) return;
      const { deleted } = await api("/api/versions/bulk-delete", { method: "POST", body: { ids: [...selecting] } });
      const gone = new Set(selecting);
      selecting = new Set();
      await reloadSong(gone.has(current.versionId) ? null : current.versionId);
      toast(`Deleted ${deleted} version${deleted === 1 ? "" : "s"}`);
    } else if (t.dataset.untag != null) {
      const tags = current.version.tags.filter((x) => !x.auto && x.tag !== t.dataset.untag).map((x) => x.tag);
      await saveTags(tags);
    } else if (t.dataset.song) {
      cameFromList = !!t.closest("#song-list, #sets-list");
      if (t.closest("#sets-list")) setActiveSet(Number(t.dataset.set) || null);
      else if (t.closest("#song-list")) setActiveSet(null);
      current.versionId = null;
      document.body.dataset.screen = "detail";
      await openSong(Number(t.dataset.song));
    } else if (t.dataset.version) {
      await openVersion(Number(t.dataset.version));
    } else if (t.dataset.newTab) {
      for (const b of document.querySelectorAll("[data-new-tab]")) b.classList.toggle("active", b === t);
      for (const f of document.querySelectorAll("#new-view form[data-tab]")) f.hidden = f.dataset.tab !== t.dataset.newTab;
      if (t.dataset.newTab === "import" && !$("#import-slot").options.length) {
        $("#import-slot").innerHTML = slotOptions({}, state.settings.default_slot ?? 0);
      }
    } else if (t.id === "import-load-names") {
      t.disabled = true;
      try {
        const { slots } = await api("/api/pedal/slots");
        const keep = Number($("#import-slot").value);
        $("#import-slot").innerHTML = slotOptions(Object.fromEntries(slots.map((x) => [x.slot, x.name])), keep);
      } finally { t.disabled = false; }
    } else if (t.id === "plan-btn") {
      const result = await runJob("Writing the song plan", () => api(`/api/versions/${current.versionId}/plan`, { method: "POST" }));
      if (result) await openSong(result.song_id, result.version_id);
    } else if (t.id === "edit-start") {
      if (editing) editing.implicit = false; else editing = startEditing();
      rerenderChainCard();
    } else if (t.id === "edit-discard" && current.version?.pedal) {
      await deskDiscard();
    } else if ((t.id === "edit-save" || t.id === "edit-save-push") && current.version?.pedal) {
      t.disabled = true;
      try { await deskSave(); } finally { t.disabled = false; }
    } else if (t.id === "edit-discard") {
      if (editCount() && !confirm(editing.live ? "Discard your edits? The pedal's slot goes back to how it was." : "Discard your edits?")) return;
      if (editing.live) await endLive(false);
      editing = null;
      closeupClose();
      rerenderChainCard();
      updateSaveBar();
    } else if (t.id === "edit-save" || t.id === "edit-save-push") {
      t.disabled = true;
      try { await saveEdits(t.id === "edit-save-push"); } finally { t.disabled = false; }
    } else if (t.dataset.pick) {
      pickModel(t.dataset.pick);
    } else if (t.dataset.explain) {
      await explainBlock(Number(t.dataset.explain));
    } else if (t.dataset.explainIo) {
      await explainBlock(null, false, t.dataset.explainIo);
    } else if (t.id === "explain-refresh") {
      const d = $("#explain-dialog");
      await explainBlock(d.dataset.io ? null : Number(d.dataset.block), true, d.dataset.io || null);
    } else if (t.id === "explain-close") {
      $("#explain-dialog").close();
    } else if (t.dataset.namBlock) {
      if (!editing) editing = startEditing();
      await blockChange([{ block: Number(t.dataset.namBlock), nam: t.dataset.nam === "1" }]);
    } else if (t.dataset.moveBlock) {
      await moveBlock(Number(t.dataset.moveBlock), Number(t.dataset.to));
    } else if (t.dataset.blockMenu) {
      await openBlockDialog(Number(t.dataset.blockMenu));
    } else if (t.id === "add-block") {
      await openBlockDialog(null);
    } else if (t.id === "block-cancel") {
      $("#block-dialog").close();
    } else if (t.id === "block-remove") {
      const index = Number($("#block-form").dataset.block);
      if (!confirm("Remove this block from the chain?")) return;
      $("#block-dialog").close();
      await blockChange([{ remove: index }]);
    } else if (t.dataset.toggleBlock) {
      const block = Number(t.dataset.toggleBlock);
      const snapshot = t.dataset.snapshot === "" ? null : Number(t.dataset.snapshot);
      const key = editKey({ block, snapshot });
      if (key in editing.edits) delete editing.edits[key];
      else editing.edits[key] = { block, snapshot, enabled: t.dataset.on !== "1" };
      rerenderChainCard();
      liveSync();
    } else if (t.dataset.snap) {
      current.snapshot = Number(t.dataset.snap);
      renderVersion();
    } else if (t.dataset.editGuitar) {
      const g = state.guitars.find((x) => x.id === Number(t.dataset.editGuitar));
      const f = $("#guitar-form");
      for (const k of ["id", "name", "pickups", "pickup_notes", "tuning", "notes"]) f.elements[k].value = g[k] ?? "";
      $("#guitar-form-title").textContent = "Edit guitar";
    } else if (t.dataset.delGuitar) {
      if (!confirm("Delete this guitar? Its versions keep their settings.")) return;
      await api(`/api/guitars/${t.dataset.delGuitar}`, { method: "DELETE" });
      await loadState();
    } else if (t.dataset.toneTab) {
      for (const b of document.querySelectorAll("[data-tone-tab]")) b.classList.toggle("active", b === t);
      for (const p of document.querySelectorAll("[data-tone-pane]")) p.hidden = p.dataset.tonePane !== t.dataset.toneTab;
      if (t.dataset.toneTab === "gear") gearOptions($("#gear-kind").value);
      if (t.dataset.toneTab === "picks") {
        renderQuickPicks();
        gearOptions($("#qp-kind").value === "model" ? "model" : "category").then(() => { $("#qp-options").innerHTML = $("#gear-options").innerHTML; });
      }
    } else if (t.dataset.editPref) {
      const p = state.preferences.find((x) => x.id === Number(t.dataset.editPref));
      const f = $("#pref-form");
      f.elements.id.value = p.id;
      f.elements.text.value = p.text;
      f.elements.guitar_id.value = p.guitar_id ?? "";
      $("#pref-form-title").textContent = "Edit tweak";
      $("#pref-submit").textContent = "Save tweak";
      f.elements.text.focus();
    } else if (t.id === "pref-reset") {
      resetPrefForm();
    } else if (t.dataset.editGear) {
      const n = state.gear_notes.find((x) => x.id === Number(t.dataset.editGear));
      const f = $("#gear-form");
      for (const k of ["id", "kind", "name", "text"]) f.elements[k].value = n[k];
      $("#gear-form-title").textContent = "Edit gear note";
      $("#gear-submit").textContent = "Save note";
      await gearOptions(n.kind);
      f.elements.text.focus();
    } else if (t.id === "gear-reset") {
      resetGearForm();
    } else if (t.dataset.toggleGear) {
      await api(`/api/gear-notes/${t.dataset.toggleGear}`, { method: "PUT", body: { active: t.dataset.active === "1" } });
      await loadState();
    } else if (t.dataset.delGear) {
      if (!confirm("Delete this gear note?")) return;
      await api(`/api/gear-notes/${t.dataset.delGear}`, { method: "DELETE" });
      await loadState();
    } else if (t.dataset.addStarter) {
      const [kind, i] = t.dataset.addStarter.split(":");
      t.disabled = true;
      await addStarter(kind, Number(i));
      await loadState();
    } else if (t.id === "add-all-starters") {
      t.disabled = true;
      const st = state.tone_starters;
      for (const [i, s] of st.tweaks.entries()) if (!starterAdded(s)) await addStarter("tweak", i);
      for (const [i, s] of st.gear_notes.entries()) if (!starterAdded(s)) await addStarter("gear", i);
      await loadState();
    } else if (t.dataset.togglePref) {
      await api(`/api/preferences/${t.dataset.togglePref}`, { method: "PUT", body: { active: t.dataset.active === "1" } });
      await loadState();
    } else if (t.dataset.delPref) {
      if (!confirm("Delete this tweak?")) return;
      await api(`/api/preferences/${t.dataset.delPref}`, { method: "DELETE" });
      await loadState();
    } else if (t.id === "new-song") {
      if (editing?.live) await endLive(false);
      editing = null;
      $("#generate-form").elements.song.value = "";
      history.pushState(null, "", "#/new");
      await route();
    } else if (t.id === "new-take") {
      const f = $("#generate-form");
      f.elements.song.value = current.song.query;
      f.dataset.songId = current.songId;
      show("new-view");
    } else if (t.id === "delete-song") {
      if (!confirm("Delete this song and every version of it?")) return;
      await api(`/api/songs/${current.songId}`, { method: "DELETE" });
      current = { songId: null, versionId: null, snapshot: 0 };
      await loadState();
      history.replaceState(null, "", "#/");
      await route();
    } else if (t.id === "guitar-reset") {
      $("#guitar-form").reset();
      $("#guitar-form").elements.id.value = "";
      $("#guitar-form-title").textContent = "Add a guitar";
    } else if (t.id === "fork-btn" || t.id === "other-guitar-btn") {
      const v = current.version;
      const f = $("#fork-form");
      const other = t.id === "other-guitar-btn";
      const wanted = Number(t.dataset.guitar || 0);
      delete t.dataset.guitar;
      const next = other ? state.guitars.find((g) => g.id === wanted) || state.guitars.find((g) => g.id !== v.guitar_id) : null;
      f.elements.guitar_id.value = next ? next.id : v.guitar_id;
      f.elements.rig_id.value = v.rig_id;
      for (const k of TOGGLES) f.elements[k].checked = !!v.options[k];
      f.elements.keep_chain.checked = other;
      f.elements.label.value = "";
      f.elements.instructions.value = "";
      $("#fork-title").textContent = other ? "Remake for another guitar" : "Fork this version";
      $("#fork-submit").textContent = other ? "Remake" : "Fork";
      $("#fork-intro").textContent = other
        ? "Same blocks in the same order, same NAM capture and footswitches. Claude re-dials gain, EQ, levels and the snapshots for the guitar you pick."
        : "Start a new branch from this version with a different guitar, rig, or toggles. Claude re-dials the preset from where it stands.";
      updateRoom(f);
      syncKeepChain(f);
      $("#fork-dialog").showModal();
    } else if (t.id === "push-btn") {
      fillSlots({});
      $("#push-dialog").showModal();
    } else if (t.id === "load-slot-names") {
      t.disabled = true;
      try {
        const { slots } = await api("/api/pedal/slots");
        fillSlots(Object.fromEntries(slots.map((s) => [s.slot, s.name])));
      } finally { t.disabled = false; }
    } else if (t.id === "fav-btn") {
      await api(`/api/songs/${current.songId}/favorite/${current.versionId}`, { method: "PUT" });
      await openSong(current.songId, current.versionId);
      toast("Marked as the favorite version");
    } else if (t.id === "rename-btn" || t.id === "duplicate-btn") {
      openNameDialog(t.id === "duplicate-btn" ? "duplicate" : "rename");
    } else if (t.id === "name-cancel") {
      $("#name-dialog").close();
    } else if (t.id === "save-notes") {
      await saveUserNotes();
    } else if (t.id === "delete-version-btn") {
      if (!confirm("Delete this version? Versions forked from it are kept.")) return;
      await api(`/api/versions/${current.versionId}`, { method: "DELETE" });
      await reloadSong(null);
    }
  } catch (err) {
    toast(err.message, true);
  }
});

function slotOptions(names, selected) {
  return Array.from({ length: state.slot_count }, (_, s) =>
    `<option value="${s}" ${s === selected ? "selected" : ""}>${slotLabel(s)} (#${s})${names[s] ? " - " + esc(names[s]) : ""}</option>`).join("");
}

function fillSlots(names) {
  const def = state.settings.default_slot ?? 31;
  $("#slot-select").innerHTML = Array.from({ length: state.slot_count }, (_, s) =>
    `<option value="${s}" ${s === def ? "selected" : ""}>${slotLabel(s)} (#${s})${names[s] ? " - " + esc(names[s]) : ""}</option>`).join("");
}

// The model list comes live from Anthropic's Models API, so new models show up on their own.
async function fillModels(saved) {
  const select = $("#model-select");
  const old = new Set(["claude-opus-5"]);  // the old prefilled default means "newest Opus"
  const chosen = !saved || old.has(saved) ? "latest-opus" : saved;
  const render = (models, latest, note) => {
    const opts = [`<option value="latest-opus">Newest Opus (now ${esc(latest)}) - recommended</option>`]
      .concat(models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} (${esc(m.id)})</option>`));
    if (chosen !== "latest-opus" && !models.some((m) => m.id === chosen)) opts.push(`<option value="${esc(chosen)}">${esc(chosen)}</option>`);
    select.innerHTML = opts.join("");
    select.value = chosen;
    $("#model-status").textContent = note;
  };
  render([], "...", "Loading the model list from Anthropic...");
  try {
    const res = await api("/api/claude-models");
    render(res.models, res.latest_opus, res.error
      ? "Couldn't load the model list (add your API key and reopen Settings). Newest Opus still works."
      : "\"Newest Opus\" moves to each new Opus by itself.");
  } catch (err) { render([], "claude-opus-5-5", err.message); }
}

function fillSettings() {
  const f = $("#settings-form");
  f.elements.anthropic_api_key.value = "";
  fillModels(state.settings.claude_model);
  f.elements.claude_effort.value = state.settings.claude_effort;
  f.elements.exp_pedal.value = state.settings.exp_pedal;
  f.elements.gate_mode.value = state.settings.gate_mode;
  f.elements.nam_captures.value = (state.settings.nam_captures || []).map(captureLine).join("\n");
  namRowEdit(null);
  renderNamRows();
  $("#nam-usb-status").textContent = state.settings.nam_usb_routing
    ? "NAM on computer: USB routing learnt."
    : "NAM on computer: run `stompxl learn-nam-usb` once so presets come ready to play.";
  const account = state.account || {};
  $("#key-field").hidden = !!account.guest;
  $("#key-status").textContent = account.guest
    ? `Signed in as ${account.email}. Claude jobs left today: ${account.claude_left}.`
    : (state.settings.has_api_key ? "An API key is set." : "No API key yet: add one to generate presets.")
      + ` Claude this month: about ${costText(state.settings.claude_cost_month || 0)} (list prices; your Anthropic console has the real bill).`;
  $("#diag-settings").hidden = !!account.guest;
  f.elements.diagnostics_token.value = "";
  $("#diag-token-status").textContent = state.settings.diagnostics_token
    ? "A diagnostics token is set: Report a problem files it with one tap." : "No diagnostics token: Report a problem copies the report and opens GitHub to paste it. A token (docs/diagnostics.md) files it with one tap.";
  $("#data-dir").textContent = account.guest
    ? "Your songs are your own: nobody else using the app sees them."
    : "Library stored in: " + state.settings.data_dir;
}

$("#song-filter").addEventListener("input", renderSongs);

async function saveTags(tags) {
  await api(`/api/versions/${current.versionId}/tags`, { method: "PUT", body: { tags } });
  await reloadSong();
}

document.addEventListener("keydown", async (e) => {
  if (e.target.id !== "add-tag" || e.key !== "Enter") return;
  e.preventDefault();
  const tag = e.target.value.trim();
  if (!tag) return;
  try {
    await saveTags([...current.version.tags.filter((x) => !x.auto).map((x) => x.tag), tag]);
    $("#add-tag")?.focus();
  } catch (err) { toast(err.message, true); }
});


// ---------------------------------------------------------------- names and notes

function openNameDialog(mode) {
  const v = current.version;
  const f = $("#name-form");
  f.dataset.mode = mode;
  const label = v.label || `v${v.id}`;
  f.elements.label.value = mode === "duplicate" ? `Copy of ${label}` : label;
  f.elements.preset_name.value = v.preset_name || "";
  $("#name-dialog-title").textContent = mode === "duplicate" ? "Duplicate this version" : "Rename this version";
  $("#name-dialog-hint").textContent = mode === "duplicate"
    ? "An exact copy: same blocks, values, snapshots and notes. Nothing is re-dialled."
    : "";
  $("#name-submit").textContent = mode === "duplicate" ? "Make the copy" : "Save";
  $("#name-dialog").showModal();
  f.elements.label.select();
}

$("#name-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { label: f.elements.label.value.trim(), preset_name: f.elements.preset_name.value };
  try {
    if (f.dataset.mode === "duplicate") {
      const result = await api(`/api/versions/${current.versionId}/duplicate`, { method: "POST", body });
      $("#name-dialog").close();
      await loadState();
      await openSong(result.song_id, result.version_id);
      toast("Copy made");
    } else {
      await api(`/api/versions/${current.versionId}/label`, { method: "PUT", body });
      $("#name-dialog").close();
      await openSong(current.songId, current.versionId);
      toast("Renamed");
    }
  } catch (err) {
    toast(err.message, true);
  }
});

async function saveUserNotes() {
  const text = $("#user-notes").value;
  await api(`/api/versions/${current.versionId}/notes`, { method: "PUT", body: { text } });
  current.version.user_notes = text;
  $("#notes-status").textContent = "Saved";
}

// Notes save themselves when you click away, so nothing typed is lost.
document.addEventListener("focusout", async (e) => {
  if (e.target.id !== "user-notes" || !current.version) return;
  if (e.target.value === (current.version.user_notes || "")) return;
  try { await saveUserNotes(); } catch (err) { toast(err.message, true); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "user-notes") $("#notes-status").textContent = "Unsaved";
});

// ---------------------------------------------------------------- phones on this Wi-Fi

function renderLan(s) {
  $("#lan-enabled").checked = s.enabled;
  $("#lan-status").textContent = !s.enabled ? "Off. Nothing outside this computer can reach the app."
    : s.listening ? `On: paired phones use http://${s.ip || "this computer"}:${s.port}/`
    : `Couldn't open port ${s.port}. Another program may be using it; turn this off and on to retry.`;
  $("#lan-pair-area").hidden = !(s.enabled && s.listening);
  const when = (t) => t ? new Date(t * 1000).toLocaleDateString() : "";
  $("#lan-devices").innerHTML = s.devices.length
    ? s.devices.map((d) => `<li><span>${esc(d.name)} <span class="hint">paired ${when(d.paired_at)}, last used ${when(d.last_seen)}</span></span>`
      + `<button type="button" class="ghost" data-lan-forget="${esc(d.id)}">Forget</button></li>`).join("")
    : `<li class="hint">No phones yet.</li>`;
}

async function openLan() {
  $("#lan-qr").hidden = true;
  $("#lan-code").textContent = "";
  try { renderLan(await api("/api/lan")); } catch (err) { return toast(err.message, true); }
  const dialog = $("#lan-dialog");
  if (!dialog.open) dialog.showModal();
}

$("#more-lan").addEventListener("click", () => openLan());
$("#lan-enabled").addEventListener("change", async (e) => {
  try { renderLan(await api("/api/lan", { method: "PUT", body: { enabled: e.target.checked } })); } catch (err) { toast(err.message, true); }
  $("#lan-qr").hidden = true;
  $("#lan-code").textContent = "";
});
$("#lan-pair").addEventListener("click", async () => {
  try {
    const p = await api("/api/lan/pair", { method: "POST" });
    $("#lan-qr").innerHTML = p.qr_svg;
    $("#lan-qr").hidden = false;
    $("#lan-code").innerHTML = `Scan this with the phone's camera within ${Math.round(p.expires_in / 60)} minutes, or open <strong>${esc(p.url)}</strong> on it, or type the code <strong>${esc(p.code)}</strong> on the phone's pairing page. The code works once.`;
  } catch (err) { toast(err.message, true); }
});
$("#lan-devices").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-lan-forget]")?.dataset.lanForget;
  if (!id) return;
  try { renderLan(await api(`/api/lan/devices/${encodeURIComponent(id)}`, { method: "DELETE" })); } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- first-run setup

async function renderSetup() {
  const s = await api("/api/setup");
  const step = (ok, title, body, action = "") =>
    `<li class="${ok ? "ok" : ""}"><span class="setup-mark">${ok ? "✓" : "○"}</span><div><strong>${title}</strong><p class="hint">${body}</p>${action}</div></li>`;
  $("#setup-steps").innerHTML = [
    step(s.guitars > 0, "Add your guitar",
      s.guitars ? `${s.guitars} guitar${s.guitars === 1 ? "" : "s"} added.` : "Presets are dialled for a guitar and its pickups.",
      `<button type="button" class="ghost" data-open="guitars-dialog">Guitars</button>`),
    step(s.api_key, "Anthropic API key (optional)",
      s.api_key_from_env ? "Using the ANTHROPIC_API_KEY set on this computer (an environment variable, maybe from another tool). To use a different key just for this app, add it here; the variable is left alone."
        : s.api_key ? "A key is set. Each preset shows what Claude cost."
        : "Claude builds and tweaks presets with your own key, from console.anthropic.com. Songs, sets, editing, imports and the pedal all work without one.",
      `<button type="button" class="ghost" data-open="settings-dialog" data-section="claude" data-title="Claude">Add a key</button>`),
    step(!!s.hx_edit, "HX Edit",
      s.hx_edit ? `Found at ${esc(s.hx_edit)}.`
        : "Install HX Edit from Line 6 (line6.com/software). The app reads the pedal's model list from it and never copies it."),
    step(s.pedal?.connected, "The pedal",
      s.pedal?.connected ? `Connected: ${esc(s.pedal.description)}.`
        : ((typeof PedalRelay !== "undefined" && PedalRelay.status().supported) ? "" : `${REMOTE_NOTE} `)
          + "Plug the HX Stomp XL into the computer running the app with a USB cable that carries data (some charging cables don't, and an old one can make sending fail), and quit HX Edit: only one app can hold the pedal at a time."
          + (s.pedal?.error ? ` The app says: ${esc(s.pedal.error)}` : "")),
  ].join("");
  return s;
}

async function openSetup() {
  await renderSetup();
  const dialog = $("#setup-dialog");
  if (!dialog.open) dialog.showModal();
}

// Shown by itself once on a new install, until Done.
async function maybeOpenSetup() {
  if ((state.account || {}).guest || maybeOpenSetup.shown) return;
  maybeOpenSetup.shown = true;
  try {
    const s = await api("/api/setup");
    if (!s.done && (!s.guitars || !s.api_key || !s.hx_edit)) await openSetup();
  } catch (_) { /* the app works without it */ }
}

$("#more-setup").addEventListener("click", () => openSetup());
$("#setup-recheck").addEventListener("click", () => renderSetup());
$("#setup-finish").addEventListener("click", async () => {
  await api("/api/setup/done", { method: "POST" });
  $("#setup-dialog").close();
  // First time through: straight on to the walkthrough.
  if (typeof helpSeen === "function" && !helpSeen()) openHelp();
});
// Back from adding a guitar or a key: tick it off.
for (const id of ["#guitars-dialog", "#settings-dialog"]) {
  $(id).addEventListener("close", () => { if ($("#setup-dialog").open) renderSetup(); });
}

// ---------------------------------------------------------------- explain

async function pollJob(jobId) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "done") return job.result;
    if (job.status !== "running") throw new Error(job.error || "That didn't work");
  }
}

async function explainBlock(index, refresh = false, io = null) {
  const view = editing?.view || current.version.view;
  const blk = io ? view.io.find((b) => b.io === io) : view.blocks[index];
  const dialog = $("#explain-dialog");
  const token = (explainBlock.token = (explainBlock.token || 0) + 1);
  dialog.dataset.block = index ?? "";
  dialog.dataset.io = io || "";
  $("#explain-title").textContent = io ? `${blk.title} block` : `${blk.model}${blk.based_on ? " · " + blk.based_on : ""}`;
  $("#explain-body").innerHTML = `<p class="hint">Asking Claude about the ${esc(blk.model)} and its settings for this song... this takes about half a minute the first time; after that it's saved.</p>`;
  if (!dialog.open) dialog.showModal();
  const edits = editing ? [...editing.committed, ...pendingEdits()] : [];
  try {
    let res = await api(`/api/versions/${current.versionId}/explain`, { method: "POST", body: { block: index ?? 0, io, edits, refresh } });
    if (res.job_id) res = await pollJob(res.job_id);
    if (token === explainBlock.token) renderExplanation(blk, res.explanation);
  } catch (err) {
    if (token === explainBlock.token) $("#explain-body").innerHTML = `<p class="explain-error">${esc(err.message)}</p>`;
  }
}

function renderExplanation(blk, ex) {
  const params = (ex.parameters || []).map((p) => {
    const now = blk.params.find((x) => x.name === p.name)?.value;
    return `<details class="explain-param"><summary><b>${esc(p.name)}</b><span class="muted">${now != null ? "now " + esc(now) + " · " : ""}try ${esc(p.recommended)}</span></summary>
      <div><p>${esc(p.what)}</p><p><b>How to set it:</b> ${esc(p.how)}</p><p><b>For this song:</b> ${esc(p.recommended)}. ${esc(p.why)}</p></div></details>`;
  }).join("");
  $("#explain-body").innerHTML = `
    <p>${esc(ex.summary)}</p>
    <p><b>In this preset:</b> ${esc(ex.role)}</p>
    <h3>Settings <span class="hint">(click one to open it)</span></h3>
    ${params}
    ${(ex.tips || []).length ? `<h3>Tips</h3><ul>${ex.tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}`;
}

// ---------------------------------------------------------------- block editor

async function blockChange(ops) {
  const before = editing.committed;
  const edits = [...before, ...pendingEdits(), ...ops];
  try {
    const result = await api(`/api/versions/${current.versionId}/preview`, { method: "POST", body: { edits } });
    editing.committed = edits;
    editing.edits = {};
    editing.view = result.view;
    if (!editing.view.snapshots[current.snapshot]) current.snapshot = 0;
    rerenderChainCard();
    liveSync();
    toast(result.changes.slice(-ops.length).join(" · "));
  } catch (err) {
    toast(err.message, true);
  }
}

async function moveBlock(from, to) {
  if (from === to || to < 0) return;
  if (!editing) editing = startEditing();
  await blockChange([{ move: from, to }]);
}

// Drag a block by its handle to reorder the chain. Pointer events cover mouse,
// pen and touch alike; the handle has touch-action: none so the page doesn't
// scroll under the finger. Dragging outside edit mode starts editing.
let drag = null;

function dropTarget(x, y) {
  const el = document.elementsFromPoint(x, y).map((n) => n.closest(".chain .block[data-index]"))
    .find((n) => n && n !== drag.el);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  // The chain wraps in a grid: left half drops before, right half after.
  const after = x > r.left + r.width / 2;
  return { el, after };
}

function clearDropMarks() {
  for (const el of document.querySelectorAll(".drop-before, .drop-after")) el.classList.remove("drop-before", "drop-after");
}

document.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest?.("[data-drag]");
  if (!handle || e.button > 0) return;
  const el = handle.closest(".block");
  e.preventDefault();
  drag = { from: Number(handle.dataset.drag), el, startX: e.clientX, startY: e.clientY, moved: false, target: null, id: e.pointerId };
  handle.setPointerCapture?.(e.pointerId);
});

document.addEventListener("pointermove", (e) => {
  if (!drag || e.pointerId !== drag.id) return;
  const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
  if (!drag.moved && Math.hypot(dx, dy) < 6) return;
  drag.moved = true;
  drag.el.classList.add("dragging");
  drag.el.style.transform = `translate(${dx}px, ${dy}px)`;
  clearDropMarks();
  drag.target = dropTarget(e.clientX, e.clientY);
  if (drag.target) drag.target.el.classList.add(drag.target.after ? "drop-after" : "drop-before");
  // Keep the chain in view when dragging near the screen's edge.
  if (e.clientY < 60) window.scrollBy(0, -12);
  else if (e.clientY > window.innerHeight - 60) window.scrollBy(0, 12);
});

async function endDrag(e) {
  if (!drag || e.pointerId !== drag.id) return;
  const { from, el, moved, target } = drag;
  drag = null;
  el.classList.remove("dragging");
  el.style.transform = "";
  clearDropMarks();
  if (!moved || !target || e.type === "pointercancel") return;
  const over = Number(target.el.dataset.index);
  // The index the block ends up at once it's out of its old place.
  let to = over + (target.after ? 1 : 0);
  if (from < to) to -= 1;
  await moveBlock(from, to);
}
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);

// Pedal types in signal-chain order; anything else follows alphabetically.
const CATEGORY_ORDER = ["Wah", "Pitch/Synth", "Dynamics", "Distortion", "Amp", "Cab", "EQ", "Filter",
  "Modulation", "Delay", "Reverb", "Volume/Pan", "Routing"];

function fillModelList(filter, selected) {
  const q = filter.trim().toLowerCase();
  const groups = {};
  for (const m of modelCache) {
    if (q && !`${m.name} ${m.category} ${m.based_on}`.toLowerCase().includes(q)) continue;
    (groups[m.category] ||= []).push(m);
  }
  const rank = (c) => { const i = CATEGORY_ORDER.indexOf(c); return i < 0 ? 99 : i; };
  const names = Object.keys(groups).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const selectedCategory = modelCache.find((m) => m.name === selected)?.category;
  $("#model-browser").innerHTML = names.length ? names.map((c) => {
    // Collapsed by default; open while filtering, and for the current model's type.
    const open = q || c === selectedCategory;
    const picks = groups[c].sort((a, b) => a.name.localeCompare(b.name)).map((m) =>
      `<button type="button" class="model-pick ${m.name === selected ? "selected" : ""}" data-pick="${esc(m.name)}">${esc(m.name)}${m.based_on ? `<small>${esc(m.based_on)}</small>` : ""}</button>`).join("");
    return `<details ${open ? "open" : ""}><summary>${esc(c)} <span class="muted">${groups[c].length}</span></summary><div class="model-grid">${picks}</div></details>`;
  }).join("") : '<div class="empty">No models match.</div>';
}

function pickModel(name) {
  const f = $("#block-form");
  f.elements.model.value = name;
  for (const b of document.querySelectorAll(".model-pick")) b.classList.toggle("selected", b.dataset.pick === name);
  fillExpParams(name, f.elements.exp_param.value);
  showModelInfo();
}

function fillExpParams(modelName, selected) {
  const m = modelCache.find((x) => x.name === modelName);
  const params = m ? m.params : [];
  const pick = params.some((p) => p.param === selected) ? selected : params.some((p) => p.param === "Pedal") ? "Pedal" : params[0]?.param;
  $("#block-form").elements.exp_param.innerHTML = params.map((p) =>
    `<option value="${esc(p.param)}" ${p.param === pick ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}

function showModelInfo() {
  const f = $("#block-form");
  const m = modelCache.find((x) => x.name === f.elements.model.value);
  $("#model-info").textContent = m ? `${m.category}${m.based_on ? " · based on " + m.based_on : ""} · DSP ~${m.dsp}%` : "";
}

async function openBlockDialog(index) {
  if (!modelCache) modelCache = (await api("/api/models")).models;
  const view = editing.view || current.version.view;
  const blk = index == null ? null : view.blocks[index];
  const f = $("#block-form");
  f.reset();
  f.dataset.block = index ?? "";
  delete f.dataset.expTouched;
  $("#block-dialog-title").textContent = blk ? `Block ${index + 1}: ${blk.model}` : "Add a block";
  $("#block-remove").hidden = !blk;
  $("#position-row").hidden = !!blk;
  if (!blk) {
    f.elements.position.innerHTML = [...view.blocks.map((b, i) => `<option value="${i}">Before ${esc(b.model)} (block ${i + 1})</option>`),
      `<option value="${view.blocks.length}" selected>At the end</option>`].join("");
  }
  const model = blk?.model || "";
  f.elements.model.value = model;
  fillModelList("", model);
  f.elements.switch.value = blk?.footswitch ?? "";
  f.elements.expression.value = blk?.expression ?? "";
  fillExpParams(model, blk?.expression_param || "Pedal");
  f.elements.heel.value = blk?.expression_heel ?? "";
  f.elements.toe.value = blk?.expression_toe ?? "";
  $("#exp-details").hidden = !f.elements.expression.value;
  f.dataset.orig = JSON.stringify({ model, switch: f.elements.switch.value });
  showModelInfo();
  $("#block-dialog").showModal();
  f.elements.filter.focus();
}

$("#block-form").addEventListener("input", (e) => {
  const f = e.currentTarget;
  if (e.target.name === "filter") fillModelList(e.target.value, f.elements.model.value);
});

$("#block-form").addEventListener("change", (e) => {
  const f = e.currentTarget;
  const name = e.target.name;
  if (["expression", "exp_param", "heel", "toe"].includes(name)) f.dataset.expTouched = "1";
  if (name === "expression") $("#exp-details").hidden = !f.elements.expression.value;
});

$("#block-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const orig = JSON.parse(f.dataset.orig);
  const model = f.elements.model.value;
  const ops = [];
  let index;
  if (f.dataset.block === "") {
    if (!model) { toast("Pick a model to add", true); return; }
    index = Number(f.elements.position.value);
    ops.push({ insert: index, model });
  } else {
    index = Number(f.dataset.block);
    if (model && model !== orig.model) ops.push({ block: index, model });
  }
  const sw = f.elements.switch.value;
  if (sw !== orig.switch) ops.push({ block: index, switch: sw ? Number(sw) : null });
  if (f.dataset.expTouched) {
    const exp = f.elements.expression.value;
    const num = (v) => (v === "" ? null : Number(v));
    ops.push(exp
      ? { block: index, expression: Number(exp), parameter: f.elements.exp_param.value, heel: num(f.elements.heel.value), toe: num(f.elements.toe.value) }
      : { block: index, expression: null });
  }
  $("#block-dialog").close();
  if (ops.length) await blockChange(ops);
});

$("#version-detail").addEventListener("change", async (e) => {
  if (!editing) return;
  if (e.target.id === "edit-scope") {
    editing.scope = e.target.checked ? "snapshot" : "all";
    rerenderChainCard();
  } else if (e.target.dataset.global) {
    recordGlobalEdit(e.target);
    liveSync();
  } else if (e.target.dataset.param) {
    recordEdit(e.target);
    liveSync();
  }
});

// Live: arrow keys and typing in a number box play straight away, without
// waiting for the box to lose focus.
$("#version-detail").addEventListener("input", (e) => {
  if (!editing?.live || !e.target.dataset.param || e.target.tagName !== "INPUT" || e.target.value === "") return;
  recordEdit(e.target);
  liveSync();
});

const TOGGLES_KEY = "stompxl.toggles";
const toggleValues = (d) => Object.fromEntries(TOGGLES.map((k) => [k, !!d[k]]));

// The DSP room box: what the rig and toggles already use, and which other
// pedals still fit on the pedal's one DSP (the same estimate the generator
// enforces, so a chain built inside it won't be refused on a push).
async function updateRoom(form) {
  const box = $("[data-room]", form);
  const rigId = form.elements.rig_id.value;
  const rig = state?.rigs.find((r) => String(r.id) === String(rigId));
  const nam = !!rig && rig.kind.startsWith("nam_");
  for (const el of form.querySelectorAll("[data-nam-only]")) el.hidden = !nam;
  if (!box || !rigId) return;
  const q = new URLSearchParams({ rig_id: rigId, ...toggleValues(formData(form)) });
  let r;
  try { r = await api(`/api/dsp-room?${q}`); } catch (_) { box.hidden = true; return; }
  box.hidden = false;
  const pct = (n) => `${Math.round(n)}%`;
  const used = Math.min(100, (r.used / r.budget) * 100);
  const reserved = r.reserved.map((b) => `${esc(b.name)} ~${pct(b.cost)}`).join(" · ") || "nothing yet";
  const fits = r.kinds.filter((k) => k.max);
  const out = r.kinds.filter((k) => !k.max);
  box.innerHTML = `
    <div class="room-head"><b>DSP room</b><span>${pct(r.left)} left · ${r.slots_left} of 8 block slots free</span></div>
    <div class="room-bar"><span style="width:${used}%"></span></div>
    <div class="hint">Already spoken for: ${reserved}</div>
    ${fits.length ? `<div class="room-list">${fits.map((k) => `<span class="room-fit" title="${esc(k.examples)}">${esc(k.kind)} ~${pct(k.cost)} <b>×${k.max}</b></span>`).join("")}</div>` : ""}
    ${r.example.length ? `<div class="hint">Fits together, e.g.: ${r.example.map(esc).join(" + ")} (~${pct(r.example_cost)})</div>` : `<div class="hint">No room for anything else: turn a toggle off or pick a lighter rig.</div>`}
    ${out.length ? `<div class="hint room-out">Won't fit: ${out.map((k) => esc(k.kind)).join(", ")}</div>` : ""}
    <div class="hint">×N = how many of that kind fit on their own. Hover for examples. Budget ${pct(r.budget)}${r.budget < 100 ? " (lowered after the pedal refused a heavier chain)" : ""}.</div>`;
}
// "Keep the same signal chain" is only for a guitar change: a different rig or
// toggle needs Claude to choose blocks again, so the box clears itself.
function syncKeepChain(f) {
  const v = current.version;
  if (!v) return;
  const same = String(f.elements.rig_id.value) === String(v.rig_id)
    && TOGGLES.every((k) => f.elements[k].checked === !!v.options[k]);
  const box = f.elements.keep_chain;
  if (!same) box.checked = false;
  box.disabled = !same;
  $("#keep-chain-hint").hidden = same;
}
// Library import: the export file goes up as is; the server checks it before
// changing anything and saves the library it replaces.
$("#library-import").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!confirm(`Replace the library here with "${file.name}"? The library here is saved first, so you can bring it back.`)) return;
  const out = $("#library-result");
  out.textContent = "Importing...";
  try {
    const res = await fetch("/api/library/import", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    out.textContent = `Imported ${body.songs ?? "the"} songs and ${body.files} files. The library that was here is saved as ${body.saved_before.split("/").pop()}.`;
    await loadState();
    toast("Library imported");
  } catch (err) {
    out.textContent = "";
    toast(`Import failed: ${err.message}`, true);
  }
});

$("#fork-form").addEventListener("change", (e) => {
  if (e.target.name !== "keep_chain" && e.target.name !== "label" && e.target.name !== "instructions") syncKeepChain(e.currentTarget);
});

for (const sel of ["#generate-form", "#fork-form"]) {
  $(sel).addEventListener("change", (e) => {
    if (e.target.type === "checkbox" || e.target.name === "rig_id") updateRoom(e.currentTarget);
  });
}
function restoreToggles() {
  try {
    const saved = JSON.parse(localStorage.getItem(TOGGLES_KEY) || "null");
    if (!saved) return;
    const f = $("#generate-form");
    for (const k of TOGGLES) if (k in saved) f.elements[k].checked = !!saved[k];
  } catch (_) { /* storage unavailable: keep the defaults */ }
}
restoreToggles();

$("#generate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const d = formData(f);
  try { localStorage.setItem(TOGGLES_KEY, JSON.stringify(toggleValues(d))); } catch (_) { /* ignore */ }
  if (!d.guitar_id) { $("#guitars-dialog").showModal(); return toast("Add a guitar first", true); }
  const songId = f.dataset.songId ? Number(f.dataset.songId) : null;
  delete f.dataset.songId;
  const result = await runJob(`Building "${d.song}"`, () => api("/api/generate", {
    method: "POST",
    body: {
      song: d.song, guitar_id: Number(d.guitar_id), rig_id: Number(d.rig_id), instructions: d.instructions,
      options: toggleValues(d), song_id: songId,
    },
  }));
  if (result) { await loadState(); await openSong(result.song_id, result.version_id); }
});

$("#blank-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  if (!d.guitar_id) { $("#guitars-dialog").showModal(); return toast("Add a guitar first", true); }
  try {
    const result = await api("/api/songs/blank", {
      method: "POST",
      body: { title: d.title, artist: d.artist, guitar_id: Number(d.guitar_id), rig_id: Number(d.rig_id),
              start: d.start, tempo: Number(d.tempo) || 120 },
    });
    e.target.reset();
    await loadState();
    await openSong(result.song_id, result.version_id);
    toast("Blank song created: click Edit values to build it");
  } catch (err) { toast(err.message, true); }
});

$("#import-form").addEventListener("change", (e) => {
  if (e.target.name !== "source") return;
  const fromPedal = e.target.value === "pedal";
  $("#import-pedal").hidden = !fromPedal;
  $("#import-file").hidden = fromPedal;
});

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const d = formData(f);
  if (!d.guitar_id) { $("#guitars-dialog").showModal(); return toast("Add a guitar first", true); }
  if (d.source === "pedal") {
    const result = await runJob(`Reading slot ${slotLabel(Number(d.slot))} from the pedal`, () => api("/api/import/pedal", {
      method: "POST",
      body: { slot: Number(d.slot), title: d.title, artist: d.artist, guitar_id: Number(d.guitar_id), rig_id: Number(d.rig_id) },
    }));
    if (result) { await loadState(); await openSong(result.song_id, result.version_id); toast("Imported from the pedal"); }
    return;
  }
  const file = f.elements.file.files[0];
  if (!file) return toast("Choose an .hlx file", true);
  let hlx;
  try { hlx = JSON.parse(await file.text()); } catch (_) { return toast("That file isn't a readable .hlx preset", true); }
  try {
    const result = await api("/api/import/hlx", {
      method: "POST",
      body: { hlx, title: d.title, artist: d.artist, guitar_id: Number(d.guitar_id), rig_id: Number(d.rig_id) },
    });
    f.reset();
    await loadState();
    await openSong(result.song_id, result.version_id);
    toast("Imported");
  } catch (err) { toast(err.message, true); }
});

$("#version-detail").addEventListener("submit", async (e) => {
  if (e.target.id !== "refine-form") return;
  e.preventDefault();
  const d = formData(e.target);
  const result = await runJob("Applying your tweak", () => api(`/api/versions/${current.versionId}/refine`, {
    method: "POST", body: { feedback: d.feedback, remember: d.remember, remember_for_guitar: d.remember_for_guitar },
  }));
  if (result) { await loadState(); await openSong(result.song_id, result.version_id); }
});

$("#fork-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  $("#fork-dialog").close();
  const body = {
    guitar_id: Number(d.guitar_id), rig_id: Number(d.rig_id), label: d.label, instructions: d.instructions,
    options: toggleValues(d), keep_chain: !!d.keep_chain,
  };
  let result;
  const res = await api(`/api/versions/${current.versionId}/fork`, { method: "POST", body }).catch((err) => toast(err.message, true));
  if (!res) return;
  if (res.job_id) result = await runJob(body.keep_chain ? "Re-dialling for the new guitar" : "Forking", async () => res);
  else result = res;
  if (result) { await loadState(); await openSong(result.song_id, result.version_id); }
});

$("#push-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const slot = Number($("#slot-select").value);
  $("#push-dialog").close();
  runJob.lastError = null;
  const result = await runJob(`Sending to slot ${slotLabel(slot)}`, () => api(`/api/versions/${current.versionId}/push`, { method: "POST", body: { slot } }));
  const failed = runJob.lastError;
  if (!result && failed && failed.dsp_refused) {
    const ok = confirm(`The pedal ran out of DSP at ${failed.dsp_refused}. Nothing was saved.\n\nMake a lighter version that fits (keeps the amp)?`);
    if (ok) {
      const fixed = await runJob("Making it fit", () => api(`/api/versions/${current.versionId}/refine`, {
        method: "POST",
        body: { feedback: `The pedal ran out of DSP placing ${failed.dsp_refused}. Make the chain lighter so it fits the Stomp XL: drop or swap ${failed.dsp_refused} (or another heavy block) for a cheaper option and keep the amp and the song's core sound.` },
      }));
      if (fixed) {
        await loadState();
        await openSong(fixed.song_id, fixed.version_id);
        toast("Lighter version ready. Send it to the pedal again.");
      }
    }
    return;
  }
  if (result) {
    await loadState();
    await openSong(result.song_id, result.version_id);
    toast(result.summary);
  }
});

$("#guitar-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  const id = d.id;
  delete d.id;
  await api(id ? `/api/guitars/${id}` : "/api/guitars", { method: id ? "PUT" : "POST", body: d });
  e.target.reset();
  e.target.elements.id.value = "";
  $("#guitar-form-title").textContent = "Add a guitar";
  await loadState();
});

$("#pref-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  const body = { text: d.text, guitar_id: d.guitar_id ? Number(d.guitar_id) : null };
  await api(d.id ? `/api/preferences/${d.id}` : "/api/preferences", { method: d.id ? "PUT" : "POST", body });
  resetPrefForm();
  await loadState();
});

$("#gear-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  const body = { kind: d.kind, name: d.name, text: d.text };
  await api(d.id ? `/api/gear-notes/${d.id}` : "/api/gear-notes", { method: d.id ? "PUT" : "POST", body });
  resetGearForm();
  await loadState();
});

$("#gear-kind").addEventListener("change", (e) => gearOptions(e.target.value));

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("#nam-row-name").value.trim()) namRowSave(); // a capture typed but not added yet
  const d = formData(e.target);
  const body = { claude_model: d.claude_model, claude_effort: d.claude_effort, exp_pedal: d.exp_pedal, gate_mode: d.gate_mode, nam_captures: parseCaptures(d.nam_captures) };
  if (d.anthropic_api_key) body.anthropic_api_key = d.anthropic_api_key;
  if (d.diagnostics_token) body.diagnostics_token = d.diagnostics_token;
  await api("/api/settings", { method: "PUT", body });
  $("#settings-dialog").close();
  await loadState();
  toast("Settings saved");
});

// New song: guitar and rig as one-tap chips (the selects stay as the form's values).
function renderPickChips() {
  for (const box of document.querySelectorAll("[data-chips-for]")) {
    const sel = $("#" + box.dataset.chipsFor);
    const opts = [...sel.options].filter((o) => o.value);
    box.innerHTML = opts.map((o) => `<button type="button" class="chip${o.value === sel.value ? " on" : ""}" data-chip-value="${esc(o.value)}" aria-pressed="${o.value === sel.value}">${esc(o.textContent)}</button>`).join("")
      + (sel.id === "guitar-select" ? `<button type="button" class="chip add" data-open="guitars-dialog">${opts.length ? "+ Guitar" : "+ Add your guitar"}</button>` : "");
  }
}
document.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-chip-value]");
  if (!chip) return;
  const sel = $("#" + chip.closest("[data-chips-for]").dataset.chipsFor);
  sel.value = chip.dataset.chipValue;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  renderPickChips();
});
for (const id of ["#guitar-select", "#rig-select"]) $(id).addEventListener("change", renderPickChips);

// NAM captures as rows: the text box stays the source, the rows edit it.
const namText = () => $("#settings-form").elements.nam_captures;
const captureLine = (c) => [c.slot ?? "-", c.name, c.description, c.cab ? "cab" : ""].join(" | ").replace(/( \| )+$/, "");
let namEditing = null; // index of the row in the form, or null for a new one
function renderNamRows() {
  const rows = parseCaptures(namText().value);
  $("#nam-rows").innerHTML = rows.map((c, i) => `<li class="nam-row${i === namEditing ? " editing" : ""}">
      <span class="slot-badge${c.slot == null ? " none" : ""}" title="${c.slot == null ? "On the computer only" : "IR slot " + c.slot}">${c.slot ?? "PC"}</span>
      <button type="button" class="nam-row-main" data-nam-edit="${i}"><b>${esc(c.name)}</b><span class="sub">${esc([c.description, c.cab ? "with cab" : ""].filter(Boolean).join(" · "))}</span></button>
      <button type="button" class="ghost small danger" data-nam-del="${i}" aria-label="Remove ${esc(c.name)}">✕</button></li>`).join("")
    || `<li class="hint">No captures yet. Add one below, or get them from TONE3000.</li>`;
}
function namRowEdit(i) {
  namEditing = i;
  const c = i == null ? {} : parseCaptures(namText().value)[i];
  $("#nam-row-slot").value = c.slot ?? "";
  $("#nam-row-name").value = c.name || "";
  $("#nam-row-desc").value = c.description || "";
  $("#nam-row-cab").checked = !!c.cab;
  $("#nam-row-save").textContent = i == null ? "+ Add capture" : "Update capture";
  $("#nam-row-clear").hidden = i == null;
}
function namRowSave() {
  const name = $("#nam-row-name").value.trim();
  if (!name) return toast("Give the capture a name", true);
  const slot = $("#nam-row-slot").value.trim();
  const rows = parseCaptures(namText().value);
  const row = { slot: /^\d+$/.test(slot) ? Number(slot) : null, name, description: $("#nam-row-desc").value.trim(), cab: $("#nam-row-cab").checked };
  if (namEditing == null) rows.push(row); else rows[namEditing] = row;
  namText().value = rows.map(captureLine).join("\n");
  namRowEdit(null);
  renderNamRows();
  toast("Tap Save to keep it");
}
$("#settings-dialog").addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.namEdit) { namRowEdit(Number(t.dataset.namEdit)); renderNamRows(); $("#nam-row-name").focus(); }
  else if (t.dataset.namDel) {
    const rows = parseCaptures(namText().value);
    rows.splice(Number(t.dataset.namDel), 1);
    namText().value = rows.map(captureLine).join("\n");
    namRowEdit(null);
    renderNamRows();
  } else if (t.id === "nam-row-save") namRowSave();
  else if (t.id === "nam-row-clear") { namRowEdit(null); renderNamRows(); }
});
$("#settings-form").elements.nam_captures.addEventListener("input", renderNamRows);

// "12 | JCM800 Crunch | Marshall JCM800 | cab" per line; "-" for no IR slot.
function parseCaptures(text) {
  return (text || "").split("\n").map((line) => line.split("|").map((p) => p.trim())).filter((p) => p[0] || p[1])
    .map((p) => p.length === 1 ? ["-", p[0]] : p)
    .map(([slot, name, description = "", cab = ""]) => ({
      slot: /^\d+$/.test(slot) ? Number(slot) : null, name, description, cab: /cab|yes/i.test(cab),
    }));
}

function namCard(v) {
  const nam = v.view.nam;
  if (!nam) return "";
  const c = nam.capture || {};
  if (nam.mode === "pedal") {
    return `<div class="card"><h3>NAM on the pedal</h3>
      <p><b>${esc(c.name)}</b> from IR slot ${esc(c.slot)}${c.cab ? " (includes its cab)" : " with a cab block after it"}.</p>
      <p class="hint">Needs the NAM A2 Lite firmware, an unofficial third-party mod (not from Line 6, at your own risk). HX Edit shows the block as "Xenomorph Fuzz"; its Min Freq knob is the IR slot.</p></div>`;
  }
  return `<div class="card"><h3>NAM on the computer</h3>
    <p>${c.suggested ? "No capture in your list fits. Find one like this" : "Load this capture in NAM"}: <b>${esc(c.name)}</b>${c.cab ? "" : ", plus a cab IR"}.</p>
    <p class="hint">Guitar into the pedal's Return L jack. DAW at 48 kHz: one track in from the Stomp's send, NAM and the IR on it, out to USB 5/6. Load this preset through HX Edit (download the .hlx); the USB push can't set up path B. To switch captures with the pedal, map this preset's MIDI program change to the capture in your plugin host (docs/nam.md).</p></div>`;
}

async function refreshPedal() {
  // A background tab leaves the pedal alone, so HX Edit can have it.
  if (document.hidden) return;
  const pill = $("#pedal-pill");
  try {
    const s = await api("/api/pedal");
    const relay = PedalRelay.status();
    if (s.connected) {
      const here = s.where ? (relay.active ? ` · on this ${s.where}` : ` · on a ${s.where}`) : "";
      pill.querySelector(".pill-text").textContent = s.description + here + (s.verified ? "" : " (untested model)");
      pill.title = s.where ? `Plugged into ${relay.active ? "this" : "a"} ${s.where}; the app reaches it through this page` : "Plugged into the computer running the app";
      pill.className = "pill " + (s.verified ? "ok" : "warn");
    } else {
      pill.querySelector(".pill-text").textContent = s.error ? "Pedal: USB unavailable" : "Pedal: not connected";
      // A phone or tablet that can't reach USB from the browser (every iPhone
      // and iPad) is a remote: the pedal goes into the computer.
      pill.title = s.error || (relay.supported
        ? "Connect the Stomp XL over USB, switch it on and quit HX Edit (only one app can use the pedal at a time)"
        : `Plug the Stomp XL into the computer running the app, switch it on and quit HX Edit there. ${REMOTE_NOTE}`);
      pill.className = "pill";
    }
  } catch (_) { pill.querySelector(".pill-text").textContent = "Pedal: ?"; }
}

loadState().then(async () => {
  updateRoom($("#generate-form"));
  if (await resumeJob()) return;
  await route();
}).catch((err) => toast(err.message, true));
refreshPedal();
setInterval(refreshPedal, 15000);
document.addEventListener("visibilitychange", refreshPedal);

// The pedal plugged into this phone or computer instead of the Mac (relay.js).
function updateRelayButton() {
  const btn = $("#relay-btn");
  const s = PedalRelay.status();
  btn.hidden = !s.supported;
  btn.textContent = s.active ? "Stop using pedal here" : "Use pedal here";
  if (state) renderMore();
}
$("#relay-btn").addEventListener("click", async () => {
  const btn = $("#relay-btn");
  btn.disabled = true;
  try {
    if (PedalRelay.status().active) {
      await PedalRelay.disconnect();
      toast("The app no longer uses the pedal on this device");
    } else {
      await PedalRelay.connect();
      toast("Pedal connected through this device. Keep this page open while sending.");
    }
  } catch (err) {
    if (err.name !== "NotFoundError") toast(err.message, true);  // NotFoundError: picker closed
  } finally {
    btn.disabled = false;
    updateRelayButton();
    refreshPedal();
  }
});
PedalRelay.onChange(() => { updateRelayButton(); refreshPedal(); });
// The status pill doubles as the connect button where this device can reach a pedal.
$("#pedal-pill").addEventListener("click", () => {
  const s = PedalRelay.status();
  if (s.supported && !$("#pedal-pill").classList.contains("ok")) $("#relay-btn").click();
  else if (s.active) toast(`${$("#pedal-pill").title}. To stop using it here, open the menu.`);
  else toast($("#pedal-pill").title);
});
updateRelayButton();
PedalRelay.resume();

// ---------------------------------------------------------------- NAM captures from TONE3000

function fillNam() {
  const st = state.settings;
  $("#t3k-status").textContent = !st.tone3000_key ? "No key saved yet."
    : st.tone3000_signed_in ? "Signed in to TONE3000." : "Key saved; sign in next.";
  $("#t3k-signin").hidden = !st.tone3000_key || st.tone3000_signed_in;
  $("#t3k-signout").hidden = !st.tone3000_signed_in;
  const guest = !!(state.account || {}).guest;
  $("#t3k-own").hidden = guest;
  $("#t3k-shared").hidden = !guest;
  $("#t3k-shared").textContent = st.tone3000_signed_in
    ? "Searches and downloads use the app owner's TONE3000 sign-in. The files are yours: download them below."
    : "The app owner hasn't signed in to TONE3000 yet, so downloads won't work until they do.";
  $("#nam-open-folder").hidden = guest;
}

$("#nam-library-open").addEventListener("click", () => { fillNam(); $("#nam-dialog").showModal(); });

$("#t3k-key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = formData(e.target).tone3000_key.trim();
  if (!key) return;
  try {
    await api("/api/settings", { method: "PUT", body: { tone3000_key: key } });
    e.target.reset();
    await loadState();
    fillNam();
  } catch (err) { toast(err.message, true); }
});

$("#t3k-signin").addEventListener("click", async () => {
  try {
    const { url } = await api("/api/tone3000/signin");
    window.open(url, "_blank");
    for (let i = 0; i < 100 && !state.settings.tone3000_signed_in; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      await loadState();
    }
    fillNam();
  } catch (err) { toast(err.message, true); }
});

$("#t3k-signout").addEventListener("click", async () => {
  await api("/api/tone3000/signout", { method: "POST" });
  await loadState();
  fillNam();
});

async function runNamJob(path, body) {
  const log = $("#nam-log");
  log.hidden = false;
  log.textContent = "Working...";
  try {
    const { job_id } = await api(path, { method: "POST", body });
    let job;
    for (;;) {
      await new Promise((r) => setTimeout(r, 1200));
      job = await api(`/api/jobs/${job_id}`);
      log.textContent = job.log.join("\n") || "Working...";
      if (job.status !== "running") break;
    }
    if (job.status !== "done") throw new Error(job.error || "That didn't work");
    const r = job.result;
    log.textContent += `\n\nAdded ${r.added.length}` + (r.failed.length ? `, skipped ${r.failed.length}` : "") +
      ((state.account || {}).guest
        ? ". Now tap Download the IR files, unzip it on your computer, and drag each .wav onto its slot in HX Edit (the number at the start of each name is its slot)."
        : `. Now drag the .wav files from ${r.ir_folder} onto their slots in HX Edit (the number at the start of each name is its slot), or tap Download the IR files on another computer.`);
    await loadState();
  } catch (err) {
    log.textContent += "\n" + err.message;
    toast(err.message, true);
  }
}

$("#nam-starter").addEventListener("click", () => runNamJob("/api/nam/starter"));
$("#nam-full-rig").addEventListener("click", () => runNamJob("/api/nam/pack/full-rig"));

$("#nam-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  await runNamJob("/api/nam/add", { tone: d.tone, slot: d.slot ? Number(d.slot) : null, name: d.name, model: d.model, description: d.description });
});

$("#t3k-search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = formData(e.target).q.trim();
  if (!q) return;
  const box = $("#t3k-results");
  box.innerHTML = '<p class="hint">Searching...</p>';
  try {
    const { tones } = await api(`/api/tone3000/search?q=${encodeURIComponent(q)}`);
    box.innerHTML = tones.length ? tones.map((t) => `<p><a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.title)}</a>
      <span class="hint">by ${esc(t.creator)} · ${esc(t.downloads)} downloads · ${esc(t.favorites)} favorites · ${t.full_rig ? "full rig" : "amp only"}</span>
      <button type="button" class="ghost" data-tone="${esc(t.id)}" data-title="${esc(t.title)}">Use</button></p>`).join("")
      : '<p class="hint">Nothing with A2 models matched.</p>';
  } catch (err) { box.innerHTML = `<p class="explain-error">${esc(err.message)}</p>`; }
});

$("#t3k-results").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tone]");
  if (!btn) return;
  const f = $("#nam-add-form");
  f.elements.tone.value = btn.dataset.tone;
  if (!f.elements.name.value) f.elements.name.value = btn.dataset.title.slice(0, 16);
  f.elements.slot.focus();
});

$("#nam-open-folder").addEventListener("click", async () => {
  const { folder } = await api("/api/nam/open-folder", { method: "POST" });
  toast("IR folder: " + folder);
});

// ---------------------------------------------------------------- diagnostics

const diag = { error: "", text: "" };

function diagBody() {
  const live = editing?.live ? [`live edit on ${editing.live.label}${editing.live.structural ? " (some edits waiting for Save & send)" : ""}`] : [];
  return { note: $("#diag-note").value, error: diag.error, user_agent: navigator.userAgent, relay: PedalRelay.status(),
           events: [...clientLog, ...live.map((l) => `now: ${l}`)] };
}

async function openDiagnostics(error = "") {
  diag.error = error;
  diag.text = "";
  $("#diag-error").hidden = !error;
  $("#diag-error").textContent = error ? `Error: ${error}` : "";
  $("#diag-result").textContent = "";
  $("#diag-preview").textContent = "Loading...";
  $("#diag-send").disabled = false;
  $("#diag-dialog").showModal();
  try {
    const res = await api("/api/diagnostics/preview", { method: "POST", body: diagBody() });
    diag.text = res.text;
    diag.issueUrl = res.issue_url;
    $("#diag-preview").textContent = res.text;
    // With a token, one tap files it. Without one, copy it and open GitHub's new-issue page.
    $("#diag-send").hidden = !res.can_send;
    $("#diag-github").hidden = res.can_send;
    $("#diag-copy").hidden = !res.can_send;
    if (!res.can_send) {
      $("#diag-result").textContent = `Copy and open GitHub copies the report and opens a new issue on ${res.repo}: paste it there. Issues there are public, so read it first under "See what gets sent".`;
    }
  } catch (err) { $("#diag-preview").textContent = err.message; }
}

// The most recent error worth reporting: one shown in the last half hour.
function recentError() {
  if (lastError && Date.now() - lastError.at < 30 * 60 * 1000) return lastError.message;
  return runJob.lastError?.error || "";
}
$("#diag-open").addEventListener("click", () => openDiagnostics(recentError()));
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-report]");
  if (!t) return;
  closeTopMenu();
  openDiagnostics(recentError());
});
document.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-diag-note]");
  if (!chip) return;
  const note = $("#diag-note");
  note.value = [note.value.trim(), chip.dataset.diagNote].filter(Boolean).join(". ");
  note.focus();
});

$("#diag-send").addEventListener("click", async () => {
  const btn = $("#diag-send");
  btn.disabled = true;
  $("#diag-result").textContent = "Sending...";
  try {
    const res = await api("/api/diagnostics/send", { method: "POST", body: diagBody() });
    if (res.ok) {
      $("#diag-result").innerHTML = `Sent. Tell Claude "diagnostics sent", or share <a href="${esc(res.url)}" target="_blank" rel="noopener">the issue</a>.`;
      return;
    }
    diag.text = res.text || diag.text;
    $("#diag-result").textContent = `${res.error} You can Copy it instead.`;
  } catch (err) { $("#diag-result").textContent = err.message; }
  btn.disabled = false;
});

$("#diag-test").addEventListener("click", async () => {
  const btn = $("#diag-test");
  btn.disabled = true;
  $("#diag-result").textContent = "Testing the pedal (about 10 seconds)...";
  try {
    const res = await api("/api/diagnostics/write-test", { method: "POST", body: {} });
    const note = $("#diag-note");
    note.value = [note.value.trim(), res.summary].filter(Boolean).join("\n\n");
    $("#diag-result").textContent = res.steps.some((s) => s.ok === false)
      ? "Test done: a write failed. Tap Send so Claude can see it, then turn the pedal off and on."
      : "Test done: every write went through. Tap Send so Claude can see it.";
    // Refresh what gets sent, so it includes the test.
    const preview = await api("/api/diagnostics/preview", { method: "POST", body: diagBody() });
    diag.text = preview.text;
    $("#diag-preview").textContent = preview.text;
  } catch (err) { $("#diag-result").textContent = `Test couldn't run: ${err.message}`; }
  btn.disabled = false;
});

$("#diag-github").addEventListener("click", async () => {
  if (!diag.text) return;
  // Open the tab first, while the tap still counts as one, then copy.
  const tab = window.open(diag.issueUrl, "_blank", "noopener");
  try {
    await navigator.clipboard.writeText(diag.text);
    $("#diag-result").textContent = "Copied. Paste it into the new issue on GitHub, then submit it.";
  } catch (_) {
    $("#diag-preview").closest("details").open = true;
    $("#diag-result").textContent = "Couldn't copy here: select the text below, copy it, and paste it into the new issue. Or Save as a file and attach it.";
  }
  if (!tab) $("#diag-result").innerHTML += ` <a href="${esc(diag.issueUrl)}" target="_blank" rel="noopener">Open the new issue</a>.`;
});

$("#diag-save").addEventListener("click", () => {
  if (!diag.text) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([diag.text], { type: "text/markdown" }));
  a.download = `problem-report-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  $("#diag-result").textContent = "Saved. Attach the file to an issue on GitHub, or send it to whoever is helping you.";
});

$("#diag-copy").addEventListener("click", async () => {
  if (!diag.text) return;
  try {
    await navigator.clipboard.writeText(diag.text);
    $("#diag-result").textContent = "Copied: paste it into the Claude thread.";
  } catch (_) {
    $("#diag-preview").closest("details").open = true;
    $("#diag-result").textContent = "Couldn't copy here: select the text below and copy it.";
  }
});
